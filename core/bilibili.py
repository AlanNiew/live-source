"""B 站直播接入：房间号解析、流地址解析、m3u8 主清单重写、分片反代

B 站直播流地址带时效签名（约 4h），且 CDN 校验 Referer（无 Referer 访问 403）。
因此不能把原始地址直接塞进播放器列表，本模块负责：
1. 通过 uid 解析直播间房间号与开播状态（getRoomInfoOld）
2. 通过房间号解析带签名 m3u8 地址（优先新接口 getRoomPlayInfo 支持高清，
   带登录 cookie 可解锁蓝光/原画；失败自动回退旧接口 playUrl 游客 720P）
3. 把原始 m3u8 主清单里的分片相对路径重写为本服务代理地址
4. 分片反代：带 Referer/UA 向 B 站 CDN 即时转拉（HLS 滑动窗口，分片实时滚动）
5. 多线路解析与备用切换：durl/url_info 返回多条 CDN 线路，主节点故障自动切备用

注意：B 站接口非官方，随时可能改版；所有请求都需容错，
解析失败时由调用方跳过该房间（聚合降级，不影响整体列表）。
"""
import json
import os
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from config import (BILIBILI_CACHE_PATH, BILIBILI_COOKIE,
                    BILIBILI_CUSTOM_ROOMS_PATH, BILIBILI_DIRECT_SEGMENTS,
                    BILIBILI_PLAY_CACHE_TTL, BILIBILI_REFERER, BILIBILI_UA)

# 上游 B 站接口地址
ROOM_INFO_API = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld"
PLAY_URL_API = "https://api.live.bilibili.com/room/v1/Room/playUrl"
PLAY_INFO_API = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
# 登录态校验接口（cookie 有效性探测）
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

# 内存缓存锁：保护 _play_cache 与房间信息缓存
_cache_lock = threading.Lock()
# 流地址解析缓存 {room_id: (expire_ts, 线路列表)}
# 线路 = [(m3u8_url, base_url, query), (备用m3u8_url, 备用base_url, 备用query), ...]
_play_cache = {}


