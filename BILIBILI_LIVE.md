# B 站直播接入 —— 完整流程与关键逻辑

> 本文档说明本服务如何把哔哩哔哩直播间接入 m3u 直播列表并稳定播放。
> 代码入口：`core/bilibili.py`（核心逻辑）、`app.py`（路由）、`core/aggregator.py`（聚合接入）、`config.py`（配置）。
> 频道管理 API 的操作细节（请求/响应/错误码/校验脚本/常见问题）见 **[BILIBILI_ROOMS_API.md](BILIBILI_ROOMS_API.md)**。

## 一、为什么不能直接给播放器 B 站地址

实测验证的两个硬性限制：

1. **防盗链**：B 站流 CDN 校验 `Referer: https://live.bilibili.com/`，无 Referer 访问 m3u8/分片直接 **403**（实测确认）。普通播放器不会带这个 Referer，直连必然失败。
2. **流地址时效**：`playUrl` 接口返回的 m3u8 地址带 `expires` + `sign` 签名参数，约 4 小时过期，过期后无法访问。

**因此设计为"服务端代理"模式**：播放器只认本服务的地址，本服务负责解析、重写、转发，全程注入 Referer/UA。

```
播放器 (VLC/盒子)
   │  只请求本服务
   ▼
本服务 (hntv-api, 5002)
   │  带 Referer/UA 向上游请求
   ▼
B站接口 (api.live.bilibili.com) → B站流 CDN (bilivideo.com)

注：直连模式下分片（大流量）由播放器直连 B 站 CDN，本服务只代理主清单（几百字节）。
```

## 二、完整流程（一次播放的生命周期）

### 阶段 1：聚合 —— 决定哪些频道进列表

触发点：后台聚合线程（测试模式 1 个线程，正式模式 2 个线程）周期性调用 `AggregatorUtils.fetch_bilibili_channels()`（`core/aggregator.py:75`）。

对 `config.py` 里 `BILIBILI_ROOMS` 配置的每个 `{"name": 频道名, "uid": UP主UID}`：

```
for item in BILIBILI_ROOMS:
    1. get_room_id(uid)         # uid → 房间号
    2. is_live(room_id)         # 实测主清单 200？→ 在播判定
    3. 在播 → 生成频道条目
        url = PUBLIC_BASE_URL + /api/bilibili/<room_id>/live.m3u8
        group = "B站直播"
```

- 未开播/解析失败 → 跳过该频道，**不影响整个列表**（优雅降级）
- 生成的频道地址是本服务的代理 URL，不是 B 站原始地址

### 阶段 2：播放器请求代理 m3u8

播放器请求 `GET /api/bilibili/<room_id>/live.m3u8`（`app.py:94`，无鉴权）。

```
build_proxied_m3u8(room_id, public_base):
    resolve_play_m3u8(room_id)      # 拿 (原始m3u8URL, 分片基础URL, 签名查询串)
    GET 原始 m3u8（带 Referer/UA）   # 拉真实主清单
    逐行重写：把分片相对路径改为本服务地址
        live_xxx-123.ts → http://本服务/api/bilibili/<room_id>/seg/live_xxx-123.ts
    注释行（#EXTM3U 等）原样保留
    返回重写后的 m3u8 文本
```

返回给播放器的清单示例：
```
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA-SEQUENCE:1786513749
#EXTINF:4.000, no desc
http://127.0.0.1:5002/api/bilibili/8178490/seg/live_3o8fwm_S8w9szi_2ku2t_2500-1786513749.ts
```

关键点：`public_base` 用**当前请求的 Host 动态生成**（`flask_request.host_url`），自动适配 localhost/局域网 IP/域名，无需额外配置。

### 阶段 3：播放器获取分片（直连 CDN / 代理两种模式）

**默认直连模式（`BILIBILI_DIRECT_SEGMENTS=true`）**：

