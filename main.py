"""项目入口：创建 Flask 应用并启动后台调度

注意：start_all() 在模块顶层调用，gunicorn 导入 main:app 时即启动三个 daemon 线程
（XML 每日更新 / 聚合刷新 / 健康监控）。因此 GUNICORN_WORKERS 必须为 1，
否则每个 worker 都会起一份线程，任务重复执行、告警邮件重复轰炸。
"""
import atexit

from app import create_app
from core.logger import get_logger
from scheduling import start_all

_logger = get_logger('main')

# 创建 Flask 应用
app = create_app()

# 启动后台调度（导入即启动，含 WSGI 部署场景）
start_all()

# 服务启停事件（入库，管理页日志可查）
try:
    from admin import db
    db.record_event('INFO', 'main', "服务启动完成（调度线程就绪，管理后台 /admin）")
except Exception:
    pass


def _on_exit():
    """进程退出钩子（dev Ctrl+C / gunicorn worker 优雅退出时触发；SIGKILL 不保证）"""
    try:
        _logger.info("服务退出")
        from admin import db
        db.record_event('INFO', 'main', "服务退出")
    except Exception:
        pass


atexit.register(_on_exit)


if __name__ == '__main__':
    # 仅直接运行时使用（开发环境）；生产用 gunicorn -c gunicorn.conf.py main:app
    _logger.info("启动开发服务器: http://0.0.0.0:5002")
    app.run(debug=False, host='0.0.0.0', port=5002)