class BilibiliUtils:
    """B 站直播工具类（房间解析 / 流解析 / m3u8 重写 / 分片反代）"""

    # ------------------------------------------------------------ 请求基础

    @staticmethod
    def _request_get(url, params=None, timeout=15):
        """带 B 站 UA/Referer 的 GET 请求（Referer 是防盗链必需项；有 cookie 则带上）"""
        headers = {
            'Referer': BILIBILI_REFERER,
            'User-Agent': BILIBILI_UA,
        }
        if BILIBILI_COOKIE:
            headers['Cookie'] = BILIBILI_COOKIE
        return requests.get(url, params=params, headers=headers, timeout=timeout)

    @staticmethod
    def _check_cookie_valid():
        """
        探测配置的 B 站 cookie 登录态是否有效（调 nav 接口看 isLogin）。
        仅用于日志提示：cookie 失效时高清不可用、静默降级游客，及时发现便于更新
        :return: True 登录有效 / False 未登录或异常
        """
        if not BILIBILI_COOKIE:
            return False
        try:
            response = BilibiliUtils._request_get(NAV_API)
            if response.status_code != 200:
                return False
            data = response.json()
            return bool((data.get('data') or {}).get('isLogin'))
        except Exception:
            return False

    # ------------------------------------------------------------ 房间解析

    @staticmethod
    def _load_room_cache():
        """读取磁盘房间缓存 {uid: room_id}；文件不存在或损坏返回空 dict"""
        try:
            if os.path.exists(BILIBILI_CACHE_PATH):
                with open(BILIBILI_CACHE_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"读取 B 站房间缓存出错: {str(e)}")
        return {}

    @staticmethod
    def _save_room_cache(cache):
        """保存磁盘房间缓存（原子写入）"""
        try:
            tmp_path = BILIBILI_CACHE_PATH + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, BILIBILI_CACHE_PATH)
        except Exception as e:
            print(f"保存 B 站房间缓存出错: {str(e)}")

    # ------------------------------------------------------------ 动态频道列表

    @staticmethod
    def load_custom_rooms():
        """
        读取运行时动态添加的频道列表 [{name, room_id}]。
        文件不存在或损坏返回空列表
        """
        try:
            if os.path.exists(BILIBILI_CUSTOM_ROOMS_PATH):
                with open(BILIBILI_CUSTOM_ROOMS_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
        except Exception as e:
            print(f"读取 B 站动态频道列表出错: {str(e)}")
        return []

    @staticmethod
    def save_custom_rooms(rooms):
        """保存运行时动态添加的频道列表（原子写入）"""
        try:
            tmp_path = BILIBILI_CUSTOM_ROOMS_PATH + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(rooms, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, BILIBILI_CUSTOM_ROOMS_PATH)
        except Exception as e:
            print(f"保存 B 站动态频道列表出错: {str(e)}")

    @staticmethod
    def add_custom_room(name, room_id):
        """
        添加一个动态频道（按 room_id 去重，已存在则更新名称）
        :return: 添加后的完整动态列表
        """
        rooms = [r for r in BilibiliUtils.load_custom_rooms()
                 if r.get("room_id") != room_id]
        rooms.append({"name": name, "room_id": room_id})
        BilibiliUtils.save_custom_rooms(rooms)
        return rooms

    @staticmethod
    def remove_custom_room(room_id):
        """
        删除一个动态频道
        :return: (是否删除成功, 删除后的完整动态列表)
        """
        rooms = BilibiliUtils.load_custom_rooms()
        new_rooms = [r for r in rooms if r.get("room_id") != room_id]
        removed = len(new_rooms) != len(rooms)
        if removed:
            BilibiliUtils.save_custom_rooms(new_rooms)
        return removed, new_rooms

    @staticmethod
    def resolve_room_by_uid(uid):
        """
        通过 UP 主 uid 解析直播间信息（getRoomInfoOld）
        :param uid: 用户 UID
        :return: dict（room_id/live_status/title/online）；失败或不存在返回 None
        """
        try:
            response = BilibiliUtils._request_get(ROOM_INFO_API, params={"mid": uid})
            if response.status_code != 200:
                return None
            data = response.json()
            if data.get('code') != 0 or not data.get('data'):
                return None
            info = data['data']
            if not info.get('roomStatus') or not info.get('roomid'):
                # roomStatus=0 表示该 UP 主未开过直播间
                return None
            return {
                "room_id": info['roomid'],
                "live_status": info.get('liveStatus', 0),
                "title": info.get('title') or '',
                "online": info.get('online', 0),
            }
        except Exception as e:
            print(f"解析 B 站房间信息出错(uid={uid}): {str(e)}")
            return None

    @staticmethod
    def get_room_id(uid):
        """
        获取 uid 对应的房间号（磁盘缓存兜底：接口失败时用上次成功结果，
        避免上游抖动导致整个 B 站分组消失）
        :param uid: 用户 UID
        :return: 房间号 int 或 None
        """
        info = BilibiliUtils.resolve_room_by_uid(uid)
        if info:
            # 更新磁盘缓存（仅房间号，live_status 每次实时查，不缓存）
            with _cache_lock:
                cache = BilibiliUtils._load_room_cache()
                cache[str(uid)] = info["room_id"]
                BilibiliUtils._save_room_cache(cache)
            return info["room_id"]
        # 接口失败 → 用缓存兜底
        with _cache_lock:
            cache = BilibiliUtils._load_room_cache()
            return cache.get(str(uid))

    # ------------------------------------------------------------ 流地址解析

    @staticmethod
    def _split_route(url):
        """把完整流地址拆成 (m3u8_url, base_url, query) 三元组"""
        parts = urlsplit(url)
        dir_path = parts.path.rsplit('/', 1)[0] + '/'
        base_url = urlunsplit((parts.scheme, parts.netloc, dir_path, '', ''))
        return url, base_url, parts.query

    @staticmethod
    def _parse_play_url(room_id):
        """
        旧接口 playUrl 解析流地址（游客 720P 兜底）。
        注意：旧接口即使登录也只给 250 档，高清须走 _parse_play_url_new
        :param room_id: 直播间房间号
        :return: 线路列表 [(m3u8_url, base_url, query), ...] 或 None
        """
        response = BilibiliUtils._request_get(
            PLAY_URL_API,
            params={"cid": room_id, "quality": 4, "platform": "h5"},
        )
        if response.status_code != 200:
            return None
        data = response.json()
        if data.get('code') != 0 or not data.get('data'):
            return None
        durl = data['data'].get('durl') or []

        routes = []
        for item in durl:
            url = item.get('url')
            if url:
                routes.append(BilibiliUtils._split_route(url))
        return routes or None

    @staticmethod
    def _parse_play_url_new(room_id):
        """
        新接口 getRoomPlayInfo 解析流地址（带 cookie 可解锁蓝光/原画）：
        - 选流规则：hls(m3u8) 优先 → avc 编码优先 → current_qn 最高档
        - 同一流的 url_info 多个 CDN host 全部解析为多线路（备用切换）
        :param room_id: 直播间房间号
        :return: 线路列表 [(m3u8_url, base_url, query), ...] 或 None
        """
        params = {
            "room_id": room_id,
            "protocol": "0,1",
            "format": "0,1,2",
            "codec": "0,1",
            "qn": 10000,       # 显式请求原画（登录态下可用）
            "platform": "web",
            "ptype": 8,
        }
        response = BilibiliUtils._request_get(PLAY_INFO_API, params=params)
        if response.status_code != 200:
            return None
        data = response.json()
        if data.get('code') != 0:
            return None
        playurl = ((data.get('data') or {}).get('playurl_info') or {}).get('playurl') or {}
        streams = playurl.get('stream') or []

        # 选流：只选 hls 类格式（ts/fmp4/m3u8，播放链路基于 m3u8 主清单重写，
        # flv 无法代理），同格式内优先 avc 编码，再取 current_qn 最高档
        best = None
        for s in streams:
            for fmt in s.get('format') or []:
                fmt_name = fmt.get('format_name') or ''
                # B 站新接口把 hls 分为 ts/fmp4 两种子格式，格式名不含 "m3u8" 字样
                if 'm3u8' not in fmt_name and fmt_name not in ('ts', 'fmp4'):
                    continue  # 只收 hls 类（ts/fmp4/m3u8），flv 流无法走重写链路
                for c in fmt.get('codec') or []:
                    codec_name = c.get('codec_name') or ''
                    current_qn = c.get('current_qn') or 0
                    priority = (
                        1 if codec_name == 'avc' else 0,  # avc 优先
                        current_qn,                        # qn 越高越好
                    )
                    if best is None or priority > best[0]:
                        best = (priority, fmt_name, codec_name, current_qn, c)

        if best is None:
            return None
        codec = best[4]
        base_url = codec.get('base_url') or ''
        url_info = codec.get('url_info') or []
        if not base_url or not url_info:
            return None

        routes = []
        for info in url_info:
            host = info.get('host') or ''
            extra = info.get('extra') or ''
            if not host:
                continue
            full_url = host + base_url
            if extra:
                full_url += ('&' if '?' in full_url else '?') + extra
            routes.append(BilibiliUtils._split_route(full_url))
        return routes or None

    @staticmethod
    def resolve_play_m3u8(room_id, force=False):
        """
        解析房间的原始流地址线路列表（内存缓存短 TTL，签名过期前刷新）。
        解析顺序：新接口（高清，带 cookie）→ 旧接口（游客 720P 兜底）→ None
        :param room_id: 直播间房间号
        :param force: True 时忽略缓存强制重新解析（m3u8 失效时的兜底）
        :return: 线路列表 [(m3u8_url, base_url, query), ...] 或 None
        """
        now = time.time()
        if not force:
            with _cache_lock:
                cached = _play_cache.get(room_id)
                if cached and cached[0] > now:
                    # 缓存格式为 (expire, 线路列表)，去掉过期时间返回线路列表
                    return cached[1]
        try:
            # 优先新接口（登录后可拿高清）；cookie 失效时新接口可能仍返回但只有 250，
            # 或请求异常——均回退旧接口保证 720P 兜底
            result = BilibiliUtils._parse_play_url_new(room_id)
            if not result:
                result = BilibiliUtils._parse_play_url(room_id)
            if result:
                with _cache_lock:
                    _play_cache[room_id] = (now + BILIBILI_PLAY_CACHE_TTL, result)
                return result
        except Exception as e:
            print(f"解析 B 站流地址出错(room={room_id}): {str(e)}")
        return None

    # ------------------------------------------------------------ 开播判定

    @staticmethod
    def _try_routes(room_id, routes, url_builder):
        """
        依次尝试多条线路（主 → 备用），返回第一个 200 的 (response, 线路三元组)。
        全部失败时强制重解析（拿新签名）再试一轮。
        :param room_id: 直播间房间号
        :param routes: 线路列表 [(m3u8_url, base_url, query), ...]
        :param url_builder: 函数，输入线路三元组，输出要请求的 URL 字符串
        :return: (response, route) 或 (None, None)
        """
        for route in routes:
            try:
                response = BilibiliUtils._request_get(url_builder(route))
                if response.status_code == 200:
                    return response, route
            except Exception:
                continue  # 主线路异常 → 试备用线路
        # 全部线路失败：签名可能过期 → 强制重解析后再试一轮（不再递归）
        routes2 = BilibiliUtils.resolve_play_m3u8(room_id, force=True)
        if routes2:
            for route in routes2:
                try:
                    response = BilibiliUtils._request_get(url_builder(route))
                    if response.status_code == 200:
                        return response, route
                except Exception:
                    continue
        return None, None

    @staticmethod
    def is_live(room_id):
        """
        判定房间是否真正在播：实测拉取 m3u8 主清单（HTTP 200 才算在播）。
        注意 playUrl 对未开播房间也会返回带签名地址，仅凭解析结果会误判；
        必须实测主清单（未开播时连接失败/超时，开播时 200）。
        多线路依次尝试，主节点故障可切备用再判定
        :param room_id: 直播间房间号
        :return: True 在播 / False 未开播或不可用
        """
        resolved = BilibiliUtils.resolve_play_m3u8(room_id)
        if not resolved:
            return False
        response, _route = BilibiliUtils._try_routes(
            room_id, resolved, lambda route: route[0])
        return response is not None

    # ------------------------------------------------------------ m3u8 重写

    @staticmethod
    def build_proxied_m3u8(room_id, public_base_url):
        """
        拉取原始 m3u8 主清单并把分片改写为可播放地址：
        - 直连模式（BILIBILI_DIRECT_SEGMENTS=True，默认）：分片改写为 B 站 CDN 绝对地址
          （相对路径 + 分片基础 URL + 签名查询串）。实测分片无 Referer 也可访问，
          防盗链只卡主清单，因此分片由客户端直连 CDN，本服务不转发大流量。
        - 代理模式（BILIBILI_DIRECT_SEGMENTS=False）：分片改写为本服务 seg 代理地址，
          分片流量经本服务转发（兼容性兜底）。
        多线路依次尝试：主线路故障自动切备用（成功线路决定分片基础 URL）
        :param room_id: 直播间房间号
        :param public_base_url: 本服务对外基础地址（仅代理模式使用）
        :return: 重写后的 m3u8 文本；解析失败返回 None
        """
        resolved = BilibiliUtils.resolve_play_m3u8(room_id)
        if not resolved:
            return None

        response, route = BilibiliUtils._try_routes(
            room_id, resolved, lambda r: r[0])
        if response is None:
            print(f"拉取 B 站 m3u8 清单出错(room={room_id})")
            return None
        _m3u8_url, base_url, query = route

        lines = []
        for line in response.text.splitlines():
            stripped = line.strip()
            # 只重写分片行（非注释行）
            if stripped and not stripped.startswith('#'):
                if BILIBILI_DIRECT_SEGMENTS:
                    # 直连模式：绝对 URL 原样保留，相对路径拼成 CDN 完整地址（带签名串）
                    if stripped.startswith(('http://', 'https://')):
                        lines.append(stripped)
                    else:
                        seg_url = base_url + stripped
                        if query:
                            seg_url += ('&' if '?' in seg_url else '?') + query
                        lines.append(seg_url)
                else:
                    # 代理模式：分片改指本服务代理
                    lines.append(f"{public_base_url.rstrip('/')}/api/bilibili/{room_id}/seg/{stripped}")
            else:
                lines.append(line)
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------ 分片反代

    @staticmethod
    def proxy_segment(room_id, seg_path):
        """
        分片反代：带 Referer/UA 向 B 站 CDN 即时转拉。
        多线路依次尝试：主节点故障自动切备用（同一套签名）
        :param room_id: 直播间房间号
        :param seg_path: 分片相对路径（如 live_xxx-123.ts）
        :return: (status_code, headers dict, 流式迭代器) 或 (500, None, None)
        """
        resolved = BilibiliUtils.resolve_play_m3u8(room_id)
        if not resolved:
            return 500, None, None

        def build_seg_url(route):
            # 分片 URL = 基础目录 + 相对路径 + 同一套签名查询串（实测必须带签名参数）
            _url, base_url, query = route
            seg_url = base_url + seg_path
            if query:
                seg_url += ('&' if '?' in seg_url else '?') + query
            return seg_url

        response, _route = BilibiliUtils._try_routes(
            room_id, resolved, build_seg_url)
        if response is None:
            return 404, None, None

        # 透传关键响应头（Content-Type 必须保留，播放器依赖）
        headers = {}
        for key in ('Content-Type', 'Content-Length', 'Cache-Control', 'Access-Control-Allow-Origin'):
            if key in response.headers:
                headers[key] = response.headers[key]
        return response.status_code, headers, response.iter_content(chunk_size=64 * 1024)
