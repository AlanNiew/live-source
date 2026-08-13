"""监控调度：常规检测循环 + 流探测循环（含 GMT+8 检测时段窗口）"""
import datetime
import threading
import time

from config import (CHECK_INTERVAL, CHECK_WINDOW_END_HOUR,
                    CHECK_WINDOW_START_HOUR, GMT8, STARTUP_DELAY,
                    STREAM_CHECK_INTERVAL)
from monitoring.checks import CheckUtils

from core.logger import get_logger
_logger = get_logger('scheduler')


class MonitorScheduler:
    """健康监控定时调度"""

    @staticmethod
    def _window_wait_seconds(now=None):
        """
        计算距离检测时段的等待秒数（GMT+8）
        :param now: 当前时间（可注入便于测试）；默认取当前 GMT+8 时间
        :return: 检测时段内返回 0；0:00-7:59 返回睡到下一个 8:00 的秒数
        """
        now = now or datetime.datetime.now(tz=GMT8)
        if CHECK_WINDOW_START_HOUR <= now.hour < CHECK_WINDOW_END_HOUR:
            return 0
        # 非检测时段（0:00-7:59）：先取当天 8:00，若已过（不可能）则顺延到明天
        next_start = now.replace(
            hour=CHECK_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
        if next_start <= now:
            next_start += datetime.timedelta(days=1)
        return int((next_start - now).total_seconds())

    @staticmethod
    def schedule_monitor():
        """
        启动 daemon 线程，定时执行健康检测（常规 10 分钟一轮 + 流探测 30 分钟一轮）
        依赖 GUNICORN_WORKERS=1 保证线程只起一次（已在 gunicorn.conf.py 配置）
        """

        def monitor_loop():
            # 首次启动延迟，等聚合任务跑完首次，避免启动初期频道数未达阈值误报
            _logger.info(f"健康监控将在 {STARTUP_DELAY} 秒后开始"
                  f"（检测时段：GMT+8 {CHECK_WINDOW_START_HOUR}:00-{CHECK_WINDOW_END_HOUR}:00）")
            time.sleep(STARTUP_DELAY)

            while True:
                try:
                    wait = MonitorScheduler._window_wait_seconds()
                    if wait > 0:
                        _logger.info(f"非检测时段，等待 {wait / 3600:.1f} 小时后恢复检测")
                        time.sleep(wait)
                        continue
                    CheckUtils.run_check_once()
                except Exception as e:
                    _logger.warning(f"健康检测循环出错: {str(e)}")
                time.sleep(CHECK_INTERVAL)

        monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        monitor_thread.start()

        # 流地址可达性低频全量探测线程（30 分钟一轮，独立状态机）
        def stream_loop():
            # 首次延迟比常规检测稍久，等聚合缓存生成
            time.sleep(STARTUP_DELAY + 30)
            _logger.info(f"流地址全量探测将在 {STARTUP_DELAY + 30} 秒后开始，"
                  f"之后每 {STREAM_CHECK_INTERVAL} 秒一轮")
            while True:
                try:
                    wait = MonitorScheduler._window_wait_seconds()
                    if wait > 0:
                        _logger.info(f"非检测时段，流探测等待 {wait / 3600:.1f} 小时后恢复")
                        time.sleep(wait)
                        continue
                    CheckUtils.run_stream_check_once()
                except Exception as e:
                    _logger.warning(f"流探测循环出错: {str(e)}")
                time.sleep(STREAM_CHECK_INTERVAL)

        stream_thread = threading.Thread(target=stream_loop, daemon=True)
        stream_thread.start()
