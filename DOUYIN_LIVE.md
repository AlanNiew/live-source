# 抖音直播接入 —— 方案 B（外部工具转流 + 自定义流频道）

> 背景：抖音网页直播接口（`webcast/room/web/enter`）要求 `__ac_signature` / `a_bogus`
> 签名，且签名算法随版本频繁更新、风控严格（见 `scripts/probe_douyin.py` 的探测结论）。
> 方案 B **不把签名算法接进本服务**，而是用社区维护的 streamlink（自带 douyin 插件）
> 解析 → ffmpeg 转 HLS → 本地静态服务 → 在管理后台添加「自定义流」频道进聚合。
> 签名更新的维护成本完全由 streamlink 升级吸收。

## 架构

```
抖音直播间            streamlink           ffmpeg             本地静态服务          HNTV API
live.douyin.com/123 ──► 解析(douyin插件) ──► HLS 切片 ────────► :8080/douyin/*.m3u8 ──► sources 表
（签名由插件处理）      （cookie 可选）     （-c copy 无损）     （nginx/http.server）    type=custom
                                                                                      ↓ 聚合
                                                                                /api/live.m3u8
```

## 部署步骤

### 1. 安装 streamlink 与 ffmpeg

```bash
pip install streamlink            # 或 apt install streamlink
# ffmpeg：apt install ffmpeg（Windows 下载解压后加 PATH）
```

### 2. 起转流（示例：央视新闻直播间）

```bash
# 目标目录
mkdir -p /opt/douyin-hls/cctv

# streamlink 解析 + ffmpeg 转 HLS（-c copy 无损，4 秒一片，窗口 6 片）
streamlink "https://live.douyin.com/282773369501" best -o - | \
  ffmpeg -i pipe:0 -c copy -f hls -hls_time 4 -hls_list_size 6 \
         -hls_flags delete_segments /opt/douyin-hls/cctv/index.m3u8
```

- 部分房间需要登录态 cookie：`streamlink --http-cookie "ttwid=..." "https://live.douyin.com/xxx" best`
- 想看可用清晰度：`streamlink "https://live.douyin.com/xxx"`（列出 quality 列表）
- **常驻后台**：包一层 `while true; do ...; sleep 10; done`，或用 systemd/nssm 托管，
  断线自动重连由 streamlink/ffmpeg 重试 + 外层循环兜底

### 3. 起静态服务

```bash
# 任选其一
python -m http.server 8080 --directory /opt/douyin-hls      # 简单
# 或 nginx：location /douyin { alias /opt/douyin-hls; }
```

验证：浏览器/播放器打开 `http://127.0.0.1:8080/cctv/index.m3u8` 能播。

### 4. 管理后台添加自定义流

- 打开 `/admin/sources` → 新增源：
  - 类型：`custom（自定义流，外部转流）`
  - 名称：如「抖音-央视新闻」
  - url：`http://127.0.0.1:8080/cctv/index.m3u8`（本服务/播放器必须能访问该地址）
- 保存后自动后台聚合（或点「立即刷新」），频道出现在 `/api/live.m3u8` 的「自定义」分组
- 改分组/改名/禁用：频道管理页正常操作（覆盖层对自定义流同样生效）

## 多房间

每个房间一个转流进程 + 一个目录；HLS 窗口 6 片（约 24 秒），可适当加大
`-hls_list_size`；转流进程建议与 API 同机（`127.0.0.1` 直连）或走内网。

## 注意事项

- **合规**：本方案仅建议自用；违反抖音用户协议的风险自行评估，勿公开提供服务
- **风控**：频繁重启/高频请求可能触发验证码或限流；streamlink 版本保持更新
- **重启保持**：转流进程与静态服务需随服务器重启自启（systemd），自定义源配置存 DB 无需重配
- **本服务不自带转流**：streamlink/ffmpeg 是外部依赖；如需「一个容器全搞定」，
  可参考 [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)（docker 部署，
  输出 TS/HLS）作为转流侧替代
