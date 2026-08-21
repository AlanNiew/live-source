"""全局配置：环境变量与常量集中管理

所有模块统一 from config import ...，避免常量散落各处。
环境变量在模块加载时读取一次（load_dotenv 唯一入口）。
"""
import datetime
import os
import re

from dotenv import load_dotenv

# 加载环境变量（全项目唯一入口；容器部署由 docker --env-file 注入，
# 本地开发读取工作目录 .env 文件）
load_dotenv()

# ---------------------------------------------------------------- 时区
# GMT+8 时区：所有日期/定时/EPG 时间戳均按此处理，不依赖容器时区
GMT8 = datetime.timezone(datetime.timedelta(hours=8))

# ---------------------------------------------------------------- 认证
# API 令牌（弱默认值兜底，生产必须显式覆盖）
API_TOKEN = os.environ.get('API_TOKEN', 'hntv-secret-token-2025')
# 上游 HNTV API 签名密钥（弱默认值兜底；main.py 旧硬编码已迁至此处）
SECRET_KEY = os.environ.get('HNTV_SECRET_KEY', '6ca114a836ac7d73')

# ---------------------------------------------------------------- 路径
# 磁盘缓存目录与文件（全部自动创建）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
XML_DATA_DIR = os.path.join(BASE_DIR, 'xml_data')
XML_FILE_PATH = os.path.join(XML_DATA_DIR, 'live.xml')
GZ_FILE_PATH = os.path.join(XML_DATA_DIR, 'live.xml.gz')
AGGREGATED_M3U_PATH = os.path.join(XML_DATA_DIR, 'aggregated.m3u')
STREAM_FAILURES_PATH = os.path.join(XML_DATA_DIR, 'stream_failures.json')
# 公开源过滤+探测后的频道缓存（官方源 1h 高频刷新时复用，避免频繁拉公开源与重复探测）
PUBLIC_CHANNELS_CACHE_PATH = os.path.join(XML_DATA_DIR, 'public_channels.json')
EMAIL_TEMPLATE_PATH = os.path.join(BASE_DIR, 'templates', 'email_alert.html')
EMAIL_MODULE_PATH = os.path.join(BASE_DIR, 'email', 'send_assistant.py')

# ---------------------------------------------------------------- 聚合
# 公开 m3u 源列表（三个源互补）：
# - iptv-org：央视全（17个），但卫视多为运营商内网IP，公网可达性差
# - hujingguang：卫视用电视台自有域名，但多为短时效防盗链签名
# - wwb521：卫视大台齐全（cztv 阿里云/bestv 百视通/mgtv 芒果 CDN），
#   实测公网可达率约 38%；走 jsdelivr CDN 拉取（raw.githubusercontent 国内不稳定）
PUBLIC_M3U_SOURCES = [
    "https://iptv-org.github.io/iptv/countries/cn.m3u",
    "https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/cnTV_AutoUpdate.m3u8",
    "https://cdn.jsdelivr.net/gh/wwb521/live@main/tv.m3u",
]

# 聚合刷新间隔（秒）——公开源部分每 6 小时刷新一次（含探测过滤）
AGGREGATE_REFRESH_INTERVAL = 6 * 60 * 60
# HNTV 官方源刷新间隔（秒）——官方接口签名有效期约 4h，2h 刷新留 2h 余量，保持签名新鲜
OFFICIAL_REFRESH_INTERVAL = 2 * 60 * 60

# 聚合时探测过滤不可达源（连续两轮失败才丢弃，避免源瞬时抖动被误杀）
FILTER_UNREACHABLE = True
STREAM_FAIL_LIMIT = 2                # 连续失败 N 轮才丢弃

# hntv 官方频道的分组名（降级路径与聚合路径共用，避免魔法字符串）
HNTV_GROUP_NAME = "河南卫视"
# 未识别分组时的默认组名
DEFAULT_GROUP_NAME = "其他"

# CCTV 开路频道中文标准名映射（编号 -> 中文副名）
# 依据央视官方频道名；付费/专业频道（台球/高尔夫/风暴等）不在此表，会被过滤掉
CCTV_NAME_MAP = {
    "CCTV-1": "CCTV-1 综合",
    "CCTV-2": "CCTV-2 财经",
    "CCTV-3": "CCTV-3 综艺",
    "CCTV-4": "CCTV-4 中文国际",
    "CCTV-5+": "CCTV-5+ 体育赛事",
    "CCTV-5": "CCTV-5 体育",
    "CCTV-6": "CCTV-6 电影",
    "CCTV-7": "CCTV-7 国防军事",
    "CCTV-8": "CCTV-8 电视剧",
    "CCTV-9": "CCTV-9 纪录",
    "CCTV-10": "CCTV-10 科教",
    "CCTV-11": "CCTV-11 戏曲",
    "CCTV-12": "CCTV-12 社会与法",
    "CCTV-13": "CCTV-13 新闻",
    "CCTV-14": "CCTV-14 少儿",
    "CCTV-15": "CCTV-15 音乐",
    "CCTV-16": "CCTV-16 奥林匹克",
    "CCTV-17": "CCTV-17 农业农村",
    "CCTV-4K": "CCTV-4K 超高清",
}

