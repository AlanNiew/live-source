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
# HNTV 官方源刷新间隔（秒）——官方接口签名有效期约 4h，3h 刷新留 1h 余量，保持签名新鲜
OFFICIAL_REFRESH_INTERVAL = 3 * 60 * 60

# 聚合时探测过滤不可达源（连续两轮失败才丢弃，避免源瞬时抖动被误杀）
FILTER_UNREACHABLE = True
STREAM_FAIL_LIMIT = 2                # 连续失败 N 轮才丢弃

# hntv 官方频道的分组名（降级路径与聚合路径共用，避免魔法字符串）
HNTV_GROUP_NAME = "河南卫视"
# 未识别分组时的默认组名
DEFAULT_GROUP_NAME = "其他"

# 分组顺序：河南卫视（hntv官方）-> 央视 -> 卫视（健康率低放最后），其余兜底
GROUP_ORDER = {HNTV_GROUP_NAME: 0, "央视": 1, "卫视": 2}

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

# ---------------------------------------------------------------- 监控
# 检测目标（monitor 跑在 api 容器内部，自检用容器内端口 5002；
# 宿主机映射端口 15002 在容器内连不上。若未来加 nginx 反代，用环境变量覆盖）
HEALTH_URL = os.environ.get('MONITOR_HEALTH_URL', 'http://localhost:5002/health')
M3U_URL = os.environ.get('MONITOR_M3U_URL', 'http://localhost:5002/api/live.m3u8')
EPG_URL = os.environ.get('MONITOR_EPG_URL', 'http://localhost:5002/api/live.xml.gz')

# 频道数低于此值视为异常（正常约 70；公开源全挂只剩 hntv 时约 15，30 居中可捕获此隐蔽故障）
MIN_CHANNEL_COUNT = 30

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
