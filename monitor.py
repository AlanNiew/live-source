import os
import time
import threading

import requests
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 检测目标（URL 硬编码，与现有 hntv API URL / aggregator 源列表的模式一致）
# 部署时容器内通过映射端口访问自身：服务对外 15002，容器内监听 5002
HEALTH_URL = os.environ.get('MONITOR_HEALTH_URL', 'http://localhost:15002/health')
M3U_URL = os.environ.get('MONITOR_M3U_URL', 'http://localhost:15002/api/live.m3u8')

# 频道数低于此值视为异常（正常约 55；公开源全挂只剩 hntv 时约 15，30 居中可捕获此隐蔽故障）
MIN_CHANNEL_COUNT = 30

CHECK_INTERVAL = 60          # 检测间隔（秒）
STARTUP_DELAY = 90           # 首次检测延迟（秒）：等聚合任务跑完首次，避免启动初期误报


class MonitorUtils:
    """服务健康监控工具类"""

    # 记录上次检测结果，用于状态翻转判断（OK / FAIL）。模块级变量，
    # 单进程内有效（GUNICORN_WORKERS=1 保证线程唯一）
    _last_status = "OK"
    # 连续失败计数，仅用于日志，不影响发邮件逻辑
    _fail_count = 0

    @staticmethod
    def check_health():
        """
        检测服务存活：/health 返回 200 且 status=healthy
        :return: True 健康 / False 异常
        """
        try:
            r = requests.get(HEALTH_URL, timeout=5)
            if r.status_code != 200:
                return False
            data = r.json()
            return data.get('status') == 'healthy'
        except Exception as e:
            print(f"健康检测请求失败: {str(e)}")
            return False

    @staticmethod
    def check_m3u():
        """
        检测核心功能：/api/live.m3u8 返回 200 且频道数 ≥ MIN_CHANNEL_COUNT
        :return: (正常bool, 频道数int)
        """
        try:
            r = requests.get(M3U_URL, timeout=10)
            if r.status_code != 200:
                return False, 0
            count = r.text.count('#EXTINF')
            return count >= MIN_CHANNEL_COUNT, count
        except Exception as e:
            print(f"m3u 检测请求失败: {str(e)}")
            return False, 0

    @staticmethod
    def send_alert(subject, message, level='error'):
        """
        发送告警邮件，复用现有 EmailNotifier
        :param subject: 标题
        :param message: 内容
        :param level: error(故障) / info(恢复)
        发送失败只记日志，不影响检测循环
        """
        try:
            # 项目内的 email/ 目录与 Python 标准库 email 同名会冲突，
            # 这里用 importlib 从绝对路径加载，绕开命名冲突
            import importlib.util
            module_path = os.path.join(os.path.dirname(__file__), 'email', 'send_assistant.py')
            spec = importlib.util.spec_from_file_location('send_assistant', module_path)
            send_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(send_module)
            EmailNotifier = send_module.EmailNotifier

            email_addr = os.environ.get('email', '').strip()
            email_pwd = os.environ.get('password', '').strip()
            if not email_addr or not email_pwd:
                print("告警邮件未发送：未配置 email/password 环境变量")
                return

            # 根据 .env 的邮箱地址推断类型（qq/163 等），默认 qq
            email_type = 'qq'
            if '@163.com' in email_addr:
                email_type = '163'
            elif '@gmail.com' in email_addr:
                email_type = 'gmail'
            elif '@outlook.com' in email_addr or '@hotmail.com' in email_addr:
                email_type = 'outlook'

            notifier = EmailNotifier(
                email_type=email_type,
                username=email_addr,
                password=email_pwd,
                from_addr=email_addr,
            )
            notifier.send_notification(title=subject, message=message, level=level)
            print(f"告警邮件已发送: [{level}] {subject}")
        except Exception as e:
            print(f"发送告警邮件出错: {str(e)}")

    @staticmethod
    def run_check_once():
        """
        执行一次完整检测，按状态机决定是否发邮件：
        - OK → FAIL：发故障告警
        - FAIL → OK：发恢复通知
        - FAIL → FAIL / OK → OK：不发（避免轰炸）
        """
        health_ok = MonitorUtils.check_health()
        m3u_ok, channel_count = MonitorUtils.check_m3u()
        current = "OK" if (health_ok and m3u_ok) else "FAIL"

        if current == "OK":
            MonitorUtils._fail_count = 0
            print(f"健康检测正常（频道数 {channel_count}）")
        else:
            MonitorUtils._fail_count += 1
            reason = []
            if not health_ok:
                reason.append("/health 不可达")
            if not m3u_ok:
                reason.append(f"频道数 {channel_count} < {MIN_CHANNEL_COUNT}")
            print(f"健康检测异常（连续第 {MonitorUtils._fail_count} 次）：{', '.join(reason)}")

        # 状态翻转时才发邮件
        if current == "FAIL" and MonitorUtils._last_status == "OK":
            MonitorUtils.send_alert(
                subject="直播服务异常",
                message=f"检测到服务异常：{', '.join(reason) if not health_ok or not m3u_ok else ''}\n"
                        f"健康检查：{'正常' if health_ok else '失败'}\n"
                        f"频道数：{channel_count}（阈值 {MIN_CHANNEL_COUNT}）\n"
                        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                level='error',
            )
        elif current == "OK" and MonitorUtils._last_status == "FAIL":
            MonitorUtils.send_alert(
                subject="直播服务已恢复",
                message=f"服务已恢复正常，当前频道数 {channel_count}\n"
                        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                level='info',
            )

        MonitorUtils._last_status = current


class MonitorScheduler:
    """健康监控定时调度（复刻 utils.py SchedulerUtils 与 aggregator AggregatorScheduler 写法）"""

    @staticmethod
    def schedule_monitor():
        """
        启动 daemon 线程，定时执行健康检测
        依赖 GUNICORN_WORKERS=1 保证线程只起一次（已在 gunicorn.conf.py 配置）
        """

        def monitor_loop():
            # 首次启动延迟，等聚合任务跑完首次，避免启动初期频道数未达阈值误报
            print(f"健康监控将在 {STARTUP_DELAY} 秒后开始")
            time.sleep(STARTUP_DELAY)

            while True:
                try:
                    MonitorUtils.run_check_once()
                except Exception as e:
                    print(f"健康检测循环出错: {str(e)}")
                time.sleep(CHECK_INTERVAL)

        monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        monitor_thread.start()
