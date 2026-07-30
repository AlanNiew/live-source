import os
import re
import time
import threading

import requests
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 复用 utils.py 中定义的数据目录，保证聚合结果与 xml 数据落在一起
from utils import XML_DATA_DIR

# 公开 m3u 源列表（代码常量，与现有 hntv API URL 的硬编码模式一致；只有密钥才进 .env）
# 两个源互补：
# - iptv-org：央视全（17个），但卫视多为运营商内网IP，公网可达性差
# - hujingguang：卫视用电视台自有域名（cztv.com/jxtvcn.com.cn 等），公网可达性高
# 聚合去重时按地址质量评分选优（域名 > 公网IP > 内网IP），自动保留可达性更好的源
PUBLIC_M3U_SOURCES = [
    "https://iptv-org.github.io/iptv/countries/cn.m3u",
    "https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/cnTV_AutoUpdate.m3u8",
]

# 聚合结果落盘缓存路径（供 /api/live.m3u8 读取，避免每次请求都实时拉公开源）
AGGREGATED_M3U_PATH = os.path.join(XML_DATA_DIR, "aggregated.m3u")

# 聚合刷新间隔（秒）——每 6 小时刷新一次公开源
AGGREGATE_REFRESH_INTERVAL = 6 * 60 * 60

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


