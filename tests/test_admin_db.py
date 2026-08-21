"""管理数据层测试：建表/源配置/频道覆盖/监控历史/日志/清理"""
import datetime
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

    def test_stream_history_batch_prune(self):
        """批量写入一轮探测：整轮落库，且只保留最近 N 条"""
        with mock.patch('admin.db.STREAM_HISTORY_KEEP', 3):
            db.save_stream_history_batch([
                ('2026-01-01 10:00:00', '央视', 'CCTV-1', 'http://u1', True, 'r1'),
                ('2026-01-01 10:00:00', '卫视', '北京卫视', 'http://u2', False, 'r1'),
            ])
            db.save_stream_history_batch([
                ('2026-01-01 10:30:00', '央视', 'CCTV-1', 'http://u1', True, 'r2'),
                ('2026-01-01 10:30:00', '卫视', '北京卫视', 'http://u2', True, 'r2'),
            ])
        rows = db.get_stream_history(100)
        self.assertEqual(len(rows), 3)  # 保留最近 3 条：r2 两条 + r1 最新一条
        r1_count = sum(1 for r in rows if r['round_id'] == 'r1')
        self.assertEqual(r1_count, 1)

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

    def test_log_prune_removes_expired_only(self):
        """超期日志按 GMT+8 阈值清理，未超期保留，节流标记置为当天"""
        old_ts = (datetime.datetime.now(tz=db.GMT8)
                  - datetime.timedelta(days=db.LOG_KEEP_DAYS + 1)).strftime('%Y-%m-%d %H:%M:%S')
        fresh_ts = db._now()
        db._execute("INSERT INTO logs (ts, level, module, message) VALUES (?,?,?,?)",
                    (old_ts, 'WARNING', 't', '过期日志'))
        db._execute("INSERT INTO logs (ts, level, module, message) VALUES (?,?,?,?)",
                    (fresh_ts, 'WARNING', 't', '新鲜日志'))
        orig_date = db._last_log_prune_date
        db._last_log_prune_date = None
        self.addCleanup(setattr, db, '_last_log_prune_date', orig_date)
        db._prune_expired_logs()
        msgs = [r['message'] for r in db.get_logs(100)]
        self.assertNotIn('过期日志', msgs)
        self.assertIn('新鲜日志', msgs)
        self.assertEqual(db._last_log_prune_date,
                         datetime.datetime.now(tz=db.GMT8).strftime('%Y-%m-%d'))

    def test_log_prune_throttled_once_per_day(self):
        """日志清理每天（GMT+8）至多执行一次，连续写日志不重复全表 DELETE"""
        orig_date = db._last_log_prune_date
        db._last_log_prune_date = None
        self.addCleanup(setattr, db, '_last_log_prune_date', orig_date)
        with mock.patch('admin.db._execute_locked', wraps=db._execute_locked) as m:
            db.save_log(db._now(), 'WARNING', 't', '第一条')
            db.save_log(db._now(), 'WARNING', 't', '第二条')
            db._prune_expired_logs()
        delete_calls = [c for c in m.call_args_list if 'DELETE FROM logs' in c.args[0]]
        self.assertEqual(len(delete_calls), 1)

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


