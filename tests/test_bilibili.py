"""B 站直播接入测试：房间解析、开播判定、m3u8 重写、分片反代、聚合接入

全部 mock 网络请求，不触碰真实直播源；用 unittest.mock 模拟 requests 响应。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from core.aggregator import AggregatorUtils
from core.bilibili import BilibiliUtils, _play_cache


def _fake_response(status_code=200, text='', json_data=None, headers=None):
    """构造 mock 的 requests.Response"""
    resp = mock.Mock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_data if json_data is not None else {}
    resp.headers = headers or {}
    return resp


class RoomResolveTest(unittest.TestCase):
    """房间号解析与磁盘缓存兜底"""

    def test_resolve_room_ok(self):
        """uid 解析成功：返回房间号/开播状态/标题"""
        resp = _fake_response(json_data={
            'code': 0,
            'data': {
                'roomStatus': 1, 'roomid': 22861369,
                'liveStatus': 1, 'title': '直播中', 'online': 100,
            },
        })
        with mock.patch.object(BilibiliUtils, '_request_get', return_value=resp):
            info = BilibiliUtils.resolve_room_by_uid(2057655323)
        self.assertEqual(info['room_id'], 22861369)
        self.assertEqual(info['live_status'], 1)

    def test_resolve_room_no_room(self):
        """UP 主未开过直播间（roomStatus=0）：返回 None"""
        resp = _fake_response(json_data={'code': 0, 'data': {'roomStatus': 0, 'roomid': 0}})
        with mock.patch.object(BilibiliUtils, '_request_get', return_value=resp):
            self.assertIsNone(BilibiliUtils.resolve_room_by_uid(12345))

    def test_resolve_room_api_error(self):
        """接口异常：返回 None"""
        with mock.patch.object(BilibiliUtils, '_request_get', side_effect=Exception('boom')):
            self.assertIsNone(BilibiliUtils.resolve_room_by_uid(2057655323))

    def test_get_room_id_cache_fallback(self):
        """接口失败时用磁盘缓存兜底（uid -> room_id）"""
        cache = {'2057655323': 22861369}
        with mock.patch.object(BilibiliUtils, '_load_room_cache', return_value=cache), \
             mock.patch.object(BilibiliUtils, '_save_room_cache'), \
             mock.patch.object(BilibiliUtils, 'resolve_room_by_uid', return_value=None):
            self.assertEqual(BilibiliUtils.get_room_id(2057655323), 22861369)

    def test_get_room_id_saves_cache(self):
        """解析成功时更新磁盘缓存"""
        info = {'room_id': 22861369, 'live_status': 1, 'title': 'x', 'online': 0}
        with mock.patch.object(BilibiliUtils, 'resolve_room_by_uid', return_value=info), \
             mock.patch.object(BilibiliUtils, '_load_room_cache', return_value={}), \
             mock.patch.object(BilibiliUtils, '_save_room_cache') as save:
            self.assertEqual(BilibiliUtils.get_room_id(2057655323), 22861369)
            save.assert_called_once()
            self.assertEqual(save.call_args[0][0], {'2057655323': 22861369})


class PlayUrlResolveTest(unittest.TestCase):
    """流地址解析（旧接口多线路）与内存缓存"""

    def setUp(self):
        # 清空模块级内存缓存，避免测试间残留影响计数
        _play_cache.clear()

    def tearDown(self):
        _play_cache.clear()

    def test_parse_play_url_ok(self):
        """旧接口解析：durl 全部解析为线路列表（含备用）"""
        resp = _fake_response(json_data={
            'code': 0,
            'data': {'durl': [
                {'url': 'https://d1--cn-gotcha104.bilivideo.com/live-bvc/123/abc.m3u8?expires=1&sign=xyz'},
                {'url': 'https://d1--cn-gotcha104b.bilivideo.com/live-bvc/123/abc.m3u8?expires=1&sign=xyz'},
            ]},
        })
        with mock.patch.object(BilibiliUtils, '_request_get', return_value=resp):
            routes = BilibiliUtils._parse_play_url(8178490)
        self.assertEqual(len(routes), 2)
        m3u8_url, base_url, query = routes[0]
        self.assertEqual(m3u8_url, 'https://d1--cn-gotcha104.bilivideo.com/live-bvc/123/abc.m3u8?expires=1&sign=xyz')
        self.assertEqual(base_url, 'https://d1--cn-gotcha104.bilivideo.com/live-bvc/123/')
        self.assertEqual(query, 'expires=1&sign=xyz')
        # 备用线路：仅 host 不同
        self.assertEqual(routes[1][1], 'https://d1--cn-gotcha104b.bilivideo.com/live-bvc/123/')

    def test_parse_play_url_empty_durl(self):
        """无 durl（如房间不存在）：返回 None"""
        resp = _fake_response(json_data={'code': 0, 'data': {'durl': []}})
        with mock.patch.object(BilibiliUtils, '_request_get', return_value=resp):
            self.assertIsNone(BilibiliUtils._parse_play_url(1))

    def test_parse_play_url_new_ok(self):
        """新接口解析：选 hls+avc+最高 qn，url_info 多 host 全解析为线路"""
        resp = _fake_response(json_data={
            'code': 0,
            'data': {'playurl_info': {'playurl': {'stream': [
                {
                    'protocol_name': 'http_stream',
                    'format': [
                        {'format_name': 'flv', 'codec': [
                            {'codec_name': 'avc', 'current_qn': 250,
                             'accept_qn': [10000, 400, 250],
                             'base_url': '/live-bvc/1/live_a.flv?',
                             'url_info': [
                                 {'host': 'https://d1--cn-gotcha04.bilivideo.com', 'extra': 'qn=250&sign=x'},
                                 {'host': 'https://d1--cn-gotcha04b.bilivideo.com', 'extra': 'qn=250&sign=x'},
                             ]},
                        ]},
                    ],
                },
                {
                    'protocol_name': 'http_stream',
                    'format': [
                        {'format_name': 'ts', 'codec': [
                            {'codec_name': 'avc', 'current_qn': 10000,
                             'accept_qn': [10000, 400, 250],
                             'base_url': '/live-bvc/2/live_a.m3u8?',
                             'url_info': [
                                 {'host': 'https://d1--cn-gotcha104.bilivideo.com', 'extra': 'qn=10000&sign=y'},
                                 {'host': 'https://d1--cn-gotcha104b.bilivideo.com', 'extra': 'qn=10000&sign=y'},
                             ]},
                        ]},
                    ],
                },
            ]}}},
        })
        with mock.patch.object(BilibiliUtils, '_request_get', return_value=resp):
            routes = BilibiliUtils._parse_play_url_new(8178490)
        # 应选 ts(hls) + avc + qn=10000（flv 流被跳过），且两条 host 都解析
        self.assertEqual(len(routes), 2)
        m3u8_url, base_url, query = routes[0]
        self.assertIn('/live-bvc/2/live_a.m3u8', m3u8_url)
        self.assertIn('qn=10000', query)
        self.assertEqual(base_url, 'https://d1--cn-gotcha104.bilivideo.com/live-bvc/2/')
        self.assertIn('d1--cn-gotcha104b', routes[1][1])
        # flv 那条流（/live-bvc/1/）应被跳过
        self.assertNotIn('/live-bvc/1/', m3u8_url)

    def test_parse_play_url_new_code_nonzero(self):
        """新接口 code!=0（cookie 过期等）：返回 None"""
        resp = _fake_response(json_data={'code': -101, 'message': 'no login'})
        with mock.patch.object(BilibiliUtils, '_request_get', return_value=resp):
            self.assertIsNone(BilibiliUtils._parse_play_url_new(8178490))

    def test_resolve_play_m3u8_prefers_new_then_old(self):
        """解析优先新接口，失败回退旧接口"""
        resp_new = _fake_response(json_data={'code': -101})
        resp_old = _fake_response(json_data={
            'code': 0, 'data': {'durl': [{'url': 'http://old.com/a.m3u8?s=1'}]}})
        with mock.patch.object(BilibiliUtils, '_request_get',
                               side_effect=[resp_new, resp_old]):
            routes = BilibiliUtils.resolve_play_m3u8(8178490)
        self.assertEqual(len(routes), 1)
        self.assertIn('old.com', routes[0][0])

    def test_resolve_play_m3u8_caches(self):
        """缓存生效：TTL 内第二次调用不再请求上游（新接口不可用时走旧接口）"""
        resp = _fake_response(json_data={
            'code': 0, 'data': {'durl': [{'url': 'http://x.com/a.m3u8?s=1'}]}})
        with mock.patch.object(BilibiliUtils, '_parse_play_url_new', return_value=None), \
             mock.patch.object(BilibiliUtils, '_request_get', return_value=resp) as req:
            BilibiliUtils.resolve_play_m3u8(8178490)
            BilibiliUtils.resolve_play_m3u8(8178490)
        self.assertEqual(req.call_count, 1)

    def test_resolve_play_m3u8_force(self):
        """force=True 忽略缓存重新解析"""
        resp = _fake_response(json_data={
            'code': 0, 'data': {'durl': [{'url': 'http://x.com/a.m3u8?s=1'}]}})
        with mock.patch.object(BilibiliUtils, '_parse_play_url_new', return_value=None), \
             mock.patch.object(BilibiliUtils, '_request_get', return_value=resp) as req:
            BilibiliUtils.resolve_play_m3u8(8178490)
            BilibiliUtils.resolve_play_m3u8(8178490, force=True)
        self.assertEqual(req.call_count, 2)

    def test_resolve_play_m3u8_cache_hit_returns_routes(self):
        """缓存命中时返回线路列表（列表元素为三元组）"""
        resp = _fake_response(json_data={
            'code': 0, 'data': {'durl': [{'url': 'http://x.com/a.m3u8?s=1'}]}})
        with mock.patch.object(BilibiliUtils, '_request_get', return_value=resp):
            first = BilibiliUtils.resolve_play_m3u8(8178490)
            cached = BilibiliUtils.resolve_play_m3u8(8178490)
        self.assertIsInstance(cached, list)
        self.assertEqual(cached, first)
        self.assertEqual(len(cached[0]), 3)


class IsLiveTest(unittest.TestCase):
    """开播判定：实测主清单 200 才算在播"""

    def test_is_live_true_when_200(self):
        """主线路 200：判定在播"""
        routes = [('http://x.com/a.m3u8?s=1', 'http://x.com/', 's=1')]
        with mock.patch.object(BilibiliUtils, 'resolve_play_m3u8', return_value=routes), \
             mock.patch.object(BilibiliUtils, '_request_get',
                               return_value=_fake_response(status_code=200)):
            self.assertTrue(BilibiliUtils.is_live(8178490))

    def test_is_live_true_when_backup_ok(self):
        """主线路异常、备用 200：判定在播（备用切换）"""
        routes = [
            ('http://main.com/a.m3u8?s=1', 'http://main.com/', 's=1'),
            ('http://backup.com/a.m3u8?s=1', 'http://backup.com/', 's=1'),
        ]
        with mock.patch.object(BilibiliUtils, 'resolve_play_m3u8', return_value=routes), \
             mock.patch.object(BilibiliUtils, '_request_get',
                               side_effect=[Exception('conn'), _fake_response(status_code=200)]):
            self.assertTrue(BilibiliUtils.is_live(8178490))

    def test_is_live_false_when_all_fail(self):
        """全部线路失败（未开播场景，实测返回 000）：判定不在播"""
        routes = [('http://x.com/a.m3u8?s=1', 'http://x.com/', 's=1')]
        with mock.patch.object(BilibiliUtils, 'resolve_play_m3u8',
                               return_value=routes), \
             mock.patch.object(BilibiliUtils, '_request_get', side_effect=Exception('conn')):
            self.assertFalse(BilibiliUtils.is_live(22861369))

    def test_is_live_false_when_resolve_fails(self):
        """流地址解析失败：判定不在播"""
        with mock.patch.object(BilibiliUtils, 'resolve_play_m3u8', return_value=None):
            self.assertFalse(BilibiliUtils.is_live(999999))


class M3u8RewriteTest(unittest.TestCase):
    """主清单分片 URL 重写（直连/代理两种模式）"""

    RAW_M3U8 = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-MEDIA-SEQUENCE:100\n"
        "#EXTINF:4.000, no desc\n"
        "live_abc-100.ts\n"
        "#EXTINF:4.000, no desc\n"
        "live_abc-101.ts\n"
    )

    def _build(self, direct, raw=None):
        """按指定模式构建重写后的 m3u8（主线路 200）"""
        resp = _fake_response(status_code=200, text=raw or self.RAW_M3U8)
        routes = [('http://cdn.com/d/live.m3u8?s=1', 'http://cdn.com/d/', 's=1')]
        with mock.patch('core.bilibili.BILIBILI_DIRECT_SEGMENTS', direct), \
             mock.patch.object(BilibiliUtils, 'resolve_play_m3u8', return_value=routes), \
             mock.patch.object(BilibiliUtils, '_request_get', return_value=resp):
            return BilibiliUtils.build_proxied_m3u8(8178490, 'http://api:5002')

    def test_direct_mode_rewrites_to_cdn(self):
        """直连模式（默认）：分片重写为 B 站 CDN 绝对地址（带签名串），注释行保留"""
        content = self._build(direct=True)
        self.assertIn('#EXTM3U', content)
        self.assertIn('#EXT-X-VERSION:3', content)
        self.assertIn('http://cdn.com/d/live_abc-100.ts?s=1', content)
        self.assertIn('http://cdn.com/d/live_abc-101.ts?s=1', content)
        self.assertNotIn('api:5002', content)

    def test_direct_mode_keeps_absolute_url(self):
        """直连模式：已为绝对 URL 的分片原样保留"""
        raw = "#EXTM3U\nhttp://other.com/seg.ts\n"
        content = self._build(direct=True, raw=raw)
        self.assertIn('http://other.com/seg.ts', content)

    def test_proxy_mode_rewrites_to_self(self):
        """代理模式：分片重写为本服务 seg 地址"""
        content = self._build(direct=False)
        self.assertIn('http://api:5002/api/bilibili/8178490/seg/live_abc-100.ts', content)
        self.assertIn('http://api:5002/api/bilibili/8178490/seg/live_abc-101.ts', content)
        self.assertNotIn('http://cdn.com/', content)

    def test_build_proxied_m3u8_backup_route(self):
        """主线路失败、备用 200：用备用线路的 base_url 重写分片"""
        resp = _fake_response(status_code=200, text=self.RAW_M3U8)
        routes = [
            ('http://main.com/d/live.m3u8?s=1', 'http://main.com/d/', 's=1'),
            ('http://backup.com/d/live.m3u8?s=1', 'http://backup.com/d/', 's=1'),
        ]
        with mock.patch('core.bilibili.BILIBILI_DIRECT_SEGMENTS', True), \
             mock.patch.object(BilibiliUtils, 'resolve_play_m3u8', return_value=routes), \
             mock.patch.object(BilibiliUtils, '_request_get',
                               side_effect=[_fake_response(status_code=403), resp]):
            content = BilibiliUtils.build_proxied_m3u8(8178490, 'http://api:5002')
        self.assertIn('http://backup.com/d/live_abc-100.ts?s=1', content)
        self.assertNotIn('main.com', content)

    def test_build_proxied_m3u8_resolve_fail(self):
        """解析失败返回 None"""
        with mock.patch.object(BilibiliUtils, 'resolve_play_m3u8', return_value=None):
            self.assertIsNone(BilibiliUtils.build_proxied_m3u8(1, 'http://api'))

    def test_build_proxied_m3u8_retry_on_expired(self):
        """主清单非 200（签名过期）→ 强制重解析后成功（直连模式）"""
        resp_fail = _fake_response(status_code=403)
        resp_ok = _fake_response(status_code=200, text='#EXTM3U\nlive_abc-100.ts\n')
        with mock.patch('core.bilibili.BILIBILI_DIRECT_SEGMENTS', True), \
             mock.patch.object(BilibiliUtils, 'resolve_play_m3u8') as resolve:
            resolve.side_effect = [
                [('http://cdn.com/a.m3u8?s=1', 'http://cdn.com/', 's=1')],
                [('http://cdn.com/b.m3u8?s=2', 'http://cdn.com/', 's=2')],
            ]
            with mock.patch.object(BilibiliUtils, '_request_get',
                                   side_effect=[resp_fail, resp_ok]) as req:
                content = BilibiliUtils.build_proxied_m3u8(8178490, 'http://api:5002')
        self.assertIn('http://cdn.com/live_abc-100.ts?s=2', content)
        self.assertEqual(req.call_count, 2)


class ProxySegmentTest(unittest.TestCase):
    """分片反代：多线路切换与头部透传"""

    def test_proxy_segment_builds_signed_url(self):
        """分片 URL = 基础目录 + 相对路径 + 签名查询串；透传 Content-Type"""
        resp = mock.Mock()
        resp.status_code = 200
        resp.headers = {'Content-Type': 'video/mp2t', 'Content-Length': '1024', 'Server': 'nginx'}
        resp.iter_content.return_value = [b'data']
        routes = [('http://cdn.com/d/a.m3u8?s=1', 'http://cdn.com/d/', 's=1')]
        with mock.patch.object(BilibiliUtils, 'resolve_play_m3u8', return_value=routes), \
             mock.patch.object(BilibiliUtils, '_request_get', return_value=resp) as req:
            status, headers, stream = BilibiliUtils.proxy_segment(8178490, 'a-100.ts')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'video/mp2t')
        self.assertNotIn('Server', headers)  # 非白名单头不透传
        self.assertEqual(list(stream), [b'data'])
        # 请求 URL 带签名查询串
        self.assertIn('http://cdn.com/d/a-100.ts', req.call_args[0][0])
        self.assertIn('s=1', req.call_args[0][0])

    def test_proxy_segment_backup_route(self):
        """主线路失败、备用 200：切备用线路拉分片"""
        resp = mock.Mock()
        resp.status_code = 200
        resp.headers = {'Content-Type': 'video/mp2t'}
        resp.iter_content.return_value = [b'data']
        routes = [
            ('http://main.com/d/a.m3u8?s=1', 'http://main.com/d/', 's=1'),
            ('http://backup.com/d/a.m3u8?s=1', 'http://backup.com/d/', 's=1'),
        ]
        with mock.patch.object(BilibiliUtils, 'resolve_play_m3u8', return_value=routes), \
             mock.patch.object(BilibiliUtils, '_request_get',
                               side_effect=[_fake_response(status_code=403), resp]) as req:
            status, _headers, _stream = BilibiliUtils.proxy_segment(8178490, 'a-100.ts')
        self.assertEqual(status, 200)
        self.assertIn('http://backup.com/d/a-100.ts', req.call_args[0][0])
        self.assertIn('s=1', req.call_args[0][0])

    def test_proxy_segment_fail(self):
        """解析失败返回 500"""
        with mock.patch.object(BilibiliUtils, 'resolve_play_m3u8', return_value=None):
            status, headers, stream = BilibiliUtils.proxy_segment(1, 'a.ts')
        self.assertEqual(status, 500)
        self.assertIsNone(headers)


class CookieTest(unittest.TestCase):
    """登录 cookie：请求携带与有效性探测"""

    def test_request_get_carries_cookie(self):
        """配置了 BILIBILI_COOKIE 时请求带上 Cookie 头"""
        with mock.patch('core.bilibili.BILIBILI_COOKIE', 'SESSDATA=abc123'), \
             mock.patch('core.bilibili.requests.get') as get:
            BilibiliUtils._request_get('http://x.com')
        headers = get.call_args[1]['headers']
        self.assertEqual(headers.get('Cookie'), 'SESSDATA=abc123')

    def test_request_get_no_cookie(self):
        """未配置 cookie 时不带 Cookie 头"""
        with mock.patch('core.bilibili.BILIBILI_COOKIE', ''), \
             mock.patch('core.bilibili.requests.get') as get:
            BilibiliUtils._request_get('http://x.com')
        headers = get.call_args[1]['headers']
        self.assertNotIn('Cookie', headers)

    def test_check_cookie_valid_true(self):
        """nav 返回 isLogin=True：cookie 有效"""
        resp = _fake_response(json_data={'code': 0, 'data': {'isLogin': True}})
        with mock.patch('core.bilibili.BILIBILI_COOKIE', 'SESSDATA=x'), \
             mock.patch.object(BilibiliUtils, '_request_get', return_value=resp):
            self.assertTrue(BilibiliUtils._check_cookie_valid())

    def test_check_cookie_valid_false(self):
        """nav 返回 isLogin=False：cookie 失效"""
        resp = _fake_response(json_data={'code': 0, 'data': {'isLogin': False}})
        with mock.patch('core.bilibili.BILIBILI_COOKIE', 'SESSDATA=x'), \
             mock.patch.object(BilibiliUtils, '_request_get', return_value=resp):
            self.assertFalse(BilibiliUtils._check_cookie_valid())

    def test_check_cookie_valid_when_not_configured(self):
        """未配置 cookie：直接返回 False"""
        with mock.patch('core.bilibili.BILIBILI_COOKIE', ''):
            self.assertFalse(BilibiliUtils._check_cookie_valid())


class AggregateIntegrationTest(unittest.TestCase):
    """B 站频道接入聚合"""

    def setUp(self):
        # 隔离动态列表，避免读真实 bilibili_custom_rooms.json 影响断言
        patcher = mock.patch.object(BilibiliUtils, 'load_custom_rooms', return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        # 隔离管理库（未初始化路径 → 走 config 兜底），避免读真实 admin.db 的源配置
        self.db_patcher = mock.patch('admin.db.ADMIN_DB_PATH',
                                     os.path.join(tempfile.mkdtemp(), 'uninit.db'))
        self.db_patcher.start()
        self.addCleanup(self.db_patcher.stop)

    @staticmethod
    def _ch(name, url, group):
        return {'name': name, 'url': url, 'group_title': group, 'tvg_name': name}

    def test_fetch_bilibili_skips_not_live(self):
        """未开播的房间被跳过，不进列表"""
        with mock.patch.object(BilibiliUtils, 'get_room_id', return_value=22861369), \
             mock.patch.object(BilibiliUtils, 'is_live', return_value=False):
            channels = AggregatorUtils.fetch_bilibili_channels()
        self.assertEqual(channels, [])

    def test_fetch_bilibili_includes_live(self):
        """全部开播时按配置顺序加入列表（央视新闻默认在首位），URL 为本服务代理地址"""
        rooms = [{'name': '央视新闻', 'uid': 222103174},
                 {'name': '河南卫视', 'uid': 2057655323}]
        room_by_uid = {222103174: 8178490, 2057655323: 22861369}
        with mock.patch('core.aggregator.BILIBILI_ROOMS', rooms), \
             mock.patch.object(BilibiliUtils, 'get_room_id',
                               side_effect=lambda uid: room_by_uid.get(uid)), \
             mock.patch.object(BilibiliUtils, 'is_live', return_value=True):
            channels = AggregatorUtils.fetch_bilibili_channels()
        self.assertEqual(len(channels), 2)
        self.assertEqual(channels[0]['name'], '央视新闻')
        self.assertEqual(channels[1]['name'], '河南卫视')
        self.assertEqual(channels[0]['group_title'], 'B站直播')
        self.assertIn('/api/bilibili/8178490/live.m3u8', channels[0]['url'])
        self.assertIn('/api/bilibili/22861369/live.m3u8', channels[1]['url'])

    def test_fetch_bilibili_mixed_live(self):
        """部分开播：只有开播的进列表（央视新闻开播、河南卫视未开播）"""
        room_by_uid = {222103174: 8178490, 2057655323: 22861369}

        def fake_is_live(room_id):
            return room_id == 8178490

        with mock.patch.object(BilibiliUtils, 'get_room_id',
                               side_effect=lambda uid: room_by_uid.get(uid)), \
             mock.patch.object(BilibiliUtils, 'is_live', side_effect=fake_is_live):
            channels = AggregatorUtils.fetch_bilibili_channels()
        self.assertEqual([c['name'] for c in channels], ['央视新闻'])

    def test_fetch_bilibili_room_fail_skip(self):
        """房间号解析失败：跳过"""
        with mock.patch.object(BilibiliUtils, 'get_room_id', return_value=None):
            channels = AggregatorUtils.fetch_bilibili_channels()
        self.assertEqual(channels, [])

    def test_aggregate_m3u_includes_bilibili_group(self):
        """聚合结果包含 B 站直播分组，且输出顺序在卫视之后"""
        hntv = [self._ch('河南卫视', 'http://hntv/1.m3u8', '河南卫视')]
        public = [self._ch('CCTV-1 综合', 'http://cctv/1.m3u8', '央视')]
        bili = [self._ch('河南卫视B站', 'http://api:5002/api/bilibili/22861369/live.m3u8', 'B站直播')]
        content = AggregatorUtils.aggregate_m3u(hntv, public, bili)
        self.assertIn('group-title="B站直播"', content)
        self.assertIn('http://api:5002/api/bilibili/22861369/live.m3u8', content)
        # 分组顺序：河南卫视 < 央视 < B站直播（按 group-title 出现顺序断言）
        self.assertLess(content.index('group-title="河南卫视"'),
                        content.index('group-title="央视"'))
        self.assertLess(content.index('group-title="央视"'),
                        content.index('group-title="B站直播"'))

    def test_aggregate_m3u_default_no_bilibili(self):
        """不传 B 站频道参数：行为与旧版一致"""
        hntv = [self._ch('河南卫视', 'http://hntv/1.m3u8', '河南卫视')]
        content = AggregatorUtils.aggregate_m3u(hntv, [])
        self.assertIn('河南卫视', content)
        self.assertNotIn('B站直播', content)


class TestModeTest(unittest.TestCase):
    """B 站测试模式：跳过 hntv/公开源，只聚合 B 站；降级路径不碰 hntv"""

    def setUp(self):
        # 隔离管理库（未初始化路径 → config 兜底），避免读真实 admin.db 的源/设置
        self.db_patcher = mock.patch('admin.db.ADMIN_DB_PATH',
                                     os.path.join(tempfile.mkdtemp(), 'uninit.db'))
        self.db_patcher.start()
        self.addCleanup(self.db_patcher.stop)

    @staticmethod
    def _ch(name, url, group):
        return {'name': name, 'url': url, 'group_title': group, 'tvg_name': name}

    def test_test_mode_skips_hntv_and_public(self):
        """测试模式：不调 hntv 拉取/公开源准备，只调 B 站收集"""
        with mock.patch('core.aggregator.BILIBILI_ONLY_MODE', True), \
             mock.patch.object(AggregatorUtils, 'fetch_hntv_channels') as hntv, \
             mock.patch.object(AggregatorUtils, 'prepare_public_channels') as pub, \
             mock.patch.object(AggregatorUtils, '_save_public_channels') as save, \
             mock.patch.object(AggregatorUtils, 'fetch_bilibili_channels',
                               return_value=[self._ch('央视新闻', 'http://bili/1.m3u8', 'B站直播')]), \
             mock.patch('core.aggregator.atomic_write_text') as write, \
             mock.patch('core.aggregator.AggregatorUtils._aggregate_lock') as lock:
            lock.acquire.return_value = True
            content = AggregatorUtils._get_aggregated_m3u_locked()
        hntv.assert_not_called()
        pub.assert_not_called()
        save.assert_not_called()
        write.assert_called_once()
        self.assertIn('B站直播', content)

    def test_full_mode_still_fetches_hntv(self):
        """非测试模式：仍走 hntv + 公开源 + B 站完整流程"""
        with mock.patch('core.aggregator.BILIBILI_ONLY_MODE', False), \
             mock.patch.object(AggregatorUtils, 'fetch_hntv_channels',
                               return_value=[self._ch('河南卫视', 'http://h/1.m3u8', '河南卫视')]), \
             mock.patch.object(AggregatorUtils, 'prepare_public_channels', return_value=[]), \
             mock.patch.object(AggregatorUtils, '_save_public_channels'), \
             mock.patch.object(AggregatorUtils, 'fetch_bilibili_channels', return_value=[]), \
             mock.patch('core.aggregator.atomic_write_text'):
            content = AggregatorUtils._get_aggregated_m3u_locked()
        self.assertIn('河南卫视', content)
        self.assertNotIn('B站直播', content)

    def test_test_mode_degrade_returns_bilibili_only(self):
        """测试模式降级：返回 B 站列表，不调 hntv 官方源"""
        with mock.patch('core.aggregator.BILIBILI_ONLY_MODE', True), \
             mock.patch.object(AggregatorUtils, 'get_aggregated_m3u', return_value=None), \
             mock.patch.object(AggregatorUtils, 'get_bilibili_only_m3u',
                               return_value="#EXTM3U\n# B站列表\n") as bili_degrade, \
             mock.patch.object(AggregatorUtils, 'get_hntv_only_m3u') as hntv_degrade, \
             mock.patch('core.aggregator.os.path.exists', return_value=False):
            content = AggregatorUtils.load_aggregated_m3u()
        self.assertEqual(content, "#EXTM3U\n# B站列表\n")
        bili_degrade.assert_called_once()
        hntv_degrade.assert_not_called()

    def test_full_mode_degrade_returns_hntv(self):
        """正式模式降级：仍返回 hntv 官方源"""
        with mock.patch('core.aggregator.BILIBILI_ONLY_MODE', False), \
             mock.patch.object(AggregatorUtils, 'get_aggregated_m3u', return_value=None), \
             mock.patch.object(AggregatorUtils, 'get_bilibili_only_m3u') as bili_degrade, \
             mock.patch.object(AggregatorUtils, 'get_hntv_only_m3u',
                               return_value="#EXTM3U\n# hntv列表\n") as hntv_degrade, \
             mock.patch('core.aggregator.os.path.exists', return_value=False):
            content = AggregatorUtils.load_aggregated_m3u()
        self.assertEqual(content, "#EXTM3U\n# hntv列表\n")
        hntv_degrade.assert_called_once()
        bili_degrade.assert_not_called()


class CustomRoomsTest(unittest.TestCase):
    """运行时动态频道列表的读写与增删"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp_dir, 'custom_rooms.json')
        self.patcher = mock.patch('core.bilibili.BILIBILI_CUSTOM_ROOMS_PATH', self.path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_load_empty_when_missing(self):
        """文件不存在：返回空列表"""
        self.assertEqual(BilibiliUtils.load_custom_rooms(), [])

    def test_add_and_load_roundtrip(self):
        """添加后落盘，可读回"""
        BilibiliUtils.add_custom_room('央视新闻', 8178490)
        rooms = BilibiliUtils.load_custom_rooms()
        self.assertEqual(rooms, [{'name': '央视新闻', 'room_id': 8178490}])

    def test_add_dedup_by_room_id(self):
        """同 room_id 重复添加：更新名称而非重复"""
        BilibiliUtils.add_custom_room('旧名', 8178490)
        BilibiliUtils.add_custom_room('新名', 8178490)
        rooms = BilibiliUtils.load_custom_rooms()
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]['name'], '新名')

    def test_remove_custom_room(self):
        """删除：存在返回 True，不存在返回 False"""
        BilibiliUtils.add_custom_room('A', 1)
        removed, rooms = BilibiliUtils.remove_custom_room(1)
        self.assertTrue(removed)
        self.assertEqual(rooms, [])
        removed, _ = BilibiliUtils.remove_custom_room(999)
        self.assertFalse(removed)


