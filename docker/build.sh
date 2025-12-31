#!/bin/bash

# 构建Docker镜像的脚本

echo "开始构建HNTV API Docker镜像..."
echo ""

# 检查Docker是否安装
if ! [ -x "$(command -v docker)" ]; then
  echo "错误: Docker未安装或未在PATH中找到。请先安装Docker。" >&2
  exit 1
fi

# 检查Docker是否正在运行
if ! docker info > /dev/null 2>&1; then
  echo "错误: Docker服务未运行。请启动Docker Desktop或Docker服务。" >&2
  exit 1
fi

# 清理旧的镜像和容器
echo "清理旧的hntv-api容器和镜像..."

# 停止并删除正在运行的容器
docker stop hntv-api 2>/dev/null || true
docker rm hntv-api 2>/dev/null || true

# 删除旧的镜像
docker rmi hntv-api 2>/dev/null || true

echo "旧的容器和镜像已清理完成"
echo ""

# 构建Docker镜像
echo "正在构建Docker镜像..."
docker build -f ./Dockerfile.prod -t hntv-api ..

if [ $? -ne 0 ]; then
    echo "错误: Docker镜像构建失败！"
    exit 1
fi

echo "Docker镜像构建成功！"
echo ""


echo "构建完成！"