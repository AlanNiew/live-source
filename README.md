# HNTV API

基于 Flask 的河南电视台（HNTV）直播 API 服务：封装 HNTV 官方接口 + 多源 m3u 聚合 + EPG 节目单 + 健康监控邮件告警。面向家庭电视场景，为播放器（盒子/App）提供稳定的直播列表与节目单。

## 功能特性

- **HNTV 官方接口封装**：带签名鉴权（`timestamp` + `sign`）的直播列表 / EPG 数据代理
- **多源 m3u 聚合**：HNTV 官方 + 3 个公开源（iptv-org / hujingguang / wwb521），按地址质量择优去重，分组输出（河南卫视 → 央视 → 卫视）
- **B 站直播接入**：配置 `BILIBILI_ROOMS` 的 UP 主 uid，开播判定后以代理地址加入列表（B 站 CDN 防盗链校验 Referer，播放器直连 403，必须经本服务 m3u8 重写 + 分片反代）
- **可达性探测过滤**：聚合时对公开源频道做探测，连续两轮不可达才丢弃，保证列表里都是可用源
- **EPG 节目单**：每日自动生成 XML / gzip 压缩版，供播放器回看/节目信息
- **健康监控告警**：定期检测服务存活/频道数/EPG/流地址可达性，异常时邮件告警（状态翻转才发，不轰炸）
- **定时任务**：EPG 每日 02:30（GMT+8）刷新、聚合每 6 小时刷新、健康检测 10 分钟一轮

## 项目结构

```
.
├── main.py               # 入口：创建 Flask 应用 + 启动后台调度（import 即启动）
├── app.py                # Flask 工厂与全部路由
├── config.py             # 全部常量/环境变量集中（token/密钥/路径/阈值/源列表/时区）
├── scheduling.py         # 三个 daemon 线程统一入口 start_all()
├── core/                 # 业务核心（零 Flask 依赖）
│   ├── hntv_client.py    # HNTV 官方 API 鉴权与请求
│   ├── bilibili.py      # B 站直播：房间解析/流解析/m3u8 重写/分片反代
│   ├── epg.py            # EPG XML 生成/读取/时间格式化
│   ├── sources.py        # 公开源拉取/解析/评分/过滤中文化
│   ├── aggregator.py     # 聚合编排/探测过滤/缓存/降级
│   └── probing.py        # 流地址可达性探测（单实现，严格/宽松口径）
├── monitoring/           # 健康监控与告警
│   ├── checks.py         # 检测项 + 两套状态机（常规/流探测）
│   ├── alerts.py         # 邮件 HTML 构建与发送
│   └── scheduler.py      # 检测循环 + 时段窗口（GMT+8 8:00-24:00）
├── email/                # 邮件发送模块（send_assistant.py）
├── templates/            # 邮件告警 HTML 模板
├── docker/               # Docker 构建与部署脚本
├── tests/                # unittest 测试（标准库，无外部依赖）
└── xml_data/             # 磁盘缓存（运行时自动生成，勿提交）
```

## 技术栈

- Python 3.9+
- Flask 2.3.3 + Flask-Caching
- Gunicorn 21.2.0（生产）
- Requests、python-dotenv

## 环境配置

### 1. 创建并配置 `.env`

```bash
API_TOKEN=你的API令牌
HNTV_SECRET_KEY=你的签名密钥
email=你的邮箱（用于接收告警）
password=邮箱授权码（QQ/163 在邮箱设置中获取）
```

**`.env` 格式要求**：`KEY=VALUE` —— 键名后无空格、值不带引号、无行内注释，否则 docker `--env-file` 解析失败或值带引号导致 SMTP 认证失败。

> 注意：`.env` 已被 git 移除跟踪并加入 `.gitignore`（历史中已清除），修改不会产生 git 冲突；**不要提交或打印其内容**，密钥变更需同步服务器 `.env`。

### 2. B 站直播频道（可选，默认已含央视新闻/河南卫视/中国应急管理）

在 `config.py` 的 `BILIBILI_ROOMS` 里新增一行即可：

