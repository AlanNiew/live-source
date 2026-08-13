"""管理数据层测试：建表/源配置/频道覆盖/监控历史/日志/清理"""
import os
import tempfile
import unittest
from unittest import mock

from admin import db


class AdminDbTest(unittest.TestCase):
    """数据库 CRUD 与清理（用临时目录隔离，不碰真实 admin.db）"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, 'test_admin.db')
        self.patcher = mock.patch('admin.db.ADMIN_DB_PATH', self.db_path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        db.init_db()

    # ------------------------------------------------------------ 源配置

    def test_add_and_get_sources(self):
        """新增公开源后可查询"""
        db.add_source('public', '测试源', 'http://example.com/a.m3u')
        rows = db.get_sources('public')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], '测试源')
        self.assertEqual(rows[0]['url'], 'http://example.com/a.m3u')
        self.assertEqual(rows[0]['enabled'], 1)

    def test_get_enabled_public_urls(self):
        """只返回启用且非空的公开源 url"""
        db.add_source('public', 'A', 'http://a.m3u', enabled=1)
        db.add_source('public', 'B', 'http://b.m3u', enabled=0)
        db.add_source('public', 'C', '', enabled=1)
        urls = db.get_enabled_public_urls()
        self.assertEqual(urls, ['http://a.m3u'])

    def test_update_and_delete_source(self):
        """更新启停/URL；删除"""
        sid = db.add_source('public', 'A', 'http://a.m3u')
        db.update_source(sid, enabled=0)
        rows = db.get_sources('public')
        self.assertEqual(rows[0]['enabled'], 0)
        db.delete_source(sid)
        self.assertEqual(db.get_sources('public'), [])

    def test_bilibili_rooms_return_room_id(self):
        """B 站房间配置：url 字段为数字时解析为 room_id"""
        db.add_source('bilibili', '央视新闻', '8178490')
        rooms = db.get_enabled_bilibili_rooms()
        self.assertEqual(rooms, [{'name': '央视新闻', 'room_id': 8178490}])

    # ------------------------------------------------------------ 频道覆盖

    def test_upsert_channel_override(self):
        """新增覆盖 → 再 upsert 更新"""
        db.upsert_channel_override('cctv1', enabled=0)
        self.assertEqual(db.get_channel_override('cctv1')['enabled'], 0)
        db.upsert_channel_override('cctv1', group_title='测试组')
        ov = db.get_channel_override('cctv1')
        self.assertEqual(ov['group_title'], '测试组')
        self.assertEqual(ov['enabled'], 0)  # 未传 enabled 保持原值

    def test_delete_channel_override(self):
        """删除覆盖恢复默认"""
        db.upsert_channel_override('cctv1', enabled=0)
        db.delete_channel_override('cctv1')
        self.assertIsNone(db.get_channel_override('cctv1'))

    # ------------------------------------------------------------ 监控历史

    def test_monitor_history_roundtrip(self):
        """保存后可查询，overall 值正确"""
        db.save_monitor_history(True, True, True, 50, 100, True)
        rows = db.get_monitor_history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['overall'], 1)
        self.assertEqual(rows[0]['channel_count'], 50)

    def test_monitor_history_prune(self):
        """超量清理：只保留最近 N 轮"""
        with mock.patch('admin.db.MONITOR_HISTORY_KEEP', 5):
            for i in range(10):
                db.save_monitor_history(True, True, True, i, 0, True)
        rows = db.get_monitor_history(100)
        self.assertEqual(len(rows), 5)
        # 保留的是最新的（channel_count 9 是最后写入的）
        self.assertEqual(rows[0]['channel_count'], 9)

    def test_stream_history_roundtrip(self):
        """流探测记录保存与不可达过滤"""
        db.save_stream_history('2026-01-01 10:00:00', '央视', 'CCTV-1', 'http://u1', True, 'r1')
        db.save_stream_history('2026-01-01 10:00:00', '卫视', '北京卫视', 'http://u2', False, 'r1')
        all_rows = db.get_stream_history()
        self.assertEqual(len(all_rows), 2)
        bad = db.get_stream_history(unreachable_only=True)
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]['channel_name'], '北京卫视')

    # ------------------------------------------------------------ 日志与设置

    def test_logs_roundtrip(self):
        """日志写入与过滤查询（用当前时间避免被 7 天清理 SQL 误删）"""
        ts = db._now()
        db.save_log(ts, 'WARNING', 'aggregator', '测试警告')
        db.save_log(ts, 'ERROR', 'checks', '测试错误')
        all_rows = db.get_logs()
        self.assertEqual(len(all_rows), 2)
        errs = db.get_logs(level='ERROR')
        self.assertEqual(len(errs), 1)
        kw = db.get_logs(keyword='警告')
        self.assertEqual(len(kw), 1)

    def test_settings_upsert(self):
        """设置项写入/读取/更新"""
        self.assertIsNone(db.get_setting('mode'))
        db.set_setting('mode', 'test')
        self.assertEqual(db.get_setting('mode'), 'test')
        db.set_setting('mode', 'full')
        self.assertEqual(db.get_setting('mode'), 'full')

    def test_logger_writes_to_db_with_ts(self):
        """logger 的 SqliteHandler：WARNING+ 写入 logs 表且 ts 非空"""
        from core.logger import get_logger
        log = get_logger('test-module')
        log.warning('测试警告 abc123')
        rows = db.get_logs(keyword='abc123')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['level'], 'WARNING')
        self.assertEqual(rows[0]['module'], 'test-module')
        # 修复点：ts 必须非空（record.asctime 不可用的 bug 已修）
        self.assertTrue(rows[0]['ts'], "日志 ts 不应为空")
        self.assertIn('-', rows[0]['ts'])  # 格式 YYYY-MM-DD

    def test_logger_skips_info_in_db(self):
        """logger 的 SqliteHandler：INFO 不进 DB（防膨胀）"""
        from core.logger import get_logger
        log = get_logger('test-module')
        log.info('普通信息不入库')
        rows = db.get_logs(keyword='普通信息不入库')
        self.assertEqual(rows, [])


class MonitorPersistTest(unittest.TestCase):
    """监控落库挂接：run_check_once / run_stream_check_once 真实写库"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.patcher = mock.patch('admin.db.ADMIN_DB_PATH',
                                  os.path.join(self.tmp_dir, 'test.db'))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        db.init_db()

    def test_run_check_once_persists(self):
        """常规检测落库：monitor_history 有记录"""
        from monitoring.checks import CheckUtils
        with mock.patch.object(CheckUtils, 'check_health', return_value=True), \
             mock.patch.object(CheckUtils, 'check_m3u', return_value=(True, 50)), \
             mock.patch.object(CheckUtils, 'check_epg', return_value=(True, 100)), \
             mock.patch('monitoring.checks.AlertUtils.send_alert'):
            CheckUtils.run_check_once()
        rows = db.get_monitor_history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['channel_count'], 50)
        self.assertEqual(rows[0]['overall'], 1)

    def test_run_stream_check_once_persists(self):
        """流探测落库：每频道一条记录，不可达可过滤"""
        from monitoring.checks import CheckUtils
        items = [
            ("http://ok/1.m3u8", "央视", "CCTV-1 综合"),
            ("http://bad/2.m3u8", "卫视", "北京卫视"),
        ]
        with mock.patch.object(CheckUtils, 'fetch_m3u_groups', return_value=items), \
             mock.patch('monitoring.checks.probe_stream',
                        side_effect=lambda u, accept_403=False: u.startswith('http://ok')), \
             mock.patch('monitoring.checks.AlertUtils.send_alert'):
            CheckUtils.run_stream_check_once()
        rows = db.get_stream_history()
        self.assertEqual(len(rows), 2)
        bad = db.get_stream_history(unreachable_only=True)
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]['channel_name'], '北京卫视')


if __name__ == '__main__':
    unittest.main()
