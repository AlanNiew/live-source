"""管理 API Blueprint：/api/admin/*（session 鉴权，JSON）

- 登录密码 ADMIN_PASSWORD（config；默认空 = 管理功能整体禁用，安全默认）
- 列表接口统一分页包裹 {items, total, page, page_size, has_more}，契约见 ADMIN_PLAN.md
- 配置变更（源/频道覆盖/设置）自动触发异步聚合刷新 + 清播放列表缓存
"""
import hmac
import json
import os
import threading
import time

from flask import Blueprint, jsonify, request, session

from config import (ADMIN_LOGIN_LOCKOUT_SECONDS, ADMIN_LOGIN_MAX_FAILURES,
                    ADMIN_PASSWORD, AGGREGATED_M3U_PATH, MAX_PAGE_SIZE)
from core.aggregator import AggregatorUtils
from core.sources import SourceUtils

admin_api = Blueprint('admin_api', __name__)

# create_app 注册时注入 Flask 简单缓存实例（配置变更后清播放列表缓存）
admin_api.cache = None

# 日志级别白名单（logs 端点过滤）
_LOG_LEVELS = {'ERROR', 'WARNING', 'INFO'}
# 源类型白名单（sources 端点）
_SOURCE_TYPES = {'public', 'bilibili'}


# ---------------------------------------------------------------- 鉴权

def _admin_disabled():
    """ADMIN_PASSWORD 为空 → 管理功能整体禁用（安全默认）"""
    return not ADMIN_PASSWORD


# ---------------------------------------------------------------- 登录防爆破

# 进程内失败计数（GUNICORN_WORKERS=1 保证单 worker 内存态有效）。
# 注：nginx 反代后 remote_addr 为 127.0.0.1，等价「全局锁定」——
# 单管理员场景反而更严格（任何来源连错 N 次全锁）。nginx 层另配 limit_req 双保险。
_login_lock = threading.Lock()
_login_failures = {}  # remote_addr -> {'count': int, 'locked_until': float}


def _login_throttled(remote_addr):
    """锁定中返回剩余秒数，未锁定返回 None"""
    now = time.time()
    with _login_lock:
        rec = _login_failures.get(remote_addr)
        if rec and rec.get('locked_until', 0) > now:
            return int(rec['locked_until'] - now) + 1
    return None


def _login_failed(remote_addr):
    """登录失败：计数 +1，连续满 N 次锁定"""
    with _login_lock:
        rec = _login_failures.setdefault(remote_addr, {'count': 0, 'locked_until': 0})
        rec['count'] += 1
        if rec['count'] >= ADMIN_LOGIN_MAX_FAILURES:
            rec['locked_until'] = time.time() + ADMIN_LOGIN_LOCKOUT_SECONDS
            rec['count'] = 0


def _login_ok(remote_addr):
    """登录成功：清空该来源失败记录"""
    with _login_lock:
        _login_failures.pop(remote_addr, None)


@admin_api.before_request
def _require_admin():
    """除登录外全部端点校验 session['admin']；未启用统一 403"""
    if request.endpoint == 'admin_api.login':
        return None
    if _admin_disabled():
        return jsonify({'error': '管理功能未启用（ADMIN_PASSWORD 为空）'}), 403
    if not session.get('admin'):
        return jsonify({'error': '未登录或会话已过期'}), 401
    return None


@admin_api.route('/login', methods=['POST'])
def login():
    """POST {password} → set session['admin']（恒定时间比较 + 防爆破锁定）"""
    if _admin_disabled():
        return jsonify({'error': '管理功能未启用（ADMIN_PASSWORD 为空）'}), 403
    remaining = _login_throttled(request.remote_addr)
    if remaining is not None:
        _audit(f"登录尝试被锁定（{request.remote_addr}）", 'WARNING')
        return jsonify({'error': f'尝试次数过多，请 {remaining} 秒后再试'}), 429
    data = request.get_json(silent=True) or {}
    password = data.get('password') or ''
    if hmac.compare_digest(password, ADMIN_PASSWORD):
        _login_ok(request.remote_addr)
        session.permanent = True  # 会话按 ADMIN_SESSION_HOURS 过期
        session['admin'] = True
        _audit(f"登录成功（{request.remote_addr}）")
        return jsonify({'status': 'success'})
    _login_failed(request.remote_addr)
    _audit(f"登录失败（{request.remote_addr}）", 'WARNING')
    return jsonify({'error': '密码错误'}), 401


