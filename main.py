"""项目入口：创建 Flask 应用并启动后台调度

注意：start_all() 在模块顶层调用，gunicorn 导入 main:app 时即启动三个 daemon 线程
（XML 每日更新 / 聚合刷新 / 健康监控）。因此 GUNICORN_WORKERS 必须为 1，
否则每个 worker 都会起一份线程，任务重复执行、告警邮件重复轰炸。
"""
from app import create_app
from scheduling import start_all

# 创建 Flask 应用
app = create_app()

# 启动后台调度（导入即启动，含 WSGI 部署场景）
start_all()


if __name__ == '__main__':
    # 仅直接运行时使用（开发环境）；生产用 gunicorn -c gunicorn.conf.py main:app
    print("\n启动Web API服务...")
    app.run(debug=False, host='0.0.0.0', port=5002)
