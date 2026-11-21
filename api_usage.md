# HNTV API 使用说明

> 面向调用方的完整端点参考（v1.0.0）。架构、缓存与坑位说明以 [AGENTS.md](AGENTS.md) 为准；
> B 站房间管理细节见 [BILIBILI_ROOMS_API.md](BILIBILI_ROOMS_API.md)，B 站实现原理见
> [BILIBILI_LIVE.md](BILIBILI_LIVE.md)，抖音/自定义流接入见 [DOUYIN_LIVE.md](DOUYIN_LIVE.md)。

## 服务概述

基于 Flask 的河南电视台（HNTV）直播 API 服务，提供：

- **多源聚合直播列表**：HNTV 官方源 + 3 个公开 m3u 源 + B 站直播 + 自定义流（外部转流）
- **EPG 节目单**：XML / gzip 两版，每日 GMT+8 02:30 刷新
- **B 站直播代理**：m3u8 主清单重写 + 分片反代（绕开 B 站 CDN 防盗链）
- **网易云音乐元数据**：搜索 / 歌单 / 播放地址 / 歌词，转发自建 ge-whale-music 接口
- **管理后台**：直播源、频道覆盖、监控、日志、运行时设置（`/admin/*` + `/api/admin/*`）

默认监听 `0.0.0.0:5002`；Docker 部署时宿主机映射为 `15002`。

## 启动与配置

```bash
# 安装依赖（清华镜像）
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 开发启动（监听 0.0.0.0:5002）
python main.py

# 生产启动（gunicorn，workers 必须为 1）
gunicorn -c gunicorn.conf.py main:app

# 跑测试
python -m unittest discover -s tests -t .
```

`.env` 常用键（完整清单见 AGENTS.md「环境变量」）：

| 键 | 说明 |
|---|---|
| `API_TOKEN` | 写操作 / 官方代理端点的 Bearer token |
| `HNTV_SECRET_KEY` | 上游 HNTV 接口签名密钥，`sign = sha256(SECRET_KEY + timestamp)` |
| `ADMIN_PASSWORD` | 管理后台登录密码；**留空则管理功能整体禁用（403）** |
| `email` / `password` | 告警收件邮箱 / 邮箱授权码（SMTP） |
| `BILIBILI_COOKIE` | 可选，B 站高清流解析用 |
| `BILIBILI_ONLY_MODE` | `true` 时只输出 B 站频道（测试用，默认 `true`）；正式使用设 `false` |
| `PUBLIC_BASE_URL` | 对外可达地址，用于生成 B 站频道代理链接 |
| `MUSIC_API_BASE` | 上游音乐接口地址，默认 `http://60.204.147.98:15001` |

## 认证

三类鉴权，按端点区分：

1. **Bearer token（`API_TOKEN`）**：`Authorization: Bearer <API_TOKEN>`，也可用查询参数 `?token=<API_TOKEN>`。
   用于 `/api/proxy`、`/api/generate-sign` 与全部写操作（B 站房间增删、歌单增删）。
2. **管理后台 session**：`/admin/*` 页面与 `/api/admin/*` 用登录 Cookie（`POST /api/admin/login`，
   密码即 `ADMIN_PASSWORD`）；未登录访问页面 302 跳登录页、访问 API 返回 401/403。
3. **无鉴权**：直播列表、EPG、B 站播放代理、音乐只读查询、`/health`。

令牌缺失或错误返回 `401 Unauthorized`（JSON 错误体由 Flask `abort` 生成）。

## 端点总览

### 直播与 EPG

| 端点 | 认证 | 说明 |
|---|---|---|
| `GET /health` | 无 | 健康检查，返回 `{"status": "healthy"}` |
| `GET /api/live.m3u8` | 无 | **多源聚合直播列表**（播放器主用，`application/x-mpegURL`，进程内缓存 10 分钟） |
| `GET /api/live.xml` | 无 | EPG 节目单 XML（读磁盘缓存，每日 02:30 刷新） |
| `GET /api/live.xml.gz` | 无 | EPG gzip 压缩版（以附件下载；文件不存在时现场生成） |
| `GET /api/proxy` | Bearer | 代理 HNTV 官方直播列表，返回 `{status, data, status_code}` |
| `GET /api/generate-sign` | Bearer | 生成上游签名，返回 `{timestamp, sign}`（调试用） |

聚合列表分组顺序固定：`河南卫视` → `央视` → `卫视` → `自定义` → `B站直播`。

### B 站直播

| 端点 | 认证 | 说明 |
|---|---|---|
| `GET /api/bilibili/<room_id>/live.m3u8` | 无 | 代理 m3u8 主清单，分片地址重写为本服务地址 |
| `GET /api/bilibili/<room_id>/seg/<path>` | 无 | 分片反代（`BILIBILI_DIRECT_SEGMENTS=false` 时播放器走这里） |
| `GET /api/bilibili/<room_id>/status` | 无 | 开播状态（实测主清单 HTTP 200 判定） |
| `GET /api/bilibili/rooms` | 无 | 列出全部 B 站频道（默认附带实时开播状态） |
| `POST /api/bilibili/rooms` | Bearer | 动态添加频道，body `{"name":"XX卫视","room_id":123456}` |
| `DELETE /api/bilibili/rooms/<room_id>` | Bearer | 删除动态添加的频道（静态配置不受影响） |

### 网易云音乐