实测关键结论：**B 站防盗链只校验 m3u8 主清单（无 Referer 403），分片 ts 无 Referer 也可直接访问（200）**。因此在直连模式下，`build_proxied_m3u8` 把主清单里的分片相对路径改写为 B 站 CDN 绝对地址（相对路径 + 分片基础 URL + 签名查询串），播放器拿到清单后**直连 CDN 拉分片，本服务不再转发分片流量**。

```
播放器 ──> 本服务（每 4 秒拉一次主清单，几百字节，重写后返回）
播放器 ──> B站 CDN（直接拉分片，大流量不走服务器）
```

**代理模式（`BILIBILI_DIRECT_SEGMENTS=false`，兼容性兜底）**：

分片改写为本服务地址，播放器请求 `GET /api/bilibili/<room_id>/seg/<相对路径>`（`app.py:111`），本服务带 Referer/UA 向 CDN 转拉后流式转发。

```
proxy_segment(room_id, seg_path):
    resolve_play_m3u8(room_id)      # 复用/刷新签名（内存缓存 30 分钟）
    seg_url = 分片基础URL + seg_path + "?" + 签名查询串
    GET seg_url（带 Referer/UA）     # 向 B 站 CDN 转拉
    透传关键响应头（Content-Type 必须保留）
    流式转发：iter_content(64KB) 逐块回传
```

- 两种模式下本服务都**不落盘、不缓存分片**——HLS 是滑动窗口（4 秒一个分片，实时滚动），分片必须即时拉取
- 分片签名与 m3u8 主清单是**同一套**签名参数
- 直连模式注意：分片直连不带我们的 UA/Referer，若 B 站收紧分片防盗链，切回代理模式即可

### 阶段 4：循环

播放器每 4 秒请求下一批分片 → 重复阶段 2/3，直到直播结束。

## 三、关键逻辑细节

### 1. 房间号解析与磁盘缓存兜底（`core/bilibili.py:73`）

- 接口：`getRoomInfoOld?mid=<uid>` → 返回房间号 + 开播状态
- `resolve_room_by_uid()` 每次实时查；成功时把 `{uid: room_id}` 写入磁盘缓存 `xml_data/bilibili_rooms.json`
- `get_room_id()`：接口失败时**用磁盘缓存兜底**，避免上游抖动导致整个 B 站分组消失

### 2. 流地址解析与内存缓存（`core/bilibili.py:124`）

- 接口：`playUrl?cid=<room_id>&quality=4&platform=h5` → 返回带签名 m3u8 地址
- `quality=4` 是 B 站清晰度最高档（原画）；最终清晰度由**直播间推流配置**决定（央视新闻等官方号只开 720P，实测请求原画也被降级为 250 档）
- 返回三元组 `(m3u8_url, base_url, query)`：`base_url` 是去掉文件名后的 CDN 目录，`query` 是签名串——分片 URL 由两者拼出
- **内存缓存**（`BILIBILI_PLAY_CACHE_TTL=1800s`）：避免播放时高频打上游接口；`force=True` 可强制重解析

### 3. 开播判定 —— 实测优先（`core/bilibili.py:178`）

**踩过的坑**：未开播房间 `playUrl` 也返回带签名地址（实测河南卫视未开播仍返回 durl），仅凭解析结果会误判在播。

正确做法 `is_live()`：
```
解析出 m3u8 → 实际请求主清单 → 200 才算在播
未开播：连接失败/超时（实测 000）
开播：200
```

聚合、`/status` 端点都用这个方法，保证列表里不会出现"看着在播实际播不了"的死频道。

### 4. 签名过期自动恢复（`core/bilibili.py:213`）

流地址 4 小时过期。长时间挂机的播放器遇到过期：

```
主清单请求非 200 → resolve_play_m3u8(force=True) 强制重解析 → 再用新签名重试一次
分片请求非 200   → 同上
```

两层都内置了"过期 → 重解析 → 重试"兜底。

### 5. 聚合互斥与降级

- 完整聚合受 `_aggregate_lock` 保护，同一时刻只有一个聚合在跑
- 聚合失败/进行中 → `load_aggregated_m3u()` 降级：测试模式返回 `get_bilibili_only_m3u()`（只拉 B 站，不碰 hntv），正式模式返回 hntv 官方源
- 播放器请求 B 站代理端点时如果解析失败 → 返回 404 占位清单，不崩溃

