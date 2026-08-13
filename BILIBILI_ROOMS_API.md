# B 站直播频道管理 API 操作文档

> 本文档说明如何通过 HTTP API 动态管理 B 站直播频道（添加/查看/删除），
> 无需修改 `config.py`、无需重启服务，改动即时生效并持久化。
> 代码入口：`app.py`（路由）、`core/bilibili.py`（动态列表读写）、`core/aggregator.py`（合并与异步刷新）。

---

## 一、概览

### 1. 三个 API 一览

| 端点 | 方法 | 鉴权 | 用途 |
|---|---|---|---|
| `/api/bilibili/rooms` | GET | 无 | 查看全部频道（静态 + 动态）+ 实时开播状态 |
| `/api/bilibili/rooms` | POST | Bearer token | 动态添加一个频道 |
| `/api/bilibili/rooms/<room_id>` | DELETE | Bearer token | 删除一个动态添加的频道 |

### 2. 鉴权方式

- POST / DELETE 需要 **Bearer token**，即 `.env` 里配置的 `API_TOKEN`（与 `/api/proxy`、`/api/generate-sign` 同一套）
- token 两种传法：
  - 请求头：`Authorization: Bearer <API_TOKEN>`
  - 查询参数：`?token=<API_TOKEN>`
- GET 列表**不需要**鉴权（播放器/监控可自由查看）

### 3. 数据持久化与生效机制

- 动态添加的频道保存在磁盘文件 `xml_data/bilibili_custom_rooms.json`（**重启不丢**）
- 添加/删除后立即触发**异步聚合刷新**（几秒内生效），并清除 `/api/live.m3u8` 的 10 分钟内存缓存
- 刷新与定时聚合任务**互斥**（同一把锁）：若定时聚合正在进行，会等它完成后立即补刷，不会并发写文件，也不会丢本次变更

### 4. 与静态配置的关系

- 频道来源 = `config.py` 的 `BILIBILI_ROOMS`（静态）+ 动态列表（json）合并
- **room_id 为唯一 key，静态优先**：动态添加的 room_id 若与静态配置重复，静态条目生效、动态条目被忽略（不会冲突报错）
- 静态配置只读，API 不能改它；动态条目可通过 API 添加/删除

---

## 二、API 详解

### 2.1 GET /api/bilibili/rooms —— 查看全部频道

**请求**
```bash
curl "http://<服务地址>:5002/api/bilibili/rooms"
```

**响应示例**
```json
{
  "rooms": [
    {
      "name": "央视新闻",
      "room_id": 8178490,
      "source": "static",
      "live": true
    },
    {
      "name": "动态添加的频道",
      "room_id": 123456,
      "source": "custom",
      "live": false
    }
  ]
}
```

**字段说明**

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string | 频道名 |
| `room_id` | int | B 站直播间房间号（`live.bilibili.com/<room_id>` 的数字） |
| `source` | string | `static` = 来自 config.py 静态配置；`custom` = 来自动态列表 |
| `live` | bool | 实时开播状态（并发探测主清单 200 判定，每个房间约 1 秒内返回） |

**说明**
- 列表按 静态配置顺序 → 动态添加顺序 排列，同 room_id 只出现一次（静态优先）
- `live=false` 的频道可能是"未开播"或"房间不存在/已失效"，都不会出现在播放列表 `/api/live.m3u8` 中

---

### 2.2 POST /api/bilibili/rooms —— 动态添加频道

**请求**
```bash
curl -X POST "http://<服务地址>:5002/api/bilibili/rooms" \
  -H "Authorization: Bearer <API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"name":"XX卫视","room_id":123456}'
```

**请求体字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `room_id` | int | 是 | 直播间房间号，**必须为正整数**。取直播间网址 `live.bilibili.com/<房间号>` 的数字 |
| `name` | string | 否 | 频道名。缺省时自动使用 `房间<room_id>`（建议填写，便于识别） |

**成功响应（200）**
```json
{
  "status": "success",
  "message": "已添加频道 XX卫视（room=123456），正在后台刷新列表",
  "rooms": [
    {"name": "XX卫视", "room_id": 123456}
  ]
}
```
- `rooms` 为添加后的完整动态列表（刚添加的条目在最后）
- 后台异步刷新完成后，该频道若正在直播，会出现在 `/api/live.m3u8` 的"B站直播"分组

**失败响应**

| 状态码 | 场景 | 响应体 |
|---|---|---|
| 401 | 未带 token / token 错误 | `{"message": "Missing or invalid token"}` |
| 400 | `room_id` 不是正整数（如字符串/0/负数） | `{"error": "room_id 必须为正整数"}` |
| 500 | 写入磁盘失败等内部错误 | `{"error": "<错误详情>"}` |

**注意事项**
- 添加**不预验证**房间是否存在/在播——无效房间会正常写入列表（GET 显示 `live:false`），但不会进入播放列表，不影响其他频道
- 建议先用校验脚本（见第五节）验证房间，再添加

---

### 2.3 DELETE /api/bilibili/rooms/<room_id> —— 删除动态频道

**请求**
```bash
curl -X DELETE "http://<服务地址>:5002/api/bilibili/rooms/123456" \
  -H "Authorization: Bearer <API_TOKEN>"
```

**成功响应（200）**
```json
{
  "status": "success",
  "message": "已删除房间 123456",
  "rooms": []
}
```
- `rooms` 为删除后的完整动态列表

**失败响应**