@admin_api.route('/logout', methods=['POST'])
def logout():
    """退出登录（清 session）"""
    _audit(f"退出登录（{request.remote_addr}）")
    session.clear()
    return jsonify({'status': 'success'})


# ---------------------------------------------------------------- 通用工具

def _paging_params():
    """解析 page/page_size（非法值回退默认；page_size 截断到 MAX_PAGE_SIZE）"""
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(1, int(request.args.get('page_size', 20)))
    except (TypeError, ValueError):
        page_size = 20
    return page, min(page_size, MAX_PAGE_SIZE)


def _envelope(items, total, page, page_size):
    """统一分页包裹（契约见 ADMIN_PLAN.md）"""
    return {
        'items': items,
        'total': total,
        'page': page,
        'page_size': page_size,
        'has_more': page * page_size < total,
    }


def _after_config_change():
    """配置变更后：异步聚合刷新 + 清播放列表缓存（尽力而为，不影响响应）"""
    try:
        AggregatorUtils.request_async_refresh()
        if admin_api.cache is not None:
            admin_api.cache.delete('transList2M3U')
    except Exception:
        pass


def _audit(message, level='INFO'):
    """管理操作审计：写入 logs 表（module=admin，管理页日志可查）；失败不影响业务"""
    try:
        from admin import db
        db.record_event(level, 'admin', message)
    except Exception:
        pass


def _override_view(ov):
    """覆盖行 → 响应视图（无覆盖返回 None）"""
    if not ov:
        return None
    return {
        'enabled': ov.get('enabled'),
        'display_name': ov.get('display_name'),
        'group_title': ov.get('group_title'),
    }


def _load_current_channels():
    """当前聚合频道列表（读聚合缓存文件；无缓存时触发生成兜底）

    :return: [{key, name, group, url}]，key=normalize_name（与聚合去重同款归一化）
    """
    text = ""
    try:
        if os.path.exists(AGGREGATED_M3U_PATH):
            with open(AGGREGATED_M3U_PATH, 'r', encoding='utf-8') as f:
                text = f.read()
        else:
            text = AggregatorUtils.load_aggregated_m3u() or ""
    except Exception:
        text = ""
    channels = []
    for ch in SourceUtils.parse_m3u_channels(text or ""):
        channels.append({
            'key': SourceUtils.normalize_name(ch['name']),
            'name': ch['name'],
            'group': ch['group_title'],
            'url': ch['url'],
        })
    return channels


# ---------------------------------------------------------------- 源管理

def _config_fallback_sources(source_type=None):
    """config 种子值兜底行（仅在对应类型无启用源时返回，标记 config_default）。

    与聚合兜底语义一致：类型下启用源为空 → 聚合回退 config（PUBLIC_M3U_SOURCES /
    BILIBILI_ROOMS），列表如实展示"当前生效来源"；id 为 None（未入库，只读）。
    """
    from admin import db
    from config import BILIBILI_ROOMS, PUBLIC_M3U_SOURCES

    rows = []
    if source_type in (None, 'public') and not db.get_enabled_public_urls():
        for i, url in enumerate(PUBLIC_M3U_SOURCES):
            rows.append({'id': None, 'type': 'public', 'name': f'默认公开源 {i + 1}',
                         'url': url, 'enabled': 1, 'sort_order': 0,
                         'created_at': None, 'updated_at': None, 'config_default': True})
    if source_type in (None, 'bilibili') and not db.get_enabled_bilibili_rooms():
        for item in BILIBILI_ROOMS:
            room_id = item.get('room_id')
            url = str(room_id) if room_id else f"uid:{item.get('uid')}"
            rows.append({'id': None, 'type': 'bilibili', 'name': item['name'],
                         'url': url, 'enabled': 1, 'sort_order': 0,
                         'created_at': None, 'updated_at': None, 'config_default': True})
    return rows


