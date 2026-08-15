"""P2-1 数据源 DB 化测试：DB 优先 + config 兜底（空表/未初始化/异常均回退种子值）"""
import os
import tempfile
import unittest
from unittest import mock

import config
import core.aggregator
from admin import db
from core.aggregator import AggregatorUtils
from core.bilibili import BilibiliUtils
from core.sources import SourceUtils


def _temp_db_path():
    """临时管理库路径（不含建表，由用例自行 init_db）"""
    return os.path.join(tempfile.mkdtemp(), 'test.db')


class SourceDbFallbackTest(unittest.TestCase):
    """公开源 url 来源：DB 优先，空表/未初始化/异常回退 config"""

    def setUp(self):
        self.db_path = _temp_db_path()
        self.db_patcher = mock.patch('admin.db.ADMIN_DB_PATH', self.db_path)
        self.db_patcher.start()
        db.init_db()
        # 固定种子值便于断言兜底
        self.seed = ['http://seed/a.m3u', 'http://seed/b.m3u']
        self.seed_patcher = mock.patch('core.sources.PUBLIC_M3U_SOURCES', self.seed)
        self.seed_patcher.start()

    def tearDown(self):
        self.seed_patcher.stop()
        self.db_patcher.stop()

    def test_empty_db_falls_back_to_config(self):
        """空表：回退 config 种子值"""
        self.assertEqual(SourceUtils.get_public_source_urls(), self.seed)

    def test_db_urls_win_in_sort_order(self):
        """DB 有启用源：按 sort_order 返回；禁用/空 url 排除"""
        db.add_source('public', 'B', 'http://db/b.m3u', sort_order=2)
        db.add_source('public', 'A', 'http://db/a.m3u', sort_order=1)
        db.add_source('public', '禁用', 'http://db/off.m3u', enabled=0)
        db.add_source('public', '空url', '', enabled=1)
        urls = SourceUtils.get_public_source_urls()
        self.assertEqual(urls, ['http://db/a.m3u', 'http://db/b.m3u'])

    def test_uninitialized_db_falls_back_without_creating_file(self):
        """库文件不存在：回退 config 且不凭空创建库文件"""
        missing = os.path.join(tempfile.mkdtemp(), 'missing', 'x.db')
        with mock.patch('admin.db.ADMIN_DB_PATH', missing):
            self.assertEqual(SourceUtils.get_public_source_urls(), self.seed)
        self.assertFalse(os.path.exists(missing))

    def test_db_exception_falls_back(self):
        """DB 查询异常：回退 config"""
        with mock.patch('admin.db.get_enabled_public_urls',
                        side_effect=RuntimeError('boom')):
            self.assertEqual(SourceUtils.get_public_source_urls(), self.seed)

    def test_fetch_all_uses_db_urls(self):
        """fetch_all_public_channels 用 DB url 逐个拉取"""
        db.add_source('public', 'A', 'http://db/a.m3u')
        with mock.patch.object(SourceUtils, 'fetch_public_m3u', return_value='') as m:
            SourceUtils.fetch_all_public_channels()
        self.assertEqual([c.args[0] for c in m.call_args_list], ['http://db/a.m3u'])


class BilibiliStaticRoomsDbTest(unittest.TestCase):
    """B 站静态房间来源：DB 优先，空表回退 config"""

    def setUp(self):
        self.db_path = _temp_db_path()
        self.db_patcher = mock.patch('admin.db.ADMIN_DB_PATH', self.db_path)
        self.db_patcher.start()
        db.init_db()
        self.seed = [{'name': '种子台', 'room_id': 10086}]
        self.seed_patcher = mock.patch('core.aggregator.BILIBILI_ROOMS', self.seed)
        self.seed_patcher.start()

    def tearDown(self):
        self.seed_patcher.stop()
        self.db_patcher.stop()

    def test_empty_db_falls_back_to_config(self):
        self.assertEqual(AggregatorUtils._get_bilibili_static_rooms(), self.seed)

    def test_db_rooms_win(self):
        """DB 有 bilibili 行：url 数字解析为 room_id"""
        db.add_source('bilibili', '央视新闻', '8178490')
        rooms = AggregatorUtils._get_bilibili_static_rooms()
        self.assertEqual(rooms, [{'name': '央视新闻', 'room_id': 8178490}])

    def test_disabled_room_excluded(self):
        db.add_source('bilibili', 'A', '1')
        db.add_source('bilibili', 'B', '2', enabled=0)
        rooms = AggregatorUtils._get_bilibili_static_rooms()
        self.assertEqual([r['name'] for r in rooms], ['A'])

    def test_list_bilibili_rooms_uses_db_static(self):
        """list_bilibili_rooms：DB 静态房间进列表（source=static），自定义列表置空"""
        db.add_source('bilibili', '央视新闻', '8178490')
        with mock.patch('core.bilibili.BILIBILI_CUSTOM_ROOMS_PATH',
                        os.path.join(tempfile.mkdtemp(), 'custom.json')):
            rooms = AggregatorUtils.list_bilibili_rooms()
        self.assertEqual(rooms,
                         [{'name': '央视新闻', 'room_id': 8178490, 'source': 'static'}])


