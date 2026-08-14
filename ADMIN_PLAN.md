# 系统维护/管理功能 —— 开发计划（feature/admin-console）

> 目标：Web 管理后台，维护直播源、监控视频源状态、查看日志，
> 动态切换/接入直播源，无需改配置/代码重新发布。
> 决策已定：前端 Flask 模板（方案 A）、公开源 DB 化、单容器同端口 /admin。

---

## 总体架构

```
┌─────────────────────────────────────────────────────┐
│  Flask 单容器（docker 不变）                          │
│                                                     │
│  /admin/* 页面（Jinja2 + Bootstrap CDN + 原生 fetch） │
│      ↓                                              │
│  /api/admin/* 管理 API（session 鉴权，JSON）          │
│      ↓                                              │
│  admin/db.py（SQLite 单文件 xml_data/admin.db）       │
│      ↓                                              │
│  现有核心：聚合（sources/aggregator/bilibili）         │
│           监控（checks/alerts/scheduler）             │
└─────────────────────────────────────────────────────┘
```

**不改动**：聚合链路核心逻辑、监控告警逻辑、播放器端点（`/api/live.m3u8` 等）。
全部只做"数据源替换（DB 优先 config 兜底）+ 结果落库"，播放端零感知。

---

## 数据层（admin/db.py）

SQLite 单文件 `xml_data/admin.db`，模块零依赖（只 import sqlite3/os），
被 core 单向引用避免循环导入。

### 表结构

```sql
-- 直播源配置（公开源 + B站房间，替代 config 静态列表）
CREATE TABLE sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,              -- 'public' | 'bilibili'
  url TEXT,                        -- public 源的 m3u 地址（bilibili 为空）
  name TEXT NOT NULL,              -- 源名称/频道名
  enabled INTEGER DEFAULT 1,       -- 1启用 0禁用
  sort_order INTEGER DEFAULT 0,    -- 排序
  created_at TEXT, updated_at TEXT
);

-- 频道级覆盖（禁用/改分组/改名，key=normalize_name 后的频道名）
CREATE TABLE channel_overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_key TEXT UNIQUE NOT NULL,
  display_name TEXT,               -- 可选：覆盖显示名
  group_title TEXT,                -- 可选：覆盖分组
  enabled INTEGER DEFAULT 1,       -- 0=禁用（聚合时跳过）
  updated_at TEXT
);

-- 常规健康检测历史（每 10min 一轮）
CREATE TABLE monitor_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                -- GMT+8 时间
  health_ok INTEGER, m3u_ok INTEGER, epg_ok INTEGER,
  channel_count INTEGER, epg_size INTEGER,
  overall INTEGER                  -- 1=OK 0=FAIL
);

-- 流探测历史（每 30min 一轮，每频道一条）
CREATE TABLE stream_check_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  group_name TEXT, channel_name TEXT, url TEXT,
  ok INTEGER,                      -- 1可达 0不可达
  round_id TEXT                    -- 同轮探测分组标识
);

-- 日志（WARNING+ 才入库防膨胀）
CREATE TABLE logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, level TEXT,    -- INFO/WARNING/ERROR
  module TEXT, message TEXT
);

-- 系统设置（模式切换等）
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
```

### 关键设计

- **sources 表替代 `config.PUBLIC_M3U_SOURCES` 和 `config.BILIBILI_ROOMS`**，
  config 保留为"首次启动种子值/兜底"：DB 空表时回退 config（向后兼容，现有测试锁行为）
- **channel_overrides 是覆盖层**：不动聚合择优逻辑，只在输出前应用（禁用/改分组/改名），侵入最小
- 清理策略：monitor_history 保留最近 500 轮；stream_check_history 保留最近 20000 条
  （约 6 天；30 分钟一轮×约 70 频道≈3400 条/天，每轮探测整轮批量落库后只清理一次）；
  logs 保留最近 7 天（GMT+8 阈值，每天至多清理一次）
- 并发写：模块级 `threading.Lock` 串行写；每操作短连接（`check_same_thread=False` + 每连接独立）

---

## 管理 API（/api/admin/*，全部需 session 登录）

