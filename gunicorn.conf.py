# Gunicorn configuration file
import os

# Server socket
bind = os.getenv('GUNICORN_BIND', '0.0.0.0:5002')
backlog = 2048

# Worker processes
workers = int(os.getenv('GUNICORN_WORKERS', 1))
worker_class = 'sync'
worker_connections = 1000
timeout = 120
keepalive = 5

# Restart workers after this many requests, to help prevent memory leaks
max_requests = 1000
max_requests_jitter = 100

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = 'hntv_api'

# Server mechanics
# 不使用 preload_app：main.py 在 import 时启动 daemon 线程（聚合/XML/监控），
# preload 会在 master fork worker 时复制线程，导致锁死/重复执行（worker 卡死 120s 超时被 SIGKILL）。
# 默认懒加载下线程只在 worker 内启动，GUNICORN_WORKERS=1 保证线程唯一。
daemon = False
pidfile = '/tmp/hntv_api.pid'
user = None
group = None
tmp_upload_dir = None

# Security
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190