# Docker 部署指南

本目录包含用于生产环境部署的 Docker 配置文件。

## 文件说明

- `Dockerfile.prod`: 用于生产环境的 Docker 镜像构建文件
- `build.ps1`: Windows 环境下的构建脚本
- `build.sh`: Linux/Mac 环境下的构建脚本
- `docker-compose.prod.yml`: 生产环境的 Docker Compose 配置

## 使用方法

### 方法一：使用构建脚本

#### Windows (PowerShell):
```powershell
./docker/build.ps1
```

#### Linux/Mac (Bash):
```bash
./docker/build.sh
```

### 方法二：使用 Docker Compose

```bash
docker-compose -f ./docker/docker-compose.prod.yml up -d
```

### 方法三：手动构建

```bash
docker build -f ./docker/Dockerfile.prod -t hntv-api-prod .
```

## 端口映射

应用将在容器的 8000 端口运行，并映射到主机的 8000 端口。

## 环境变量

- `ENV=production`: 设置为生产环境