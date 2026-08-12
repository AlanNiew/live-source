"""告警邮件链路测试脚本：部署后一键验证 SMTP 发送是否正常

用法（服务器宿主机执行，仅依赖标准库）：
    python3 scripts/test_email.py                # 发给自己（.env 里的 email）
    python3 scripts/test_email.py 某人@qq.com    # 或指定收件人

退出码：0=发送成功；1=失败（缺失配置/发送失败）
"""
import importlib.util
import os
import sys

# 项目根目录（脚本位于 scripts/ 下）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env():
    """解析项目根 .env 的 email/password（标准库实现，不依赖 python-dotenv）"""
    env = {}
    env_path = os.path.join(BASE_DIR, '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                env[key.strip()] = value.strip()
    return env


def load_notifier():
    """importlib 加载 email/send_assistant.py（与 monitoring/alerts.py 生产链路一致）"""
    module_path = os.path.join(BASE_DIR, 'email', 'send_assistant.py')
    spec = importlib.util.spec_from_file_location('send_assistant', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EmailNotifier


def main():
    to_addr = sys.argv[1] if len(sys.argv) > 1 else None

    env = load_env()
    email_addr = env.get('email') or os.environ.get('email', '').strip()
    email_pwd = env.get('password') or os.environ.get('password', '').strip()

    if not email_addr or not email_pwd:
        print("错误: 未配置 email/password（检查项目根 .env 或环境变量）")
        return 1

    to_addr = to_addr or email_addr

    # 按邮箱推断 SMTP 类型（与生产逻辑一致）
    email_type = 'qq'
    if '@163.com' in email_addr:
        email_type = '163'
    elif '@gmail.com' in email_addr:
        email_type = 'gmail'
    elif '@outlook.com' in email_addr or '@hotmail.com' in email_addr:
        email_type = 'outlook'

    # 构造中文主题测试邮件（验证 RFC2047 编码 + HTML MIME）
    subject = "[测试] HNTV 告警邮件链路验证（请忽略）"
    html = (
        "<div style='font-family:sans-serif;padding:20px;'>"
        "<h2 style='color:#27ae60;'>✅ HNTV 邮件链路测试</h2>"
        "<p>如果收到本邮件，说明 SMTP 发送、中文主题编码、HTML 渲染均正常。</p>"
        f"<p>发送时间：{__import__('time').strftime('%Y-%m-%d %H:%M:%S')}</p>"
        "</div>"
    )

    try:
        notifier = load_notifier()(
            email_type=email_type, username=email_addr,
            password=email_pwd, from_addr=email_addr)
        ok = notifier.send(
            to_addrs=[to_addr], subject=subject,
            content=html, content_type='html')
    except Exception as e:
        print(f"发送异常: {type(e).__name__}: {e}")
        return 1

    if ok:
        print(f"测试邮件发送成功 → {to_addr}（请查收确认）")
        return 0
    print(f"测试邮件发送失败 → {to_addr}（详情见上方日志）")
    return 1


if __name__ == '__main__':
    sys.exit(main())
