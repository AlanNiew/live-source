"""P2-2 频道覆盖层测试：禁用/改名/改分组只影响输出；bilibili_only_mode 动态开关"""
import os
import tempfile
import unittest
from unittest import mock

import core.aggregator
from admin import db
from core.aggregator import AggregatorUtils


def _ch(name, url, group, **extra):
    """构造聚合频道 dict（与聚合管线字段一致）"""
    ch = {'name': name, 'url': url, 'group_title': group, 'tvg_name': name}
    ch.update(extra)
    return ch


class ChannelOverrideTest(unittest.TestCase):
    """aggregate_m3u 输出前应用频道覆盖：禁用跳过、改名、改分组"""

    def setUp(self):
        self.db_patcher = mock.patch('admin.db.ADMIN_DB_PATH',
                                     os.path.join(tempfile.mkdtemp(), 'test.db'))
        self.db_patcher.start()
        db.init_db()
        # 每个用例重置覆盖缓存，避免 TTL 缓存串扰
        core.aggregator._override_cache.update({'expire': 0.0, 'data': {}})

    def tearDown(self):
        self.db_patcher.stop()

    def _agg(self, public):
        hntv = [_ch('河南卫视', 'http://h/1.m3u8', '河南卫视', cid=1)]
        return AggregatorUtils.aggregate_m3u(hntv, public, [])

    def test_no_overrides_unchanged(self):
        """无覆盖配置：输出不变"""
        content = self._agg([_ch('CCTV-1 综合', 'http://c/1.m3u8', '央视')])
        self.assertIn('CCTV-1 综合', content)
        self.assertIn('河南卫视', content)

    def test_disabled_channel_skipped(self):
        """enabled=0：该频道从输出移除，其余频道不受影响"""
        db.upsert_channel_override('CCTV-1 综合', enabled=0)
        content = self._agg([_ch('CCTV-1 综合', 'http://c/1.m3u8', '央视')])
        self.assertNotIn('CCTV-1 综合', content)
        self.assertIn('河南卫视', content)

    def test_rename_display_name(self):
        """display_name：输出显示名用新名（tvg 字段保持原名不变）"""
        db.upsert_channel_override('CCTV-1 综合', display_name='中央一套')
        content = self._agg([_ch('CCTV-1 综合', 'http://c/1.m3u8', '央视')])
        self.assertIn(',中央一套', content)
        self.assertNotIn(',CCTV-1 综合', content)

    def test_regroup(self):
        """group_title：输出分组被覆盖"""
        db.upsert_channel_override('CCTV-1 综合', group_title='测试组')
        content = self._agg([_ch('CCTV-1 综合', 'http://c/1.m3u8', '央视')])
        self.assertIn('group-title="测试组"', content)

    def test_override_cache_ttl(self):
        """覆盖查询有 TTL 缓存：有效期内不重复查库；失效后重查"""
        db.upsert_channel_override('CCTV-1 综合', enabled=0)
        public = [_ch('CCTV-1 综合', 'http://c/1.m3u8', '央视')]
        with mock.patch('admin.db.get_channel_overrides',
                        wraps=db.get_channel_overrides) as m:
            self._agg(public)   # 第一次：查库
            self._agg(public)   # 第二次：命中缓存
            self.assertEqual(m.call_count, 1)
            core.aggregator._override_cache['expire'] = 0.0
            self._agg(public)   # 缓存失效：重查
            self.assertEqual(m.call_count, 2)

    def test_override_error_does_not_break_aggregation(self):
        """覆盖查询异常：回退空覆盖，聚合照常输出"""
        core.aggregator._override_cache.update({'expire': 0.0, 'data': {}})
        with mock.patch('admin.db.get_channel_overrides',
                        side_effect=RuntimeError('boom')):
            content = self._agg([_ch('CCTV-1 综合', 'http://c/1.m3u8', '央视')])
        self.assertIn('CCTV-1 综合', content)


class BilibiliOnlyModeTest(unittest.TestCase):
    """bilibili_only_mode：DB 设置优先，未设置回退 env；未初始化库不创建文件"""

    def setUp(self):
        self.db_patcher = mock.patch('admin.db.ADMIN_DB_PATH',
                                     os.path.join(tempfile.mkdtemp(), 'test.db'))
        self.db_patcher.start()
        db.init_db()

    def tearDown(self):
        self.db_patcher.stop()

    def test_falls_back_to_env_when_unset(self):
        with mock.patch('core.aggregator.BILIBILI_ONLY_MODE', True):
            self.assertTrue(AggregatorUtils.is_bilibili_only_mode())
        with mock.patch('core.aggregator.BILIBILI_ONLY_MODE', False):
            self.assertFalse(AggregatorUtils.is_bilibili_only_mode())

    def test_db_setting_overrides_env(self):
        db.set_setting('bilibili_only_mode', 'true')
        with mock.patch('core.aggregator.BILIBILI_ONLY_MODE', False):
            self.assertTrue(AggregatorUtils.is_bilibili_only_mode())
        db.set_setting('bilibili_only_mode', 'false')
        with mock.patch('core.aggregator.BILIBILI_ONLY_MODE', True):
            self.assertFalse(AggregatorUtils.is_bilibili_only_mode())

    def test_uninitialized_db_falls_back_without_creating(self):
        missing = os.path.join(tempfile.mkdtemp(), 'missing', 'x.db')
        with mock.patch('admin.db.ADMIN_DB_PATH', missing), \
             mock.patch('core.aggregator.BILIBILI_ONLY_MODE', True):
            self.assertTrue(AggregatorUtils.is_bilibili_only_mode())
        self.assertFalse(os.path.exists(missing))


if __name__ == '__main__':
    unittest.main()