@admin_api.route('/sources/import-defaults', methods=['POST'])
def sources_import_defaults():
    """把 config 兜底源导入 DB（管理界面「导入默认源」）。

    - 幂等：已存在同名/同 url 的行跳过，可重复点击
    - B 站 uid 条目服务端解析房间号（磁盘缓存兜底），解析失败跳过并在 skipped 中列出
    """
    from admin import db
    from config import BILIBILI_ROOMS, PUBLIC_M3U_SOURCES

    imported = 0
    skipped = []
    existing_public = {r['url'] for r in db.get_sources('public')}
    for i, url in enumerate(PUBLIC_M3U_SOURCES):
        if url in existing_public:
            continue
        db.add_source('public', f'默认公开源 {i + 1}', url)
        imported += 1
    existing_bili = {r['url'] for r in db.get_sources('bilibili')}
    for item in BILIBILI_ROOMS:
        name = item['name']
        room_id = item.get('room_id')
        if room_id is None:
            from core.bilibili import BilibiliUtils
            room_id = BilibiliUtils.get_room_id(item.get('uid'))
            if room_id is None:
                skipped.append(name)
                continue
        url = str(room_id)
        if url in existing_bili:
            continue
        db.add_source('bilibili', name, url)
        imported += 1
    _after_config_change()
    _audit(f"导入默认源: 新增 {imported} 个，跳过 {len(skipped)} 个")
    return jsonify({'status': 'success', 'imported': imported, 'skipped': skipped})


@admin_api.route('/sources/refresh', methods=['POST'])
def sources_refresh():
    """立即触发异步聚合刷新 + 清播放列表缓存"""
    _after_config_change()
    _audit("触发聚合刷新")
    return jsonify({'status': 'success', 'message': '已请求后台刷新'})


@admin_api.route('/sources', methods=['GET', 'POST'])
def sources():
    from admin import db
    if request.method == 'GET':
        source_type = request.args.get('type')
        if source_type is not None and source_type not in _SOURCE_TYPES:
            return jsonify({'error': 'type 必须为 public 或 bilibili'}), 400
        enabled = request.args.get('enabled')
        if enabled is not None and enabled not in ('0', '1'):
            return jsonify({'error': 'enabled 必须为 0 或 1'}), 400
        q = (request.args.get('q') or '').strip().lower()
        sort = request.args.get('sort', 'sort_order')
        order = request.args.get('order', 'asc')
        page, page_size = _paging_params()

        items = []
        for r in db.get_sources():
            if source_type is not None and r['type'] != source_type:
                continue
            if enabled is not None and r['enabled'] != int(enabled):
                continue
            if q and q not in (r['name'] or '').lower() and q not in (r['url'] or '').lower():
                continue
            item = dict(r)
            item['config_default'] = False
            items.append(item)
        # config 兜底行（该类型无启用源时展示"当前生效来源"，只读）
        for r in _config_fallback_sources(source_type):
            if enabled is not None and r['enabled'] != int(enabled):
                continue
            if q and q not in (r['name'] or '').lower() and q not in (r['url'] or '').lower():
                continue
            items.append(r)
        # 默认 sort_order 升序（管理端手排）；sort=id 按 id；order=desc 反转
        key_fn = (lambda x: x['id']) if sort == 'id' else (lambda x: x['sort_order'])
        items.sort(key=key_fn, reverse=(order == 'desc'))
        total = len(items)
        start = (page - 1) * page_size
        return jsonify(_envelope(items[start:start + page_size], total, page, page_size))

    # POST 新增源
    data = request.get_json(silent=True) or {}
    source_type = data.get('type')
    name = (data.get('name') or '').strip()
    url = (data.get('url') or '').strip() or None
    if source_type not in _SOURCE_TYPES:
        return jsonify({'error': 'type 必须为 public 或 bilibili'}), 400
    if not name:
        return jsonify({'error': 'name 不能为空'}), 400
    if source_type == 'public' and not url:
        return jsonify({'error': 'public 源必须提供 url'}), 400
    if source_type == 'bilibili' and url is not None and not url.isdigit():
        return jsonify({'error': 'bilibili 源 url 必须为房间号数字'}), 400
    enabled = data.get('enabled', True)
    if not isinstance(enabled, bool):
        return jsonify({'error': 'enabled 必须为布尔值'}), 400
    try:
        sort_order = int(data.get('sort_order') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'sort_order 必须为整数'}), 400
    source_id = db.add_source(source_type, name, url,
                              enabled=1 if enabled else 0,
                              sort_order=sort_order)
    if source_id is None:
        return jsonify({'error': '写入失败'}), 500
    _after_config_change()
    _audit(f"新增源: {source_type} {name}")
    return jsonify({'status': 'success', 'id': source_id}), 201


