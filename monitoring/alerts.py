"""邮件告警：HTML 模板渲染与发送（复用 email/send_assistant.py）"""
import importlib.util
import os
import time

from config import EMAIL_MODULE_PATH, EMAIL_TEMPLATE_PATH

from core.logger import get_logger
_logger = get_logger('alerts')


class AlertUtils:
    """告警邮件工具类（构建 HTML + 发送）"""

    @staticmethod
    def build_html(title, level, checks, extra_info=None):
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
            with open(EMAIL_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
                template = f.read()
        except Exception as e:
            _logger.warning(f"读取邮件模板失败，使用空模板: {str(e)}")
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
        发送 HTML 告警邮件，复用 email/send_assistant.py 的 EmailNotifier
        :param subject: 标题
        :param checks: 检测项列表 [{name, status, detail}]
        :param level: error(故障) / info(恢复)
        :param extra_info: 额外信息 dict
        发送失败只记日志，不影响检测循环
        """
        try:
            # 项目内的 email/ 目录与 Python 标准库 email 同名会冲突，
            # 这里用 importlib 从绝对路径加载，绕开命名冲突
            spec = importlib.util.spec_from_file_location('send_assistant', EMAIL_MODULE_PATH)
            send_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(send_module)
            EmailNotifier = send_module.EmailNotifier

            email_addr = os.environ.get('email', '').strip()
            email_pwd = os.environ.get('password', '').strip()
            if not email_addr or not email_pwd:
                _logger.warning("告警邮件未发送：未配置 email/password 环境变量")
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
            html_content = AlertUtils.build_html(subject, level, checks, extra_info)

            notifier = EmailNotifier(
                email_type=email_type,
                username=email_addr,
                password=email_pwd,
                from_addr=email_addr,
            )
            # 用 HTML 格式发送（绕过纯文本的 send_notification）
            sent = notifier.send(
                to_addrs=[email_addr],
                subject=f"[{'故障告警' if level == 'error' else '服务恢复'}] {subject}",
                content=html_content,
                content_type='html',
            )
            if sent:
                _logger.info(f"告警邮件已发送: [{level}] {subject}")
            else:
                _logger.warning(f"告警邮件发送失败: [{level}] {subject}")
        except Exception as e:
            _logger.warning(f"发送告警邮件出错: {str(e)}")
