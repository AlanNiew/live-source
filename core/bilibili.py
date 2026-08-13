"""B 站直播接入：房间号解析、流地址解析、m3u8 主清单重写、分片反代

B 站直播流地址带时效签名（约 4h），且 CDN 校验 Referer（无 Referer 访问 403）。
因此不能把原始地址直接塞进播放器列表，本模块负责：
1. 通过 uid 解析直播间房间号与开播状态（getRoomInfoOld）
2. 通过房间号解析带签名 m3u8 地址（playUrl?platform=h5，内存缓存短 TTL）
3. 把原始 m3u8 主清单里的分片相对路径重写为本服务代理地址
4. 分片反代：带 Referer/UA 向 B 站 CDN 即时转拉（HLS 滑动窗口，分片实时滚动）

注意：B 站接口非官方，随时可能改版；所有请求都需容错，
解析失败时由调用方跳过该房间（聚合降级，不影响整体列表）。
"""
import json
import os
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from config import (BILIBILI_CACHE_PATH, BILIBILI_DIRECT_SEGMENTS,
                    BILIBILI_PLAY_CACHE_TTL, BILIBILI_REFERER, BILIBILI_UA)

# 上游 B 站接口地址
ROOM_INFO_API = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld"
PLAY_URL_API = "https://api.live.bilibili.com/room/v1/Room/playUrl"

# 内存缓存锁：保护 _play_cache 与房间信息缓存
_cache_lock = threading.Lock()
# 流地址解析缓存 {room_id: (expire_ts, 原始m3u8URL, 分片基础URL, 查询串)}
_play_cache = {}


class BilibiliUtils:
    """B 站直播工具类（房间解析 / 流解析 / m3u8 重写 / 分片反代）"""

    # ------------------------------------------------------------ 请求基础

    @staticmethod
    def _request_get(url, params=None, timeout=15):
        """带 B 站 UA/Referer 的 GET 请求（Referer 是防盗链必需项）"""
        headers = {
            'Referer': BILIBILI_REFERER,
            'User-Agent': BILIBILI_UA,
        }
        return requests.get(url, params=params, headers=headers, timeout=timeout)

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
    def _parse_play_url(room_id):
        """
        调用 playUrl 接口解析流地址
        :param room_id: 直播间房间号
        :return: (原始m3u8完整URL, 分片基础URL, 查询串) 或 None
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
        if not durl or not durl[0].get('url'):
            return None

        m3u8_url = durl[0]['url']
        # 拆分出分片基础目录（去掉末尾文件名）与签名查询串
        parts = urlsplit(m3u8_url)
        dir_path = parts.path.rsplit('/', 1)[0] + '/'
        base_url = urlunsplit((parts.scheme, parts.netloc, dir_path, '', ''))
        return m3u8_url, base_url, parts.query

    @staticmethod
    def resolve_play_m3u8(room_id, force=False):
        """
        解析房间的原始 m3u8 地址（内存缓存短 TTL，签名过期前刷新）
        :param room_id: 直播间房间号
        :param force: True 时忽略缓存强制重新解析（m3u8 失效时的兜底）
        :return: (原始m3u8URL, 分片基础URL, 查询串) 或 None
        """
        now = time.time()
        if not force:
            with _cache_lock:
                cached = _play_cache.get(room_id)
                if cached and cached[0] > now:
                    # 缓存格式为 (expire, m3u8_url, base_url, query)，去掉过期时间返回三元组
                    return cached[1:]
        try:
            result = BilibiliUtils._parse_play_url(room_id)
            if result:
                with _cache_lock:
                    _play_cache[room_id] = (now + BILIBILI_PLAY_CACHE_TTL,) + result
                return result
        except Exception as e:
            print(f"解析 B 站流地址出错(room={room_id}): {str(e)}")
        return None

    # ------------------------------------------------------------ 开播判定

    @staticmethod
    def is_live(room_id):
        """
        判定房间是否真正在播：实测拉取 m3u8 主清单（HTTP 200 才算在播）。
        注意 playUrl 对未开播房间也会返回带签名地址，仅凭解析结果会误判；
        必须实测主清单（未开播时连接失败/超时，开播时 200）
        :param room_id: 直播间房间号
        :return: True 在播 / False 未开播或不可用
        """
        resolved = BilibiliUtils.resolve_play_m3u8(room_id)
        if not resolved:
            return False
        try:
            response = BilibiliUtils._request_get(resolved[0])
            return response.status_code == 200
        except Exception:
            return False

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
        :param room_id: 直播间房间号
        :param public_base_url: 本服务对外基础地址（仅代理模式使用）
        :return: 重写后的 m3u8 文本；解析失败返回 None
        """
        resolved = BilibiliUtils.resolve_play_m3u8(room_id)
        if not resolved:
            return None
        m3u8_url, base_url, query = resolved

        # 拉取主清单（带 Referer/UA）
        try:
            response = BilibiliUtils._request_get(m3u8_url)
            if response.status_code != 200:
                # 签名过期等场景 → 强制重新解析后再试一次
                resolved = BilibiliUtils.resolve_play_m3u8(room_id, force=True)
                if not resolved:
                    return None
                m3u8_url, base_url, query = resolved
                response = BilibiliUtils._request_get(m3u8_url)
                if response.status_code != 200:
                    return None
        except Exception as e:
            print(f"拉取 B 站 m3u8 清单出错(room={room_id}): {str(e)}")
            return None

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
        分片反代：带 Referer/UA 向 B 站 CDN 即时转拉
        :param room_id: 直播间房间号
        :param seg_path: 分片相对路径（如 live_xxx-123.ts）
        :return: (status_code, headers dict, 流式迭代器) 或 (500, None, None)
        """
        resolved = BilibiliUtils.resolve_play_m3u8(room_id)
        if not resolved:
            return 500, None, None
        _m3u8_url, base_url, query = resolved

        # 分片 URL = 基础目录 + 相对路径 + 同一套签名查询串（实测必须带签名参数）
        seg_url = base_url + seg_path
        if query:
            seg_url += ('&' if '?' in seg_url else '?') + query

        try:
            response = BilibiliUtils._request_get(seg_url)
            if response.status_code != 200:
                # 签名过期 → 强制重解析再试一次
                resolved = BilibiliUtils.resolve_play_m3u8(room_id, force=True)
                if not resolved:
                    return response.status_code, None, None
                _m3u8_url, base_url, query = resolved
                seg_url = base_url + seg_path
                if query:
                    seg_url += ('&' if '?' in seg_url else '?') + query
                response = BilibiliUtils._request_get(seg_url)
                if response.status_code != 200:
                    return response.status_code, None, None

            # 透传关键响应头（Content-Type 必须保留，播放器依赖）
            headers = {}
            for key in ('Content-Type', 'Content-Length', 'Cache-Control', 'Access-Control-Allow-Origin'):
                if key in response.headers:
                    headers[key] = response.headers[key]
            return response.status_code, headers, response.iter_content(chunk_size=64 * 1024)
        except Exception as e:
            print(f"B 站分片反代出错(room={room_id}, seg={seg_path}): {str(e)}")
            return 500, None, None
