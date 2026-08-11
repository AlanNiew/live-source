"""公开源处理：m3u 拉取/解析、地址质量评分、频道过滤与中文化"""
import re

import requests

from config import CARRIER_IP_PREFIXES, CCTV_NAME_MAP, PUBLIC_M3U_SOURCES, SIGN_PARAM_PAT


class SourceUtils:
    """公开 m3u 源工具类"""

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
    def fetch_all_public_channels():
        """拉取全部公开源并解析为频道列表"""
        channels = []
        for url in PUBLIC_M3U_SOURCES:
            m3u_text = SourceUtils.fetch_public_m3u(url)
            channels.extend(SourceUtils.parse_m3u_channels(m3u_text))
        return channels

    @staticmethod
    def iter_m3u_entries(m3u_text):
        """
        迭代 m3u 文本，产出 (EXTINF 行, 播放地址行) 条目
        共享的行级解析核心（parse_m3u_channels 与监控探测共用）
        :param m3u_text: m3u 原始文本
        :yield: (EXTINF 行字符串, 播放地址行)
        """
        if not m3u_text:
            return
        lines = m3u_text.strip().splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            # EXTINF 行后面紧跟一个播放地址行
            if line.startswith("#EXTINF") and i + 1 < len(lines):
                yield line, lines[i + 1].strip()
                i += 2
            else:
                i += 1

    @staticmethod
    def parse_m3u_channels(m3u_text):
        """
        按行解析 m3u 文本，提取频道列表
        :param m3u_text: m3u 原始文本
        :return: 频道 dict 列表，每个 dict 含 name/tvg_name/group_title/url
        """
        channels = []
        for line, url in SourceUtils.iter_m3u_entries(m3u_text):
            # 清洗多线路后缀：`$` 后是线路标记（tvbox 语法）、`;` 分隔备选地址，
            # 都只保留第一路，避免把整串当 URL 请求 404
            url = url.split('$')[0].split(';')[0].strip()

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

    @staticmethod
    def score_url(url):
        """
        评估流地址的公网可达性质量分，用于同名频道多来源时择优保留
        :param url: 流地址
        :return: 质量分（无签名域名=3 > 公网IP/签名域名=2 > 疑似内网IP=1 > 其他=0）
        """
        match = re.match(r'https?://([^\[/:]+)', url)
        if not match:
            return 0
        host = match.group(1)
        # 域名（含子域）通常指向 CDN/电视台官网，公网可达性最好
        if not re.match(r'\d+\.\d+\.\d+\.\d+$', host):
            score = 3
            # 带时效签名的域名源（cztv auth_key / jxtvcn token 等）会过期，降 1 分，
            # 让同台无签名源（如 wwb521 的 CDN 源）胜出
            if SIGN_PARAM_PAT.search(url):
                score = 2
            return score
        # 疑似运营商内网 IP
        if host.startswith(CARRIER_IP_PREFIXES):
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
                ch["_resolution"] = SourceUtils.extract_resolution(raw)
                ch["name"] = CCTV_NAME_MAP[bare]
                ch["tvg_name"] = CCTV_NAME_MAP[bare]
                ch["group_title"] = "央视"
                result.append(ch)
                continue

            # 2. 卫视频道：名称含"卫视"的保留（已为中文）
            if "卫视" in raw:
                # 记录原始分辨率（如 "河南卫视 (2160p)" -> 2160）
                ch["_resolution"] = SourceUtils.extract_resolution(raw)
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
    def normalize_name(name):
        """
        频道名归一化，用于去重对齐
        :param name: 原始频道名
        :return: 归一化后的频道名
        """
        normalized = name.strip()
        # 去掉尾部分辨率后缀，如 "河南卫视 (2160p)" -> "河南卫视"
        normalized = re.sub(r'\s*\(\d+[piK]+.*\)\s*$', '', normalized)
        return normalized.strip()