| 端点 | 认证 | 说明 |
|---|---|---|
| `GET /api/music/playlists` | 无 | 歌单列表（静态配置 + 动态歌单合并去重） |
| `GET /api/music/playlist?id=&pageNum=&pageSize=` | 无 | 歌单详情 + 歌曲分页（`pageNum` 默认 1，`pageSize` 默认 50，上限 100） |
| `GET /api/music/search?keyword=&limit=` | 无 | 搜索歌曲（`limit` 默认 10，上限 50） |
| `GET /api/music/play?id=&level=` | 无 | 播放地址（`level` 默认 `exhigh`），返回 `{"url": ..., "level": ...}` |
| `GET /api/music/lyrics?id=` | 无 | 歌词 |
| `POST /api/music/playlists` | Bearer | 添加动态歌单，body `{"id":"歌单ID","name":"可选"}` |
| `DELETE /api/music/playlists/<playlist_id>` | Bearer | 删除动态歌单（静态配置不受影响） |

> 音乐播放地址是网易 CDN 直链（带时效签名），由客户端播放器直连，本服务不反代流。

### 管理后台（session 鉴权）

| 端点 | 说明 |
|---|---|
| `GET /admin/login` | 登录页 |
| `GET /admin` | 仪表盘（频道数 / 分组健康 / 最近日志） |
| `GET /admin/sources` | 源管理（public / bilibili / custom 增删启停、导入默认源、立即刷新） |
| `GET /admin/channels` | 频道管理（禁用 / 改分组覆盖，分页） |
| `GET /admin/monitor` | 监控（健康历史 + 流探测明细） |
| `GET /admin/logs` | 日志（级别 / 关键词过滤、分页） |
| `GET /admin/settings` | 运行时设置（刷新周期、探测参数、告警收件人等） |

管理 API（统一前缀 `/api/admin`，JSON 响应，契约见 [ADMIN_PLAN.md](ADMIN_PLAN.md)）：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/login`、`/logout` | POST | 登录 / 登出（建立、清除 session） |
| `/sources` | GET、POST | 源列表（支持 `type`、`enabled` 过滤）/ 新增源 |
| `/sources/<id>` | PUT、DELETE | 修改 / 删除源 |
| `/sources/import-defaults` | POST | 把 config 默认源落库 |
| `/sources/refresh` | POST | 立即触发后台聚合 |
| `/channels` | GET | 聚合后频道 + 覆盖状态（分页 / 搜索 / 排序） |
| `/channels/<key>` | PUT、DELETE | 设置 / 清除频道覆盖（禁用、改名、改分组） |
| `/monitor/summary`、`/monitor/history`、`/monitor/streams` | GET | 监控汇总 / 健康历史 / 流探测明细 |
| `/logs` | GET | 日志查询（级别、关键词、分页） |
| `/settings` | GET、PUT | 运行时设置读取 / 修改 |

## 请求示例

```bash
# 健康检查
curl http://127.0.0.1:5002/health

# 聚合直播列表（播放器直接使用）
curl -o live.m3u8 http://127.0.0.1:5002/api/live.m3u8

# EPG
curl -o live.xml http://127.0.0.1:5002/api/live.xml
curl -O http://127.0.0.1:5002/api/live.xml.gz

# 生成上游签名（需 token，header 或 ?token= 均可）
curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:5002/api/generate-sign

# 代理 HNTV 官方直播列表
curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:5002/api/proxy

# 动态添加 B 站频道
curl -X POST http://127.0.0.1:5002/api/bilibili/rooms \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"XX卫视","room_id":123456}'

# 音乐：搜索 / 取播放地址
curl "http://127.0.0.1:5002/api/music/search?keyword=周杰伦&limit=5"
curl "http://127.0.0.1:5002/api/music/play?id=123456"
```

## 缓存与刷新语义

| 数据 | 刷新策略 |
|---|---|
| `aggregated.m3u`（`/api/live.m3u8`） | 公开源每 6h、官方源每 2h；启动/首请求立即聚合一次；首请求窗口期降级返回官方列表 |
| `live.xml(.gz)`（`/api/live.xml`） | 每日 GMT+8 02:30 刷新；`/api/live.xml` 优先读 gz 缓存，文件缺失才现场生成 |
| B 站流地址 | 内存缓存 30 分钟（签名约 4h 过期，失败自动重解析） |
| 聚合结果 HTTP 缓存 | 进程内 10 分钟（Flask-Caching） |
| 音乐 | 直接透传上游，上游自带 redis 缓存与 100 次/分钟/IP 限流 |

改动聚合/过滤逻辑后，需删除 `xml_data/aggregated.m3u`（及 `stream_failures.json`）再重启，
否则旧结果会一直生效。

## 错误处理

| 状态码 | 含义 |
|---|---|
| `400` | 参数缺失/非法（如分页参数非整数、源类型非法） |
| `401` | token 缺失或无效 / 管理 API 未登录 |
| `403` | 未配置 `ADMIN_PASSWORD`，管理功能整体禁用 |
| `404` | 房间/歌单不存在 |
| `500` | 服务内部错误（上游异常时带错误信息） |
| `502` | 上游（音乐接口等）返回非 200 并被转换为网关错误 |

## 技术说明

- Python 3.9+、Flask 2.3.3 + Flask-Caching、Requests、python-dotenv；生产用 Gunicorn（workers=1）
- 上游 HNTV 接口鉴权：请求头 `timestamp` + `sign`，`sign = sha256(SECRET_KEY + timestamp)`
- 所有日期/定时均按 GMT+8 处理，不依赖容器时区