```
# 认证
POST /api/admin/login          {password} → set session
POST /api/admin/logout

# 源管理
GET    /api/admin/sources                  全部源（public+bilibili）
POST   /api/admin/sources                  {type, url?, name} 新增
PUT    /api/admin/sources/<id>             {name?, url?, enabled?, sort_order?}
DELETE /api/admin/sources/<id>
POST   /api/admin/sources/refresh          → request_async_refresh() 立即聚合
POST   /api/admin/sources/import-defaults  → 把 config 兜底源落库（幂等；uid 解析失败跳过）

# 频道覆盖
GET    /api/admin/channels                 聚合后频道 + 覆盖状态（分页/搜索）
PUT    /api/admin/channels/<key>           {enabled?, group_title?, display_name?}
DELETE /api/admin/channels/<key>           删除覆盖恢复默认

# 监控
GET /api/admin/monitor/summary             当前健康/频道数/最近轮次
GET /api/admin/monitor/history             健康趋势
GET /api/admin/monitor/streams             流探测明细

# 日志
GET /api/admin/logs                        ?level=&q=&page=

# 设置
GET /api/admin/settings
PUT /api/admin/settings                    {bilibili_only_mode?}
```

**鉴权**：管理 API 全部校验 `session['admin']`；`.env` 新增 `ADMIN_PASSWORD`（默认空 = 管理界面禁用，安全默认）。
**操作审计**：登录成功/失败、退出、源增删改/导入/刷新、频道覆盖变更、设置变更均写入 logs 表（module=admin，
管理页日志可查）；关键事件（服务启停/聚合完成/告警结果/EPG 更新）以 INFO 入库（record_event）。

### 分页/排序契约（P2 开工前已固化）

**通用响应包裹**（所有列表接口统一）：

```json
{"items": [...], "total": 123, "page": 1, "page_size": 20, "has_more": true}
```

**通用参数**：

| 参数 | 默认 | 规则 |
|---|---|---|
| `page` | 1 | 1 起；越界返回空 items（total 不变） |
| `page_size` | 20 | 上限 200（超出截断），下限 1 |
| `order` | 各端点默认 | `asc`/`desc`；非法值回退默认（宽松，不报错） |
| `q` | 无 | 子串匹配（大小写不敏感），匹配字段见各端点 |

**各端点约定**：

| 端点 | sort 白名单 | 默认排序 | q 匹配字段 | 其他过滤 |
|---|---|---|---|---|
| GET /api/admin/sources | id / sort_order | sort_order ASC, id ASC | name、url | type=public\|bilibili、enabled=0\|1 || GET /api/admin/channels | name / group | 聚合输出顺序（河南卫视→央视→卫视→B站） | 频道名（含覆盖后显示名） | 无 |
| GET /api/admin/monitor/history | 无（固定 id） | id DESC（新→旧） | 无 | 无 |
| GET /api/admin/monitor/streams | ts / ok | id DESC | channel_name、url | unreachable=1 |
| GET /api/admin/logs | 无（固定 id） | id DESC | message、module | level=ERROR\|WARNING\|INFO |

**源列表兜底语义**：GET /sources 返回「当前生效来源」——DB 行 + 该类型无启用源时的 config 兜底行
（`config_default: true`、`id: null`，只读展示，与聚合回退语义一致）；
`POST /sources/import-defaults` 一键把兜底源落库（幂等，B 站 uid 条目服务端解析房间号、失败跳过）。

**频道覆盖语义**：

- `key` = `normalize_name` 后的频道名（聚合去重同款归一化，URL 编码后放路径）
- 频道列表项：`{key, name, group, url, enabled, override}`；`enabled=false` 的频道不在 m3u 中，
  但**仍出现在列表**（`enabled=false`、`url=null`、`override.enabled=0`），便于管理端重新启用
- PUT body 三字段全部可选、可组合；`enabled=false` 聚合时跳过，改分组/改名仅影响输出
- 变更后自动 `request_async_refresh()` + 清播放列表缓存

**设置语义**：运行时设置「DB 优先、config 兜底」，支持键：
`bilibili_only_mode`(bool) / `min_channel_count`(int) / `stream_fail_limit`(int) /
`monitor_history_keep`(int) / `stream_history_keep`(int) / `log_keep_days`(int) /
`group_health_ratios`(JSON，组名->0~1) / `public_base_url`(str，B 站频道 URL 基础地址) /
`alert_enabled`(bool，false 时 send_alert 整体静默)；对后续聚合/监控轮次即时生效（调度线程布局按启动时 env 定死）。
初始化脚本 `scripts/seed_admin_settings.py`（幂等，`--reset` 覆盖）；密钥类（API_TOKEN/email 等）不落库，保持 .env。

