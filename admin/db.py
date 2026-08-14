"""管理数据层：SQLite 单文件（源配置/频道覆盖/监控历史/日志/设置）

- 零依赖（只 import sqlite3/os/threading/config），被 core/monitoring 单向引用，避免循环导入
- 每操作短连接（check_same_thread=False + 独立连接），模块级写锁串行化并发写
- 所有写操作 try/except 兜底：数据层失败不影响核心业务（聚合/监控照常跑）
- 运行时设置（get_effective_*）：DB 优先、config 兜底，供监控/聚合/清理动态读取
"""
import datetime
import json
import os
import sqlite3
import threading

from config import (ADMIN_DB_PATH, GMT8, LOG_KEEP_DAYS, MONITOR_HISTORY_KEEP,
                    STREAM_HISTORY_KEEP)

# 模块级写锁：SQLite 并发写串行化（监控线程 + API 线程）。
# 用 RLock：数据层写失败打日志 → SqliteHandler 回写 save_log 会重入本锁（同线程）
_db_lock = threading.RLock()

# 表结构定义（首次建库时执行）
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  url TEXT,
  name TEXT NOT NULL,
  enabled INTEGER DEFAULT 1,
  sort_order INTEGER DEFAULT 0,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS channel_overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_key TEXT UNIQUE NOT NULL,
  display_name TEXT,
  group_title TEXT,
  enabled INTEGER DEFAULT 1,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS monitor_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  health_ok INTEGER, m3u_ok INTEGER, epg_ok INTEGER,
  channel_count INTEGER, epg_size INTEGER,
  overall INTEGER
);
CREATE TABLE IF NOT EXISTS stream_check_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  group_name TEXT, channel_name TEXT, url TEXT,
  ok INTEGER,
  round_id TEXT
);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT, module TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE INDEX IF NOT EXISTS idx_monitor_ts ON monitor_history(ts);
CREATE INDEX IF NOT EXISTS idx_stream_ts ON stream_check_history(ts);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
"""


def _now():
    """当前 GMT+8 时间字符串（与项目其他时间一致）"""
    return datetime.datetime.now(tz=GMT8).strftime('%Y-%m-%d %H:%M:%S')


def _connect():
    """新建数据库连接（每操作短连接）"""
    conn = sqlite3.connect(ADMIN_DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _execute_locked(sql, params=()):
    """持锁状态下执行写操作（内部用，调用方须已持有 _db_lock）；
    成功返回受影响行数，异常返回 None（不抛出，数据层失败不影响核心业务）"""
    try:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except Exception as e:
        _get_db_logger().warning(f"管理数据写入失败: {str(e)}")
        return None


def _execute(sql, params=()):
    """执行写操作（加锁串行化），返回受影响行数（失败返回 0）"""
    with _db_lock:
        result = _execute_locked(sql, params)
    return 0 if result is None else result


# 数据层日志（惰性引用 core.logger：叶子模块，不反向依赖 admin，无循环导入；
# 保持模块级"零依赖"约定，仅异常路径使用）
_db_logger = None


def _get_db_logger():
    global _db_logger
    if _db_logger is None:
        from core.logger import get_logger
        _db_logger = get_logger('admin_db')
    return _db_logger


def _query(sql, params=()):
    """执行查询，返回 dict 列表（只读不加锁）"""
    try:
        conn = _connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        _get_db_logger().warning(f"管理数据读取失败: {str(e)}")
        return []


def init_db():
    """建库建表（首次自动创建；目录不存在自动创建）"""
    try:
        os.makedirs(os.path.dirname(ADMIN_DB_PATH), exist_ok=True)
        with _db_lock:
            conn = _connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception as e:
        _get_db_logger().warning(f"管理数据库初始化失败: {str(e)}")
        return False


def db_ready():
    """管理库是否已初始化（文件存在且已建 sources 表）。

    未初始化时调用方直接走 config 兜底（种子值），避免查询时
    凭空创建空库文件、污染缓存目录，也避免对无表空库反复报错。
    """
    try:
        if not os.path.exists(ADMIN_DB_PATH):
            return False
        rows = _query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sources'")
        return bool(rows)
    except Exception:
        return False


# ---------------------------------------------------------------- 运行时设置

def _effective_raw(key):
    """settings 表原始值；未初始化/异常返回 None（不抛错）"""
    try:
        if db_ready():
            return get_setting(key)
    except Exception:
        pass
    return None


def get_effective_int(key, default):
    """DB 设置优先的整数读取：未初始化/未设置/解析失败一律回退 default"""
    raw = _effective_raw(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def get_effective_str(key, default):
    """DB 设置优先的字符串读取（strip），未设置回退 default"""
    raw = _effective_raw(key)
    if raw is None:
        return default
    return raw.strip()


def get_effective_bool(key, default):
    """DB 设置优先的布尔读取（'1'/'true'/'yes'/'on' 视为真），回退 default"""
    raw = _effective_raw(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def get_effective_json(key, default):
    """DB 设置优先的 JSON 对象读取（非法 JSON/非 dict 回退 default）"""
    raw = _effective_raw(key)
    if raw is None:
        return default
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def is_alert_enabled(default=True):
    """告警开关：DB 设置 alert_enabled 布尔优先，未设置回退 default"""
    return get_effective_bool('alert_enabled', default)


# ---------------------------------------------------------------- 源配置

def get_sources(source_type=None):
    """查询源列表；type 为 None 返回全部，否则按类型过滤"""
    if source_type:
        return _query("SELECT * FROM sources WHERE type=? ORDER BY sort_order, id",
                      (source_type,))
    return _query("SELECT * FROM sources ORDER BY type, sort_order, id")


def get_enabled_public_urls():
    """已启用的公开源 url 列表（供聚合；空则调用方回退 config）"""
    rows = _query("SELECT url FROM sources WHERE type='public' AND enabled=1 "
                  "AND url IS NOT NULL AND url!='' ORDER BY sort_order, id")
    return [r['url'] for r in rows]


def get_enabled_bilibili_rooms():
    """已启用的 B 站房间配置列表 [{name, room_id} 或 {name, uid}]（供聚合；空则回退 config）"""
    rows = _query("SELECT * FROM sources WHERE type='bilibili' AND enabled=1 "
                  "ORDER BY sort_order, id")
    result = []
    for r in rows:
        item = {"name": r['name']}
        # url 字段存 room_id 或 uid（P2 定义：url 存 room_id）
        if r['url'] and str(r['url']).isdigit():
            item['room_id'] = int(r['url'])
        result.append(item)
    return result


def add_source(source_type, name, url=None, enabled=1, sort_order=0):
    """新增源；返回新 id 或 None"""
    ts = _now()
    _execute(
        "INSERT INTO sources (type, url, name, enabled, sort_order, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (source_type, url, name, 1 if enabled else 0, sort_order, ts, ts))
    rows = _query("SELECT id FROM sources WHERE type=? AND name=? ORDER BY id DESC LIMIT 1",
                  (source_type, name))
    return rows[0]['id'] if rows else None


def update_source(source_id, name=None, url=None, enabled=None, sort_order=None):
    """更新源；字段为 None 表示不改"""
    sets, params = [], []
    if name is not None:
        sets.append("name=?")
        params.append(name)
    if url is not None:
        sets.append("url=?")
        params.append(url)
    if enabled is not None:
        sets.append("enabled=?")
        params.append(1 if enabled else 0)
    if sort_order is not None:
        sets.append("sort_order=?")
        params.append(sort_order)
    if not sets:
        return 0
    sets.append("updated_at=?")
    params.append(_now())
    params.append(source_id)
    return _execute(f"UPDATE sources SET {', '.join(sets)} WHERE id=?", params)


def delete_source(source_id):
    """删除源"""
    return _execute("DELETE FROM sources WHERE id=?", (source_id,))


# ---------------------------------------------------------------- 频道覆盖

def get_channel_overrides():
    """全部频道覆盖 {channel_key: dict}"""
    rows = _query("SELECT * FROM channel_overrides")
    return {r['channel_key']: r for r in rows}


def get_channel_override(channel_key):
    """单个频道覆盖 dict 或 None"""
    rows = _query("SELECT * FROM channel_overrides WHERE channel_key=?", (channel_key,))
    return rows[0] if rows else None


def upsert_channel_override(channel_key, display_name=None, group_title=None, enabled=None):
    """新增或更新频道覆盖（key 唯一）"""
    existing = get_channel_override(channel_key)
    if existing:
        sets, params = [], []
        if display_name is not None:
            sets.append("display_name=?")
            params.append(display_name)
        if group_title is not None:
            sets.append("group_title=?")
            params.append(group_title)
        if enabled is not None:
            sets.append("enabled=?")
            params.append(1 if enabled else 0)
        if not sets:
            return 0
        sets.append("updated_at=?")
        params.append(_now())
        params.append(channel_key)
        return _execute(f"UPDATE channel_overrides SET {', '.join(sets)} "
                        "WHERE channel_key=?", params)
    _execute(
        "INSERT INTO channel_overrides (channel_key, display_name, group_title, enabled, updated_at) "
        "VALUES (?,?,?,?,?)",
        (channel_key, display_name, group_title, 1 if enabled is None else (1 if enabled else 0),
         _now()))
    return 1


def delete_channel_override(channel_key):
    """删除频道覆盖（恢复默认）"""
    return _execute("DELETE FROM channel_overrides WHERE channel_key=?", (channel_key,))


# ---------------------------------------------------------------- 监控历史

def save_monitor_history(health_ok, m3u_ok, epg_ok, channel_count, epg_size, overall):
    """保存一轮常规健康检测结果，并清理超量旧数据"""
    _execute(
        "INSERT INTO monitor_history (ts, health_ok, m3u_ok, epg_ok, channel_count, epg_size, overall) "
        "VALUES (?,?,?,?,?,?,?)",
        (_now(), 1 if health_ok else 0, 1 if m3u_ok else 0, 1 if epg_ok else 0,
         channel_count, epg_size, 1 if overall else 0))
    # 清理：只保留最近 N 轮（DB 设置优先，config 兜底）
    _execute("DELETE FROM monitor_history WHERE id NOT IN "
             "(SELECT id FROM monitor_history ORDER BY id DESC LIMIT ?)",
             (get_effective_int('monitor_history_keep', MONITOR_HISTORY_KEEP),))


def get_monitor_history(limit=100, offset=0):
    """最近 N 轮健康检测历史（新→旧），offset 分页"""
    return _query("SELECT * FROM monitor_history ORDER BY id DESC LIMIT ? OFFSET ?",
                  (limit, offset))


def count_monitor_history():
    """健康检测历史总轮数"""
    rows = _query("SELECT COUNT(*) AS n FROM monitor_history")
    return rows[0]['n'] if rows else 0


def save_stream_history_batch(rows):
    """批量保存一轮流探测记录（每频道一条），整轮落库后只清理一次。

    rows: [(ts, group_name, channel_name, url, ok, round_id), ...]
    相比逐条 save_stream_history，一轮约 70 条记录只在锁内写一遍、清理一次，
    避免每 30 分钟一轮的探测逐条触发全表 DELETE。
    """
    if not rows:
        return 0
    _sql = ("INSERT INTO stream_check_history (ts, group_name, channel_name, url, ok, round_id) "
            "VALUES (?,?,?,?,?,?)")
    with _db_lock:
        for ts, group_name, channel_name, url, ok, round_id in rows:
            _execute_locked(_sql, (ts, group_name, channel_name, url, 1 if ok else 0, round_id))
        # 整轮清理：只保留最近 N 条（按 id 倒序；DB 设置优先，config 兜底）
        _execute_locked("DELETE FROM stream_check_history WHERE id NOT IN "
                        "(SELECT id FROM stream_check_history ORDER BY id DESC LIMIT ?)",
                        (get_effective_int('stream_history_keep', STREAM_HISTORY_KEEP),))
    return len(rows)


def save_stream_history(ts, group_name, channel_name, url, ok, round_id):
    """保存一条流探测记录（单条便捷接口，等价于单元素批量写入）"""
    return save_stream_history_batch([(ts, group_name, channel_name, url, ok, round_id)])


# 流探测排序白名单：sort 参数 → 排序列（order=asc 反转方向）
_STREAM_SORT_COLS = {'id': 'id', 'ts': 'ts', 'ok': 'ok'}


def _stream_order_clause(sort, order):
    """流探测列表 ORDER BY 片段（白名单列，防止 SQL 注入）"""
    col = _STREAM_SORT_COLS.get(sort, 'id')
    direction = 'ASC' if order == 'asc' else 'DESC'
    return f"{col} {direction}, id {direction}"


def get_stream_history(limit=200, offset=0, unreachable_only=False,
                       keyword=None, sort='id', order='desc'):
    """最近流探测记录，支持不可达过滤/关键词/排序/offset 分页"""
    where, params = [], []
    if unreachable_only:
        where.append("ok=0")
    if keyword:
        where.append("(channel_name LIKE ? OR url LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like])
    sql = "SELECT * FROM stream_check_history"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + _stream_order_clause(sort, order) + " LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return _query(sql, params)


def count_stream_history(unreachable_only=False, keyword=None):
    """流探测记录总数（与 get_stream_history 同过滤口径）"""
    where, params = [], []
    if unreachable_only:
        where.append("ok=0")
    if keyword:
        where.append("(channel_name LIKE ? OR url LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like])
    sql = "SELECT COUNT(*) AS n FROM stream_check_history"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = _query(sql, params)
    return rows[0]['n'] if rows else 0


# ---------------------------------------------------------------- 日志

# 上次日志清理日期（GMT+8），用于「每天至多清理一次」的节流
_last_log_prune_date = None


def _prune_expired_logs():
    """清理超过 LOG_KEEP_DAYS 天的日志；每天（GMT+8）至多执行一次。

    ts 为 GMT+8 定宽字符串（YYYY-MM-DD HH:MM:SS），字典序比较即时间序比较；
    阈值同样按 GMT+8 计算，不依赖容器本地时区（AGENTS.md：全程 GMT+8）。
    """
    global _last_log_prune_date
    now = datetime.datetime.now(tz=GMT8)
    today = now.strftime('%Y-%m-%d')
    with _db_lock:
        if _last_log_prune_date == today:  # 锁内二次确认，防止并发重复清理
            return
        cutoff = (now - datetime.timedelta(
            days=get_effective_int('log_keep_days', LOG_KEEP_DAYS))).strftime('%Y-%m-%d %H:%M:%S')
        if _execute_locked("DELETE FROM logs WHERE ts < ?", (cutoff,)) is not None:
            _last_log_prune_date = today


def save_log(ts, level, module, message):
    """写一条日志（WARNING+ 才由 logger 调用，防膨胀）"""
    _execute("INSERT INTO logs (ts, level, module, message) VALUES (?,?,?,?)",
             (ts, level, module, message[:2000]))
    # 清理超期日志：每天（GMT+8）至多一次，避免每条日志都触发全表 DELETE
    _prune_expired_logs()


def record_event(level, module, message):
    """记录关键事件（白名单，量小不膨胀）：管理操作审计/服务启停/聚合完成/告警结果。

    与 SqliteHandler 的「WARNING+ 才入库」门槛不同，本函数显式入库任意级别，
    供管理页日志查询（INFO 级别即关键事件）。
    """
    save_log(_now(), level, module, message[:2000])


def get_logs(limit=200, offset=0, level=None, keyword=None):
    """查询日志（新→旧）；level/keyword 可选过滤，offset 分页"""
    sql = "SELECT * FROM logs WHERE 1=1"
    params = []
    if level:
        sql += " AND level=?"
        params.append(level.upper())
    if keyword:
        sql += " AND (message LIKE ? OR module LIKE ?)"
        like = f"%{keyword}%"
        params.extend([like, like])
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return _query(sql, params)


def count_logs(level=None, keyword=None):
    """日志总数（与 get_logs 同过滤口径）"""
    sql = "SELECT COUNT(*) AS n FROM logs WHERE 1=1"
    params = []
    if level:
        sql += " AND level=?"
        params.append(level.upper())
    if keyword:
        sql += " AND (message LIKE ? OR module LIKE ?)"
        like = f"%{keyword}%"
        params.extend([like, like])
    rows = _query(sql, params)
    return rows[0]['n'] if rows else 0


# ---------------------------------------------------------------- 设置

def get_setting(key, default=None):
    """读设置项"""
    rows = _query("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]['value'] if rows else default


def get_settings():
    """全部设置项 {key: value}"""
    rows = _query("SELECT * FROM settings")
    return {r['key']: r['value'] for r in rows}


def set_setting(key, value):
    """写设置项（UPSERT）"""
    existing = get_setting(key)
    if existing is None:
        _execute("INSERT INTO settings (key, value) VALUES (?,?)", (key, value))
    else:
        _execute("UPDATE settings SET value=? WHERE key=?", (value, key))
