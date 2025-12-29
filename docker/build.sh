#!/bin/bash

# 生产环境Docker镜像构建脚本
set -e

# 镜像名称
IMAGE_NAME="hntv-api"
TAG="latest"

# 构建Docker镜像
echo "开始构建Docker镜像: $IMAGE_NAME:$TAG"
docker build -f ./docker/Dockerfile.prod -t $IMAGE_NAME:$TAG .

# 显示构建完成信息
echo "Docker镜像构建完成: $IMAGE_NAME:$TAG"