# 疑似运营商 IPTV 内网 IP 段前缀（公网环境通常不可达），用于地址质量评分。
# 注：112./120./218. 开头实测含公网可达 CDN（112.27.235.94 吉林/120.76.248.139 阿里云/218.84.12.186），
# 已从前缀中移除，避免误伤
CARRIER_IP_PREFIXES = (
    '118.', '111.', '117.', '183.', '39.', '27.',
    '125.', '61.', '211.', '60.', '175.',
)

# 带时效防盗链签名参数的地址（公开源抓取后缓存期间会过期），域名源降 1 分
# 注意：hntv 官方源不参与公开源择优，不受影响
SIGN_PARAM_PAT = re.compile(
    r'[?&](auth_key|authKey|sign|token|wsSecret|wsTime|expire|expires|txSecret|GuardEncType|accountinfo)=',
    re.I)

# ---------------------------------------------------------------- B站直播
# B 站直播间列表（默认频道：央视新闻 / 河南卫视 / 中国应急管理）。
# 新增频道格式（两种写法二选一）：
#   {"name": "频道名", "room_id": 房间号}   # 推荐：房间号即直播间网址 live.bilibili.com/<房间号> 的数字
#   {"name": "频道名", "uid": UP主UID}       # 兼容：通过 uid 解析房间号（需查 uid）
# 聚合时判断开播，开播的频道才加入 m3u 列表（地址为本服务的代理 URL，播放器只认本服务）。
# 更推荐用运行时 API 动态添加（POST /api/bilibili/rooms），无需改配置重启。
BILIBILI_ROOMS = [
    {"name": "央视新闻", "uid": 222103174},
]

# B 站直播防盗链：CDN 校验 Referer 与 UA，代理转拉时必须注入
BILIBILI_REFERER = "https://live.bilibili.com/"
BILIBILI_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# B 站登录 Cookie（解锁蓝光/原画；留空 = 游客仅 720P）。
# 只需 SESSDATA（实测仅 SESSDATA 即可解锁原画，HttpOnly 需从 DevTools/Network 抓取）。
# 配置在 .env：BILIBILI_COOKIE=SESSDATA=xxx; buvid3=yyy
# 安全：与 API_TOKEN 同等敏感（账号凭据），勿提交、勿打印；cookie 失效自动降级游客画质
BILIBILI_COOKIE = os.environ.get('BILIBILI_COOKIE', '').strip()

# 房间信息/流地址解析的磁盘缓存路径（流地址签名约 4h 过期，缓存设短 TTL 兜底）
BILIBILI_CACHE_PATH = os.path.join(XML_DATA_DIR, 'bilibili_rooms.json')
# 运行时动态添加的频道列表（POST /api/bilibili/rooms 写入，重启不丢）。
# 注意：与 BILIBILI_CACHE_PATH 语义不同——那是 uid→room_id 的解析缓存，不要混用
BILIBILI_CUSTOM_ROOMS_PATH = os.path.join(XML_DATA_DIR, 'bilibili_custom_rooms.json')
# 流地址解析结果的内存缓存 TTL（秒）：地址带时效签名，过期必须重新解析
BILIBILI_PLAY_CACHE_TTL = 1800

# B 站直播频道所在分组（输出顺序放最后，见 GROUP_ORDER）
BILIBILI_GROUP_NAME = "B站直播"

# 本服务对外基础地址（聚合生成 B 站代理频道 URL 时用）：
# 播放器/盒子必须能访问该地址。容器内默认 localhost 仅开发用，
# 生产部署需通过环境变量覆盖为宿主机映射地址（如 http://服务器IP:15002）
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'http://localhost:5002')

# B 站分片直连模式：True 时主清单由本服务代理重写、分片指向 B 站 CDN 直连
# （省服务器带宽，实测分片无 Referer 也可访问，防盗链只卡主清单）；
# False 时回退全代理（分片也经本服务转发，兼容性兜底，B 站收紧分片防盗链时用）
BILIBILI_DIRECT_SEGMENTS = os.environ.get('BILIBILI_DIRECT_SEGMENTS', 'true').lower() == 'true'

# 分组顺序：河南卫视（hntv官方）-> 央视 -> 卫视（健康率低放最后）-> B站直播，其余兜底
GROUP_ORDER = {HNTV_GROUP_NAME: 0, "央视": 1, "卫视": 2, BILIBILI_GROUP_NAME: 3}

# B 站直播测试模式：开启时聚合跳过 hntv 官方源与公开源拉取，只收集 B 站直播频道
# （测试 B 站接入期间启用，避免每次启动拉公开源+探测 70 频道拖慢重启；
# 正式使用设 BILIBILI_ONLY_MODE=false 恢复完整聚合）
BILIBILI_ONLY_MODE = os.environ.get('BILIBILI_ONLY_MODE', 'true').lower() == 'true'

