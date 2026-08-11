import os
import re
import time
import threading
import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# GMT+8 时区（与 utils.py 的 GMT8 一致，所有定时/时间判断均按此，不依赖容器时区）
GMT8 = datetime.timezone(datetime.timedelta(hours=8))

# 检测目标（URL 硬编码，与现有 hntv API URL / aggregator 源列表的模式一致）
# 注意：monitor 跑在 api 容器内部，自检地址要用容器内端口 5002（gunicorn 监听端口），
# 不能用宿主机映射端口 15002（容器内 localhost 是容器自身，15002 连不上）。
# 本地开发环境 python main.py 同样监听 5002，此默认值通用。
# 若未来加 nginx 反代，需用环境变量覆盖指向反代入口（如 http://nginx/health）。
HEALTH_URL = os.environ.get('MONITOR_HEALTH_URL', 'http://localhost:5002/health')
M3U_URL = os.environ.get('MONITOR_M3U_URL', 'http://localhost:5002/api/live.m3u8')
EPG_URL = os.environ.get('MONITOR_EPG_URL', 'http://localhost:5002/api/live.xml.gz')

# 频道数低于此值视为异常（正常约 55；公开源全挂只剩 hntv 时约 15，30 居中可捕获此隐蔽故障）
MIN_CHANNEL_COUNT = 30

# 检测时段（GMT+8）：仅 8:00 - 24:00 检测，0:00-7:59 不检测（不消耗流量/不打扰）
CHECK_WINDOW_START_HOUR = 8     # 开始（含）
CHECK_WINDOW_END_HOUR = 24      # 结束（不含）

CHECK_INTERVAL = 600         # 健康检测间隔（秒），10 分钟一轮
STARTUP_DELAY = 90           # 首次检测延迟（秒）：等聚合任务跑完首次，避免启动初期误报

# 流地址可达性检测（低频全量探测）配置
STREAM_CHECK_INTERVAL = 1800        # 全量流探测间隔（秒），30 分钟一轮
STREAM_CHECK_CONCURRENCY = 10       # 并发探测数
STREAM_PROBE_TIMEOUT = 8            # 单流探测超时（秒）
STREAM_USER_AGENT = 'hntv-api-monitor'

# 分组可达率阈值（按用户对分组的重要性分级）：
# - 河南卫视（官方源，权重最高）：低于 90% 告警
# - 央视（iptv-org 源，稳定）：低于 80% 告警
# - 地方卫视（免费源，可达率天然低）：低于 20% 才告警
GROUP_HEALTH_RATIOS = {
    "河南卫视": 0.9,
    "央视": 0.8,
    "卫视": 0.2,
}
DEFAULT_GROUP_RATIO = 0.5           # 未配置分组的兜底阈值

# 参与邮件告警的分组：卫视组健康率低是常态，目前只检测并在日志展示，
# 不触发告警邮件；河南卫视/央视异常才发邮件
ALERT_GROUPS = ("河南卫视", "央视")


