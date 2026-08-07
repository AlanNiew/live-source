# AGENTS.md

HNTV（河南电视台）直播 API 服务：Flask 封装 HNTV 官方接口 + 多源 m3u 聚合 + EPG XML + 健康监控邮件告警。全部代码为中文注释，交互与文档用中文。

## 常用命令

- 启动（开发）：`python main.py` —— 监听 `0.0.0.0:5002`
- 生产：`gunicorn -c gunicorn.conf.py main:app`
- 依赖安装走清华镜像：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`
- 无测试框架、无 lint/format 配置。改完手动验证：起服务后 curl `/health`、`/api/live.m3u8`、`/api/live.xml`、`/api/live.xml.gz`

## 环境变量（.env）

键：`API_TOKEN`、`HNTV_SECRET_KEY`、`email`、`password`（QQ/163 邮箱授权码）。`.env` 已被 git 跟踪但含密钥，**不要 commit 或打印其内容**。

- `API_TOKEN` / `HNTV_SECRET_KEY` 在 `utils.py` 有弱默认值兜底（`hntv-secret-token-2025` / `6ca114a836ac7d73`），生产必须显式覆盖
- `main.py:34` 的 `calculate_sha256_with_timestamp` 也硬编码了同名默认密钥，改密钥需同步改这里
- 上游 HNTV API 鉴权 = `timestamp` + `sign` 两个请求头，`sign = sha256(SECRET_KEY + timestamp)`（见 `utils.py:ApiUtils`）

## 架构与运行关键点

- `main.py` 路由：`/api/proxy`、`/api/generate-sign`（需 Bearer token）、`/api/live.m3u8`、`/api/live.xml`、`/api/live.xml.gz`、`/health`
- **调度线程在导入时启动，不在 `__main__` 才启动**：`main.py:226` 的 `else` 分支会被 gunicorn 导入触发，每个模块 import 都会起线程。因此 **`GUNICORN_WORKERS` 必须为 1**（`gunicorn.conf.py` 默认已是 1），否则 XML 更新/聚合刷新/健康检测会重复执行、告警邮件重复轰炸
- 三个后台 daemon 线程：`SchedulerUtils.schedule_daily_xml_update`（每天 GMT+8 02:30 刷 XML）、`AggregatorScheduler.schedule_aggregate_refresh`（每 6h 刷聚合）、`MonitorScheduler.schedule_monitor`（每 60s 检测 + 状态翻转才发邮件）

## 磁盘缓存（改逻辑后必须清缓存验证）

`xml_data/` 目录，全部 `os.makedirs(... exist_ok=True)` 自动创建：

- `live.xml` / `live.xml.gz`：EPG 数据，**每天仅 02:30 刷新一次**。`/api/live.xml` 直接读缓存文件（优先 gz）；`/api/live.xml.gz` 文件不存在才现场生成
- `aggregated.m3u`：多源聚合结果（hntv 官方 + 2 个公开源），每 6h 刷新；`/api/live.m3u8` 读它，无缓存才触发首次生成
- **改了聚合/过滤逻辑后，手动删除 `xml_data/aggregated.m3u` 再重启**，否则旧结果一直生效（首次请求会重新生成）

## 聚合逻辑（aggregator.py）

- `PUBLIC_M3U_SOURCES` 两个公开源；`filter_and_translate` 只保留央视开路频道（`CCTV_NAME_MAP`）+ 含"卫视"的频道，其余全过滤
- 同名多来源按地址质量择优：域名 > 公网 IP > 疑似运营商内网 IP（`score_url` / `CARRIER_IP_PREFIXES`），再比分辨率
- hntv 官方频道优先级最高，公开源只补充官方没有的频道

## 监控告警（monitor.py）

- 默认自检地址写死 `localhost:15002`（对应 `docker/build.sh` 运行容器时的 `15002:5002` 映射）；若用 `docker-compose.prod.yml`（映射 `5002:5002`）则检测不到自身，需设 `MONITOR_HEALTH_URL` 等环境变量覆盖
- `MIN_CHANNEL_COUNT=30`：正常聚合约 55 频道，若公开源全挂只剩 hntv 约 15，会触发告警
- 邮件模板在 `templates/email_alert.html`（占位符填充，`_build_html`）

## 已知坑

- 项目内 `email/` 目录与 Python 标准库 `email` 同名冲突。`monitor.py:send_alert` 用 `importlib.util.spec_from_file_location` 从绝对路径加载 `send_assistant.py`；**新代码不要 `import email` 或 `from email...`**，照抄 monitor 的加载方式
- 所有日期/定时/EPG 时间戳均按 **GMT+8** 处理（`utils.py:GMT8`），不要用本地时区
- `git status` 显示 `.env`、`__pycache__/`、`xml_data/` 被改动/未跟踪：`.gitignore` 只有 `/.idea/`，**不要把缓存与密钥提交**
- `api_usage.md` 与 README 部分过时（如端口、workers 数），以代码为准
