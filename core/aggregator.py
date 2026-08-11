"""聚合编排：多源合并去重择优、探测过滤、缓存落盘、跨轮失败记录"""
import json
import os
from concurrent.futures import ThreadPoolExecutor

from config import (AGGREGATED_M3U_PATH, FILTER_UNREACHABLE, GROUP_ORDER,
                    STREAM_CHECK_CONCURRENCY, STREAM_FAILURES_PATH,
                    STREAM_FAIL_LIMIT, XML_DATA_DIR)
from core.hntv_client import ApiUtils
from core.probing import probe_stream
from core.sources import SourceUtils


class AggregatorUtils:
    """多源直播源聚合工具类"""

    @staticmethod
    def fetch_hntv_channels():
        """
        拉取 hntv 官方频道（优先级最高）
        :return: 频道 dict 列表（空列表表示拉取失败）
        """
        hntv_channels = []
        try:
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
        return hntv_channels

    @staticmethod
    def pick_best_public(public_channels):
        """
        公开源同台多来源择优（按地址质量、分辨率）
        :param public_channels: 已过滤+中文化的公开源频道
        :return: key -> (频道, 地址质量分, 分辨率) 的 dict
        """
        public_best = {}
        for ch in public_channels:
            key = SourceUtils.normalize_name(ch["name"])
            score = SourceUtils.score_url(ch["url"])
            res = ch.get("_resolution", 0)
            if key not in public_best:
                public_best[key] = (ch, score, res)
            else:
                _ch, old_score, old_res = public_best[key]
                # 地址质量更高，或质量相同但分辨率更高，则替换
                if score > old_score or (score == old_score and res > old_res):
                    public_best[key] = (ch, score, res)
        return public_best

    @staticmethod
    def aggregate_m3u(hntv_channels, public_channels):
        """
        合并 hntv 官方频道与公开源频道：
        - 按频道名去重，hntv 官方源优先（同名保留官方地址）
        - 公开源只补充 hntv 没有的频道
        - 公开源内同台多个分辨率时，保留清晰度最高的一个
        - 公开源补充频道做可达性探测过滤（连续两轮失败才丢弃，官方源跳过）
        :param hntv_channels: hntv 官方频道列表（优先级最高）
        :param public_channels: 公开源频道列表（已过滤+中文化，只补充 hntv 没有的）
        :return: 合并后的 m3u 文本
        """
        merged = {}
        order = []  # 保持频道出现顺序，便于结果可读

        # hntv 官方源先入（优先级最高，同名时官方地址始终保留）
        for ch in hntv_channels:
            key = SourceUtils.normalize_name(ch["name"])
            if key not in merged:
                merged[key] = ch
                order.append(key)

        # 公开源补充 hntv 没有的频道（同台按地址质量/分辨率择优）
        public_best = AggregatorUtils.pick_best_public(public_channels)
        for key, (ch, _score, _res) in public_best.items():
            if key not in merged:
                merged[key] = ch
                order.append(key)

        # 分组顺序：河南卫视（hntv官方）-> 央视 -> 卫视（健康率低放最后），其余兜底
        order.sort(key=lambda k: GROUP_ORDER.get(merged[k]["group_title"], 3))

        # 探测过滤：公开源补充频道连续两轮不可达才丢弃（hntv 官方源跳过）
        hntv_keys = {SourceUtils.normalize_name(ch["name"]) for ch in hntv_channels}
        AggregatorUtils.filter_unreachable(merged, order, hntv_keys)

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

    # ------------------------------------------------------------ 探测过滤

    @staticmethod
    def _load_failures():
        """读取跨轮失败记录 {url: 连续失败次数}；文件不存在或损坏返回空 dict"""
        try:
            if os.path.exists(STREAM_FAILURES_PATH):
                with open(STREAM_FAILURES_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"读取失败记录出错: {str(e)}")
        return {}

    @staticmethod
    def _save_failures(failures):
        """保存失败记录"""
        try:
            os.makedirs(XML_DATA_DIR, exist_ok=True)
            with open(STREAM_FAILURES_PATH, 'w', encoding='utf-8') as f:
                json.dump(failures, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存失败记录出错: {str(e)}")

    @staticmethod
    def filter_unreachable(merged, order, hntv_keys):
        """
        对合并结果做可达性探测过滤（仅公开源补充的频道）：
        - 宽松判定探测（200/206/403 均可达）；本轮不可达计数+1，
          连续 STREAM_FAIL_LIMIT 轮失败才丢弃，本轮可达清空计数
        - hntv 官方频道跳过，永不因探测被过滤
        :param merged: key -> 频道 dict（会被就地修改）
        :param order: 频道 key 顺序列表（会被就地修改）
        :param hntv_keys: hntv 官方频道 key 集合
        """
        if not FILTER_UNREACHABLE:
            return

        # 只探测公开源补充的频道
        probe_keys = [k for k in order if k not in hntv_keys]
        urls = {k: merged[k]["url"] for k in probe_keys}

        failures = AggregatorUtils._load_failures()
        probed_urls = set(urls.values())

        # 并发探测（宽松判定：403 也算可达）
        results = {}
        with ThreadPoolExecutor(max_workers=STREAM_CHECK_CONCURRENCY) as executor:
            for url, ok in zip(urls.values(),
                               executor.map(lambda u: probe_stream(u, accept_403=True), urls.values())):
                results[url] = ok

        dropped = []
        for key in probe_keys:
            url = urls[key]
            if results[url]:
                failures.pop(url, None)
            else:
                failures[url] = failures.get(url, 0) + 1
                if failures[url] >= STREAM_FAIL_LIMIT:
                    dropped.append(key)

        for key in dropped:
            order.remove(key)
            del merged[key]
        if dropped:
            print(f"探测过滤：丢弃 {len(dropped)} 个连续 {STREAM_FAIL_LIMIT} 轮不可达的频道")
            for key in dropped:
                print(f"  丢弃: {key}")

        # 保存失败记录（只保留本轮探测过的 URL，自动裁剪过期项）
        failures = {u: c for u, c in failures.items() if u in probed_urls}
        AggregatorUtils._save_failures(failures)

    # ------------------------------------------------------------ 缓存落盘

    @staticmethod
    def get_aggregated_m3u():
        """
        拉取所有源并合并，落盘到缓存文件
        :return: 聚合后的 m3u 文本；失败时返回 None
        """
        try:
            # 1. 拉取 hntv 官方频道
            hntv_channels = AggregatorUtils.fetch_hntv_channels()

            # 2. 拉取公开源频道
            public_channels = SourceUtils.fetch_all_public_channels()

            # 3. 过滤+中文化（只保留央视开路+卫视，英文名转中文）
            public_channels = SourceUtils.filter_and_translate(public_channels)

            # 4. 合并去重 + 探测过滤
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

    # ------------------------------------------------------------ 降级路径

    @staticmethod
    def get_hntv_only_m3u():
        """仅返回 hntv 官方频道（聚合失败时的降级路径，保证不比现状更差）"""
        response = ApiUtils.get_hntv_live_list()
        if response.status_code != 200:
            return "#EXTM3U\n# Error: Failed to fetch data"

        data = response.json()
        m3u_content = "#EXTM3U\n\n"
        if isinstance(data, list):
            for item in data:
                name = item.get('name', 'Unknown')
                cid = item.get('cid')
                streams = item.get('video_streams') or item.get('streams', [])
                if streams:
                    stream_url = streams[0]
                    m3u_content += f'#EXTINF:-1 tvg-id="{cid}" tvg-name="{name}" group-title="河南卫视",{name}\n'
                    m3u_content += f'{stream_url}\n\n'

        return m3u_content

    @staticmethod
    def trans_list_to_m3u():
        """
        直播列表接口主路径：优先返回多源聚合结果（hntv 官方 + 公开源央视/卫视），
        聚合为空/异常时降级为 hntv 官方源
        :return: m3u 文本
        """
        try:
            aggregated = AggregatorUtils.load_aggregated_m3u()
            if aggregated and "#EXTM3U" in aggregated:
                return aggregated
            print("聚合结果为空，降级为 hntv 官方源")
        except Exception as e:
            print(f"读取聚合结果失败，降级为 hntv 官方源: {str(e)}")

        return AggregatorUtils.get_hntv_only_m3u()
