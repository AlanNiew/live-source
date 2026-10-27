"""音乐接入：ge-whale-music 网易云接口代理 + 歌单管理

用户自建网易云接口服务（ge-whale-music，见 MUSIC_API_BASE）提供
搜索 / 歌曲详情 / 歌单分页 / 播放地址 / 歌词 等能力。本模块把它收敛成
hntv-api 的 /api/music/* 端点，客户端（mytv-android 音乐模块）只认本服务。

设计要点：
1. 纯元数据转发：播放地址是网易 CDN 直链（.mp3），由播放器直连，
   不经过本服务，不消耗服务器带宽，也不需要流反代
2. 歌单管理：静态（config.MUSIC_PLAYLISTS）+ 动态（music_playlists.json）
   两处合并去重，与 B 站房间的"静态配置+动态 json"模式一致
3. 上游 ge-whale-music 自带 redis 缓存与限流，本层只做轻量透传，
   上游异常时把错误码透传给调用方（播放端据此提示"歌曲不可播"等）
"""
import json
import os
import threading

import requests

from config import MUSIC_API_BASE, MUSIC_PLAYLISTS, MUSIC_PLAYLISTS_PATH, MUSIC_REQUEST_TIMEOUT
from core.logger import get_logger

_logger = get_logger('music')

# 歌单列表读写锁（json 原子写 + 并发保护）
_playlists_lock = threading.Lock()


class MusicUtils:
    """音乐接口工具类（上游转发 / 歌单管理）"""

    # ------------------------------------------------------------ 请求基础

    @staticmethod
    def _request_get(path, params=None, timeout=MUSIC_REQUEST_TIMEOUT):
        """GET 上游 ge-whale-music（统一 Base URL 拼接）"""
        return requests.get(f"{MUSIC_API_BASE}{path}", params=params, timeout=timeout)

    @staticmethod
    def _request_post(path, json_body=None, timeout=MUSIC_REQUEST_TIMEOUT):
        """POST 上游 ge-whale-music（歌单详情是 POST）"""
        return requests.post(f"{MUSIC_API_BASE}{path}", json=json_body, timeout=timeout)

    @staticmethod
    def _parse_response(response):
        """
        解析上游统一响应 {code, message, data}。
        :return: (code, message, data)；网络异常时 code=0, message=异常信息
        """
        try:
            payload = response.json()
        except Exception as e:
            _logger.warning(f"上游 {MUSIC_API_BASE} 响应非 JSON: {str(e)}")
            return 0, "上游响应格式异常", None
        return payload.get('code', 0), payload.get('message', ''), payload.get('data')

    @staticmethod
    def _call(path, params=None, json_body=None):
        """
        统一调用入口：请求上游 + 解析响应，网络/JSON 异常兜底。
        :return: (code, message, data)
        """
        try:
            if json_body is not None:
                response = MusicUtils._request_post(path, json_body=json_body)
            else:
                response = MusicUtils._request_get(path, params=params)
        except Exception as e:
            _logger.warning(f"请求上游 {MUSIC_API_BASE}{path} 失败: {str(e)}")
            return 0, str(e), None
        return MusicUtils._parse_response(response)

    # ------------------------------------------------------------ 业务转发

    @staticmethod
    def search(keyword, limit=5):
        """搜索歌曲（GET /search）→ data 为歌曲列表"""
        return MusicUtils._call('/search', params={'keyword': keyword, 'limit': limit})

    @staticmethod
    def music_detail(music_id):
        """获取单曲详情（GET /music）→ data 为歌曲 dict"""
        return MusicUtils._call('/music', params={'id': music_id})

    @staticmethod
    def player_url(music_id, level='exhigh'):
        """获取播放地址（GET /player）→ data 为 {url}，网易 CDN 直链 mp3"""
        return MusicUtils._call('/player', params={'id': music_id, 'level': level})

    @staticmethod
    def playlist_detail(playlist_id, page_num=1, page_size=50):
        """
        获取歌单详情（POST /playlist）→ data 为
        {playlist: {id,name,coverImgUrl,...}, tracks: [...], pageNum, pageSize, total, pageCount}
        """
        return MusicUtils._call('/playlist', json_body={
            'id': str(playlist_id),
            'pageNum': page_num,
            'pageSize': page_size,
        })

    @staticmethod
    def lyrics(music_id):
        """获取歌词（GET /lyrics）→ data 为 {lyrics}（第一期客户端未展示，预留）"""
        return MusicUtils._call('/lyrics', params={'id': music_id})

    # ------------------------------------------------------------ 歌单管理

    @staticmethod
    def load_playlists():
        """
        读取运行时动态添加的歌单列表 [{id, name}]。
        文件不存在或损坏返回空列表
        """
        try:
            if os.path.exists(MUSIC_PLAYLISTS_PATH):
                with open(MUSIC_PLAYLISTS_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
        except Exception as e:
            _logger.warning(f"读取音乐动态歌单列表出错: {str(e)}")
        return []

    @staticmethod
    def save_playlists(playlists):
        """保存运行时动态添加的歌单列表（原子写入）"""
        try:
            tmp_path = MUSIC_PLAYLISTS_PATH + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(playlists, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, MUSIC_PLAYLISTS_PATH)
        except Exception as e:
            _logger.warning(f"保存音乐动态歌单列表出错: {str(e)}")

    @staticmethod
    def list_playlists():
        """汇总歌单列表：静态 config + 动态 json 合并去重（动态优先），返回 [{id, name}]"""
        merged = {}
        for item in MUSIC_PLAYLISTS:
            pid = str(item.get('id'))
            merged[pid] = {'id': pid, 'name': item.get('name') or f'歌单{pid}'}
        for item in MusicUtils.load_playlists():
            pid = str(item.get('id'))
            merged[pid] = {'id': pid, 'name': item.get('name') or f'歌单{pid}'}
        return list(merged.values())

    @staticmethod
    def add_playlist(playlist_id, name=None):
        """
        添加一个动态歌单（按 id 去重，已存在则更新名称）。
        name 缺省时尝试从上游歌单详情拉取名称。
        :return: 添加后的完整动态列表
        :raises: 上游拉取失败/歌单不存在时抛异常（调用方转 4xx）
        """
        pid = str(playlist_id)
        if not name:
            code, message, data = MusicUtils.playlist_detail(pid, page_num=1, page_size=1)
            if code != 200 or not data:
                raise ValueError(f"歌单 {pid} 不存在或无法访问（code={code} {message}）")
            name = ((data.get('playlist') or {}).get('name')) or f'歌单{pid}'

        with _playlists_lock:
            playlists = [p for p in MusicUtils.load_playlists() if str(p.get('id')) != pid]
            playlists.append({'id': pid, 'name': name.strip()})
            MusicUtils.save_playlists(playlists)
        return MusicUtils.load_playlists()

    @staticmethod
    def remove_playlist(playlist_id):
        """
        删除动态歌单（静态 config 里的不受影响）。
        :return: (是否删除, 删除后的完整动态列表)
        """
        pid = str(playlist_id)
        with _playlists_lock:
            playlists = MusicUtils.load_playlists()
            remaining = [p for p in playlists if str(p.get('id')) != pid]
            if len(remaining) == len(playlists):
                return False, playlists
            MusicUtils.save_playlists(remaining)
        return True, MusicUtils.load_playlists()