@admin_api.route('/sources/<int:source_id>', methods=['PUT', 'DELETE'])
def source_item(source_id):
    from admin import db
    if request.method == 'DELETE':
        if not db.delete_source(source_id):
            return jsonify({'error': f'源 {source_id} 不存在'}), 404
        _after_config_change()
        _audit(f"删除源 #{source_id}")
        return jsonify({'status': 'success'})

    data = request.get_json(silent=True) or {}
    fields = {k: data[k] for k in ('name', 'url', 'enabled', 'sort_order') if k in data}
    if not fields:
        return jsonify({'error': '至少提供一个字段'}), 400
    if 'name' in fields and not (fields['name'] or '').strip():
        return jsonify({'error': 'name 不能为空'}), 400
    if 'enabled' in fields and not isinstance(fields['enabled'], bool):
        return jsonify({'error': 'enabled 必须为布尔值'}), 400
    # url 校验按源类型：bilibili 存房间号数字；public 不能清空
    if 'url' in fields:
        row = next((r for r in db.get_sources() if r['id'] == source_id), None)
        if row is None:
            return jsonify({'error': f'源 {source_id} 不存在'}), 404
        url = (fields['url'] or '').strip() or None
        if row['type'] == 'public' and not url:
            return jsonify({'error': 'public 源的 url 不能为空'}), 400
        if row['type'] == 'bilibili' and url is not None and not url.isdigit():
            return jsonify({'error': 'bilibili 源 url 必须为房间号数字'}), 400
        fields['url'] = url
    try:
        if 'sort_order' in fields:
            fields['sort_order'] = int(fields['sort_order'])
    except (TypeError, ValueError):
        return jsonify({'error': 'sort_order 必须为整数'}), 400
    affected = db.update_source(source_id, **fields)
    if not affected:
        return jsonify({'error': f'源 {source_id} 不存在或无变化'}), 404
    _after_config_change()
    _audit(f"更新源 #{source_id}: {', '.join(fields)}")
    return jsonify({'status': 'success'})


# ---------------------------------------------------------------- 频道覆盖

