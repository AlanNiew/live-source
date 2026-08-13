"""监控状态机测试：分组阈值判定、卫视仅日志不告警、恢复通知、列表失败告警"""
import os
import tempfile
import unittest
from unittest import mock

from admin import db
from config import XML_DATA_DIR
from monitoring.checks import CheckUtils


class StreamCheckStateMachineTest(unittest.TestCase):
    """流探测状态机测试（mock fetch/probe/send_alert，验证翻转与分组判定）"""

    def setUp(self):
        # 隔离监控落库：用临时 DB，验证落库代码真实执行且不污染生产库
        self.tmp_dir = tempfile.mkdtemp()
        self.patcher_db = mock.patch('admin.db.ADMIN_DB_PATH',
                                     os.path.join(self.tmp_dir, 'test.db'))
        self.patcher_db.start()
        self.addCleanup(self.patcher_db.stop)
        db.init_db()

        self.mails = []
        self.items = []

        patcher_fetch = mock.patch.object(
            CheckUtils, 'fetch_m3u_groups',
            side_effect=lambda: list(self.items))
        patcher_probe = mock.patch('monitoring.checks.probe_stream',
                                   side_effect=lambda u, accept_403=False: self.probe_results.get(u, True))
        patcher_alert = mock.patch('monitoring.checks.AlertUtils.send_alert',
                                   side_effect=lambda **kw: self.mails.append(kw))
        patcher_fetch.start()
        patcher_probe.start()
        patcher_alert.start()
        self.addCleanup(patcher_fetch.stop)
        self.addCleanup(patcher_probe.stop)
        self.addCleanup(patcher_alert.stop)

    def test_weishi_fail_no_alert(self):
        """只有卫视不达标：不发邮件，状态保持 OK（卫视仅日志展示）"""
        self.items = [
            ("http://ok/1.m3u8", "河南卫视", "河南卫视"),
            ("http://ok/2.m3u8", "央视", "CCTV-1 综合"),
            ("http://bad/3.m3u8", "卫视", "北京卫视"),
        ]
        self.probe_results = {"http://bad/3.m3u8": False}
        CheckUtils._stream_last_status = "OK"
        CheckUtils.run_stream_check_once()
        self.assertEqual(self.mails, [])
        self.assertEqual(CheckUtils._stream_last_status, "OK")

    def test_cctv_fail_sends_alert(self):
        """央视不达标：发故障邮件，明细含三组与不可达频道列表"""
        self.items = [
            ("http://ok/1.m3u8", "河南卫视", "河南卫视"),
            ("http://bad/2.m3u8", "央视", "CCTV-1 综合"),
            ("http://bad/3.m3u8", "卫视", "北京卫视"),
        ]
        self.probe_results = {"http://bad/2.m3u8": False, "http://bad/3.m3u8": False}
        CheckUtils._stream_last_status = "OK"
        CheckUtils.run_stream_check_once()
        self.assertEqual(len(self.mails), 1)
        self.assertEqual(self.mails[0]['level'], 'error')
        names = [c['name'] for c in self.mails[0]['checks']]
        self.assertEqual([n.split('（')[0] for n in names], ['河南卫视', '央视', '卫视'])
        # 不可达频道明细应出现在邮件额外信息里
        extra = self.mails[0]['extra_info']
        self.assertIn('不可达频道', extra)
        self.assertIn('CCTV-1 综合', extra['不可达频道'])
        self.assertIn('北京卫视', extra['不可达频道'])

    def test_recover_sends_info(self):
        """FAIL -> OK：发恢复通知"""
        self.items = [("http://ok/1.m3u8", "河南卫视", "河南卫视")]
        self.probe_results = {}
        CheckUtils._stream_last_status = "FAIL"
        CheckUtils.run_stream_check_once()
        self.assertEqual(len(self.mails), 1)
        self.assertEqual(self.mails[0]['level'], 'info')

    def test_list_fetch_fail_sends_alert(self):
        """聚合列表拉取失败：系统性故障，仍发告警"""
        self.items = []
        CheckUtils._stream_last_status = "OK"
        CheckUtils.run_stream_check_once()
        self.assertEqual(len(self.mails), 1)
        self.assertEqual(self.mails[0]['level'], 'error')


if __name__ == '__main__':
    unittest.main()
