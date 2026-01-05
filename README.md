# HNTV API

基于Flask的直播电视API服务，提供邮件助手功能和定时任务处理。

## 项目结构

```
.
├── docker/                 # Docker相关配置
│   ├── build.sh           # 构建脚本
│   ├── run.sh             # 运行脚本
│   └── docker-compose.prod.yml # 生产环境编排文件
├── email/                 # 邮件助手模块
│   ├── email_assistant.py # 邮件助手主逻辑
│   └── send_assistant.py  # 邮件发送助手
├── main.py               # 主应用入口
├── utils.py              # 工具函数
├── gunicorn.conf.py      # Gunicorn配置
└── requirements.txt      # 依赖包列表
```

## 技术栈

- Python 3.x
- Flask == 2.3.3
- Gunicorn == 21.2.0
- Requests == 2.32.5
- Flask-Caching == 2.1.0
- python-dotenv == 1.2.1

## 环境配置

1. 创建虚拟环境：

```bash
python -m venv venv
```

2. 激活虚拟环境：

```bash
# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

3. 安装依赖（使用清华源加速）：

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

## 运行项目

### 本地运行

```bash
python main.py
```

### 生产环境运行（使用Gunicorn）

```bash
gunicorn -c gunicorn.conf.py main:app
```

### Docker运行

```bash
# 后台运行服务
docker-compose -f docker/docker-compose.prod.yml up -d
```

## 缓存配置

- 默认缓存过期时间：10分钟
- 使用Flask-Caching进行缓存管理

## 注意事项

- 生产环境中Gunicorn建议启用单个进程以避免定时任务重复执行
- 对于基本不变的XML数据，项目会每天定时获取并保存为本地文件，响应时直接返回该文件
- 建议使用压缩传输以减少网络带宽消耗