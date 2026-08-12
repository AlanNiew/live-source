"""聚合测试：两轮淘汰语义、官方源跳过、403 宽松、失败记录读写与裁剪"""
import json
import os
import tempfile
import unittest
from unittest import mock

from config import XML_DATA_DIR
from core.aggregator import AggregatorUtils
from core.sources import SourceUtils


class AggregatorFilterTest(unittest.TestCase):
    """探测过滤逻辑测试（mock probe_stream 控制结果，失败记录用临时文件）"""

    def setUp(self):
        # 隔离失败记录文件
        self.tmp_dir = tempfile.mkdtemp()
        self.fail_path = os.path.join(self.tmp_dir, 'failures.json')
        patcher_load = mock.patch.object(
            AggregatorUtils, '_load_failures',
            side_effect=lambda: json.load(open(self.fail_path, encoding='utf-8'))
            if os.path.exists(self.fail_path) else {})
        patcher_save = mock.patch.object(
            AggregatorUtils, '_save_failures',
            side_effect=lambda f: json.dump(f, open(self.fail_path, 'w', encoding='utf-8')))
        patcher_load.start()
        patcher_save.start()
        self.addCleanup(patcher_load.stop)
        self.addCleanup(patcher_save.stop)

        # 探测结果控制：url -> 可达
        self.results = {}

        def fake_probe(url, accept_403=False, user_agent=None):
            return self.results.get(url, True)

        patcher_probe = mock.patch('core.aggregator.probe_stream', side_effect=fake_probe)
        self.probe_mock = patcher_probe.start()
        self.addCleanup(patcher_probe.stop)

    def _build(self):
        """公开频道列表（新签名：filter_unreachable 只接收公开频道）"""
        return [
            {'name': 'CCTV-1 综合', 'url': 'http://good/cctv1.m3u8', 'group_title': '央视'},
            {'name': '北京卫视', 'url': 'http://bad/bjws.m3u8', 'group_title': '卫视'},
        ]

    def test_first_fail_kept(self):
        """第一轮失败：保留，失败计数=1"""
        channels = self._build()
        self.results = {'http://bad/bjws.m3u8': False}
        self.probe_mock.reset_mock()
        kept = AggregatorUtils.filter_unreachable(channels)
        self.assertEqual(len(kept), 2, "第一轮失败应保留")
        rec = json.load(open(self.fail_path, encoding='utf-8'))
        self.assertEqual(rec.get('http://bad/bjws.m3u8'), 1)
        probed_urls = [c.args[0] for c in self.probe_mock.call_args_list]
        self.assertIn('http://bad/bjws.m3u8', probed_urls)

    def test_second_fail_dropped(self):
        """连续两轮失败：丢弃（预置第一轮失败计数=1）"""
        json.dump({'http://bad/bjws.m3u8': 1}, open(self.fail_path, 'w', encoding='utf-8'))
        channels = self._build()
        self.results = {'http://bad/bjws.m3u8': False}
        kept = AggregatorUtils.filter_unreachable(channels)
        self.assertEqual([c['name'] for c in kept], ['CCTV-1 综合'])
        rec = json.load(open(self.fail_path, encoding='utf-8'))
        self.assertEqual(rec.get('http://bad/bjws.m3u8'), 2)

    def test_recover_clears_count(self):
        """恢复可达：保留并清空计数"""
        json.dump({'http://bad/bjws.m3u8': 1}, open(self.fail_path, 'w', encoding='utf-8'))
        channels = self._build()
        self.results = {'http://bad/bjws.m3u8': True}
        kept = AggregatorUtils.filter_unreachable(channels)
        self.assertEqual(len(kept), 2)
        rec = json.load(open(self.fail_path, encoding='utf-8'))
        self.assertNotIn('http://bad/bjws.m3u8', rec)

    def test_stale_record_pruned(self):
        """源里不再出现的 URL 失败记录被裁剪"""
        json.dump({'http://old/gone.m3u8': 2}, open(self.fail_path, 'w', encoding='utf-8'))
        channels = self._build()
        self.results = {'http://bad/bjws.m3u8': True}
        AggregatorUtils.filter_unreachable(channels)
        rec = json.load(open(self.fail_path, encoding='utf-8'))
        self.assertNotIn('http://old/gone.m3u8', rec)


