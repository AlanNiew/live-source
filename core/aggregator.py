"""聚合编排：多源合并去重择优、探测过滤、缓存落盘、跨轮失败记录"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from config import (AGGREGATED_M3U_PATH, BILIBILI_GROUP_NAME, BILIBILI_ONLY_MODE,
                    BILIBILI_ROOMS, CHANNEL_OVERRIDE_CACHE_TTL, FILTER_UNREACHABLE,
                    GROUP_ORDER, HNTV_GROUP_NAME, PUBLIC_BASE_URL,
                    PUBLIC_CHANNELS_CACHE_PATH, STREAM_CHECK_CONCURRENCY,
                    STREAM_FAILURES_PATH, STREAM_FAIL_LIMIT, STREAM_PROBE_UA_LOOSE)
from core.atomic_io import atomic_write_text
from core.bilibili import BilibiliUtils
from core.hntv_client import ApiUtils
from core.logger import get_logger
from core.probing import probe_stream
from core.sources import SourceUtils

_logger = get_logger('aggregator')


def _log(msg):
    """聚合日志统一走 logger（线程名由 logger format 自动带上）"""
    _logger.info(msg)


# 异步刷新协调：POST 添加/删除房间后请求立即聚合刷新。
# _refresh_pending 标志用于合并多次请求（只保留一个 worker），
# worker 阻塞获取 _aggregate_lock 保证与定时任务不并发。
_refresh_pending = False
_refresh_flag_lock = threading.Lock()

# 聚合完成回调（app 层注册：刷新完成后清播放列表缓存）。
# 定时/手动刷新统一走这里，避免配置变更后旧缓存再被请求缓存 10 分钟
_refresh_callbacks = []


def register_refresh_callback(cb):
    """注册聚合完成回调（create_app 注册 flask 缓存清理；回调异常不阻断聚合）"""
    if cb not in _refresh_callbacks:
        _refresh_callbacks.append(cb)


def _fire_refresh_callbacks():
    """聚合结果已落盘后触发全部回调（尽力而为，异常吞掉）"""
    for cb in list(_refresh_callbacks):
        try:
            cb()
        except Exception:
            pass

# 频道覆盖层内存缓存（管理后台配置的禁用/改分组/改名）：
# {channel_key: {enabled, display_name, group_title}}，TTL 见 CHANNEL_OVERRIDE_CACHE_TTL。
# 聚合与频道列表共享；查询失败回退空 dict（不阻断聚合）
_override_cache = {"expire": 0.0, "data": {}}


def _get_channel_overrides():
    """频道覆盖配置 {channel_key: row}，带 TTL 内存缓存（未初始化库直接回退空）"""
    now = time.time()
    if now >= _override_cache["expire"]:
        try:
            from admin import db
            _override_cache["data"] = db.get_channel_overrides() if db.db_ready() else {}
        except Exception:
            _override_cache["data"] = {}
        _override_cache["expire"] = now + CHANNEL_OVERRIDE_CACHE_TTL
    return _override_cache["data"]


class AggregatorUtils:
    """多源直播源聚合工具类"""

    # 聚合互斥锁：同一时刻只允许一个完整聚合在跑。
    # 启动时后台聚合进行中，首请求触发 get_aggregated_m3u 会拿不到锁，
    # 由 load_aggregated_m3u 降级返回官方源列表（秒级响应），避免重复全量聚合与长阻塞
    _aggregate_lock = threading.Lock()

    @staticmethod
    def _extract_hntv_item(item):
        """
        从 HNTV 官方频道条目提取聚合字段
        :param item: 官方接口返回的单个频道 dict
        :return: 聚合频道 dict（无可用流地址返回 None）
        """
        name = item.get('name', 'Unknown')
        cid = item.get('cid')
        streams = item.get('video_streams') or item.get('streams', [])
        if not streams:
            return None
        # 数据层只保留原始 cid；tvg-id 的显示值（str(cid) 兜底 name 或直拼）由各输出点决定
        return {
            "name": name,
            "cid": cid,
            "group_title": HNTV_GROUP_NAME,
            "url": streams[0],
        }

    @staticmethod
    def fetch_hntv_channels():
        """
        拉取 hntv 官方频道（优先级最高）
        :return: 频道 dict 列表（空列表表示拉取失败）
        """
        hntv_channels = []
        try:
            response = ApiUtils.get_hntv_live_list()
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    for item in data:
                        ch = AggregatorUtils._extract_hntv_item(item)
                        if ch:
                            hntv_channels.append(ch)
        except Exception as e:
            _log(f"拉取 hntv 官方源出错: {str(e)}")
        return hntv_channels

    @staticmethod
    def is_bilibili_only_mode():
        """
        B 站测试模式开关（聚合时实时判定）：
        - 管理 DB settings 表有 bilibili_only_mode 值 → 以 DB 为准（管理后台可切换）
        - 未设置/库未初始化 → 回退 env BILIBILI_ONLY_MODE
        调度线程布局在启动时按 env 定死，本开关只影响聚合轮次内容
        """
        from admin import db
        return db.get_effective_bool('bilibili_only_mode', BILIBILI_ONLY_MODE)

    @staticmethod
    def _stream_fail_limit():
        """聚合探测连续失败轮数：DB 设置优先，config 兜底"""
        from admin import db
        return db.get_effective_int('stream_fail_limit', STREAM_FAIL_LIMIT)

    @staticmethod
    def _public_base_url():
        """本服务对外基础地址（B 站频道 URL 生成用）：DB 设置优先，config/env 兜底。
        可在管理后台「设置」页改，无需重启"""
        from admin import db
        return db.get_effective_str('public_base_url', PUBLIC_BASE_URL)

    @staticmethod
    def _get_bilibili_static_rooms():
        """
        B 站静态房间配置来源：管理 DB（sources 表 type=bilibili）优先，
        空表/未初始化/异常回退 config.BILIBILI_ROOMS 种子值（行为与旧版一致）
        :return: [{name, room_id} 或 {name, uid}] 列表
        """
        try:
            from admin import db
            if db.db_ready():
                rooms = db.get_enabled_bilibili_rooms()
                if rooms:
                    return rooms
        except Exception:
            pass
        return BILIBILI_ROOMS

    @staticmethod
    def list_bilibili_rooms():
        """
        列出全部 B 站频道（静态配置 + 动态列表合并）：
        - 以 room_id 为唯一 key 去重，静态配置优先
        - 静态条目支持 room_id 直填或 uid（uid 走 get_room_id 解析，磁盘缓存兜底）
        :return: [{name, room_id, source}]，source 为 static/custom
        """
        result = []
        seen = set()

        for item in AggregatorUtils._get_bilibili_static_rooms():
            room_id = item.get("room_id")
            if room_id is None and item.get("uid") is not None:
                room_id = BilibiliUtils.get_room_id(item["uid"])
            if room_id is None or room_id in seen:
                continue
            seen.add(room_id)
            result.append({"name": item["name"], "room_id": room_id, "source": "static"})

        for item in BilibiliUtils.load_custom_rooms():
            room_id = item.get("room_id")
            if room_id is not None and room_id not in seen:
                seen.add(room_id)
                result.append({
                    "name": item.get("name") or f"房间{room_id}",
                    "room_id": room_id,
                    "source": "custom",
                })
        return result

    @staticmethod
    def fetch_bilibili_channels():
        """
        拉取 B 站直播频道（开播的才加入，未开播自动跳过）：
        - 数据源 = 静态配置 + 动态列表（room_id 去重，静态优先）
        - 实测主清单判定在播（playUrl 解析结果不可信，未开播也返回地址）
        - 地址为本服务代理 URL（播放器直连原始地址会 403 防盗链）
        :return: B 站直播频道 dict 列表（可能为空）
        """
        channels = []
        for item in AggregatorUtils.list_bilibili_rooms():
            room_id = item["room_id"]
            name = item["name"]
            if not BilibiliUtils.is_live(room_id):
                _log(f"B站直播跳过（未开播）: {name} (room={room_id})")
                continue
            channels.append({
                "name": name,
                "url": f"{AggregatorUtils._public_base_url().rstrip('/')}/api/bilibili/{room_id}/live.m3u8",
                "group_title": BILIBILI_GROUP_NAME,
                "tvg_name": name,
            })
            _log(f"B站直播已加入: {name} (room={room_id})")
        return channels

    @staticmethod
    def pick_best_public(public_channels):
        """
        公开源同台多来源择优（按地址质量、分辨率）
        :param public_channels: 已过滤+中文化的公开源频道
        :return: key -> (频道, 地址质量分, 分辨率) 的 dict
        """
        public_best = {}
        for ch in public_channels:
            key = SourceUtils.normalize_name(ch["name"])
            score = SourceUtils.score_url(ch["url"])
            res = ch.get("_resolution", 0)
            if key not in public_best:
                public_best[key] = (ch, score, res)
            else:
                _ch, old_score, old_res = public_best[key]
                # 地址质量更高，或质量相同但分辨率更高，则替换
                if score > old_score or (score == old_score and res > old_res):
                    public_best[key] = (ch, score, res)
        return public_best

    @staticmethod
    def aggregate_m3u(hntv_channels, public_channels, bilibili_channels=None):
        """
        合并 hntv 官方频道、公开源频道与 B 站直播频道：
        - 按频道名去重，hntv 官方源优先（同名保留官方地址）
        - 公开源只补充 hntv 没有的频道
        - 公开源内同台多个分辨率时，保留清晰度最高的一个
        - B 站直播频道独立分组，不参与同台去重（频道名不冲突）
        注：可达性探测过滤已在 prepare_public_channels 阶段完成（官方源永不探测）
        :param hntv_channels: hntv 官方频道列表（优先级最高）
        :param public_channels: 公开源频道列表（已过滤+中文化+探测过滤）
        :param bilibili_channels: B 站直播频道列表（已判定开播，可选）
        :return: 合并后的 m3u 文本
        """
        bilibili_channels = bilibili_channels or []
        merged = {}
        order = []  # 保持频道出现顺序，便于结果可读

        # hntv 官方源先入（优先级最高，同名时官方地址始终保留）
        for ch in hntv_channels:
            key = SourceUtils.normalize_name(ch["name"])
            if key not in merged:
                merged[key] = ch
                order.append(key)

        # 公开源补充 hntv 没有的频道（同台按地址质量/分辨率择优）
        public_best = AggregatorUtils.pick_best_public(public_channels)
        for key, (ch, _score, _res) in public_best.items():
            if key not in merged:
                merged[key] = ch
                order.append(key)

        # B 站直播频道（独立分组，直接追加；不参与去重）
        for ch in bilibili_channels:
            key = SourceUtils.normalize_name(ch["name"])
            if key not in merged:
                merged[key] = ch
                order.append(key)

        # 频道覆盖（管理后台：禁用/改分组/改名）：只在输出前应用，不改动择优/去重逻辑
        overrides = _get_channel_overrides()
        for key in list(order):
            ov = overrides.get(key)
            if not ov:
                continue
            if ov.get("enabled") == 0:
                merged.pop(key, None)
                order.remove(key)
                continue
            if ov.get("display_name"):
                merged[key] = dict(merged[key], name=ov["display_name"])
            if ov.get("group_title"):
                merged[key] = dict(merged[key], group_title=ov["group_title"])

        # 分组顺序：河南卫视（hntv官方）-> 央视 -> 卫视（健康率低放最后）-> B站直播，其余兜底
        order.sort(key=lambda k: GROUP_ORDER.get(merged[k]["group_title"], 3))

        # 生成 m3u 文本
        m3u_content = "#EXTM3U\n\n"
        for key in order:
            ch = merged[key]
            # tvg-id/tvg-name 取值：hntv 官方频道用 cid（str 兜底 name），公开源频道用其 tvg_name
            if "cid" in ch:
                tvg_id = str(ch["cid"]) if ch["cid"] is not None else ch["name"]
            else:
                tvg_id = ch["tvg_name"]
            m3u_content += (
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{tvg_id}" '
                f'group-title="{ch["group_title"]}",{ch["name"]}\n'
                f'{ch["url"]}\n\n'
            )

        _log(f"聚合完成：hntv {len(hntv_channels)} 个 + 公开补充 "
              f"{len(merged) - len(hntv_channels) - len(bilibili_channels)} 个 + "
              f"B站直播 {len(bilibili_channels)} 个 = 共 {len(merged)} 个频道")
        return m3u_content

    # ------------------------------------------------------------ 探测过滤

    @staticmethod
    def _load_failures():
        """读取跨轮失败记录 {url: 连续失败次数}；文件不存在或损坏返回空 dict"""
        try:
            if os.path.exists(STREAM_FAILURES_PATH):
                with open(STREAM_FAILURES_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
        except Exception as e:
            _log(f"读取失败记录出错: {str(e)}")
        return {}

    @staticmethod
    def _save_failures(failures):
        """保存失败记录（原子写入）"""
        try:
            atomic_write_text(STREAM_FAILURES_PATH, json.dumps(failures, ensure_ascii=False, indent=2))
        except Exception as e:
            _log(f"保存失败记录出错: {str(e)}")

    @staticmethod
    def filter_unreachable(channels):
        """
        对公开源频道列表做可达性探测过滤（仅在 prepare_public_channels 阶段调用）：
        - 宽松判定探测（200/206/403 均可达）；本轮不可达计数+1，
          连续 STREAM_FAIL_LIMIT 轮失败才丢弃，本轮可达清空计数
        - 官方频道不经过本方法（永不因探测被过滤）
        :param channels: 已择优的公开频道 dict 列表
        :return: 过滤后的频道列表（连续两轮失败者被剔除，第一轮失败保留）
        """
        if not FILTER_UNREACHABLE:
            return channels

        urls = [ch["url"] for ch in channels]
        failures = AggregatorUtils._load_failures()
        probed_urls = set(urls)

        # 并发探测（宽松判定：403 也算可达；用聚合专用 UA 保持历史行为）
        results = {}
        with ThreadPoolExecutor(max_workers=STREAM_CHECK_CONCURRENCY) as executor:
            for url, ok in zip(urls,
                               executor.map(
                                   lambda u: probe_stream(u, accept_403=True,
                                                         user_agent=STREAM_PROBE_UA_LOOSE),
                                   urls)):
                results[url] = ok

        kept = []
        dropped = []
        fail_limit = AggregatorUtils._stream_fail_limit()
        for ch in channels:
            url = ch["url"]
            if results[url]:
                failures.pop(url, None)
                kept.append(ch)
            else:
                failures[url] = failures.get(url, 0) + 1
                if failures[url] >= fail_limit:
                    dropped.append(ch)
                else:
                    kept.append(ch)  # 首轮失败保留，给第二次机会

        if dropped:
            _log(f"探测过滤：丢弃 {len(dropped)} 个连续 {fail_limit} 轮不可达的频道")
            for ch in dropped:
                _log(f"  丢弃: {ch['name']}")

        # 保存失败记录（只保留本轮探测过的 URL，自动裁剪过期项）
        failures = {u: c for u, c in failures.items() if u in probed_urls}
        AggregatorUtils._save_failures(failures)
        return kept

    # ------------------------------------------------------------ 缓存落盘

    @staticmethod
    def prepare_public_channels():
        """
        准备公开源频道：拉源 -> 过滤中文化 -> 同台择优 -> 探测过滤
        :return: 过滤后的公开频道 dict 列表
        """
        public_channels = SourceUtils.fetch_all_public_channels()
        public_channels = SourceUtils.filter_and_translate(public_channels)
        # 同台择优（每台一个），再探测过滤
        best = AggregatorUtils.pick_best_public(public_channels)
        best_list = [ch for ch, _score, _res in best.values()]
        return AggregatorUtils.filter_unreachable(best_list)

    @staticmethod
    def _save_public_channels(channels):
        """保存公开源频道缓存（官方源高频刷新时复用，原子写入）"""
        try:
            atomic_write_text(PUBLIC_CHANNELS_CACHE_PATH,
                              json.dumps(channels, ensure_ascii=False, indent=2))
        except Exception as e:
            _log(f"保存公开源缓存出错: {str(e)}")

    @staticmethod
    def _load_public_channels():
        """
        读取公开源频道缓存；文件不存在或损坏返回 None
        :return: 频道列表或 None
        """
        try:
            if os.path.exists(PUBLIC_CHANNELS_CACHE_PATH):
                with open(PUBLIC_CHANNELS_CACHE_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else None
        except Exception as e:
            _log(f"读取公开源缓存出错: {str(e)}")
        return None

    @staticmethod
    def get_aggregated_m3u():
        """
        完整聚合刷新（公开源低频，6h 一轮）：拉取所有源并合并，落盘缓存。
        互斥锁防重入：已有聚合在跑（如启动时后台首刷）时立即返回 None，
        由调用方决定降级，避免重复全量聚合
        :return: 聚合后的 m3u 文本；失败或被占用返回 None
        """
        if not AggregatorUtils._aggregate_lock.acquire(blocking=False):
            _log("聚合已在运行中，跳过本次聚合")
            return None
        try:
            return AggregatorUtils._get_aggregated_m3u_locked()
        except Exception as e:
            _log(f"生成聚合 m3u 出错: {str(e)}")
            return None
        finally:
            AggregatorUtils._aggregate_lock.release()

    @staticmethod
    def request_async_refresh():
        """
        请求异步聚合刷新（POST 添加/删除房间后调用）：
        - 合并多次请求：已有 worker 待跑时直接返回，不重复开线程
        - 与定时任务不并发：worker 阻塞获取 _aggregate_lock，定时聚合进行中则等其完成
        """
        global _refresh_pending
        with _refresh_flag_lock:
            if _refresh_pending:
                return
            _refresh_pending = True
        worker = threading.Thread(target=AggregatorUtils._async_refresh_worker,
                                  daemon=True, name='聚合-手动刷新')
        worker.start()

    @staticmethod
    def _async_refresh_worker():
        """异步刷新 worker：循环消费 _refresh_pending 标志，直到无待刷请求"""
        global _refresh_pending
        while True:
            with _refresh_flag_lock:
                if not _refresh_pending:
                    return
                _refresh_pending = False
            try:
                # 阻塞获取聚合锁：与定时聚合/官方源刷新互斥，保证同一时刻只有一个写文件
                with AggregatorUtils._aggregate_lock:
                    AggregatorUtils._get_aggregated_m3u_locked()
            except Exception as e:
                _log(f"手动刷新出错: {str(e)}")

    @staticmethod
    def _get_aggregated_m3u_locked():
        """聚合内部实现（调用方必须已持有 _aggregate_lock）"""
        try:
            # B 站测试模式：跳过 hntv 官方源与公开源拉取，只收集 B 站直播频道
            # （频繁重启测试时避免拉公开源+探测 70 频道拖慢启动）
            if AggregatorUtils.is_bilibili_only_mode():
                _log("测试模式（bilibili_only_mode）：跳过 hntv/公开源，仅聚合 B 站直播")
                hntv_channels = []
                public_channels = []
            else:
                # 1. 拉取 hntv 官方频道
                hntv_channels = AggregatorUtils.fetch_hntv_channels()

                # 2. 准备公开源频道（拉源+过滤+择优+探测过滤）并缓存
                public_channels = AggregatorUtils.prepare_public_channels()
                AggregatorUtils._save_public_channels(public_channels)

            # 3. 收集 B 站直播频道（开播判定）
            bilibili_channels = AggregatorUtils.fetch_bilibili_channels()

            # 4. 合并生成 m3u 并落盘（原子写入）
            m3u_content = AggregatorUtils.aggregate_m3u(
                hntv_channels, public_channels, bilibili_channels)
            atomic_write_text(AGGREGATED_M3U_PATH, m3u_content)
            _log(f"聚合结果已保存到 {AGGREGATED_M3U_PATH}")
            _fire_refresh_callbacks()
            # 关键事件入库（管理页日志可查）
            try:
                from admin import db
                db.record_event('INFO', 'aggregator',
                                f"聚合完成: 共 {m3u_content.count('#EXTINF')} 个频道已保存")
            except Exception:
                pass

            return m3u_content
        except Exception as e:
            _log(f"生成聚合 m3u 出错: {str(e)}")
            return None

    @staticmethod
    def refresh_official_only():
        """
        官方源高频刷新（3h 一轮）：只拉 hntv 官方频道，复用公开源缓存重新合并。
        官方接口签名有时效，需高频刷新保持新鲜；不拉公开源、不重复探测。
        公开源缓存未就绪时回退完整聚合。
        加锁：与公开源聚合/手动刷新互斥，杜绝并发写 aggregated.m3u
        :return: 聚合后的 m3u 文本；失败时返回 None
        """
        if not AggregatorUtils._aggregate_lock.acquire(blocking=False):
            _log("聚合已在运行中，跳过官方源刷新")
            return None
        try:
            return AggregatorUtils._refresh_official_only_locked()
        except Exception as e:
            _log(f"官方源刷新出错: {str(e)}")
            return None
        finally:
            AggregatorUtils._aggregate_lock.release()

    @staticmethod
    def _refresh_official_only_locked():
        """官方源刷新内部实现（调用方必须已持有 _aggregate_lock）"""
        try:
            # B 站测试模式：跳过 hntv 官方源与公开源，仅刷新 B 站直播频道
            if AggregatorUtils.is_bilibili_only_mode():
                _log("测试模式（bilibili_only_mode）：官方源刷新跳过 hntv/公开源")
                hntv_channels = []
                public_channels = []
            else:
                hntv_channels = AggregatorUtils.fetch_hntv_channels()

                public_channels = AggregatorUtils._load_public_channels()
                if public_channels is None:
                    _log("公开源缓存未就绪，回退完整聚合")
                    # 已持锁，直接调 locked 版避免重入
                    return AggregatorUtils._get_aggregated_m3u_locked()

            bilibili_channels = AggregatorUtils.fetch_bilibili_channels()
            m3u_content = AggregatorUtils.aggregate_m3u(
                hntv_channels, public_channels, bilibili_channels)
            atomic_write_text(AGGREGATED_M3U_PATH, m3u_content)
            _log(f"官方源刷新完成，已更新 {AGGREGATED_M3U_PATH}"
                  f"（hntv {len(hntv_channels)} 个 + 公开 {len(public_channels)} 个 + "
                  f"B站直播 {len(bilibili_channels)} 个）")
            _fire_refresh_callbacks()
            # 关键事件入库（管理页日志可查）
            try:
                from admin import db
                db.record_event('INFO', 'aggregator',
                                f"官方源刷新完成: 共 {m3u_content.count('#EXTINF')} 个频道")
            except Exception:
                pass
            return m3u_content
        except Exception as e:
            _log(f"官方源刷新出错: {str(e)}")
            return None

    @staticmethod
    def load_aggregated_m3u():
        """
        读取落盘的聚合结果；文件不存在则触发一次生成：
        - 无缓存且后台聚合正在进行（锁被占）→ 降级返回官方源列表（秒级响应），
          避免首请求在启动窗口期长时间阻塞
        - 无缓存且无聚合在跑 → 现场完整聚合
        :return: 聚合 m3u 文本
        """
        try:
            if os.path.exists(AGGREGATED_M3U_PATH):
                with open(AGGREGATED_M3U_PATH, 'r', encoding='utf-8') as f:
                    return f.read()
            # 不存在则生成
            _log("聚合缓存文件不存在，触发首次生成")
            content = AggregatorUtils.get_aggregated_m3u()
            if content:
                return content
            # 聚合失败或被占用（后台聚合进行中）→ 降级保证有内容返回：
            # 测试模式返回 B 站列表（不碰 hntv/公开源），正式模式返回 hntv 官方源
            bili_only = AggregatorUtils.is_bilibili_only_mode()
            _log("聚合不可用（生成失败或进行中），降级返回 "
                  + ("B站直播" if bili_only else "hntv 官方源"))
            if bili_only:
                return AggregatorUtils.get_bilibili_only_m3u()
            return AggregatorUtils.get_hntv_only_m3u()
        except Exception as e:
            _log(f"读取聚合缓存出错: {str(e)}")
            return "#EXTM3U\n# 读取聚合缓存出错\n"

    # ------------------------------------------------------------ 降级路径

    @staticmethod
    def get_bilibili_only_m3u():
        """仅返回 B 站直播频道（测试模式的降级路径，不碰 hntv/公开源）"""
        m3u_content = "#EXTM3U\n\n"
        for ch in AggregatorUtils.fetch_bilibili_channels():
            m3u_content += (
                f'#EXTINF:-1 tvg-id="{ch["name"]}" tvg-name="{ch["name"]}" '
                f'group-title="{ch["group_title"]}",{ch["name"]}\n'
                f'{ch["url"]}\n\n'
            )
        return m3u_content

    @staticmethod
    def get_hntv_only_m3u():
        """仅返回 hntv 官方频道（聚合失败时的降级路径，保证不比现状更差）"""
        response = ApiUtils.get_hntv_live_list()
        if response.status_code != 200:
            return "#EXTM3U\n# Error: Failed to fetch data"

        m3u_content = "#EXTM3U\n\n"
        data = response.json()
        if isinstance(data, list):
            for item in data:
                ch = AggregatorUtils._extract_hntv_item(item)
                if ch:
                    # tvg-id 直拼原始 cid（f-string 对 None 输出 "None" 字符串，
                    # 与旧版逐字节一致，tests/test_equivalence.py 已锁定）
                    m3u_content += (
                        f'#EXTINF:-1 tvg-id="{ch["cid"]}" tvg-name="{ch["name"]}" '
                        f'group-title="{HNTV_GROUP_NAME}",{ch["name"]}\n'
                        f'{ch["url"]}\n\n'
                    )

        return m3u_content

    @staticmethod
    def trans_list_to_m3u():
        """
        直播列表接口主路径：优先返回多源聚合结果（hntv 官方 + 公开源央视/卫视），
        聚合为空/异常时降级为 hntv 官方源
        :return: m3u 文本
        """
        try:
            aggregated = AggregatorUtils.load_aggregated_m3u()
            if aggregated and "#EXTM3U" in aggregated:
                return aggregated
            _log("聚合结果为空，降级为 hntv 官方源")
        except Exception as e:
            _log(f"读取聚合结果失败，降级为 hntv 官方源: {str(e)}")

        return AggregatorUtils.get_hntv_only_m3u()
