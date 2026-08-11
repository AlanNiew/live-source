"""健康检测项：服务存活/直播列表/EPG/流探测，含常规与流探测两套状态机"""
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

from config import (ALERT_GROUPS, DEFAULT_GROUP_RATIO, EPG_URL,
                    GROUP_HEALTH_RATIOS, HEALTH_URL, M3U_URL,
                    MIN_CHANNEL_COUNT, STREAM_CHECK_CONCURRENCY)
from core.probing import probe_stream
from core.sources import SourceUtils
from monitoring.alerts import AlertUtils


class CheckUtils:
    """健康检测工具类"""

    # 记录上次检测结果，用于状态翻转判断（OK / FAIL）。模块级变量，
    # 单进程内有效（GUNICORN_WORKERS=1 保证线程唯一）
    _last_status = "OK"
    # 连续失败计数，仅用于日志，不影响发邮件逻辑
    _fail_count = 0
    # 流探测独立状态机（低频全量探测，30 分钟一轮）
    _stream_last_status = "OK"
    _stream_fail_count = 0

    # ------------------------------------------------------------ 检测项

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
    def fetch_m3u_groups():
        """
        拉取聚合 m3u 文本并解析出 (url, group) 列表
        只统计 http(s) 流：rtmp 等非 HTTP 协议 requests 无法探测，跳过不计入分母
        :return: (url, group) 列表；拉取失败返回空列表
        """
        try:
            r = requests.get(M3U_URL, timeout=10)
            if r.status_code != 200:
                return []
            items = []
            for extinf, url in SourceUtils.iter_m3u_entries(r.text):
                if not url.startswith(('http://', 'https://')):
                    continue
                m = re.search(r'group-title="([^"]*)"', extinf)
                items.append((url, m.group(1) if m else "其他"))
            return items
        except Exception as e:
            print(f"流地址解析请求失败: {str(e)}")
            return []

    # ------------------------------------------------------------ 常规状态机

    @staticmethod
    def run_check_once():
        """
        执行一次完整检测（health/m3u 列表/epg），按状态机决定是否发邮件：
        - OK → FAIL：发故障告警
        - FAIL → OK：发恢复通知
        - FAIL → FAIL / OK → OK：不发（避免轰炸）
        """
        health_ok = CheckUtils.check_health()
        m3u_ok, channel_count = CheckUtils.check_m3u()
        epg_ok, epg_size = CheckUtils.check_epg()
        current = "OK" if (health_ok and m3u_ok and epg_ok) else "FAIL"

        if current == "OK":
            CheckUtils._fail_count = 0
            print(f"健康检测正常（频道数 {channel_count}，节目单 {epg_size}KB）")
        else:
            CheckUtils._fail_count += 1
            reason = []
            if not health_ok:
                reason.append("/health 不可达")
            if not m3u_ok:
                reason.append(f"频道数 {channel_count} < {MIN_CHANNEL_COUNT}")
            if not epg_ok:
                reason.append("节目单不可用")
            print(f"健康检测异常（连续第 {CheckUtils._fail_count} 次）：{', '.join(reason)}")

        # 状态翻转时才发邮件
        if current == "FAIL" and CheckUtils._last_status == "OK":
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
            AlertUtils.send_alert(
                subject="直播服务异常",
                checks=checks,
                level='error',
                extra_info={"连续失败次数": f"第 {CheckUtils._fail_count} 次"},
            )
        elif current == "OK" and CheckUtils._last_status == "FAIL":
            checks = [
                {"name": "服务存活 (/health)", "status": True, "detail": "响应正常"},
                {"name": "直播源 (频道数)", "status": True, "detail": f"{channel_count} 个频道"},
                {"name": "节目单 (EPG)", "status": True, "detail": f"{epg_size} KB"},
            ]
            AlertUtils.send_alert(
                subject="直播服务已恢复",
                checks=checks,
                level='info',
            )

        CheckUtils._last_status = current

    # ------------------------------------------------------------ 流探测状态机

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
        items = CheckUtils.fetch_m3u_groups()
        if not items:
            current = "FAIL"
            print("流探测异常：聚合列表拉取失败或无流地址")
            checks = [{
                "name": "流地址可达性",
                "status": False,
                "detail": "聚合列表拉取失败或无流地址",
            }]
        else:
            # 并发探测所有流（严格判定：仅 200/206 可达，与聚合过滤的宽松口径区分）
            with ThreadPoolExecutor(max_workers=STREAM_CHECK_CONCURRENCY) as executor:
                results = list(executor.map(probe_stream, [u for u, _ in items]))
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
            CheckUtils._stream_fail_count += 1
            print(f"流探测异常（连续第 {CheckUtils._stream_fail_count} 次）")
        else:
            CheckUtils._stream_fail_count = 0

        # 状态翻转时才发邮件
        if current == "FAIL" and CheckUtils._stream_last_status == "OK":
            AlertUtils.send_alert(
                subject="直播流可达性异常",
                checks=checks,
                level='error',
                extra_info={"连续失败次数": f"第 {CheckUtils._stream_fail_count} 次"},
            )
        elif current == "OK" and CheckUtils._stream_last_status == "FAIL":
            AlertUtils.send_alert(
                subject="直播流可达性已恢复",
                checks=checks,
                level='info',
            )
        CheckUtils._stream_last_status = current
