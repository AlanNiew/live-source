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

# 构建Docker镜像
echo "正在构建Docker镜像..."
docker build -f ./Dockerfile.prod -t hntv-api ..

if [ $? -ne 0 ]; then
    echo "错误: Docker镜像构建失败！"
    exit 1
fi

echo "Docker镜像构建成功！"
echo ""

# 可选：构建完成后运行容器
read -p "是否立即运行容器？(y/N) " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "正在启动容器..."
    docker-compose -f ./docker-compose.prod.yml up -d
    if [ $? -eq 0 ]; then
        echo "容器启动成功！服务将在 http://localhost:5002 上可用"
    else
        echo "错误: 容器启动失败！"
        exit 1
    fi
fi

echo "构建完成！"