### 6. 测试模式（`BILIBILI_ONLY_MODE`，默认开启）

`config.py`：`BILIBILI_ONLY_MODE = os.environ.get('BILIBILI_ONLY_MODE', 'true')`

- 聚合跳过 hntv 官方源与公开源拉取（避免每次启动拉公开源+探测 70 频道拖慢重启），只聚合 B 站
- 监控频道数阈值自动联动：`MIN_CHANNEL_COUNT` 测试模式默认 1（否则 1-2 个频道会误报"频道数过少"告警邮件）
- 官方源刷新线程不启动（无 hntv 签名需刷新，避免重复采集）
- 正式使用：`BILIBILI_ONLY_MODE=false`

## 四、接口清单

| 端点 | 说明 |
|---|---|
| `GET /api/bilibili/<room_id>/live.m3u8` | 代理 m3u8 主清单（分片重写为本服务地址） |
| `GET /api/bilibili/<room_id>/seg/<path>` | 分片反代（注入 Referer/UA 转拉） |
| `GET /api/bilibili/<room_id>/status` | 开播状态 JSON（`{"room_id", "live", "playable"}`） |
| `GET /api/bilibili/rooms` | 列出全部频道（静态+动态，默认附带实时开播状态） |
| `POST /api/bilibili/rooms` | 动态添加频道（需 token） |
| `DELETE /api/bilibili/rooms/<room_id>` | 删除动态频道（需 token） |

## 五、配置说明（config.py）

| 配置 | 默认 | 说明 |
|---|---|---|
| `BILIBILI_ROOMS` | 央视新闻/河南卫视/中国应急管理 | 静态频道列表：`{"name", "room_id"}` 或 `{"name", "uid"}` |
| `BILIBILI_CUSTOM_ROOMS_PATH` | `xml_data/bilibili_custom_rooms.json` | 运行时动态添加的频道（API 写入，重启不丢） |
| `BILIBILI_REFERER` | `https://live.bilibili.com/` | 防盗链 Referer |
| `BILIBILI_UA` | Chrome UA | 请求 UA |
| `BILIBILI_COOKIE` | 空 | B 站登录 SESSDATA（解锁蓝光/原画；留空 = 游客 720P） |
| `BILIBILI_PLAY_CACHE_TTL` | 1800s | 流地址内存缓存 |
| `BILIBILI_CACHE_PATH` | `xml_data/bilibili_rooms.json` | uid→房间号解析磁盘缓存 |
| `BILIBILI_GROUP_NAME` | `B站直播` | 输出分组名 |
| `PUBLIC_BASE_URL` | `http://localhost:5002` | 聚合生成的频道 URL 基础地址（**容器/生产必须覆盖**为播放器可达地址） |
| `BILIBILI_DIRECT_SEGMENTS` | `true` | 分片直连 CDN（省服务器带宽）；`false` 回退全代理 |
| `BILIBILI_ONLY_MODE` | `true` | 测试模式开关 |

### 新增频道（两种方式）

**方式一：运行时 API 动态添加（推荐，无需改配置重启）**

```bash
# 查看当前频道
curl "http://IP:5002/api/bilibili/rooms"

# 添加（需 API_TOKEN）
curl -X POST "http://IP:5002/api/bilibili/rooms" \
  -H "Authorization: Bearer <API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"name":"XX卫视","room_id":123456}'

# 删除
curl -X DELETE "http://IP:5002/api/bilibili/rooms/123456" \
  -H "Authorization: Bearer <API_TOKEN>"
```

或用校验脚本（自动验证房间存在/在播）：
```bash
python scripts/add_bili_room.py 123456                          # 验证 + 打印配置行
python scripts/add_bili_room.py 123456 "XX卫视" --api http://IP:5002 --token <API_TOKEN>   # 直接调 API 添加
```

**方式二：改静态配置（config.py，重启生效）**

