"""调度测试：检测时段窗口计算（GMT+8 8:00-24:00）"""
import datetime
import unittest

from config import GMT8
from monitoring.scheduler import MonitorScheduler


class WindowWaitTest(unittest.TestCase):

    def _t(self, hour, minute=0, second=0):
        return datetime.datetime(2026, 8, 11, hour, minute, second, tzinfo=GMT8)

    def test_in_window(self):
        """8:00-23:59 在检测窗口内，等待 0 秒"""
        self.assertEqual(MonitorScheduler._window_wait_seconds(self._t(8, 0, 0)), 0)
        self.assertEqual(MonitorScheduler._window_wait_seconds(self._t(12, 0, 0)), 0)
        self.assertEqual(MonitorScheduler._window_wait_seconds(self._t(23, 59, 59)), 0)

    def test_before_window(self):
        """0:00-7:59 睡到当天 8:00"""
        self.assertEqual(MonitorScheduler._window_wait_seconds(self._t(7, 59, 59)), 1)
        self.assertEqual(MonitorScheduler._window_wait_seconds(self._t(0, 0, 0)), 8 * 3600)
        self.assertEqual(MonitorScheduler._window_wait_seconds(self._t(3, 30, 0)), int(4.5 * 3600))
        self.assertEqual(MonitorScheduler._window_wait_seconds(self._t(7, 0, 0)), 3600)


if __name__ == '__main__':
    unittest.main()
