#!/bin/bash

# HNTV API Docker 部署脚本：构建镜像 + 运行容器
# 用法：
#   ./build.sh          # 默认：清理容器 -> 构建 -> 运行
#   ./build.sh build    # 仅构建（不动容器）
#   ./build.sh run      # 仅清理并运行已有镜像

# 模式：all（默认）/ build / run
MODE="${1:-all}"
CONTAINER_NAME="hntv-api"
IMAGE_NAME="hntv-api"

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

# 清理旧容器（镜像保留，构建时复用旧镜像缓存层，避免重新拉取基础镜像和重装依赖）
cleanup_container() {
  echo "清理旧容器 ${CONTAINER_NAME}..."
  docker stop ${CONTAINER_NAME} 2>/dev/null || true
  docker rm ${CONTAINER_NAME} 2>/dev/null || true
  echo "旧容器已清理完成"
  echo ""
}

# 构建Docker镜像
build_image() {
  echo "正在构建Docker镜像..."
  docker build -f ./Dockerfile.prod -t ${IMAGE_NAME} ..
  if [ $? -ne 0 ]; then
    echo "错误: Docker镜像构建失败！" >&2
    exit 1
  fi
  echo "Docker镜像构建成功！"
  echo ""
}

# 运行容器
run_container() {
  echo "正在启动容器 ${CONTAINER_NAME}..."
  docker run -d \
    --name ${CONTAINER_NAME} \
    -p 15002:5002 \
    -e GUNICORN_WORKERS=1 \
    ${IMAGE_NAME}
  if [ $? -ne 0 ]; then
    echo "错误: 容器启动失败！" >&2
    exit 1
  fi
  echo ""
  echo "容器 ${CONTAINER_NAME} 已成功启动！"
  echo "访问地址: http://localhost:15002"
  echo "运行状态: $(docker ps -f name=${CONTAINER_NAME} --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}')"
}

case "$MODE" in
  build)
    build_image
    ;;
  run)
    cleanup_container
    run_container
    ;;
  all)
    cleanup_container
    build_image
    run_container
    ;;
  *)
    echo "用法: $0 [build|run|all]" >&2
    exit 1
    ;;
esac
