# HNTV API 服务使用说明

## 服务概述

本服务提供了一个封装的API接口，用于获取HNTV（河南电视台）的直播节目信息。服务包含认证机制，使用令牌进行访问控制。

## 环境配置

1. 在 `.env` 文件中配置API令牌：
   ```
   API_TOKEN=your-secure-api-token-here
   ```

2. 安装依赖：
   ```
   pip install -r requirements.txt
   ```

## 启动服务

```bash
python main.py
```

服务将在 `http://127.0.0.1:5000` 启动。

## API端点

### 1. 生成签名端点
- **URL**: `/api/generate-sign`
- **方法**: GET
- **认证**: 需要API令牌
- **功能**: 生成当前时间戳和基于"6ca114a836ac7d73"字符串的SHA256签名

#### 请求示例:
```bash
curl -H "Authorization: Bearer your-api-token" http://127.0.0.1:5000/api/generate-sign
```

#### 响应格式:
```json
{
  "timestamp": "1766972944",
  "sign": "9a4c1636d9352df6fbdbbd801ed93c961efe0cdafa9ab1748deed6e91d94698a"
}
```

### 2. API代理端点
- **URL**: `/api/proxy`
- **方法**: GET
- **认证**: 需要API令牌
- **功能**: 代理请求到HNTV API并返回节目信息

#### 请求示例:
```bash
curl -H "Authorization: Bearer your-api-token" http://127.0.0.1:5000/api/proxy
```

#### 响应格式:
```json
{
  "status": "success",
  "data": "...",  // 原始API响应数据
  "status_code": 200
}
```

### 3. 健康检查端点
- **URL**: `/health`
- **方法**: GET
- **认证**: 无需认证
- **功能**: 检查服务是否正常运行

#### 请求示例:
```bash
curl http://127.0.0.1:5000/health
```

#### 响应格式:
```json
{
  "status": "healthy"
}
```

## 认证方式

API使用Bearer Token进行认证。在请求头中包含Authorization字段：

```
Authorization: Bearer your-api-token
```

或在查询参数中包含token字段：

```
?token=your-api-token
```

## 错误处理

- `401 Unauthorized`: 令牌缺失或无效
- `500 Internal Server Error`: 服务器内部错误

## 技术说明

- 使用Flask作为Web框架
- 使用requests库进行HTTP请求
- 使用hashlib库进行SHA256计算
- 使用python-dotenv管理环境变量