@admin_api.route('/channels', methods=['GET'])
def channels():
    from admin import db
    sort = request.args.get('sort', '')
    order = request.args.get('order', 'asc')
    q = (request.args.get('q') or '').strip().lower()
    page, page_size = _paging_params()

    overrides = db.get_channel_overrides()
    # display_name → channel_key 反查：改名后的频道在 m3u 中显示为新名
    display_to_key = {}
    for key, ov in overrides.items():
        if ov.get('display_name'):
            display_to_key[ov['display_name']] = key

    items = []
    seen = set()
    for ch in _load_current_channels():
        key = display_to_key.get(ch['name'], ch['key'])
        ov = overrides.get(key)
        seen.add(key)
        # 覆盖 enabled=0：频道已从 m3u 消失，但聚合缓存可能尚未刷新，
        # 这里按覆盖层语义直接呈现为禁用（url=null）
        disabled = bool(ov and ov.get('enabled') == 0)
        items.append({
            'key': key,
            'name': ch['name'],
            'group': ch['group'],
            'url': None if disabled else ch['url'],
            'enabled': not disabled,
            'override': _override_view(ov),
        })

    # 被禁用的频道不在 m3u 中：补虚拟记录（url=null），便于管理端重新启用
    for key, ov in overrides.items():
        if key in seen or ov.get('enabled') != 0:
            continue
        items.append({
            'key': key,
            'name': ov.get('display_name') or key,
            'group': ov.get('group_title') or '未知',
            'url': None,
            'enabled': False,
            'override': _override_view(ov),
        })

    if q:
        items = [i for i in items if q in i['name'].lower() or q in i['key'].lower()]
    if sort == 'name':
        items.sort(key=lambda i: i['name'], reverse=(order == 'desc'))
    elif sort == 'group':
        items.sort(key=lambda i: (i['group'], i['name']), reverse=(order == 'desc'))
    total = len(items)
    start = (page - 1) * page_size
    return jsonify(_envelope(items[start:start + page_size], total, page, page_size))


@admin_api.route('/channels/<path:key>', methods=['PUT', 'DELETE'])
def channel_item(key):
    from admin import db
    if request.method == 'DELETE':
        if not db.delete_channel_override(key):
            return jsonify({'error': f'频道 {key} 无覆盖配置'}), 404
        _after_config_change()
        _audit(f"清除频道覆盖: {key}")
        return jsonify({'status': 'success'})

    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled')
    group_title = data.get('group_title')
    display_name = data.get('display_name')
    if enabled is None and group_title is None and display_name is None:
        return jsonify({'error': '至少提供 enabled/group_title/display_name 之一'}), 400
    if enabled is not None and not isinstance(enabled, bool):
        return jsonify({'error': 'enabled 必须为布尔值'}), 400
    for field, value in (('group_title', group_title), ('display_name', display_name)):
        if value is not None and not isinstance(value, str):
            return jsonify({'error': f'{field} 必须为字符串'}), 400
    db.upsert_channel_override(key, display_name, group_title, enabled)
    _after_config_change()
    changed = [k for k in ('enabled', 'group_title', 'display_name') if k in data]
    _audit(f"频道覆盖 {key}: {', '.join(changed)}")
    return jsonify({'status': 'success'})


# ---------------------------------------------------------------- 监控

@admin_api.route('/monitor/summary', methods=['GET'])
def monitor_summary():
    from admin import db
    from monitoring.checks import CheckUtils
    latest = db.get_monitor_history(1)
    recent = db.get_monitor_history(24)
    return jsonify({
        'health': {'status': CheckUtils._last_status, 'fail_count': CheckUtils._fail_count},
        'stream': {'status': CheckUtils._stream_last_status,
                   'fail_count': CheckUtils._stream_fail_count},
        'last_check': latest[0] if latest else None,
        'recent': [{'ts': r['ts'], 'overall': r['overall'],
                    'channel_count': r['channel_count'], 'epg_size': r['epg_size']}
                   for r in reversed(recent)],
    })


@admin_api.route('/monitor/history', methods=['GET'])
def monitor_history():
    from admin import db
    page, page_size = _paging_params()
    rows = db.get_monitor_history(page_size, (page - 1) * page_size)
    return jsonify(_envelope(rows, db.count_monitor_history(), page, page_size))