class PublicBaseUrlDbTest(unittest.TestCase):
    """public_base_url：DB 设置优先，聚合生成 B 站频道 URL 时使用"""

    def setUp(self):
        self.db_patcher = mock.patch('admin.db.ADMIN_DB_PATH',
                                     os.path.join(tempfile.mkdtemp(), 't.db'))
        self.db_patcher.start()
        db.init_db()

    def tearDown(self):
        self.db_patcher.stop()

    def test_db_public_base_url_wins(self):
        """DB 设置 public_base_url 后，B 站频道 URL 用它生成"""
        db.set_setting('public_base_url', 'http://db.example:15002')
        with mock.patch('core.bilibili.BILIBILI_CUSTOM_ROOMS_PATH',
                        os.path.join(tempfile.mkdtemp(), 'custom.json')), \
             mock.patch.object(BilibiliUtils, 'is_live', return_value=True):
            channels = AggregatorUtils.fetch_bilibili_channels()
        self.assertTrue(channels)
        self.assertTrue(all(
            c['url'].startswith('http://db.example:15002/api/bilibili/')
            for c in channels))

    def test_fallback_to_config(self):
        """未设置：回退 config PUBLIC_BASE_URL"""
        self.assertEqual(AggregatorUtils._public_base_url(),
                         config.PUBLIC_BASE_URL)


class CustomChannelsTest(unittest.TestCase):
    """自定义流频道（type=custom）：DB 读取 + 进聚合 + 去重"""

    def setUp(self):
        self.db_patcher = mock.patch('admin.db.ADMIN_DB_PATH',
                                     os.path.join(tempfile.mkdtemp(), 't.db'))
        self.db_patcher.start()
        db.init_db()
        # 隔离覆盖缓存，避免串扰
        core.aggregator._override_cache.update({'expire': 0.0, 'data': {}})

    def tearDown(self):
        self.db_patcher.stop()

    def test_get_custom_channels_from_db(self):
        """启用且 url 非空的自定义源才返回，group=自定义"""
        db.add_source('custom', '抖音-央视新闻', 'http://127.0.0.1:8080/cctv/index.m3u8')
        db.add_source('custom', '禁用流', 'http://127.0.0.1:8080/x.m3u8', enabled=0)
        channels = AggregatorUtils._get_custom_channels()
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]['name'], '抖音-央视新闻')
        self.assertEqual(channels[0]['group_title'], '自定义')

    def test_aggregate_includes_custom_group(self):
        """自定义流进聚合：group-title=自定义，位于卫视之后、B站之前"""
        db.add_source('custom', '抖音-央视新闻', 'http://127.0.0.1:8080/cctv/index.m3u8')
        hntv = [{'name': '河南卫视', 'cid': 1, 'group_title': '河南卫视', 'url': 'http://h/1.m3u8'}]
        public = [{'name': '北京卫视', 'tvg_name': '北京卫视', 'group_title': '卫视', 'url': 'http://w/1.m3u8'}]
        bili = [{'name': 'B站台', 'tvg_name': 'B站台', 'group_title': 'B站直播', 'url': 'http://b/1.m3u8'}]
        content = AggregatorUtils.aggregate_m3u(
            hntv, public, bili, custom_channels=AggregatorUtils._get_custom_channels())
        self.assertIn('group-title="自定义"', content)
        self.assertIn('http://127.0.0.1:8080/cctv/index.m3u8', content)
        # 顺序：卫视 < 自定义 < B站直播
        self.assertLess(content.index('group-title="卫视"'),
                        content.index('group-title="自定义"'))
        self.assertLess(content.index('group-title="自定义"'),
                        content.index('group-title="B站直播"'))

    def test_custom_dedup_with_hntv(self):
        """自定义流与官方同名：官方优先，自定义不覆盖"""
        db.add_source('custom', '河南卫视', 'http://127.0.0.1:8080/hn.m3u8')
        hntv = [{'name': '河南卫视', 'cid': 1, 'group_title': '河南卫视', 'url': 'http://h/1.m3u8'}]
        content = AggregatorUtils.aggregate_m3u(
            hntv, [], custom_channels=AggregatorUtils._get_custom_channels())
        self.assertIn('http://h/1.m3u8', content)
        self.assertNotIn('http://127.0.0.1:8080/hn.m3u8', content)


if __name__ == '__main__':
    unittest.main()