class AggregateLockTest(unittest.TestCase):
    """聚合互斥锁与首请求降级测试"""

    def tearDown(self):
        # 确保锁不残留，避免影响其他测试
        try:
            AggregatorUtils._aggregate_lock.release()
        except RuntimeError:
            pass

    def test_get_aggregated_locked_returns_none(self):
        """锁被占用时 get_aggregated_m3u 立即返回 None，不重复执行"""
        AggregatorUtils._aggregate_lock.acquire()
        try:
            with mock.patch.object(AggregatorUtils, '_get_aggregated_m3u_locked') as m:
                self.assertIsNone(AggregatorUtils.get_aggregated_m3u())
                m.assert_not_called()
        finally:
            AggregatorUtils._aggregate_lock.release()

    def test_get_aggregated_releases_lock_on_exception(self):
        """内部实现抛异常时锁必须释放，且返回 None"""
        with mock.patch.object(AggregatorUtils, '_get_aggregated_m3u_locked',
                               side_effect=RuntimeError('boom')):
            self.assertIsNone(AggregatorUtils.get_aggregated_m3u())
        # 锁应已释放，可再次获取
        self.assertTrue(AggregatorUtils._aggregate_lock.acquire(blocking=False))
        AggregatorUtils._aggregate_lock.release()

    def test_load_degrades_when_aggregating(self):
        """无缓存且后台聚合进行中（锁被占）：load 降级返回官方源列表，不阻塞"""
        AggregatorUtils._aggregate_lock.acquire()
        try:
            with mock.patch('core.aggregator.os.path.exists', return_value=False), \
                 mock.patch.object(AggregatorUtils, 'get_hntv_only_m3u',
                                   return_value="#EXTM3U\n# 降级测试\n"):
                content = AggregatorUtils.load_aggregated_m3u()
            self.assertEqual(content, "#EXTM3U\n# 降级测试\n")
        finally:
            AggregatorUtils._aggregate_lock.release()

    def test_load_full_aggregate_when_idle(self):
        """无缓存且无聚合在跑：现场完整聚合"""
        with mock.patch('core.aggregator.os.path.exists', return_value=False), \
             mock.patch.object(AggregatorUtils, 'get_aggregated_m3u',
                               return_value="#EXTM3U\n# 完整聚合\n"):
            content = AggregatorUtils.load_aggregated_m3u()
        self.assertEqual(content, "#EXTM3U\n# 完整聚合\n")


class SourceUtilsTest(unittest.TestCase):
    """公开源解析/评分/过滤逻辑测试"""

    def test_parse_m3u_with_line_suffix(self):
        """多线路后缀（$ 线路标记 / ; 备选地址）清洗"""
        m3u = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-name="北京卫视" group-title="卫视",北京卫视\n'
            "http://a.com/1.m3u8$LR•IPV4『线路1』\n"
            '#EXTINF:-1 tvg-name="CCTV-1" group-title="央视",CCTV-1综合\n'
            "http://b.com/2.m3u8;http://c.com/backup.m3u8\n"
        )
        channels = SourceUtils.parse_m3u_channels(m3u)
        self.assertEqual(channels[0]['url'], "http://a.com/1.m3u8")
        self.assertEqual(channels[1]['url'], "http://b.com/2.m3u8")

    def test_score_url_signed_domain_penalty(self):
        """带时效签名的域名源降 1 分"""
        self.assertEqual(SourceUtils.score_url("http://ali-m-l.cztv.com/1.m3u8"), 3)
        self.assertEqual(SourceUtils.score_url("http://zwebl02.cztv.com/1.m3u8?auth_key=abc"), 2)
        self.assertEqual(SourceUtils.score_url("http://8.138.7.223/tv/hxws.m3u8"), 2)
        self.assertEqual(SourceUtils.score_url("http://39.134.115.163:8080/PLTV/1.m3u8"), 1)

    def test_filter_and_translate(self):
        """只保留央视开路 + 卫视，并中文化"""
        channels = [
            {'name': 'CCTV-1 (1080p)', 'tvg_name': 'CCTV1', 'group_title': '央视', 'url': 'u1'},
            {'name': 'BRTV 北京卫视', 'tvg_name': 'BRTV', 'group_title': '卫视', 'url': 'u2'},
            {'name': 'CNBC', 'tvg_name': 'CNBC', 'group_title': '财经', 'url': 'u3'},
        ]
        result = SourceUtils.filter_and_translate(channels)
        self.assertEqual([c['name'] for c in result], ['CCTV-1 综合', '北京卫视'])
        self.assertEqual(result[1]['group_title'], '卫视')


if __name__ == '__main__':
    unittest.main()