@admin_api.route('/monitor/streams', methods=['GET'])
def monitor_streams():
    from admin import db
    unreachable = request.args.get('unreachable') == '1'
    q = (request.args.get('q') or '').strip()
    sort = request.args.get('sort', 'id')
    order = request.args.get('order', 'desc')
    if sort not in ('id', 'ts', 'ok'):  # 白名单，非法值回退默认（契约：宽松）
        sort = 'id'
    if order not in ('asc', 'desc'):
        order = 'desc'
    page, page_size = _paging_params()
    rows = db.get_stream_history(page_size, (page - 1) * page_size,
                                 unreachable_only=unreachable, keyword=q or None,
                                 sort=sort, order=order)
    total = db.count_stream_history(unreachable_only=unreachable, keyword=q or None)
    return jsonify(_envelope(rows, total, page, page_size))


# ---------------------------------------------------------------- 日志

@admin_api.route('/logs', methods=['GET'])
def logs():
    from admin import db
    level = (request.args.get('level') or '').upper()
    if level and level not in _LOG_LEVELS:
        return jsonify({'error': 'level 必须为 ERROR/WARNING/INFO'}), 400
    q = (request.args.get('q') or '').strip()
    page, page_size = _paging_params()
    rows = db.get_logs(page_size, (page - 1) * page_size,
                       level=level or None, keyword=q or None)
    total = db.count_logs(level=level or None, keyword=q or None)
    return jsonify(_envelope(rows, total, page, page_size))


# ---------------------------------------------------------------- 设置

# 设置白名单：key -> (存储格式, 描述)；密钥类（API_TOKEN/email 等）不落库，保持 .env
_SETTING_KEYS = {
    'bilibili_only_mode': 'bool',
    'min_channel_count': 'int',
    'stream_fail_limit': 'int',
    'monitor_history_keep': 'int',
    'stream_history_keep': 'int',
    'log_keep_days': 'int',
    'group_health_ratios': 'json',
    'public_base_url': 'str',
    'alert_enabled': 'bool',
    'alert_recipients': 'str',
    # 定时任务周期（秒）与监控探测参数（下一轮生效）
    'aggregate_refresh_interval': 'int',
    'official_refresh_interval': 'int',
    'monitor_interval': 'int',
    'stream_check_interval': 'int',
    'monitor_window_start': 'int',
    'monitor_window_end': 'int',
    'startup_delay': 'int',
    'stream_check_concurrency': 'int',
    'stream_probe_timeout': 'int',
}


def _settings_effective():
    """全部设置的有效值（DB 优先、config 兜底）"""
    from admin import db
    from config import (AGGREGATE_REFRESH_INTERVAL, CHECK_INTERVAL,
                        CHECK_WINDOW_END_HOUR, CHECK_WINDOW_START_HOUR,
                        GROUP_HEALTH_RATIOS, LOG_KEEP_DAYS, MIN_CHANNEL_COUNT,
                        MONITOR_HISTORY_KEEP, OFFICIAL_REFRESH_INTERVAL,
                        PUBLIC_BASE_URL, STARTUP_DELAY, STREAM_CHECK_CONCURRENCY,
                        STREAM_CHECK_INTERVAL, STREAM_FAIL_LIMIT,
                        STREAM_HISTORY_KEEP, STREAM_PROBE_TIMEOUT)
    return {
        'bilibili_only_mode': AggregatorUtils.is_bilibili_only_mode(),
        'min_channel_count': db.get_effective_int('min_channel_count', MIN_CHANNEL_COUNT),
        'stream_fail_limit': db.get_effective_int('stream_fail_limit', STREAM_FAIL_LIMIT),
        'monitor_history_keep': db.get_effective_int(
            'monitor_history_keep', MONITOR_HISTORY_KEEP),
        'stream_history_keep': db.get_effective_int(
            'stream_history_keep', STREAM_HISTORY_KEEP),
        'log_keep_days': db.get_effective_int('log_keep_days', LOG_KEEP_DAYS),
        'group_health_ratios': db.get_effective_json(
            'group_health_ratios', GROUP_HEALTH_RATIOS),
        'public_base_url': AggregatorUtils._public_base_url(),
        'alert_enabled': db.is_alert_enabled(default=True),
        'alert_recipients': db.get_effective_str('alert_recipients', None) or '',
        'aggregate_refresh_interval': db.get_effective_int(
            'aggregate_refresh_interval', AGGREGATE_REFRESH_INTERVAL),
        'official_refresh_interval': db.get_effective_int(
            'official_refresh_interval', OFFICIAL_REFRESH_INTERVAL),
        'monitor_interval': db.get_effective_int('monitor_interval', CHECK_INTERVAL),
        'stream_check_interval': db.get_effective_int(
            'stream_check_interval', STREAM_CHECK_INTERVAL),
        'monitor_window_start': db.get_effective_int(
            'monitor_window_start', CHECK_WINDOW_START_HOUR),
        'monitor_window_end': db.get_effective_int(
            'monitor_window_end', CHECK_WINDOW_END_HOUR),
        'startup_delay': db.get_effective_int('startup_delay', STARTUP_DELAY),
        'stream_check_concurrency': db.get_effective_int(
            'stream_check_concurrency', STREAM_CHECK_CONCURRENCY),
        'stream_probe_timeout': db.get_effective_int(
            'stream_probe_timeout', STREAM_PROBE_TIMEOUT),
    }


