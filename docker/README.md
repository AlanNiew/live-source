# HNTV API - Docker 部署指南

本项目使用 Docker 和 Gunicorn 在生产环境中部署 Flask 应用，以解决开发服务器警告问题。

## 架构说明

- **应用服务器**: Gunicorn (生产级 WSGI 服务器)
- **容器化**: Docker
- **环境**: 生产环境

## 系统要求

- Docker
- Docker Compose

## 部署方式

### Linux/macOS 系统 (或 WSL)

```bash
# 进入 docker 目录
cd docker

# 构建并运行
./build.sh
```

### 手动部署

```bash
# 构建镜像
docker build -f Dockerfile.prod -t hntv-api ..

# 启动容器
docker-compose -f docker-compose.prod.yml up -d
```

## 配置说明

- 端口: `5002`
- 工作进程数: `4` (可通过环境变量 `GUNICORN_WORKERS` 调整)
- 超时时间: `120` 秒
- 自动重启: 容器异常退出后自动重启

## 访问服务

服务启动后，可通过以下地址访问:

- API 服务: `http://localhost:5002`
- 健康检查: `http://localhost:5002/health`

## Gunicorn 配置

Gunicorn 服务器配置文件位于项目根目录的 `gunicorn.conf.py`，可根据需要调整以下参数：

- `workers`: 工作进程数
- `timeout`: 请求超时时间
- `max_requests`: 工作进程重启请求数
- `loglevel`: 日志级别

## 环境变量

可在 `docker-compose.prod.yml` 中添加环境变量:

```yaml
services:
  api:
    # ... 其他配置
    environment:
      - ENV=production
      - GUNICORN_WORKERS=4  # 自定义工作进程数
```

## 日志

应用日志会输出到控制台，可通过以下命令查看:

```bash
docker logs hntv-api
```