```python
BILIBILI_ROOMS = [
    {"name": "央视新闻", "room_id": 8178490},       # 推荐：房间号即 live.bilibili.com/<房间号> 的数字
    {"name": "河南卫视", "uid": 2057655323},         # 兼容：填 UP 主 UID
    {"name": "新频道", "room_id": <房间号>},
]
```

- 房间号获取：直播间网址 `live.bilibili.com/<房间号>` 的数字，**无需查 uid**
- 房间号唯一，静态配置优先（动态添加的同房间会被忽略）
- 添加后立即异步刷新生效（不与定时任务并发）；无效房间靠开播判定自动跳过

## 六、稳定性设计与已知限制

### 清晰度与登录 Cookie

B 站直播清晰度受**登录态**限制：

| 状态 | 效果 |
|---|---|
| 未配置 cookie（游客） | 最高 720P（超清 250 档） |
| 配置 `BILIBILI_COOKIE`（登录 SESSDATA） | 解锁蓝光 1080P / 原画（取决于直播间推流与账号等级） |

**原理**（实测验证）：
- 旧接口 `playUrl` 即使登录也只给 720P；高清须走**新接口 `getRoomPlayInfo`**
- 新接口带有效 SESSDATA → `current_qn=10000`（原画），流地址无 `_2500` 后缀、码率显著提升
- 仅 `SESSDATA` 即可解锁（HttpOnly cookie，需从 DevTools Network 抓取，`document.cookie` 读不到）

**配置方法**（`.env`，与密钥同等敏感，勿提交）：
```
BILIBILI_COOKIE=SESSDATA=你的SESSDATA值
```

**自动降级**：cookie 失效/新接口异常 → 自动回退旧接口游客 720P，链路不中断；解析时会探测登录态，失效打日志提示更新。

### 备用线路切换

`durl`/`url_info` 天然返回多条 CDN 线路（主 + 备用，内容相同），服务**主线路故障自动切备用**，全部失败才强制重解析。对播放器无感（频道/URL 不变）。

### 已做的容错

- 所有上游请求 try/except，失败只记日志不崩溃
- 解析双接口降级链：新接口（高清）→ 旧接口（720P 兜底）→ None
- cookie 失效自动降级游客画质，并探测登录态打日志提示
- CDN 多线路自动切换（主节点故障自动切备用）
- 房间号解析磁盘缓存兜底
- 签名过期自动重解析重试（主清单 + 分片两层）
- 聚合失败优雅降级，B 站分组消失不影响 hntv/公开源
- 未开播频道自动跳过，不进列表

### 已知限制（不可控）

| 限制 | 说明 |
|---|---|
| 上游接口非官方 | B 站可能改版/加风控，需跟进调整 |
| 流地址 4h 时效 | 长时间挂机需播放器自动重拉 m3u8（VLC 默认会） |
| 带宽中转 | 直连模式几乎不占服务器带宽（只代理主清单）；代理模式流量全走服务器 |
| 清晰度受限 | 取决于直播间推流配置（央视新闻仅 720P） |
| 特殊直播间 | 需登录/付费/密码房播不了 |
| 并发能力 | 同步阻塞转发（gunicorn sync worker），大量并发可能延迟 |

### 带宽占用说明

- **直连模式（默认）**：服务器每 4 秒为每个观看端转发一个约 1KB 的主清单，带宽约 2Kbps/端；分片（~1.5Mbps/端）由客户端直连 B 站 CDN，不占服务器带宽
- **代理模式**：所有分片流量经服务器中转，约 1.5Mbps/端
- **磁盘**：两种模式都不缓存分片（流式转发），磁盘只有 KB 级的房间缓存/频道列表，可忽略

### 测试建议

- 本机：`vlc --network-caching=5000 "http://localhost:5002/api/bilibili/<room_id>/live.m3u8"`
- 局域网设备：设 `PUBLIC_BASE_URL=http://<本机IP>:5002` 后重启，设备 VLC 打开对应地址
- 单元测试：`python -m unittest tests.test_bilibili`（全部 mock，不碰真实源）