---

## 核心改造点（侵入最小化）

### 1. 公开源 DB 化（改 core/sources.py:fetch_all_public_channels）

```python
# 原：for url in PUBLIC_M3U_SOURCES:
# 改：
def get_public_source_urls():
    urls = AdminDB.get_enabled_public_urls()   # from admin.db
    return urls or list(PUBLIC_M3U_SOURCES)    # 空表/未初始化回退 config
```

- 查询前先过 `AdminDB.db_ready()`（文件不存在直接跳过，避免凭空创建空库文件）
- BILIBILI_ROOMS 同理 → `AdminDB.get_enabled_bilibili_rooms()` 兜底（aggregator.list_bilibili_rooms）

### 2. 聚合输出应用频道覆盖（改 core/aggregator.py:aggregate_m3u）

- 生成 m3u 前对每频道查 channel_overrides：enabled=0 → 跳过；group_title/display_name → 覆盖
- 覆盖查询做内存缓存（TTL 60s），避免每频道一次 SQL

### 3. 监控落库（改 monitoring/checks.py 两处，非侵入）

- run_check_once 末尾 → AdminDB.save_monitor_history(...)
- run_stream_check_once 末尾 → AdminDB.save_stream_history_batch(...)（整轮批量写入 + 一次清理）
- 包裹 try/except，落库失败只记日志不影响检测

### 4. 日志改造（core/logger.py，替换全部 print）

- `get_logger(module)` → logging.Logger，双 handler：
  - 滚动文件 `xml_data/app.log`（10MB × 3）
  - SQLite `logs` 表（仅 WARNING+）
- 项目内 ~60 处 `print(` → `logger.info/warning/error(...)`
- 线程前缀逻辑（`[线程名]`）并入 logger format
- 保留 print 的即时性：开发环境 INFO 打到 stdout（StreamHandler）

---

## 页面设计（admin/templates/，Bootstrap 5 CDN + 原生 JS）

| 页面 | 路由 | 内容 |
|---|---|---|
| 登录 | /admin/login | 密码表单 |
| 仪表盘 | /admin | 卡片（频道数/分组数/源健康）+ 最近健康时间线 + 最近日志 |
| 源管理 | /admin/sources | 公开源 CRUD + B站房间 + 立即刷新按钮 |
| 频道管理 | /admin/channels | 分页表格：频道 + 禁用开关 + 改分组 |
| 监控 | /admin/monitor | 健康历史 + 流探测明细（过滤不可达） |
| 日志 | /admin/logs | 级别/关键词过滤 + 分页 |

---

## 分阶段实施

| 阶段 | 内容 | 验证标准 |
|---|---|---|
| P1 | admin/db.py 数据层 + logging 改造 + 监控落库 | ✅ 已完成（unittest 全绿；清理逻辑按 GMT+8） |
| P2 | sources DB 化（兜底）+ 频道覆盖层 + 管理 API（契约已固化） | ✅ 已完成（146 测试全绿；curl 管理 API 全通） |
| P3 | Web 界面（登录+5 页面） | ✅ 已完成（151 测试全绿；登录→页面渲染→API 数据链路冒烟通过） |
| P4 | 打磨（趋势图/分页/docker 适配） | ✅ 代码完成（仪表盘趋势图 + 页码分页 + xml_data 持久卷）；容器构建需在部署机验证（本机无 docker） |

---

## 测试策略

- P1：db.py 建表/读写/清理单测；logger 模块单测
- P2：sources 空表→config 兜底等价测试；频道覆盖应用测试；管理 API 端点测试（Flask test client + session）
- P3：页面渲染 smoke test（200）
- 现有测试持续全绿为回归底线（DB 兜底设计保证不动 config 默认行为）

---

## 风险与决策备忘

| 项 | 决策 |
|---|---|
| 循环导入 | admin/db.py 零依赖 core，只被 core 单向引用 |
| 并发写 SQLite | threading.Lock 串行写 + 每操作短连接 |
| 日志/历史膨胀 | 清理策略（500 轮/7 天/2000 条），配置化 |
| 管理界面安全 | ADMIN_PASSWORD 默认空=禁用；公网部署提示 nginx+HTTPS |
| config 静态项 | PUBLIC_M3U_SOURCES/BILIBILI_ROOMS 保留为种子值，不删 |