class MonitorUtils:
    """服务健康监控工具类"""

    # 记录上次检测结果，用于状态翻转判断（OK / FAIL）。模块级变量，
    # 单进程内有效（GUNICORN_WORKERS=1 保证线程唯一）
    _last_status = "OK"
    # 连续失败计数，仅用于日志，不影响发邮件逻辑
    _fail_count = 0
    # 流探测独立状态机（低频全量探测，10 分钟一轮）
    _stream_last_status = "OK"
    _stream_fail_count = 0

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
    def check_epg():
        """
        检测 EPG 节目单：/api/live.xml.gz 返回 200 且内容非空
        只验证可下载，不解压解析（gzip 内容有即视为正常）
        :return: (正常bool, 大小KBint)
        """
        try:
            r = requests.get(EPG_URL, timeout=15)
            if r.status_code != 200:
                return False, 0
            size_kb = len(r.content) // 1024
            return size_kb > 0, size_kb
        except Exception as e:
            print(f"epg 检测请求失败: {str(e)}")
            return False, 0

    @staticmethod
    def fetch_m3u_urls():
        """
        拉取聚合 m3u 文本并解析出所有流地址
        只统计 http(s) 流：rtmp 等非 HTTP 协议 requests 无法探测，跳过不计入分母
        :return: (url, group) 列表；拉取失败返回空列表
        """
        try:
            r = requests.get(M3U_URL, timeout=10)
            if r.status_code != 200:
                return []
            items = []
            cur_group = "其他"
            for line in r.text.splitlines():
                line = line.strip()
                if line.startswith('#EXTINF'):
                    m = re.search(r'group-title="([^"]*)"', line)
                    if m:
                        cur_group = m.group(1)
                elif line.startswith(('http://', 'https://')):
                    items.append((line, cur_group))
            return items
        except Exception as e:
            print(f"流地址解析请求失败: {str(e)}")
            return []

    @staticmethod
    def probe_stream(url):
        """
        探测单个流地址可达性：GET + Range 请求读少量字节即断开
        （HEAD 对直播源不可靠，很多返回 404；Range 206 也算成功）
        :param url: 流地址
        :return: True 可达 / False 不可达
        """
        r = None
        try:
            r = requests.get(
                url, timeout=STREAM_PROBE_TIMEOUT, stream=True,
                headers={'Range': 'bytes=0-1024', 'User-Agent': STREAM_USER_AGENT},
            )
            if r.status_code in (200, 206):
                try:
                    return bool(next(r.iter_content(1024)))
                except StopIteration:
                    return False
            return False
        except Exception:
            return False
        finally:
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass

    @staticmethod
    def check_streams():
        """
        全量并发探测聚合列表中的流地址（不分组的整体统计）
        :return: (可达数, 总数)；列表拉不到时返回 (0, 0)
        """
        items = MonitorUtils.fetch_m3u_urls()
        if not items:
            return 0, 0
        ok = 0
        with ThreadPoolExecutor(max_workers=STREAM_CHECK_CONCURRENCY) as executor:
            for result in executor.map(MonitorUtils.probe_stream, [u for u, _ in items]):
                if result:
                    ok += 1
        return ok, len(items)

    @staticmethod
    def run_stream_check_once():
        """
        执行一次全量流探测，按分组可达率阈值判定（河南卫视 90% / 央视 80% / 卫视 20%）：
        任一组低于其阈值即整体 FAIL，按独立状态机决定是否发邮件（翻转才发）：
        - OK → FAIL：发"直播流可达性异常"告警（邮件列出各组明细）
        - FAIL → OK：发恢复通知
        - 持续同态：不发（避免轰炸）
        独立于 run_check_once 的常规状态机，低频运行（30 分钟一轮）
        """
        items = MonitorUtils.fetch_m3u_urls()
        if not items:
            current = "FAIL"
            print("流探测异常：聚合列表拉取失败或无流地址")
            checks = [{
                "name": "流地址可达性",
                "status": False,
                "detail": "聚合列表拉取失败或无流地址",
            }]
        else:
            # 并发探测所有流
            with ThreadPoolExecutor(max_workers=STREAM_CHECK_CONCURRENCY) as executor:
                results = list(executor.map(MonitorUtils.probe_stream, [u for u, _ in items]))
            # 按分组统计
            groups = defaultdict(lambda: [0, 0])  # group -> [可达数, 总数]
            for (url, group), ok in zip(items, results):
                groups[group][1] += 1
                if ok:
                    groups[group][0] += 1
            # 逐组判定：低于分组阈值即不达标（卫视组只展示不参与告警）
            checks = []
            all_ok = True
            for group, (ok_count, total) in groups.items():
                ratio = ok_count / total
                threshold = GROUP_HEALTH_RATIOS.get(group, DEFAULT_GROUP_RATIO)
                group_ok = ratio >= threshold
                if group in ALERT_GROUPS and not group_ok:
                    all_ok = False
                checks.append({
                    "name": f"{group}（阈值 {threshold:.0%}）",
                    "status": group_ok,
                    "detail": f"{ok_count}/{total} 可达（{ratio:.0%}）",
                })
            current = "OK" if all_ok else "FAIL"
            for c in checks:
                print(f"流探测 [{c['name']}]: {'达标' if c['status'] else '不达标'} - {c['detail']}")

        if current == "FAIL":
            MonitorUtils._stream_fail_count += 1
            print(f"流探测异常（连续第 {MonitorUtils._stream_fail_count} 次）")
        else:
            MonitorUtils._stream_fail_count = 0

        # 状态翻转时才发邮件
        if current == "FAIL" and MonitorUtils._stream_last_status == "OK":
            MonitorUtils.send_alert(
                subject="直播流可达性异常",
                checks=checks,
                level='error',
                extra_info={"连续失败次数": f"第 {MonitorUtils._stream_fail_count} 次"},
            )
        elif current == "OK" and MonitorUtils._stream_last_status == "FAIL":
            MonitorUtils.send_alert(
                subject="直播流可达性已恢复",
                checks=checks,
                level='info',
            )
        MonitorUtils._stream_last_status = current

    # 邮件 HTML 模板路径（从文件加载，便于后期维护样式而不改代码）
    TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), 'templates', 'email_alert.html')

    @staticmethod
    def _build_html(title, level, checks, extra_info=None):
        """
        构建告警邮件的 HTML 内容（从 templates/email_alert.html 加载模板并填充）
        :param title: 标题（如"直播服务异常"）
        :param level: error(故障) / info(恢复)
        :param checks: 检测项列表，每项为 dict(name, status, detail)
        :param extra_info: 额外信息 dict（如连续失败次数）
        :return: HTML 字符串
        """
        # 故障用红色调，恢复用绿色调
        is_error = level == 'error'
        theme_color = '#e74c3c' if is_error else '#27ae60'
        icon = '⚠️' if is_error else '✅'
        banner_text = '故障告警' if is_error else '服务恢复'

        # 构建检测项表格行
        rows = ''
        for c in checks:
            status_badge = (
                '<span style="color:#fff;background:#27ae60;padding:2px 8px;border-radius:3px;font-size:12px;">正常</span>'
                if c['status'] else
                '<span style="color:#fff;background:#e74c3c;padding:2px 8px;border-radius:3px;font-size:12px;">异常</span>'
            )
            rows += (
                f'<tr>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{c["name"]}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{status_badge}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#666;font-size:13px;">{c["detail"]}</td>'
                f'</tr>'
            )

        # 额外信息行
        extra_rows = ''
        if extra_info:
            for k, v in extra_info.items():
                extra_rows += (
                    f'<tr>'
                    f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{k}</td>'
                    f'<td style="padding:8px 12px;border-bottom:1px solid #eee;" colspan="2">{v}</td>'
                    f'</tr>'
                )

        # 从模板文件读取并填充占位符
        try:
            with open(MonitorUtils.TEMPLATE_PATH, 'r', encoding='utf-8') as f:
                template = f.read()
        except Exception as e:
            print(f"读取邮件模板失败，使用空模板: {str(e)}")
            template = '{rows}'

        return template.format(
            theme_color=theme_color,
            icon=icon,
            banner_text=banner_text,
            title=title,
            rows=rows,
            extra_rows=extra_rows,
            time_str=time.strftime('%Y-%m-%d %H:%M:%S'),
        )

    @staticmethod
    def send_alert(subject, checks, level='error', extra_info=None):
        """
        发送 HTML 告警邮件，复用现有 EmailNotifier
        :param subject: 标题
        :param checks: 检测项列表 [{name, status, detail}]
        :param level: error(故障) / info(恢复)
        :param extra_info: 额外信息 dict
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

            # 构建 HTML 内容
            html_content = MonitorUtils._build_html(subject, level, checks, extra_info)

            notifier = EmailNotifier(
                email_type=email_type,
                username=email_addr,
                password=email_pwd,
                from_addr=email_addr,
            )
            # 用 HTML 格式发送（绕过纯文本的 send_notification）
            notifier.send(
                to_addrs=[email_addr],
                subject=f"[{'故障告警' if level == 'error' else '服务恢复'}] {subject}",
                content=html_content,
                content_type='html',
            )
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
        epg_ok, epg_size = MonitorUtils.check_epg()
        current = "OK" if (health_ok and m3u_ok and epg_ok) else "FAIL"

        if current == "OK":
            MonitorUtils._fail_count = 0
            print(f"健康检测正常（频道数 {channel_count}，节目单 {epg_size}KB）")
        else:
            MonitorUtils._fail_count += 1
            reason = []
            if not health_ok:
                reason.append("/health 不可达")
            if not m3u_ok:
                reason.append(f"频道数 {channel_count} < {MIN_CHANNEL_COUNT}")
            if not epg_ok:
                reason.append("节目单不可用")
            print(f"健康检测异常（连续第 {MonitorUtils._fail_count} 次）：{', '.join(reason)}")

        # 状态翻转时才发邮件
        if current == "FAIL" and MonitorUtils._last_status == "OK":
            checks = [
                {
                    "name": "服务存活 (/health)",
                    "status": health_ok,
                    "detail": "响应正常" if health_ok else "不可达或状态异常",
                },
                {
                    "name": "直播源 (频道数)",
                    "status": m3u_ok,
                    "detail": f"{channel_count} 个频道" if m3u_ok
                              else f"{channel_count} 个，低于阈值 {MIN_CHANNEL_COUNT}",
                },
                {
                    "name": "节目单 (EPG)",
                    "status": epg_ok,
                    "detail": f"{epg_size} KB" if epg_ok else "不可用或为空",
                },
            ]
            MonitorUtils.send_alert(
                subject="直播服务异常",
                checks=checks,
                level='error',
                extra_info={"连续失败次数": f"第 {MonitorUtils._fail_count} 次"},
            )
        elif current == "OK" and MonitorUtils._last_status == "FAIL":
            checks = [
                {"name": "服务存活 (/health)", "status": True, "detail": "响应正常"},
                {"name": "直播源 (频道数)", "status": True, "detail": f"{channel_count} 个频道"},
                {"name": "节目单 (EPG)", "status": True, "detail": f"{epg_size} KB"},
            ]
            MonitorUtils.send_alert(
                subject="直播服务已恢复",
                checks=checks,
                level='info',
            )

        MonitorUtils._last_status = current


class MonitorScheduler:
    """健康监控定时调度（复刻 utils.py SchedulerUtils 与 aggregator AggregatorScheduler 写法）"""

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
        启动 daemon 线程，定时执行健康检测
        依赖 GUNICORN_WORKERS=1 保证线程只起一次（已在 gunicorn.conf.py 配置）
        """

        def monitor_loop():
            # 首次启动延迟，等聚合任务跑完首次，避免启动初期频道数未达阈值误报
            print(f"健康监控将在 {STARTUP_DELAY} 秒后开始（检测时段：GMT+8 {CHECK_WINDOW_START_HOUR}:00-{CHECK_WINDOW_END_HOUR}:00）")
            time.sleep(STARTUP_DELAY)

            while True:
                try:
                    wait = MonitorScheduler._window_wait_seconds()
                    if wait > 0:
                        print(f"非检测时段，等待 {wait / 3600:.1f} 小时后恢复检测")
                        time.sleep(wait)
                        continue
                    MonitorUtils.run_check_once()
                except Exception as e:
                    print(f"健康检测循环出错: {str(e)}")
                time.sleep(CHECK_INTERVAL)

        monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        monitor_thread.start()

        # 流地址可达性低频全量探测线程（10 分钟一轮，独立状态机）
        def stream_loop():
            # 首次延迟比常规检测稍久，等聚合缓存生成
            time.sleep(STARTUP_DELAY + 30)
            print(f"流地址全量探测将在 {STARTUP_DELAY + 30} 秒后开始，之后每 {STREAM_CHECK_INTERVAL} 秒一轮")
            while True:
                try:
                    wait = MonitorScheduler._window_wait_seconds()
                    if wait > 0:
                        print(f"非检测时段，流探测等待 {wait / 3600:.1f} 小时后恢复")
                        time.sleep(wait)
                        continue
                    MonitorUtils.run_stream_check_once()
                except Exception as e:
                    print(f"流探测循环出错: {str(e)}")
                time.sleep(STREAM_CHECK_INTERVAL)

        stream_thread = threading.Thread(target=stream_loop, daemon=True)
        stream_thread.start()