| 状态码 | 场景 | 响应体 |
|---|---|---|
| 401 | 未带 token / token 错误 | `{"message": "Missing or invalid token"}` |
| 404 | room_id 不在动态列表中 | `{"error": "房间 123456 不在动态列表中"}` |

**注意事项**
- 只能删除**动态添加**（source=custom）的频道；静态配置（source=static）的频道不受影响，删除其 room_id 会返回 404
- 删除静态频道需编辑 `config.py` 并重启

---

## 三、完整操作流程示例

### 场景：查看 → 验证 → 添加 → 确认生效 → 删除

**1. 查看当前频道**
```bash
curl "http://192.168.1.107:5002/api/bilibili/rooms"
```

**2. 校验目标房间**（用脚本验证存在性和开播状态）
```bash
python scripts/add_bili_room.py 123456
# 输出：
#   验证房间 123456 ...
#   频道名: XX卫视
#   uid:    123456789
#   状态:   在播
```

**3. 动态添加**
```bash
curl -X POST "http://192.168.1.107:5002/api/bilibili/rooms" \
  -H "Authorization: Bearer 你的API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"XX卫视","room_id":123456}'
```

**4. 确认已加入列表**（等 5~10 秒异步刷新）
```bash
# 方式一：房间列表里应出现 source=custom 且 live=true 的条目
curl "http://192.168.1.107:5002/api/bilibili/rooms"

# 方式二：播放列表里应出现该频道（B站直播分组）
curl "http://192.168.1.107:5002/api/live.m3u8"
```

**5. 播放器播放**
```
VLC/APP 打开：http://192.168.1.107:5002/api/live.m3u8
或直接播放单频道：http://192.168.1.107:5002/api/bilibili/123456/live.m3u8
```

**6. 删除频道**
```bash
curl -X DELETE "http://192.168.1.107:5002/api/bilibili/rooms/123456" \
  -H "Authorization: Bearer 你的API_TOKEN"
```

---

## 四、Windows PowerShell 用户示例

PowerShell 的 `curl` 是 `Invoke-WebRequest` 别名，用 `curl.exe` 或 `Invoke-RestMethod`：

```powershell
# 查看
Invoke-RestMethod "http://127.0.0.1:5002/api/bilibili/rooms"

# 添加
$token = "你的API_TOKEN"
$body = @{ name = "XX卫视"; room_id = 123456 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:5002/api/bilibili/rooms" `
  -Headers @{ Authorization = "Bearer $token" } `
  -ContentType "application/json" -Body $body

# 删除
Invoke-RestMethod -Method Delete -Uri "http://127.0.0.1:5002/api/bilibili/rooms/123456" `
  -Headers @{ Authorization = "Bearer $token" }
```

---

## 五、校验脚本 scripts/add_bili_room.py

推荐配合使用：先验证房间再添加，防止手滑添加无效房间号。

### 用法

| 命令 | 作用 |
|---|---|
| `python scripts/add_bili_room.py 123456` | 验证房间（存在性/uid/开播状态），打印配置行 |
| `python scripts/add_bili_room.py 123456 "XX卫视"` | 指定频道名 |
| `python scripts/add_bili_room.py 123456 "XX卫视" --api http://IP:5002 --token <TOKEN>` | 验证后直接调 API 添加 |

### 输出示例（只验证）
```
验证房间 123456 ...
  频道名: XX卫视
  uid:    123456789
  状态:   在播
  标题:   XX卫视 正在直播

可直接粘贴进 config.py BILIBILI_ROOMS 的配置行：
    {"name": "XX卫视", "room_id": 123456},
```

### 说明
- `--api` 模式下 token 缺省从项目根 `.env` 的 `API_TOKEN` 读取
- 退出码：0=成功；1=房间不存在或失败（可用于脚本化判断）
- 只依赖标准库，服务器/本机均可直接运行

---

## 六、常见问题

**Q1：添加后多久生效？**
几秒内（异步刷新）。如果恰好定时聚合在跑，会等它完成后立即补刷，最迟不会超过下一次定时轮（测试模式 6h / 正式模式 3h），且不会因等待而丢失本次变更。

**Q2：添加了无效/不存在的房间号会怎样？**
正常写入动态列表，GET 显示 `live:false`，不会进入播放列表，不影响其他频道。建议先用校验脚本验证。

**Q3：重启服务后动态添加的频道还在吗？**
在。动态列表持久化在 `xml_data/bilibili_custom_rooms.json`，重启自动加载。

**Q4：能删除静态配置的频道吗？**
不能通过 API 删除（返回 404）。静态频道需编辑 `config.py` 的 `BILIBILI_ROOMS` 后重启。

**Q5：添加的频道名和静态配置重复会怎样？**
room_id 唯一、静态优先：动态条目被忽略，不会重复出现，也不会报错。

**Q6：API 被他人调用怎么办？**
写操作（POST/DELETE）有 token 鉴权保护（`.env` 的 `API_TOKEN`，生产请设置为强随机值）。GET 是公开的（只读，无风险）。

**Q7：B 站房间号怎么获取？**
直播间网址 `live.bilibili.com/<房间号>` 里的数字就是 room_id，例如 `live.bilibili.com/8178490` → room_id=8178490。无需查 uid。

**Q8：本机开代理（科学上网）时 API 报 ProxyError？**
requests 默认走系统代理，代理连不上 B 站 CDN 会报 `ProxyError`。关掉代理或让代理对 `bilivideo.com` 直连即可；服务端（国内服务器）一般无此问题。
