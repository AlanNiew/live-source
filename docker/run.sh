#!/bin/bash

# 运行HNTV API Docker容器的脚本

echo "开始运行HNTV API容器..."
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

# 设置容器名称
CONTAINER_NAME="hntv-api"

# 检查容器是否已在运行
if [ "$(docker ps -q -f name=^/${CONTAINER_NAME}$)" ]; then
    echo "容器 ${CONTAINER_NAME} 正在运行，正在停止..."
    docker stop ${CONTAINER_NAME}
fi

# 检查是否存在已停止的容器并删除
if [ "$(docker ps -aq -f status=exited -f name=^/${CONTAINER_NAME}$)" ]; then
    echo "删除已存在的容器 ${CONTAINER_NAME}..."
    docker rm ${CONTAINER_NAME}
fi

# 运行容器
echo "正在启动容器 ${CONTAINER_NAME}..."
docker run -d \
  --name ${CONTAINER_NAME} \
  -p 15002:5002 \
  -e GUNICORN_WORKERS=1 \
  hntv-api

if [ $? -ne 0 ]; then
    echo "错误: 容器启动失败！"
    exit 1
fi

echo ""
echo "容器 ${CONTAINER_NAME} 已成功启动！"
echo "访问地址: http://localhost:15002"
echo "运行状态: $(docker ps -f name=${CONTAINER_NAME} --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}')"