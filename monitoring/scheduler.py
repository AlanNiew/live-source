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
    def _window_params():
        """检测时段与周期：settings 优先，config 兜底（下一轮生效）"""
        from admin import db
        return {
            'start_hour': db.get_effective_int('monitor_window_start', CHECK_WINDOW_START_HOUR),
            'end_hour': db.get_effective_int('monitor_window_end', CHECK_WINDOW_END_HOUR),
        }

    @staticmethod
    def _window_wait_seconds(now=None):
        """
        计算距离检测时段的等待秒数（GMT+8）
        :param now: 当前时间（可注入便于测试）；默认取当前 GMT+8 时间
        :return: 检测时段内返回 0；时段外返回睡到下个时段的秒数
        """
        params = MonitorScheduler._window_params()
        start_hour, end_hour = params['start_hour'], params['end_hour']
        now = now or datetime.datetime.now(tz=GMT8)
        if start_hour <= now.hour < end_hour:
            return 0
        # 非检测时段：取当天下一时段起点；若已过则顺延到明天
        next_start = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        if next_start <= now:
            next_start += datetime.timedelta(days=1)
        return int((next_start - now).total_seconds())

    @staticmethod
    def _startup_delay():
        """首次检测延迟：settings 优先，config 兜底"""
        from admin import db
        return db.get_effective_int('startup_delay', STARTUP_DELAY)

    @staticmethod
    def _monitor_interval():
        """常规检测周期（秒）：settings 优先，config 兜底"""
        from admin import db
        return db.get_effective_int('monitor_interval', CHECK_INTERVAL)

    @staticmethod
    def _stream_check_interval():
        """流探测周期（秒）：settings 优先，config 兜底"""
        from admin import db
        return db.get_effective_int('stream_check_interval', STREAM_CHECK_INTERVAL)

    @staticmethod
    def schedule_monitor():
        """
        启动 daemon 线程，定时执行健康检测（常规 10 分钟一轮 + 流探测 30 分钟一轮）
        依赖 GUNICORN_WORKERS=1 保证线程只起一次（已在 gunicorn.conf.py 配置）
        """

        def monitor_loop():
            # 首次启动延迟，等聚合任务跑完首次，避免启动初期频道数未达阈值误报
            startup = MonitorScheduler._startup_delay()
            params = MonitorScheduler._window_params()
            _logger.info(f"健康监控将在 {startup} 秒后开始"
                  f"（检测时段：GMT+8 {params['start_hour']}:00-{params['end_hour']}:00）")
            time.sleep(startup)

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
                # 周期动态化：每轮睡前重读（下一轮生效）
                time.sleep(MonitorScheduler._monitor_interval())

        monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        monitor_thread.start()

        # 流地址可达性低频全量探测线程（独立状态机）
        def stream_loop():
            # 首次延迟比常规检测稍久，等聚合缓存生成
            startup = MonitorScheduler._startup_delay()
            time.sleep(startup + 30)
            _logger.info(f"流地址全量探测将在 {startup + 30} 秒后开始，"
                  f"之后每 {MonitorScheduler._stream_check_interval()} 秒一轮")
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
                # 周期动态化：每轮睡前重读（下一轮生效）
                time.sleep(MonitorScheduler._stream_check_interval())

        stream_thread = threading.Thread(target=stream_loop, daemon=True)
        stream_thread.start()
