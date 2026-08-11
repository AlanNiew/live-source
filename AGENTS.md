# AGENTS.md

HNTV（河南电视台）直播 API 服务：Flask 封装 HNTV 官方接口 + 多源 m3u 聚合 + EPG XML + 健康监控邮件告警。全部代码为中文注释，交互与文档用中文。

## 常用命令

- 启动（开发）：`python main.py` —— 监听 `0.0.0.0:5002`
- 生产：`gunicorn -c gunicorn.conf.py main:app`
- 测试：`python -m unittest discover tests`（标准库 unittest，无外部依赖；探测类测试起本地 HTTP 服务，不碰真实直播源）
- 依赖安装走清华镜像：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`
- 无 lint/format 配置。改完验证：跑 unittest + 起服务 curl `/health`、`/api/live.m3u8`、`/api/live.xml`、`/api/live.xml.gz`

## 项目结构（模块职责）

- `main.py`：入口薄层（`create_app()` + `scheduling.start_all()`）；`app.py`：Flask 工厂与全部路由
- `config.py`：**全部常量/环境变量集中**（token/密钥/路径/阈值/源列表/时区），`load_dotenv` 唯一入口
- `core/`（业务核心，零 Flask 依赖）：`hntv_client.py`（HNTV 官方 API 鉴权与请求）、`epg.py`（EPG XML）、`sources.py`（公开源拉取/解析/评分/过滤）、`aggregator.py`（聚合编排/探测过滤/缓存/降级）、`probing.py`（流探测单实现）
- `monitoring/`：`checks.py`（检测项+两套状态机）、`alerts.py`（邮件 HTML+发送）、`scheduler.py`（检测循环+时段窗口）
- `scheduling.py`：三个 daemon 线程统一入口 `start_all()`
- `tests/`：unittest（探测判据/两轮淘汰/状态机/时段窗口）

## 环境变量（.env）

键：`API_TOKEN`、`HNTV_SECRET_KEY`、`email`、`password`（QQ/163 邮箱授权码）。`.env` 已被 git 移除跟踪（历史中已清除，见 `.env.example` 打码模板），**不要提交或打印其内容**。

- `API_TOKEN` / `HNTV_SECRET_KEY` 在 `config.py` 有弱默认值兜底（`hntv-secret-token-2025` / `6ca114a836ac7d73`），生产必须显式覆盖
- 上游 HNTV API 鉴权 = `timestamp` + `sign` 两个请求头，`sign = sha256(SECRET_KEY + timestamp)`（`core/hntv_client.py:CryptoUtils`）
- 容器部署：`.dockerignore` 排除 `.env`（密钥不进镜像），`docker/build.sh` 用 `--env-file` 注入；**格式必须 `KEY=VALUE`（键名后无空格、值不带引号、无行内注释）**，否则 docker 解析失败或值带引号导致 SMTP 认证失败

## 架构与运行关键点

- `app.py` 路由：`/api/proxy`、`/api/generate-sign`（需 Bearer token）、`/api/live.m3u8`、`/api/live.xml`、`/api/live.xml.gz`、`/health`
- **调度线程在导入时启动**：`main.py` 模块顶层调 `start_all()`，gunicorn 导入 `main:app` 即触发。因此 **`GUNICORN_WORKERS` 必须为 1**（`gunicorn.conf.py` 默认已是 1），否则 XML 更新/聚合刷新/健康检测重复执行、告警邮件重复轰炸
- **`gunicorn.conf.py` 不要开 `preload_app`**：preload 会在 master fork worker 时复制 daemon 线程，导致 worker 锁死（120s 超时被 SIGKILL，健康检测全超时）——踩过的坑，见 commit fd5d9ca
- 三个后台 daemon 线程（`scheduling.py:start_all`）：XML 每日更新（每天 GMT+8 02:30 刷 EPG）、聚合刷新（每 6h 刷聚合，启动立即首刷）、健康监控（常规 10 分钟一轮 + 流探测 30 分钟一轮，状态翻转才发邮件）

## 磁盘缓存（改逻辑后必须清缓存验证）

`xml_data/` 目录，全部 `os.makedirs(... exist_ok=True)` 自动创建：

- `live.xml` / `live.xml.gz`：EPG 数据，**每天仅 02:30 刷新一次**。`/api/live.xml` 直接读缓存文件（优先 gz）；`/api/live.xml.gz` 文件不存在才现场生成
- `aggregated.m3u`：多源聚合结果（hntv 官方 + 3 个公开源），每 6h 刷新；`/api/live.m3u8` 读它，无缓存才触发首次生成
- `stream_failures.json`：聚合探测的跨轮失败记录（连续两轮失败才丢弃频道）
- **改了聚合/过滤逻辑后，手动删除 `xml_data/aggregated.m3u`（和 `stream_failures.json`）再重启**，否则旧结果一直生效

## 聚合逻辑（core/sources.py + core/aggregator.py）

- `PUBLIC_M3U_SOURCES` 三个公开源（iptv-org / hujingguang / wwb521-jsdelivr）；`filter_and_translate` 只保留央视开路频道（`CCTV_NAME_MAP`）+ 含"卫视"的频道，其余全过滤
- 同名多来源按地址质量择优：无签名域名(3) > 公网IP/签名域名(2) > 疑似运营商内网 IP(1)（`score_url` / `CARRIER_IP_PREFIXES`），再比分辨率；`112./120./218.` 前缀含公网可达 CDN，已从内网前缀剔除
- hntv 官方频道优先级最高，公开源只补充官方没有的频道；分组输出顺序固定 河南卫视 -> 央视 -> 卫视
- **聚合时探测过滤**：公开源补充频道宽松探测（200/206/403 可达），连续两轮失败才丢弃；hntv 官方源跳过永不探测；失败记录跨轮持久化

## 监控告警（monitoring/）

- 默认自检地址 `localhost:5002`（monitor 跑在容器内部，用 gunicorn 监听端口；宿主机映射端口 `15002` 在容器内连不上）。若未来加 nginx 反代，需设 `MONITOR_HEALTH_URL` 等环境变量指向反代入口
- `MIN_CHANNEL_COUNT=30`：正常聚合约 70 频道，若公开源全挂只剩 hntv 约 15，会触发告警
- 常规检测 10 分钟一轮（health/m3u 列表/epg）；**低频全量流探测**每 30 分钟并发探测列表里所有流（GET+Range 读少量字节，200/206 算可达），独立状态机
- **分组分级阈值**：河南卫视 90% / 央视 80% / 卫视 20%（`GROUP_HEALTH_RATIOS`）；邮件告警只看重要组（`ALERT_GROUPS`），卫视不达标仅日志展示；聚合列表拉取失败视为系统性故障仍告警
- **检测时段**：仅 GMT+8 8:00-24:00 检测，0:00-7:59 两个循环都休眠到下一个 8:00（`_window_wait_seconds`，按 GMT+8 判断，不依赖容器时区）
- 邮件模板在 `templates/email_alert.html`（占位符填充，`monitoring/alerts.py:build_html`）

## 已知坑

- 项目内 `email/` 目录与 Python 标准库 `email` 同名冲突。`monitoring/alerts.py:send_alert` 用 `importlib.util.spec_from_file_location` 从绝对路径加载 `send_assistant.py`；**新代码不要 `import email` 或 `from email...`**，照抄 alerts.py 的加载方式
- 所有日期/定时/EPG 时间戳均按 **GMT+8** 处理（`config.py:GMT8`），不要用本地时区
- `git status` 显示 `xml_data/` 未跟踪：`.gitignore` 已排除 `.env`、`__pycache__/`、`*.pyc`、`/.idea/`，**不要把缓存与密钥提交**
- 容器内无 `.env` 文件是正常的（`.dockerignore` 排除 + `--env-file` 注入），进容器排查用 `env | cut -d= -f1` 看键名
- `api_usage.md` 与 README 部分过时（如端口、workers 数、旧文件名），以代码为准