class SettingsEffectiveTest(unittest.TestCase):
    """运行时设置：DB 优先、config 兜底的动态读取"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_patcher = mock.patch('admin.db.ADMIN_DB_PATH',
                                     os.path.join(self.tmp_dir, 't.db'))
        self.db_patcher.start()
        db.init_db()

    def tearDown(self):
        self.db_patcher.stop()

    def test_fallback_when_unset(self):
        """未设置：回退 default"""
        self.assertEqual(db.get_effective_int('min_channel_count', 30), 30)
        self.assertEqual(db.get_effective_bool('x', True), True)
        self.assertEqual(db.get_effective_json('r', {'a': 1}), {'a': 1})

    def test_db_wins(self):
        """DB 有值：优先（bool 兼容 'true'/'false'，json 解析 dict）"""
        db.set_setting('min_channel_count', '5')
        db.set_setting('bilibili_only_mode', 'false')
        db.set_setting('group_health_ratios', '{"卫视": 0.1}')
        self.assertEqual(db.get_effective_int('min_channel_count', 30), 5)
        self.assertFalse(db.get_effective_bool('bilibili_only_mode', True))
        self.assertEqual(db.get_effective_json('group_health_ratios', {}),
                         {'卫视': 0.1})

    def test_bad_value_falls_back(self):
        """非法值（非整数/非 JSON）：回退 default"""
        db.set_setting('min_channel_count', 'abc')
        db.set_setting('group_health_ratios', 'not-json')
        self.assertEqual(db.get_effective_int('min_channel_count', 30), 30)
        self.assertEqual(db.get_effective_json('group_health_ratios', {'a': 1}),
                         {'a': 1})

    def test_effective_str_and_alert_enabled(self):
        """字符串设置与告警开关：DB 优先、默认回退"""
        self.assertTrue(db.is_alert_enabled(default=True))
        self.assertEqual(db.get_effective_str('public_base_url', 'http://d'),
                         'http://d')
        db.set_setting('public_base_url', ' http://192.168.1.9:5002 ')
        db.set_setting('alert_enabled', 'false')
        self.assertEqual(db.get_effective_str('public_base_url', 'http://d'),
                         'http://192.168.1.9:5002')
        self.assertFalse(db.is_alert_enabled(default=True))

    def test_record_event_info_visible(self):
        """record_event：INFO 级显式入库（关键事件/审计），管理页日志可查"""
        db.record_event('INFO', 'main', '服务启动完成测试事件')
        rows = db.get_logs(level='INFO', keyword='服务启动完成')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['module'], 'main')

    def test_alert_disabled_skips_send(self):
        """alert_enabled=false：send_alert 入口直接跳过，不加载发送模块"""
        from monitoring.alerts import AlertUtils
        db.set_setting('alert_enabled', 'false')
        with mock.patch('monitoring.alerts.importlib.util.spec_from_file_location') as m:
            AlertUtils.send_alert('测试', [{'name': 'x', 'status': True, 'detail': 'd'}])
        m.assert_not_called()

    def test_check_m3u_uses_db_threshold(self):
        """check_m3u 阈值动态读取：DB 设置优先"""
        from monitoring.checks import CheckUtils
        db.set_setting('min_channel_count', '3')
        m3u = ('#EXTM3U\n#EXTINF:-1 tvg-id="1",A\nhttp://a\n'
               '#EXTINF:-1 tvg-id="2",B\nhttp://b\n'
               '#EXTINF:-1 tvg-id="3",C\nhttp://c\n')
        with mock.patch('monitoring.checks.requests.get') as m:
            resp = mock.Mock()
            resp.status_code = 200
            resp.text = m3u
            m.return_value = resp
            ok, count = CheckUtils.check_m3u()
        self.assertTrue(ok)
        self.assertEqual(count, 3)

    def test_check_m3u_falls_back_to_config_threshold(self):
        """未设置：回退 config 阈值（正式模式 30），3 个频道不达标"""
        from monitoring.checks import CheckUtils
        m3u = ('#EXTM3U\n#EXTINF:-1 tvg-id="1",A\nhttp://a\n'
               '#EXTINF:-1 tvg-id="2",B\nhttp://b\n'
               '#EXTINF:-1 tvg-id="3",C\nhttp://c\n')
        with mock.patch('monitoring.checks.requests.get') as m, \
             mock.patch('core.aggregator.AggregatorUtils.is_bilibili_only_mode',
                        return_value=False), \
             mock.patch('monitoring.checks.MIN_CHANNEL_COUNT', 30):
            resp = mock.Mock()
            resp.status_code = 200
            resp.text = m3u
            m.return_value = resp
            ok, count = CheckUtils.check_m3u()
        self.assertFalse(ok)
        self.assertEqual(count, 3)


if __name__ == '__main__':
    unittest.main()
