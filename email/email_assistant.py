import smtplib
from email.mime.text import MIMEText
from email.header import Header


def send_email_simple(smtp_server, port, username, password,
                      from_addr, to_addrs, subject, content):
    """
    发送纯文本邮件
    参数：
        smtp_server: SMTP服务器地址
        port: 端口号
        username: 邮箱账号
        password: 邮箱密码/授权码
        from_addr: 发件人邮箱
        to_addrs: 收件人邮箱列表
        subject: 邮件主题
        content: 邮件内容
    """
    # 创建邮件内容
    msg = MIMEText(content, 'plain', 'utf-8')
    msg['From'] = Header(from_addr)
    msg['To'] = ', '.join(to_addrs) if isinstance(to_addrs, list) else to_addrs
    msg['Subject'] = Header(subject, 'utf-8')

    try:
        # 连接SMTP服务器
        if port == 465:
            # SSL加密方式
            server = smtplib.SMTP_SSL(smtp_server, port)
        else:
            # TLS加密方式
            server = smtplib.SMTP(smtp_server, port)
            server.starttls()

        # 登录邮箱
        server.login(username, password)

        # 发送邮件
        server.sendmail(from_addr, to_addrs, msg.as_string())

        print("邮件发送成功！")
        return True

    except Exception as e:
        print(f"邮件发送失败：{e}")
        return False

    finally:
        if 'server' in locals():
            server.quit()
