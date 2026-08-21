import smtplib
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional, Union

# 日志：优先走项目统一日志（core.logger）；本模块可能被 importlib 独立加载或脚本复用，
# 项目日志不可用时回退标准 logging（保留原 print 信息不丢失）
try:
    from core.logger import get_logger
    _log = get_logger('email')
except Exception:
    import logging
    _log = logging.getLogger('email')
    if not _log.handlers:
        _log.addHandler(logging.StreamHandler())
        _log.setLevel(logging.INFO)


class EmailNotifier:
    """邮件通知器"""

    # 常用邮箱服务器配置
    EMAIL_CONFIGS = {
        "qq": {
            "smtp_server": "smtp.qq.com",
            "port": 465,
            "ssl": True
        },
        "163": {
            "smtp_server": "smtp.163.com",
            "port": 465,
            "ssl": True
        },
        "gmail": {
            "smtp_server": "smtp.gmail.com",
            "port": 587,
            "ssl": False
        },
        "outlook": {
            "smtp_server": "smtp.office365.com",
            "port": 587,
            "ssl": False
        }
    }

    def __init__(self, email_type: str = "qq",
                 username: str = None,
                 password: str = None,
                 from_addr: str = None):
        """
        初始化邮件通知器
        参数：
            email_type: 邮箱类型，支持 qq/163/gmail/outlook
            username: 邮箱账号
            password: 邮箱密码/授权码
            from_addr: 发件人邮箱
        """
        if email_type not in self.EMAIL_CONFIGS:
            raise ValueError(f"不支持的邮箱类型，支持的类型：{list(self.EMAIL_CONFIGS.keys())}")

        self.config = self.EMAIL_CONFIGS[email_type]
        self.username = username or from_addr
        self.password = password
        self.from_addr = from_addr or username

    def send(self,
             to_addrs: Union[str, List[str]],
             subject: str,
             content: str,
             content_type: str = "plain",
             cc_addrs: Optional[Union[str, List[str]]] = None,
             bcc_addrs: Optional[Union[str, List[str]]] = None) -> bool:
        """
        发送邮件
        参数：
            to_addrs: 收件人邮箱（单个或列表）
            subject: 邮件主题
            content: 邮件内容
            content_type: 内容类型，plain（纯文本）或 html（HTML格式）
            cc_addrs: 抄送人
            bcc_addrs: 密送人
        返回：
            发送是否成功
        """
        # 确保收件人是列表形式
        if isinstance(to_addrs, str):
            to_addrs = [to_addrs]

        # 创建邮件
        if content_type == "html":
            msg = MIMEMultipart('alternative')
            text_part = MIMEText(content, 'html', 'utf-8')
            msg.attach(text_part)
        else:
            msg = MIMEText(content, 'plain', 'utf-8')

        msg['From'] = self.from_addr
        msg['To'] = ', '.join(to_addrs)
        # 主题可能含中文，需显式 RFC2047 编码（Python 3.9 直接赋值非 ASCII 会报 ascii codec 错误）
        msg['Subject'] = Header(subject, 'utf-8')

        # 添加抄送
        if cc_addrs:
            if isinstance(cc_addrs, str):
                cc_addrs = [cc_addrs]
            msg['Cc'] = ', '.join(cc_addrs)
            to_addrs.extend(cc_addrs)

        # 添加密送（不在邮件头显示）
        if bcc_addrs:
            if isinstance(bcc_addrs, str):
                bcc_addrs = [bcc_addrs]
            to_addrs.extend(bcc_addrs)

        # 网络/服务端偶发断连（如 "Connection unexpectedly closed"），重试一次；
        # 认证失败不重试（凭证问题重试无意义）
        server = None
        for attempt in range(2):
            try:
                # 连接服务器
                if self.config['ssl']:
                    server = smtplib.SMTP_SSL(self.config['smtp_server'], self.config['port'])
                else:
                    server = smtplib.SMTP(self.config['smtp_server'], self.config['port'])
                    server.starttls()

                # 登录
                server.login(self.username, self.password)

                # 发送邮件
                server.sendmail(self.from_addr, to_addrs, msg.as_string())

                print(f"邮件发送成功！收件人：{to_addrs}")
                _log.info(f"邮件发送成功！收件人：{to_addrs}")
                return True

            except smtplib.SMTPAuthenticationError:
                print("认证失败，请检查用户名和密码/授权码")
                _log.error("认证失败，请检查用户名和密码/授权码")
                return False
            except smtplib.SMTPException as e:
                print(f"SMTP错误：{e}")
                _log.warning(f"SMTP错误：{e}")
                if attempt == 0:
                    time.sleep(2)  # 短暂等待后重试
                    continue
                return False
            except Exception as e:
                print(f"邮件发送失败：{e}")
                _log.warning(f"邮件发送失败：{e}")
                if attempt == 0:
                    time.sleep(2)
                    continue
                return False
            finally:
                # 连接可能已失效（断连/认证失败），quit 需防二次异常，避免掩盖真实错误
                if server is not None:
                    try:
                        server.quit()
                    except Exception:
                        pass
                server = None

        return False

    def send_notification(self, title: str, message: str, level: str = "info") -> bool:
        """发送通知邮件（带格式）"""
        if level == "error":
            subject = f"[紧急] {title}"
            content = f"""
            ⚠️ 系统错误通知 ⚠️

            错误标题：{title}
            错误详情：{message}

            时间：{self._get_current_time()}

            请及时处理！
            """
        elif level == "warning":
            subject = f"[警告] {title}"
            content = f"""
            ⚠️ 系统警告通知 ⚠️

            警告标题：{title}
            警告详情：{message}

            时间：{self._get_current_time()}

            请注意检查！
            """
        else:
            subject = f"[通知] {title}"
            content = f"""
            系统通知

            通知标题：{title}
            通知详情：{message}

            时间：{self._get_current_time()}
            """

        return self.send(
            to_addrs=[self.from_addr],  # 默认发给自己
            subject=subject,
            content=content
        )

    @staticmethod
    def _get_current_time():
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