class AggregatorUtils:
    """多源直播源聚合工具类"""

    @staticmethod
    def fetch_public_m3u(url):
        """
        拉取单个公开 m3u 源
        :param url: m3u 源地址
        :return: 成功返回 m3u 文本，失败返回空字符串（不抛异常，单源挂掉不影响其他）
        """
        try:
            response = requests.get(url, timeout=20)
            if response.status_code != 200:
                print(f"拉取公开源失败({response.status_code}): {url}")
                return ""
            print(f"拉取公开源成功: {url}")
            return response.text
        except Exception as e:
            print(f"拉取公开源出错: {url} -> {str(e)}")
            return ""

    @staticmethod
    def parse_m3u_channels(m3u_text):
        """
        按行解析 m3u 文本，提取频道列表
        :param m3u_text: m3u 原始文本
        :return: 频道 dict 列表，每个 dict 含 name/tvg_name/group_title/url
        """
        channels = []
        if not m3u_text:
            return channels

        lines = m3u_text.strip().splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            # EXTINF 行后面紧跟一个播放地址行
            if line.startswith("#EXTINF") and i + 1 < len(lines):
                url = lines[i + 1].strip()

                # 解析 tvg-name / group-title 属性
                tvg_name = ""
                group_title = "其他"

                tvg_match = re.search(r'tvg-name="([^"]*)"', line)
                if tvg_match:
                    tvg_name = tvg_match.group(1)

                group_match = re.search(r'group-title="([^"]*)"', line)
                if group_match:
                    group_title = group_match.group(1)

                # 频道名取 EXTINF 行末尾逗号后的部分
                name = line.split(",")[-1].strip()

                channels.append({
                    "name": name,
                    "tvg_name": tvg_name or name,
                    "group_title": group_title,
                    "url": url,
                })
                i += 2
            else:
                i += 1

        return channels

    @staticmethod
    def extract_resolution(name):
        """
        从频道名提取分辨率数值，用于同台多分辨率时选最高清
        :param name: 频道名（如 "CCTV-1 (1080p)"）
        :return: 分辨率高度数值（1080）；无法识别返回 0
        """
        match = re.search(r'\((\d+)[piK]', name)
        return int(match.group(1)) if match else 0

    # 疑似运营商 IPTV 内网 IP 段前缀（这些段多为移动/电信/联通的 IPTV 专网，
    # 公网环境通常不可达）。用于地址质量评分，优先选域名等公网可达的流
    CARRIER_IP_PREFIXES = (
        '118.', '112.', '111.', '117.', '120.', '183.', '39.', '27.',
        '125.', '61.', '218.', '211.', '60.', '175.',
    )

    @staticmethod
    def score_url(url):
        """
        评估流地址的公网可达性质量分，用于同名频道多来源时择优保留
        :param url: 流地址
        :return: 质量分（域名=3 > 公网IP=2 > 疑似内网IP=1 > 其他=0）
        """
        match = re.match(r'https?://([^\[/:]+)', url)
        if not match:
            return 0
        host = match.group(1)
        # 域名（含子域）通常指向 CDN/电视台官网，公网可达性最好
        if not re.match(r'\d+\.\d+\.\d+\.\d+$', host):
            return 3
        # 疑似运营商内网 IP
        if host.startswith(AggregatorUtils.CARRIER_IP_PREFIXES):
            return 1
        # 其他公网 IP
        return 2

    @staticmethod
    def filter_and_translate(channels):
        """
        过滤公开源频道，只保留央视开路频道 + 各省卫视，并把英文名中文化
        :param channels: 解析出的公开源频道列表
        :return: 过滤+中文化后的频道列表
        """
        result = []
        for ch in channels:
            raw = ch["name"]

            # 1. CCTV 开路频道：匹配 CCTV_NAME_MAP 的 key（去掉分辨率后缀后比对）
            bare = re.sub(r'\s*\(\d+[piK]+.*\)\s*$', '', raw).strip()
            if bare in CCTV_NAME_MAP:
                # 记录原始分辨率，供去重时选最高清；再用中文标准名替换显示名
                ch["_resolution"] = AggregatorUtils.extract_resolution(raw)
                ch["name"] = CCTV_NAME_MAP[bare]
                ch["tvg_name"] = CCTV_NAME_MAP[bare]
                ch["group_title"] = "央视"
                result.append(ch)
                continue

            # 2. 卫视频道：名称含"卫视"的保留（已为中文）
            if "卫视" in raw:
                # 记录原始分辨率（如 "河南卫视 (2160p)" -> 2160）
                ch["_resolution"] = AggregatorUtils.extract_resolution(raw)
                # BRTV 北京卫视 这种带英文前缀的，去掉前缀只留中文部分
                cn_part = re.search(r'([\u4e00-\u9fa5]+卫视)', raw)
                if cn_part:
                    ch["name"] = cn_part.group(1)
                    ch["tvg_name"] = cn_part.group(1)
                ch["group_title"] = "卫视"
                result.append(ch)
                continue

            # 3. 其余全部过滤（地方台/英文台/付费频道/国际版等）

        print(f"过滤+中文化：{len(channels)} 个 -> 保留 {len(result)} 个（央视+卫视）")
        return result

    @staticmethod
    def normalize_name(name, source_prefix=""):
        """
        频道名归一化，用于去重对齐
        :param name: 原始频道名
        :param source_prefix: 来源前缀（为未来多省台接入预留，当前不传；
                              届时如 "河南" 可把裸名地面频道区分开）
        :return: 归一化后的频道名
        """
        normalized = name.strip()
        # 去掉尾部分辨率后缀，如 "河南卫视 (2160p)" -> "河南卫视"
        normalized = re.sub(r'\s*\(\d+[piK]+.*\)\s*$', '', normalized)
        normalized = normalized.strip()
        if source_prefix:
            normalized = f"{source_prefix}-{normalized}"
        return normalized

    @staticmethod
    def aggregate_m3u(hntv_channels, public_channels):
        """
        合并 hntv 官方频道与公开源频道：
        - 按频道名去重，hntv 官方源优先（同名保留官方地址）
        - 公开源只补充 hntv 没有的频道
        - 公开源内同台多个分辨率时，保留清晰度最高的一个
        :param hntv_channels: hntv 官方频道列表（优先级最高）
        :param public_channels: 公开源频道列表（已过滤+中文化，只补充 hntv 没有的）
        :return: 合并后的 m3u 文本
        """
        merged = {}
        order = []  # 保持频道出现顺序，便于结果可读

        # hntv 官方源先入（优先级最高，同名时官方地址始终保留）
        for ch in hntv_channels:
            key = AggregatorUtils.normalize_name(ch["name"])
            if key not in merged:
                merged[key] = ch
                order.append(key)

        # 公开源补充 hntv 没有的频道；同名多来源时按(地址质量, 分辨率)择优
        # 地址质量高的（域名 > 公网IP > 内网IP）优先，保证公网可达性
        public_best = {}  # key -> (频道, 地址质量分, 分辨率)
        for ch in public_channels:
            key = AggregatorUtils.normalize_name(ch["name"])
            score = AggregatorUtils.score_url(ch["url"])
            res = ch.get("_resolution", 0)
            if key not in public_best:
                public_best[key] = (ch, score, res)
            else:
                _ch, old_score, old_res = public_best[key]
                # 地址质量更高，或质量相同但分辨率更高，则替换
                if score > old_score or (score == old_score and res > old_res):
                    public_best[key] = (ch, score, res)

        for key, (ch, _score, _res) in public_best.items():
            if key not in merged:  # 只补充 hntv 没有的
                merged[key] = ch
                order.append(key)

        # 生成 m3u 文本
        m3u_content = "#EXTM3U\n\n"
        for key in order:
            ch = merged[key]
            m3u_content += (
                f'#EXTINF:-1 tvg-id="{ch["tvg_name"]}" tvg-name="{ch["tvg_name"]}" '
                f'group-title="{ch["group_title"]}",{ch["name"]}\n'
                f'{ch["url"]}\n\n'
            )

        print(f"聚合完成：hntv {len(hntv_channels)} 个 + 公开补充 "
              f"{len(merged) - len(hntv_channels)} 个 = 共 {len(merged)} 个频道")
        return m3u_content

    @staticmethod
    def get_aggregated_m3u():
        """
        拉取所有源并合并，落盘到缓存文件
        :return: 聚合后的 m3u 文本；失败时返回 None
        """
        try:
            # 1. 拉取 hntv 官方频道（复用现有 ApiUtils）
            hntv_channels = []
            try:
                from utils import ApiUtils
                response = ApiUtils.get_hntv_live_list()
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list):
                        for item in data:
                            name = item.get('name', 'Unknown')
                            cid = item.get('cid')
                            streams = item.get('video_streams') or item.get('streams', [])
                            if streams:
                                hntv_channels.append({
                                    "name": name,
                                    "tvg_name": str(cid) if cid is not None else name,
                                    "group_title": "河南卫视",
                                    "url": streams[0],
                                })
            except Exception as e:
                print(f"拉取 hntv 官方源出错: {str(e)}")

            # 2. 拉取公开源频道
            public_channels = []
            for url in PUBLIC_M3U_SOURCES:
                m3u_text = AggregatorUtils.fetch_public_m3u(url)
                public_channels.extend(AggregatorUtils.parse_m3u_channels(m3u_text))

            # 3. 过滤+中文化（只保留央视开路+卫视，英文名转中文）
            public_channels = AggregatorUtils.filter_and_translate(public_channels)

            # 4. 合并去重
            m3u_content = AggregatorUtils.aggregate_m3u(hntv_channels, public_channels)

            # 5. 落盘缓存
            os.makedirs(XML_DATA_DIR, exist_ok=True)
            with open(AGGREGATED_M3U_PATH, 'w', encoding='utf-8') as f:
                f.write(m3u_content)
            print(f"聚合结果已保存到 {AGGREGATED_M3U_PATH}")

            return m3u_content
        except Exception as e:
            print(f"生成聚合 m3u 出错: {str(e)}")
            return None

    @staticmethod
    def load_aggregated_m3u():
        """
        读取落盘的聚合结果；文件不存在则触发一次生成
        :return: 聚合 m3u 文本
        """
        try:
            if os.path.exists(AGGREGATED_M3U_PATH):
                with open(AGGREGATED_M3U_PATH, 'r', encoding='utf-8') as f:
                    return f.read()
            # 不存在则生成
            print("聚合缓存文件不存在，触发首次生成")
            content = AggregatorUtils.get_aggregated_m3u()
            return content if content else "#EXTM3U\n# 聚合数据生成失败\n"
        except Exception as e:
            print(f"读取聚合缓存出错: {str(e)}")
            return "#EXTM3U\n# 读取聚合缓存出错\n"


class AggregatorScheduler:
    """聚合结果定时刷新调度（复刻 utils.py 的 SchedulerUtils 写法）"""

    @staticmethod
    def schedule_aggregate_refresh():
        """
        启动 daemon 线程，定时刷新聚合结果
        依赖 GUNICORN_WORKERS=1 保证线程只起一次（已在 gunicorn.conf.py 配置）
        """

        def refresh_loop():
            while True:
                try:
                    # 首次启动立即刷新一次，之后按间隔刷新
                    AggregatorUtils.get_aggregated_m3u()
                    print("聚合 m3u 已刷新")
                    time.sleep(AGGREGATE_REFRESH_INTERVAL)
                except Exception as e:
                    print(f"定时刷新聚合 m3u 出错: {str(e)}")
                    time.sleep(60)  # 出错后等 1 分钟再试，避免狂跑

        scheduler_thread = threading.Thread(target=refresh_loop, daemon=True)
        scheduler_thread.start()
