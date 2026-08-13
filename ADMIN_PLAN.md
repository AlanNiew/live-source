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
- 清理策略：monitor_history 保留最近 500 轮；stream_check_history 保留最近 2000 条；
  logs 保留最近 7 天（WARNING+ 数据量小）
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

# 频道覆盖
GET  /api/admin/channels                  聚合后频道 + 覆盖状态（分页/搜索）
PUT  /api/admin/channels/<key>            {enabled?, group_title?, display_name?}

# 监控
GET /api/admin/monitor/summary             当前健康/频道数/最近轮次
GET /api/admin/monitor/history             ?limit=500 健康趋势
GET /api/admin/monitor/streams             ?page=&unreachable=1 流探测明细

# 日志
GET /api/admin/logs                        ?level=&q=&page=

# 设置
GET /api/admin/settings
PUT /api/admin/settings                    {bilibili_only_mode?}
```

**鉴权**：管理 API 全部校验 `session['admin']`；`.env` 新增 `ADMIN_PASSWORD`（默认空 = 管理界面禁用，安全默认）。

---

## 核心改造点（侵入最小化）

### 1. 公开源 DB 化（改 core/sources.py:fetch_all_public_channels）

```python
# 原：for url in PUBLIC_M3U_SOURCES:
# 改：
def get_public_source_urls():
    urls = AdminDB.get_enabled_public_urls()   # from admin.db
    return urls or list(PUBLIC_M3U_SOURCES)    # 空表回退 config
```

BILIBILI_ROOMS 同理 → `AdminDB.get_enabled_bilibili_rooms()` 兜底。

### 2. 聚合输出应用频道覆盖（改 core/aggregator.py:aggregate_m3u）

- 生成 m3u 前对每频道查 channel_overrides：enabled=0 → 跳过；group_title/display_name → 覆盖
- 覆盖查询做内存缓存（TTL 60s），避免每频道一次 SQL

### 3. 监控落库（改 monitoring/checks.py 两处，非侵入）

- run_check_once 末尾 → AdminDB.save_monitor_history(...)
- run_stream_check_once 末尾 → AdminDB.save_stream_history(...)
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
| P1 | admin/db.py 数据层 + logging 改造 + 监控落库 | unittest 全绿；admin.db 有数据 |
| P2 | sources DB 化（兜底）+ 频道覆盖层 + 管理 API | 现有 96 测试无回归；curl 管理 API 全通 |
| P3 | Web 界面（登录+5 页面） | 浏览器完整操作 |
| P4 | 打磨（趋势图/分页/docker 适配） | 生产容器全流程 |

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
