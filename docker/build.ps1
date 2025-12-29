# 生产环境Docker镜像构建脚本 (PowerShell版本)
$ErrorActionPreference = "Stop"

# 镜像名称
$IMAGE_NAME = "hntv-api-prod"
$TAG = "latest"

# 构建Docker镜像
Write-Host "开始构建Docker镜像: $IMAGE_NAME`:$TAG"
docker build -f ./docker/Dockerfile.prod -t "$IMAGE_NAME`:$TAG" .

# 检查构建是否成功
if ($LASTEXITCODE -eq 0) {
    Write-Host "Docker镜像构建完成: $IMAGE_NAME`:$TAG" -ForegroundColor Green
    
    # 询问是否运行容器进行测试
    $response = Read-Host "是否要运行容器进行测试? (y/n)"
    if ($response -match "^y$|^Y$") {
        Write-Host "启动容器..."
        docker run -d -p 5002:5002 --name hntv-api-container "$IMAGE_NAME`:$TAG"
        Write-Host "容器已启动，应用将在 http://localhost:5002 可用" -ForegroundColor Green
    }
} else {
    Write-Host "Docker镜像构建失败" -ForegroundColor Red
    exit 1
}