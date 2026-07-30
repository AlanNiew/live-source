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
# 第一阶段只接入 iptv-org 的中国区源：实测可拉取，151 个频道，国际大项目最稳定
PUBLIC_M3U_SOURCES = [
    "https://iptv-org.github.io/iptv/countries/cn.m3u",
]

# 聚合结果落盘缓存路径（供 /api/live.m3u8 读取，避免每次请求都实时拉公开源）
AGGREGATED_M3U_PATH = os.path.join(XML_DATA_DIR, "aggregated.m3u")

# 聚合刷新间隔（秒）——每 6 小时刷新一次公开源
AGGREGATE_REFRESH_INTERVAL = 6 * 60 * 60


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
        合并 hntv 官方频道与公开源频道，按频道名去重，hntv 优先
        :param hntv_channels: hntv 官方频道列表（优先级高，同名保留其地址）
        :param public_channels: 公开源频道列表（只补充 hntv 没有的频道）
        :return: 合并后的 m3u 文本
        """
        merged = {}
        order = []  # 保持频道出现顺序，便于结果可读

        def add_channel(ch, source_label):
            key = AggregatorUtils.normalize_name(ch["name"])
            if key not in merged:
                merged[key] = ch
                order.append(key)

        # hntv 官方源先入（同名时官方地址优先保留）
        for ch in hntv_channels:
            add_channel(ch, "hntv")

        # 公开源补充 hntv 没有的频道
        for ch in public_channels:
            add_channel(ch, "public")

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

            # 3. 合并去重
            m3u_content = AggregatorUtils.aggregate_m3u(hntv_channels, public_channels)

            # 4. 落盘缓存
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
