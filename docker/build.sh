#!/bin/bash

# 生产环境Docker镜像构建脚本
set -e

# 镜像名称
IMAGE_NAME="hntv-api-prod"
TAG="latest"

# 构建Docker镜像
echo "开始构建Docker镜像: $IMAGE_NAME:$TAG"
docker build -f ./docker/Dockerfile.prod -t $IMAGE_NAME:$TAG .

# 显示构建完成信息
echo "Docker镜像构建完成: $IMAGE_NAME:$TAG"

# 可选：运行容器进行测试
echo "是否要运行容器进行测试? (y/n)"
read -r response
if [[ "$response" =~ ^[Yy]$ ]]; then
    echo "启动容器..."
    docker run -d -p 5002:5002 --name hntv-api-container $IMAGE_NAME:$TAG
    echo "容器已启动，应用将在 http://localhost:5002 可用"
fi