@admin_api.route('/settings', methods=['GET', 'PUT'])
def settings():
    from admin import db
    if request.method == 'GET':
        return jsonify({
            'settings': db.get_settings(),
            'effective': _settings_effective(),
        })

    data = request.get_json(silent=True) or {}
    updates = {k: v for k, v in data.items() if k in _SETTING_KEYS}
    if not updates:
        return jsonify({'error': '无支持的设置字段（支持：' + '、'.join(_SETTING_KEYS) + '）'}), 400

    # 校验
    for key, value in updates.items():
        kind = _SETTING_KEYS[key]
        if kind == 'bool':
            if not isinstance(value, bool):
                return jsonify({'error': f'{key} 必须为布尔值'}), 400
        elif kind == 'int':
            if isinstance(value, bool) or not isinstance(value, int):
                return jsonify({'error': f'{key} 必须为整数'}), 400
            if key == 'monitor_window_start':
                if not (0 <= value <= 23):
                    return jsonify({'error': f'{key} 必须在 0-23 之间（GMT+8 小时）'}), 400
            elif key == 'monitor_window_end':
                if not (1 <= value <= 24):
                    return jsonify({'error': f'{key} 必须在 1-24 之间（GMT+8 小时）'}), 400
            elif value < 1:
                return jsonify({'error': f'{key} 必须为不小于 1 的整数'}), 400
        elif kind == 'str':
            if not isinstance(value, str):
                return jsonify({'error': f'{key} 必须为字符串'}), 400
            if key == 'public_base_url':
                if not value.strip().startswith(('http://', 'https://')):
                    return jsonify({'error': f'{key} 必须以 http:// 或 https:// 开头'}), 400
                updates[key] = value.strip()
            elif key == 'alert_recipients':
                # 逗号分隔的邮箱列表，可留空（留空 = 收件人默认发件人自身）
                items = [v.strip() for v in value.split(',') if v.strip()]
                if any('@' not in v for v in items):
                    return jsonify({'error': f'{key} 必须是逗号分隔的有效邮箱'}), 400
                updates[key] = ', '.join(items)
        elif kind == 'json':
            if not isinstance(value, dict) or not all(
                    isinstance(k, str) and isinstance(v, (int, float))
                    and not isinstance(v, bool) and 0 < v <= 1
                    for k, v in value.items()):
                return jsonify({'error': f'{key} 必须为 JSON 对象（组名 -> 0~1 数值）'}), 400
            updates[key] = json.dumps(value, ensure_ascii=False)

    # 落库（bool/int 统一存字符串）
    for key, value in updates.items():
        db.set_setting(key, value if isinstance(value, str) else str(value).lower())
    _after_config_change()
    _audit(f"设置变更: {', '.join(updates)}")
    return jsonify({'status': 'success', 'effective': _settings_effective()})
