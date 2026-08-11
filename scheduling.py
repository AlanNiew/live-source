"""定时调度统一入口：XML 每日更新 + 聚合刷新 + 健康监控

三个 daemon 线程均在 import 时启动（main.py 导入即触发，含 WSGI 部署场景）。
因此 GUNICORN_WORKERS 必须为 1（gunicorn.conf.py 默认已配置），
否则线程会随每个 worker 重复启动，导致任务重复执行、告警邮件重复轰炸。
"""
import datetime
import threading
import time

from config import AGGREGATE_REFRESH_INTERVAL, GMT8
from core.aggregator import AggregatorUtils
from core.epg import XmlUtils
from monitoring.scheduler import MonitorScheduler


def schedule_daily_xml_update():
    """每天 GMT+8 02:30 刷新 EPG XML 数据"""

    def update_xml_daily():
        while True:
            try:
                # 计算到明天 02:30 的时间间隔（秒），使用GMT+8时区
                now = datetime.datetime.now(tz=GMT8)
                tomorrow = now + datetime.timedelta(days=1)
                next_update = tomorrow.replace(hour=2, minute=30, second=0, microsecond=0)
                time_to_wait = (next_update - now).total_seconds()

                print(f"等待 {time_to_wait} 秒后更新XML数据...")
                time.sleep(time_to_wait)

                XmlUtils.get_and_save_xml_data()
                print("XML数据已更新")
            except Exception as e:
                print(f"定时更新XML数据时出错: {str(e)}")

    scheduler_thread = threading.Thread(target=update_xml_daily, daemon=True)
    scheduler_thread.start()


def schedule_aggregate_refresh():
    """每 6 小时刷新一次多源聚合结果（启动时立即执行一次）"""

    def refresh_loop():
        while True:
            try:
                # 首次启动立即刷新一次，之后按间隔刷新
                AggregatorUtils.get_aggregated_m3u()
                print("聚合 m3u 已刷新")
                time.sleep(AGGREGATE_REFRESH_INTERVAL)
            except Exception as e:
                print(f"定时刷新聚合 m3u 出错: {str(e)}")
                time.sleep(60)  # 出错后等 1 分钟再试，避免狂跑

    scheduler_thread = threading.Thread(target=refresh_loop, daemon=True)
    scheduler_thread.start()


def start_all():
    """统一启动全部后台调度（main.py 导入时调用一次）"""
    schedule_daily_xml_update()
    print("定时XML更新任务已启动")

    schedule_aggregate_refresh()
    print("定时聚合刷新任务已启动")

    MonitorScheduler.schedule_monitor()
    print("健康监控任务已启动")
