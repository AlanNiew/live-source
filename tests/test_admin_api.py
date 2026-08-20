"""管理 API 端点测试（Flask test client + session 鉴权 + 分页契约）"""
import os
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote

import core.aggregator
from admin import db
from app import create_app

# 固定聚合缓存内容（3 个频道，模拟 xml_data/aggregated.m3u）
SAMPLE_M3U = """#EXTM3U

#EXTINF:-1 tvg-id="1" tvg-name="河南卫视" group-title="河南卫视",河南卫视
http://hntv/henan.m3u8

#EXTINF:-1 tvg-id="CCTV-1 综合" tvg-name="CCTV-1 综合" group-title="央视",CCTV-1 综合
http://cctv/1.m3u8

#EXTINF:-1 tvg-id="北京卫视" tvg-name="北京卫视" group-title="卫视",北京卫视
http://ws/bj.m3u8
"""


class AdminApiTest(unittest.TestCase):
    """管理 API 全端点：鉴权守卫 / 源 CRUD / 频道覆盖 / 监控 / 日志 / 设置 / 分页契约"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_patcher = mock.patch('admin.db.ADMIN_DB_PATH',
                                     os.path.join(self.tmp_dir, 'test.db'))
        self.db_patcher.start()
        db.init_db()
        self.m3u_path = os.path.join(self.tmp_dir, 'agg.m3u')
        with open(self.m3u_path, 'w', encoding='utf-8') as f:
            f.write(SAMPLE_M3U)
        self.m3u_patcher = mock.patch('admin.api.AGGREGATED_M3U_PATH', self.m3u_path)
        self.m3u_patcher.start()
        self.pass_patcher = mock.patch('admin.api.ADMIN_PASSWORD', 'testpass')
        self.pass_patcher.start()
        # 写接口会触发异步刷新（后台线程 + 网络）：替换为 no-op 计数
        self.refresh_patcher = mock.patch(
            'core.aggregator.AggregatorUtils.request_async_refresh')
        self.refresh_mock = self.refresh_patcher.start()
        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        self.refresh_patcher.stop()
        self.pass_patcher.stop()
        self.m3u_patcher.stop()
        self.db_patcher.stop()

    def _login(self, password='testpass'):
        return self.client.post('/api/admin/login', json={'password': password})

    # ------------------------------------------------------------ 鉴权

    def test_login_logout_flow(self):
        """错误密码 401；正确登录后可用；登出后 401"""
        self.assertEqual(self._login('wrong').status_code, 401)
        self.assertEqual(self._login().status_code, 200)
        self.assertEqual(self.client.get('/api/admin/sources').status_code, 200)
        self.client.post('/api/admin/logout')
        self.assertEqual(self.client.get('/api/admin/sources').status_code, 401)

    def test_login_sets_permanent_session(self):
        """登录成功：会话标记 permanent（按 ADMIN_SESSION_HOURS 过期）"""
        self._login()
        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get('_permanent'))

    def test_login_lockout_after_failures(self):
        """连续 5 次失败锁定：正确密码也 429；到期后恢复"""
        import time as _time
        with mock.patch.dict('admin.api._login_failures', {}, clear=True):
            for _ in range(5):
                self.assertEqual(self._login('wrong').status_code, 401)
            # 锁定中：即使密码正确也拒绝
            resp = self.client.post('/api/admin/login', json={'password': 'testpass'})
            self.assertEqual(resp.status_code, 429)
            # 锁定期过后恢复可登录
            future = _time.time() + 301
            with mock.patch('admin.api.time.time', return_value=future):
                resp = self.client.post('/api/admin/login', json={'password': 'testpass'})
            self.assertEqual(resp.status_code, 200)

    def test_disabled_when_password_empty(self):
        """ADMIN_PASSWORD 为空：登录与全部端点 403（安全默认）"""
        with mock.patch('admin.api.ADMIN_PASSWORD', ''):
            self.assertEqual(self._login().status_code, 403)
            self.assertEqual(self.client.get('/api/admin/sources').status_code, 403)

    def test_all_endpoints_guard(self):
        """未登录：除 login 外全部端点 401"""
        for method, path in [
            ('GET', '/api/admin/sources'), ('GET', '/api/admin/channels'),
            ('GET', '/api/admin/monitor/summary'), ('GET', '/api/admin/monitor/history'),
            ('GET', '/api/admin/monitor/streams'), ('GET', '/api/admin/logs'),
            ('GET', '/api/admin/settings'), ('POST', '/api/admin/sources/refresh'),
            ('POST', '/api/admin/logout'),
        ]:
            resp = getattr(self.client, method.lower())(path)
            self.assertEqual(resp.status_code, 401, f"{method} {path} 未登录应 401")

    # ------------------------------------------------------------ 源管理

    def test_sources_crud(self):
        self._login()
        resp = self.client.post('/api/admin/sources',
                                json={'type': 'public', 'name': '测试源',
                                      'url': 'http://x/a.m3u'})
        self.assertEqual(resp.status_code, 201)
        sid = resp.get_json()['id']

        data = self.client.get('/api/admin/sources?type=public').get_json()
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['url'], 'http://x/a.m3u')

        resp = self.client.put(f'/api/admin/sources/{sid}', json={'enabled': False})
        self.assertEqual(resp.status_code, 200)
        items = self.client.get('/api/admin/sources?type=public&enabled=0').get_json()['items']
        self.assertEqual(len(items), 1)

        self.assertEqual(self.client.delete(f'/api/admin/sources/{sid}').status_code, 200)
        self.assertEqual(self.client.delete(f'/api/admin/sources/{sid}').status_code, 404)

        # 校验错误
        self.assertEqual(self.client.post('/api/admin/sources',
                                         json={'type': 'x', 'name': 'n'}).status_code, 400)
        self.assertEqual(self.client.post('/api/admin/sources',
                                         json={'type': 'public', 'name': 'n'}).status_code, 400)
        self.assertEqual(self.client.post('/api/admin/sources',
                                         json={'type': 'bilibili', 'name': 'n',
                                               'url': 'abc'}).status_code, 400)
        self.assertEqual(self.client.post('/api/admin/sources',
                                         json={'type': 'public', 'name': 'n',
                                               'url': 'http://x/n.m3u',
                                               'enabled': 'no'}).status_code, 400)
        # bilibili 源 PUT url 校验：非数字 400，合法房间号 200
        resp = self.client.post('/api/admin/sources',
                                json={'type': 'bilibili', 'name': 'B站台', 'url': '8178490'})
        bid = resp.get_json()['id']
        self.assertEqual(self.client.put(f'/api/admin/sources/{bid}',
                                         json={'url': 'abc'}).status_code, 400)
        self.assertEqual(self.client.put(f'/api/admin/sources/{bid}',
                                         json={'url': '12345'}).status_code, 200)

    def test_sources_pagination_and_sort(self):
        self._login()
        for name, order in [('C', 3), ('A', 1), ('B', 2)]:
            self.client.post('/api/admin/sources',
                             json={'type': 'public', 'name': name,
                                   'url': f'http://x/{name}.m3u', 'sort_order': order})
        data = self.client.get('/api/admin/sources?type=public&page=1&page_size=2').get_json()
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['page_size'], 2)
        self.assertTrue(data['has_more'])
        self.assertEqual([i['name'] for i in data['items']], ['A', 'B'])  # sort_order 升序

        page2 = self.client.get('/api/admin/sources?type=public&page=2&page_size=2').get_json()
        self.assertEqual([i['name'] for i in page2['items']], ['C'])
        self.assertFalse(page2['has_more'])

        desc = self.client.get('/api/admin/sources?type=public&order=desc&page_size=10').get_json()
        self.assertEqual([i['name'] for i in desc['items']], ['C', 'B', 'A'])

        q = self.client.get('/api/admin/sources?type=public&q=A').get_json()
        self.assertEqual(q['total'], 1)
        self.assertEqual(q['items'][0]['name'], 'A')

    def test_refresh_endpoint(self):
        self._login()
        resp = self.client.post('/api/admin/sources/refresh')
        self.assertEqual(resp.status_code, 200)
        self.refresh_mock.assert_called_once()

    def test_sources_shows_config_defaults_when_db_empty(self):
        """DB 空：列表展示 config 兜底行（当前生效来源，config_default=true、id=null）"""
        import config
        self._login()
        n_public = len(config.PUBLIC_M3U_SOURCES)
        n_bili = len(config.BILIBILI_ROOMS)
        data = self.client.get('/api/admin/sources?page_size=100').get_json()
        self.assertEqual(data['total'], n_public + n_bili)
        defaults = [i for i in data['items'] if i['config_default']]
        self.assertEqual(len(defaults), n_public + n_bili)
        self.assertTrue(all(i['id'] is None for i in defaults))
        publics = [i for i in defaults if i['type'] == 'public']
        self.assertEqual(len(publics), n_public)
        self.assertTrue(all(u['url'].startswith('http') for u in publics))
        bili = [i for i in defaults if i['type'] == 'bilibili']
        self.assertEqual(len(bili), n_bili)
        self.assertTrue(all(u['url'] for u in bili))  # uid:xxx 或房间号

    def test_sources_hides_config_defaults_when_db_has_enabled(self):
        """DB 有启用源：该类型不再显示兜底行"""
        import config
        self._login()
        self.client.post('/api/admin/sources',
                         json={'type': 'public', 'name': '自建源', 'url': 'http://x/a.m3u'})
        data = self.client.get('/api/admin/sources?page_size=100').get_json()
        publics = [i for i in data['items'] if i['type'] == 'public']
        self.assertEqual(len(publics), 1)  # 只有 DB 行
        self.assertFalse(publics[0]['config_default'])
        # bilibili 仍无启用源 → 兜底行保留
        bili = [i for i in data['items'] if i['type'] == 'bilibili']
        self.assertEqual(len(bili), len(config.BILIBILI_ROOMS))

    def test_import_defaults_idempotent(self):
        """导入默认源：幂等；uid 解析失败跳过"""
        import config
        self._login()
        n_public = len(config.PUBLIC_M3U_SOURCES)
        n_bili = len(config.BILIBILI_ROOMS)
        # 全部 bilibili uid 解析成功
        room_ids = iter(range(1000000, 1000000 + n_bili))
        with mock.patch('core.bilibili.BilibiliUtils.get_room_id',
                        side_effect=lambda uid: next(room_ids)):
            resp = self.client.post('/api/admin/sources/import-defaults')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['imported'], n_public + n_bili)
        self.assertEqual(data['skipped'], [])
        # 已入库：无兜底行
        items = self.client.get('/api/admin/sources?page_size=100').get_json()['items']
        self.assertFalse(any(i['config_default'] for i in items))
        self.assertEqual(len(items), n_public + n_bili)
        # 重复导入：幂等（0 新增）；用新迭代器（旧迭代器已耗尽）
        room_ids2 = iter(range(1000000, 1000000 + n_bili))
        with mock.patch('core.bilibili.BilibiliUtils.get_room_id',
                        side_effect=lambda uid: next(room_ids2)):
            resp2 = self.client.post('/api/admin/sources/import-defaults')
        self.assertEqual(resp2.get_json()['imported'], 0)
        self.assertEqual(self.client.get('/api/admin/sources?page_size=100').get_json()['total'],
                         n_public + n_bili)

    # ------------------------------------------------------------ 频道覆盖

    def test_channels_list_shape(self):
        """频道列表：聚合输出顺序 + key=normalize_name + 无覆盖时 override=null"""
        self._login()
        data = self.client.get('/api/admin/channels').get_json()
        self.assertEqual(data['total'], 3)
        names = [i['name'] for i in data['items']]
        self.assertEqual(names, ['河南卫视', 'CCTV-1 综合', '北京卫视'])
        cctv = data['items'][1]
        self.assertEqual(cctv['key'], 'CCTV-1 综合')
        self.assertTrue(cctv['enabled'])
        self.assertIsNone(cctv['override'])

    def test_channels_override_cycle(self):
        """禁用→列表仍可见（enabled=false/url=null）→改名→删除覆盖恢复默认"""
        self._login()
        key = 'CCTV-1 综合'
        path = f'/api/admin/channels/{quote(key)}'

        resp = self.client.put(path, json={'enabled': False})
        self.assertEqual(resp.status_code, 200)
        data = self.client.get('/api/admin/channels').get_json()
        item = next(i for i in data['items'] if i['key'] == key)
        self.assertFalse(item['enabled'])
        self.assertIsNone(item['url'])
        self.assertEqual(item['override']['enabled'], 0)

        self.client.put(path, json={'display_name': '中央一套'})
        data = self.client.get('/api/admin/channels').get_json()
        item = next(i for i in data['items'] if i['key'] == key)
        self.assertEqual(item['override']['display_name'], '中央一套')

        self.assertEqual(self.client.delete(path).status_code, 200)
        data = self.client.get('/api/admin/channels').get_json()
        item = next(i for i in data['items'] if i['key'] == key)
        self.assertIsNone(item['override'])
        self.assertTrue(item['enabled'])
        self.assertIsNotNone(item['url'])

        self.assertEqual(self.client.delete(path).status_code, 404)
        self.assertEqual(self.client.put(path, json={}).status_code, 400)
        self.assertEqual(self.client.put(path, json={'enabled': 1}).status_code, 400)

    def test_channels_sort_and_search(self):
        self._login()
        desc = self.client.get('/api/admin/channels?sort=name&order=desc').get_json()
        names = [i['name'] for i in desc['items']]
        self.assertEqual(names, sorted(names, reverse=True))
        q = self.client.get('/api/admin/channels?q=' + quote('北京')).get_json()
        self.assertEqual(q['total'], 1)
        self.assertEqual(q['items'][0]['name'], '北京卫视')

    # ------------------------------------------------------------ 监控

    def test_monitor_history_pagination(self):
        self._login()
        for i in range(3):
            db.save_monitor_history(True, True, True, i, 10, True)
        data = self.client.get('/api/admin/monitor/history?page=1&page_size=2').get_json()
        self.assertEqual(data['total'], 3)
        self.assertEqual(len(data['items']), 2)
        self.assertTrue(data['has_more'])
        self.assertEqual(data['items'][0]['channel_count'], 2)  # 新→旧
        page2 = self.client.get('/api/admin/monitor/history?page=2&page_size=2').get_json()
        self.assertEqual(page2['items'][0]['channel_count'], 0)

    def test_monitor_streams_filters(self):
        self._login()
        db.save_stream_history('2026-01-01 10:00:00', '央视', 'CCTV-1', 'http://u1', True, 'r1')
        db.save_stream_history('2026-01-01 10:00:00', '央视', 'CCTV-2', 'http://u2', False, 'r1')
        db.save_stream_history('2026-01-01 10:30:00', '卫视', '北京卫视', 'http://u3', True, 'r2')
        data = self.client.get('/api/admin/monitor/streams').get_json()
        self.assertEqual(data['total'], 3)
        bad = self.client.get('/api/admin/monitor/streams?unreachable=1').get_json()
        self.assertEqual(bad['total'], 1)
        self.assertEqual(bad['items'][0]['channel_name'], 'CCTV-2')
        kw = self.client.get('/api/admin/monitor/streams?q=' + quote('北京')).get_json()
        self.assertEqual(kw['total'], 1)
        by_ts = self.client.get('/api/admin/monitor/streams?sort=ts&order=asc').get_json()
        self.assertEqual(by_ts['items'][0]['channel_name'], 'CCTV-1')

    def test_monitor_summary(self):
        self._login()
        db.save_monitor_history(True, True, True, 50, 100, True)
        with mock.patch('monitoring.checks.CheckUtils._last_status', 'FAIL'), \
             mock.patch('monitoring.checks.CheckUtils._stream_last_status', 'OK'):
            data = self.client.get('/api/admin/monitor/summary').get_json()
        self.assertEqual(data['health']['status'], 'FAIL')
        self.assertEqual(data['stream']['status'], 'OK')
        self.assertIsNotNone(data['last_check'])
        self.assertEqual(len(data['recent']), 1)

    # ------------------------------------------------------------ 日志

    def test_logs_filters(self):
        self._login()
        ts = db._now()
        db.save_log(ts, 'ERROR', 'checks', '测试错误信息')
        db.save_log(ts, 'WARNING', 'aggregator', '普通警告')
        data = self.client.get('/api/admin/logs').get_json()
        self.assertGreaterEqual(data['total'], 2)  # 含登录审计等 INFO 事件
        err = self.client.get('/api/admin/logs?level=error').get_json()
        self.assertEqual(err['total'], 1)
        self.assertEqual(err['items'][0]['level'], 'ERROR')
        kw = self.client.get('/api/admin/logs?q=' + quote('测试')).get_json()
        self.assertEqual(kw['total'], 1)
        self.assertEqual(self.client.get('/api/admin/logs?level=DEBUG').status_code, 400)

    def test_audit_logs_recorded(self):
        """管理操作审计：登录成功/失败、设置变更、源变更写入 logs 表（module=admin）"""
        self._login()
        # 设置变更审计（INFO）
        self.client.put('/api/admin/settings', json={'min_channel_count': 25})
        rows = db.get_logs(level='INFO', keyword='设置变更')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['module'], 'admin')
        # 登录失败审计（WARNING）
        self._login('wrong')
        bad = db.get_logs(level='WARNING', keyword='登录失败')
        self.assertEqual(len(bad), 1)
        # 源新增审计（INFO，含名称）
        resp = self.client.post('/api/admin/sources',
                                json={'type': 'public', 'name': '审计源',
                                      'url': 'http://x/a.m3u'})
        self.assertEqual(resp.status_code, 201)
        added = db.get_logs(level='INFO', keyword='新增源')
        self.assertEqual(len(added), 1)
        self.assertIn('审计源', added[0]['message'])

    # ------------------------------------------------------------ 设置

    def test_settings(self):
        self._login()
        data = self.client.get('/api/admin/settings').get_json()
        self.assertEqual(data['effective']['bilibili_only_mode'],
                         core.aggregator.BILIBILI_ONLY_MODE)
        resp = self.client.put('/api/admin/settings', json={'bilibili_only_mode': False})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()['effective']['bilibili_only_mode'])
        data = self.client.get('/api/admin/settings').get_json()
        self.assertEqual(data['settings']['bilibili_only_mode'], 'false')
        self.assertEqual(self.client.put('/api/admin/settings', json={}).status_code, 400)
        self.assertEqual(self.client.put('/api/admin/settings',
                                         json={'bilibili_only_mode': 'no'}).status_code, 400)

    def test_settings_extra_keys(self):
        """扩展设置键：int 阈值 / 分组健康率 JSON 的读写与校验"""
        self._login()
        # 有效写入
        resp = self.client.put('/api/admin/settings', json={
            'min_channel_count': 25,
            'stream_fail_limit': 3,
            'monitor_history_keep': 600,
            'stream_history_keep': 30000,
            'log_keep_days': 14,
            'group_health_ratios': {'河南卫视': 0.95, '央视': 0.85},
        })
        self.assertEqual(resp.status_code, 200)
        eff = resp.get_json()['effective']
        self.assertEqual(eff['min_channel_count'], 25)
        self.assertEqual(eff['stream_fail_limit'], 3)
        self.assertEqual(eff['log_keep_days'], 14)
        self.assertEqual(eff['group_health_ratios'], {'河南卫视': 0.95, '央视': 0.85})
        # 持久化后可读回
        data = self.client.get('/api/admin/settings').get_json()
        self.assertEqual(data['settings']['min_channel_count'], '25')
        self.assertIn('"河南卫视"', data['settings']['group_health_ratios'])
        # 校验错误
        self.assertEqual(self.client.put('/api/admin/settings',
                                         json={'min_channel_count': 0}).status_code, 400)
        self.assertEqual(self.client.put('/api/admin/settings',
                                         json={'min_channel_count': 'x'}).status_code, 400)
        self.assertEqual(self.client.put('/api/admin/settings',
                                         json={'group_health_ratios': {'卫视': 1.5}}).status_code, 400)
        self.assertEqual(self.client.put('/api/admin/settings',
                                         json={'group_health_ratios': [0.5]}).status_code, 400)

    def test_settings_public_base_url_and_alert_enabled(self):
        """public_base_url / alert_enabled：读写与校验"""
        self._login()
        resp = self.client.put('/api/admin/settings', json={
            'public_base_url': ' http://192.168.1.9:15002 ',
            'alert_enabled': False,
        })
        self.assertEqual(resp.status_code, 200)
        eff = resp.get_json()['effective']
        self.assertEqual(eff['public_base_url'], 'http://192.168.1.9:15002')  # strip
        self.assertFalse(eff['alert_enabled'])
        data = self.client.get('/api/admin/settings').get_json()
        self.assertEqual(data['settings']['public_base_url'], 'http://192.168.1.9:15002')
        self.assertEqual(data['settings']['alert_enabled'], 'false')
        # 校验：非 URL / 空串
        self.assertEqual(self.client.put('/api/admin/settings',
                                         json={'public_base_url': 'not-a-url'}).status_code, 400)
        self.assertEqual(self.client.put('/api/admin/settings',
                                         json={'public_base_url': '   '}).status_code, 400)

    def test_settings_alert_recipients(self):
        """alert_recipients：逗号分隔多邮箱读写与校验"""
        self._login()
        resp = self.client.put('/api/admin/settings', json={
            'alert_recipients': ' a@qq.com, b@163.com ',
        })
        self.assertEqual(resp.status_code, 200)
        eff = resp.get_json()['effective']
        self.assertEqual(eff['alert_recipients'], 'a@qq.com, b@163.com')
        data = self.client.get('/api/admin/settings').get_json()
        self.assertEqual(data['settings']['alert_recipients'], 'a@qq.com, b@163.com')
        # 非法邮箱 / 空值
        self.assertEqual(self.client.put('/api/admin/settings',
                                         json={'alert_recipients': 'not-an-email'}).status_code, 400)
        self.assertEqual(self.client.put('/api/admin/settings',
                                         json={'alert_recipients': 'a@qq.com, broken'}).status_code, 400)
        # 空串允许（回退发件人自身）
        self.assertEqual(self.client.put('/api/admin/settings',
                                         json={'alert_recipients': '  '}).status_code, 200)
        self.assertEqual(self.client.get('/api/admin/settings').get_json()
                         ['effective']['alert_recipients'], '')


if __name__ == '__main__':
    unittest.main()
