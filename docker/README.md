# HNTV API - Docker 部署指南

本指南说明使用 Docker 部署 HNTV API 服务（Gunicorn + Flask）。

## 系统要求

- Docker
- Docker Compose（可选，使用 `docker-compose.prod.yml` 时）

## 部署方式

### 一键部署（推荐）

```bash
./docker/build.sh              # 清理容器 -> 构建 -> 启动（任意目录执行）
./docker/build.sh build        # 仅构建镜像
./docker/build.sh run          # 仅清理并重启容器
```

- 构建时**保留旧镜像**（复用缓存层，避免重复拉取基础镜像/依赖）
- 启动容器时自动注入 `.env`（`--env-file`），密钥不进镜像
- 容器时区已设为 `Asia/Shanghai`，日志显示上海本地时间

### Docker Compose 部署

```bash
docker compose -f docker/docker-compose.prod.yml up -d --build
```

## 配置说明

- 服务端口：容器内 `5002`，宿主机映射 `15002`（浏览器/播放器访问 `http://服务器IP:15002`）
- 工作进程数：`GUNICORN_WORKERS=1`（必须为 1，调度线程在 import 时启动，多 worker 会重复执行定时任务）
- 环境变量：由 `--env-file` 从项目根目录 `.env` 注入（`API_TOKEN`/`HNTV_SECRET_KEY`/`email`/`password`）
- 自动重启：容器异常退出后自动重启（`restart: unless-stopped`）

## 访问服务

- API 服务：`http://localhost:15002`
- 健康检查：`http://localhost:15002/health`
- 直播列表：`http://localhost:15002/api/live.m3u8`
- EPG 节目单：`http://localhost:15002/api/live.xml.gz`

## 日志

```bash
docker logs -f hntv-api
```

## 常见问题

- **`invalid env file ... contains whitespaces`**：`.env` 格式错误，要求 `KEY=VALUE`（键名后无空格、值不带引号、无行内注释）
- **容器内没有 `.env` 文件**：正常，`.dockerignore` 有意排除（密钥不进镜像），环境变量由 `--env-file` 运行时注入，验证用 `docker exec hntv-api env | cut -d= -f1`
- **健康检测报 Connection refused/超时**：monitor 在容器内自检 `localhost:5002`（gunicorn 监听端口），宿主机映射的 `15002` 在容器内不可用，属正常设计