class ListRoomsTest(unittest.TestCase):
    """静态+动态合并去重（room_id 唯一，静态优先）"""

    def setUp(self):
        # 隔离管理库（未初始化路径 → 走 config 兜底），避免读真实 admin.db 的源配置
        self.db_patcher = mock.patch('admin.db.ADMIN_DB_PATH',
                                     os.path.join(tempfile.mkdtemp(), 'uninit.db'))
        self.db_patcher.start()
        self.addCleanup(self.db_patcher.stop)

    def test_static_priority_and_dedup(self):
        """静态优先：动态与静态同 room_id 的被剔除；新房间追加"""
        rooms = [{'name': '央视新闻', 'uid': 222103174},
                 {'name': '河南卫视', 'uid': 2057655323},
                 {'name': '中国应急管理', 'uid': 3707002299615617}]
        uid_map = {222103174: 8178490, 2057655323: 22861369, 3707002299615617: 1706660507}
        with mock.patch('core.aggregator.BILIBILI_ROOMS', rooms), \
             mock.patch.object(BilibiliUtils, 'get_room_id',
                               side_effect=lambda uid: uid_map.get(uid)), \
             mock.patch.object(BilibiliUtils, 'load_custom_rooms', return_value=[
                 {'name': '动态重复', 'room_id': 8178490},   # 与静态重复，应被剔除
                 {'name': '动态新频道', 'room_id': 99999},    # 新房间，保留
             ]):
            rooms = AggregatorUtils.list_bilibili_rooms()
        self.assertEqual([r['room_id'] for r in rooms],
                         [8178490, 22861369, 1706660507, 99999])
        self.assertEqual(rooms[0]['name'], '央视新闻')
        self.assertEqual(rooms[0]['source'], 'static')
        self.assertEqual(rooms[-1]['name'], '动态新频道')
        self.assertEqual(rooms[-1]['source'], 'custom')

    def test_room_id_direct_in_static(self):
        """静态配置支持 room_id 直填（不调 get_room_id）"""
        with mock.patch('core.aggregator.BILIBILI_ROOMS',
                        [{'name': '某频道', 'room_id': 12345}]), \
             mock.patch.object(BilibiliUtils, 'get_room_id') as get_room, \
             mock.patch.object(BilibiliUtils, 'load_custom_rooms', return_value=[]):
            rooms = AggregatorUtils.list_bilibili_rooms()
        self.assertEqual([r['room_id'] for r in rooms], [12345])
        get_room.assert_not_called()

    def test_fetch_includes_custom_live_room(self):
        """动态添加的开播房间被采集进列表"""
        with mock.patch.object(BilibiliUtils, 'get_room_id', return_value=None), \
             mock.patch.object(BilibiliUtils, 'load_custom_rooms',
                               return_value=[{'name': '动态频道', 'room_id': 99999}]), \
             mock.patch.object(BilibiliUtils, 'is_live', return_value=True):
            channels = AggregatorUtils.fetch_bilibili_channels()
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]['name'], '动态频道')
        self.assertIn('/api/bilibili/99999/live.m3u8', channels[0]['url'])