```python
BILIBILI_ROOMS = [
    {"name": "央视新闻", "uid": 222103174},       # 默认频道
    {"name": "河南卫视", "uid": 2057655323},       # 默认频道
    {"name": "中国应急管理", "uid": 3707002299615617},  # 默认频道
    {"name": "你想要的频道", "uid": 123456789},    # 手动新增：频道名 + UP 主 UID
]
```

**UID 获取方法**（二选一）：
- 打开目标直播间，用 `curl "https://api.live.bilibili.com/room/v1/Room/room_init?id=<房间号>"`，响应里 `uid` 字段即 UP 主 UID（房间号取直播间网址 `live.bilibili.com/<房间号>` 的数字）
- 或直接打开 UP 主空间主页 `space.bilibili.com/<UID>`，地址栏的数字就是 UID

新增后删除 `xml_data/aggregated.m3u` 再重启，等下一次聚合（或手动触发）生效。

### 3. 安装依赖（清华镜像加速）

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

## 运行

### 本地开发

```bash
python main.py        # 监听 0.0.0.0:5002
```

### 生产（Gunicorn）

```bash
gunicorn -c gunicorn.conf.py main:app
```

### Docker 部署

```bash
./docker/build.sh          # 清理容器 -> 构建（保留镜像缓存）-> 启动
./docker/build.sh build    # 仅构建
./docker/build.sh run      # 仅重启容器
```

从任意目录执行均可（脚本自动定位自身位置）。服务入口端口 `15002`，容器内 `5002`。

## API 端点

| 端点 | 认证 | 说明 |
|---|---|---|
| `GET /health` | 无 | 健康检查 |
| `GET /api/live.m3u8` | 无 | 多源聚合直播列表（播放器主用） |
| `GET /api/live.xml` | 无 | EPG 节目单 XML |
| `GET /api/live.xml.gz` | 无 | EPG 节目单 gzip 压缩版 |
| `GET /api/proxy` | Bearer token | 代理 HNTV 官方直播列表 |
| `GET /api/generate-sign` | Bearer token | 生成上游签名（调试用） |
| `GET /api/bilibili/<room_id>/live.m3u8` | 无 | B 站直播代理 m3u8（分片重写为本服务地址） |
| `GET /api/bilibili/<room_id>/seg/<path>` | 无 | B 站直播分片反代（注入 Referer/UA 转拉） |
| `GET /api/bilibili/<room_id>/status` | 无 | B 站直播开播状态（实测主清单判定） |

## 测试

```bash
python -m unittest discover tests
```

标准库 unittest，无外部依赖；探测类测试起本地 HTTP 服务模拟，不触碰真实直播源。

## 架构要点

- **调度线程在导入时启动**（`main.py` 顶层调 `scheduling.start_all()`），因此 **`GUNICORN_WORKERS` 必须为 1**，且 **`gunicorn.conf.py` 不要开 `preload_app`**（会复制 daemon 线程导致 worker 卡死）
- **磁盘缓存**（`xml_data/`）：`live.xml(.gz)` 每天 02:30 刷新、`aggregated.m3u` 每 6h 刷新、`stream_failures.json` 探测失败跨轮记录。改动聚合逻辑后删除 `aggregated.m3u` 再重启验证
- **监控告警**：常规检测 10 分钟一轮 + 流探测 30 分钟一轮，仅 GMT+8 8:00-24:00 执行；分组分级阈值（河南卫视 90% / 央视 80% / 卫视 20%），卫视不达标仅日志展示；状态翻转才发邮件
- **所有时间按 GMT+8** 处理，不依赖容器时区

## 注意事项

- 上游 HNTV 接口鉴权 = `timestamp` + `sign` 请求头，`sign = sha256(SECRET_KEY + timestamp)`
- 项目内 `email/` 目录与标准库 `email` 同名冲突，新代码请勿 `import email`
- 免费公开卫视源可达率天然较低（约 20-40%），聚合已做探测过滤与分组排序优化；央视与河南卫视官方源保持稳定
- **B 站直播**：接口非官方可能随时改版；未开播房间 `playUrl` 也会返回地址，以实测拉取 m3u8 主清单 200 判定在播；`PUBLIC_BASE_URL` 环境变量需指向播放器可达的地址（容器部署必须覆盖为宿主机映射地址），否则列表里的 B 站频道对播放器不可用