# ---------------------------------------------------------------- 管理后台
# 管理界面/管理 API 的登录密码（session 鉴权；留空 = 禁用管理功能，安全默认）
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '').strip()
# 管理会话安全（上线加固）：
# - SESSION_COOKIE_SECURE：nginx 反代 + HTTPS 后设为 true，会话 Cookie 仅经 TLS 传输
# - ADMIN_SESSION_HOURS：登录会话有效期（小时，登录时 session.permanent=True 生效）
# - 登录防爆破：连续失败 ADMIN_LOGIN_MAX_FAILURES 次锁定 ADMIN_LOGIN_LOCKOUT_SECONDS 秒
SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true'
ADMIN_SESSION_HOURS = int(os.environ.get('ADMIN_SESSION_HOURS', '12'))
ADMIN_LOGIN_MAX_FAILURES = int(os.environ.get('ADMIN_LOGIN_MAX_FAILURES', '5'))
ADMIN_LOGIN_LOCKOUT_SECONDS = int(os.environ.get('ADMIN_LOGIN_LOCKOUT_SECONDS', '300'))
# 管理数据 SQLite 单文件（源配置/频道覆盖/监控历史/日志）
ADMIN_DB_PATH = os.environ.get('ADMIN_DB_PATH', os.path.join(XML_DATA_DIR, 'admin.db'))
# 滚动日志文件路径（logging 双 handler：文件 + SQLite logs 表）
LOG_FILE_PATH = os.environ.get('LOG_FILE_PATH', os.path.join(XML_DATA_DIR, 'app.log'))
# 历史数据保留上限（清理策略，防 DB 无限膨胀）
MONITOR_HISTORY_KEEP = int(os.environ.get('MONITOR_HISTORY_KEEP', '500'))     # 常规检测轮数
# 流探测条数：30 分钟一轮 × 约 70 频道 ≈ 3400 条/天，20000 约保留 6 天
STREAM_HISTORY_KEEP = int(os.environ.get('STREAM_HISTORY_KEEP', '20000'))
LOG_KEEP_DAYS = int(os.environ.get('LOG_KEEP_DAYS', '7'))                     # 日志保留天数
# 管理 API 列表分页：每页条数上限（超出截断）
MAX_PAGE_SIZE = 200
# 频道覆盖层内存缓存 TTL（秒）：聚合/频道列表查询覆盖配置的缓存时长
CHANNEL_OVERRIDE_CACHE_TTL = 60

# ---------------------------------------------------------------- 监控
# 检测目标（monitor 跑在 api 容器内部，自检用容器内端口 5002；
# 宿主机映射端口 15002 在容器内连不上。若未来加 nginx 反代，用环境变量覆盖）
HEALTH_URL = os.environ.get('MONITOR_HEALTH_URL', 'http://localhost:5002/health')
M3U_URL = os.environ.get('MONITOR_M3U_URL', 'http://localhost:5002/api/live.m3u8')
EPG_URL = os.environ.get('MONITOR_EPG_URL', 'http://localhost:5002/api/live.xml.gz')

# 频道数低于此值视为异常（正常约 70；公开源全挂只剩 hntv 时约 15，30 居中可捕获此隐蔽故障）
# B 站测试模式（BILIBILI_ONLY_MODE=true）下列表只有 1-2 个频道，自动用低阈值 1 避免误报；
# 仍可用环境变量 MIN_CHANNEL_COUNT 显式覆盖
MIN_CHANNEL_COUNT = int(os.environ.get(
    'MIN_CHANNEL_COUNT', '1' if BILIBILI_ONLY_MODE else '30'))

# 检测时段（GMT+8）：仅 8:00 - 24:00 检测，0:00-7:59 不检测（不消耗流量/不打扰）
CHECK_WINDOW_START_HOUR = 8     # 开始（含）
CHECK_WINDOW_END_HOUR = 24      # 结束（不含）

CHECK_INTERVAL = 600         # 健康检测间隔（秒），10 分钟一轮
STARTUP_DELAY = 90           # 首次检测延迟（秒）：等聚合任务跑完首次，避免启动初期误报

# 流地址可达性检测（低频全量探测）配置
STREAM_CHECK_INTERVAL = 1800        # 全量流探测间隔（秒），30 分钟一轮
STREAM_CHECK_CONCURRENCY = 10       # 并发探测数
STREAM_PROBE_TIMEOUT = 8            # 单流探测超时（秒）
STREAM_USER_AGENT = 'hntv-api-monitor'        # 监控探测 UA
STREAM_PROBE_UA_LOOSE = 'hntv-api-aggregator'  # 聚合过滤探测 UA（与监控区分，避免源端拒答差异）

# 分组可达率阈值（按用户对分组的重要性分级）：
# - 河南卫视（官方源，权重最高）：低于 90% 告警
# - 央视（iptv-org 源，稳定）：低于 80% 告警
# - 地方卫视（免费源，可达率天然低）：低于 20% 才告警
GROUP_HEALTH_RATIOS = {
    "河南卫视": 0.9,
    "央视": 0.8,
    "卫视": 0.2,
}
DEFAULT_GROUP_RATIO = 0.5           # 未配置分组的兜底阈值

# 参与邮件告警的分组：卫视组健康率低是常态，只检测并在日志展示，不触发告警邮件
ALERT_GROUPS = ("河南卫视", "央视")