class AsyncRefreshTest(unittest.TestCase):
    """异步刷新：合并多次请求，worker 消费标志"""

    def tearDown(self):
        import core.aggregator as agg
        agg._refresh_pending = False

    def test_request_async_refresh_coalesces(self):
        """多次请求合并：只启动一个 worker 线程"""
        import core.aggregator as agg
        agg._refresh_pending = False
        with mock.patch('core.aggregator.threading.Thread') as thread_cls:
            AggregatorUtils.request_async_refresh()
            AggregatorUtils.request_async_refresh()  # 合并，不再启动线程
        self.assertEqual(thread_cls.call_count, 1)

    def test_worker_consumes_flag_and_refreshes(self):
        """worker 消费标志并执行一次锁定聚合"""
        import core.aggregator as agg
        agg._refresh_pending = True
        with mock.patch.object(AggregatorUtils, '_get_aggregated_m3u_locked') as locked:
            AggregatorUtils._async_refresh_worker()
        locked.assert_called_once()
        self.assertFalse(agg._refresh_pending)


class BilibiliRoomsApiTest(unittest.TestCase):
    """管理 API：GET 列表 / POST 添加 / DELETE 删除（含鉴权）"""

    def setUp(self):
        from app import create_app
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_get_rooms(self):
        """GET 无鉴权：列出静态+动态，默认附带实时开播状态"""
        rooms = [{'name': '央视新闻', 'room_id': 8178490, 'source': 'static'},
                 {'name': '动态频道', 'room_id': 99999, 'source': 'custom'}]
        with mock.patch.object(AggregatorUtils, 'list_bilibili_rooms', return_value=rooms), \
             mock.patch.object(BilibiliUtils, 'is_live',
                               side_effect=lambda rid: rid == 8178490):
            resp = self.client.get('/api/bilibili/rooms')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['rooms']
        self.assertEqual(data[0]['live'], True)
        self.assertEqual(data[1]['live'], False)

    def test_post_requires_token(self):
        """POST 无 token：401"""
        resp = self.client.post('/api/bilibili/rooms', json={'name': 'x', 'room_id': 123})
        self.assertEqual(resp.status_code, 401)

    def test_post_invalid_room_id(self):
        """POST room_id 非法：400"""
        with mock.patch('app.TokenUtils.verify_token', return_value=True):
            resp = self.client.post('/api/bilibili/rooms', json={'name': 'x', 'room_id': 'abc'},
                                    headers={'Authorization': 'Bearer t'})
        self.assertEqual(resp.status_code, 400)

    def test_post_adds_and_refreshes(self):
        """POST 合法：写动态列表 + 触发异步刷新"""
        with mock.patch('app.TokenUtils.verify_token', return_value=True), \
             mock.patch.object(BilibiliUtils, 'add_custom_room',
                               return_value=[{'name': 'x', 'room_id': 123}]) as add, \
             mock.patch.object(AggregatorUtils, 'request_async_refresh') as refresh:
            resp = self.client.post('/api/bilibili/rooms',
                                    json={'name': '某频道', 'room_id': 123},
                                    headers={'Authorization': 'Bearer t'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('success', resp.get_json()['status'])
        add.assert_called_once_with('某频道', 123)
        refresh.assert_called_once()

    def test_delete_room(self):
        """DELETE：删除动态房间 + 触发刷新"""
        with mock.patch('app.TokenUtils.verify_token', return_value=True), \
             mock.patch.object(BilibiliUtils, 'remove_custom_room',
                               return_value=(True, [])) as remove, \
             mock.patch.object(AggregatorUtils, 'request_async_refresh') as refresh:
            resp = self.client.delete('/api/bilibili/rooms/123',
                                      headers={'Authorization': 'Bearer t'})
        self.assertEqual(resp.status_code, 200)
        remove.assert_called_once_with(123)
        refresh.assert_called_once()

    def test_delete_room_not_found(self):
        """DELETE 不存在的房间：404"""
        with mock.patch('app.TokenUtils.verify_token', return_value=True), \
             mock.patch.object(BilibiliUtils, 'remove_custom_room', return_value=(False, [])):
            resp = self.client.delete('/api/bilibili/rooms/999',
                                      headers={'Authorization': 'Bearer t'})
        self.assertEqual(resp.status_code, 404)


if __name__ == '__main__':
    unittest.main()
