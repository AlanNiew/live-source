"""行为等价对照测试：新实现 vs 旧版参考实现（从 git show 8193bc9^ 提取）

用途：防止重构/修复引入旧版不具备的边界行为差异。
旧版参考逻辑内联于此（utils.py 的 get_hntv_only_m3u / monitor.py 的 fetch_m3u_urls），
对同一输入断言新旧输出逐字节一致。
"""
import re
import unittest
from unittest import mock

from core.aggregator import AggregatorUtils
from monitoring.checks import CheckUtils

# ---------------------------------------------------------------- 旧版参考实现

def _legacy_get_hntv_only_m3u(data):
    """旧版 M3uUtils.get_hntv_only_m3u 的列表构建逻辑（tvg-id 直拼 cid）"""
    m3u_content = "#EXTM3U\n\n"
    if isinstance(data, list):
        for item in data:
            name = item.get('name', 'Unknown')
            cid = item.get('cid')
            streams = item.get('video_streams') or item.get('streams', [])
            if streams:
                stream_url = streams[0]
                m3u_content += (
                    f'#EXTINF:-1 tvg-id="{cid}" tvg-name="{name}" '
                    f'group-title="河南卫视",{name}\n{stream_url}\n\n'
                )
    return m3u_content


def _legacy_fetch_m3u_groups(text):
    """旧版 MonitorUtils.fetch_m3u_urls 的行级状态机（cur_group 跨行继承）"""
    items = []
    cur_group = "其他"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF'):
            m = re.search(r'group-title="([^"]*)"', line)
            if m:
                cur_group = m.group(1)
        elif line.startswith(('http://', 'https://')):
            items.append((line, cur_group))
    return items


# ---------------------------------------------------------------- 测试

class LegacyGetHntvOnlyM3uEquivalenceTest(unittest.TestCase):
    """get_hntv_only_m3u：新版输出与旧版逐字节一致"""

    def _run_new(self, data, status=200):
        with mock.patch('core.aggregator.ApiUtils.get_hntv_live_list') as m:
            resp = mock.Mock()
            resp.status_code = status
            resp.json.return_value = data
            m.return_value = resp
            return AggregatorUtils.get_hntv_only_m3u()

    def test_normal_cid(self):
        """cid 为数字/字符串：一致"""
        data = [
            {'name': '河南卫视', 'cid': 145, 'video_streams': ['http://a/1.m3u8']},
            {'name': '新闻频道', 'cid': '149', 'streams': ['http://b/2.m3u8']},
        ]
        self.assertEqual(self._run_new(data), _legacy_get_hntv_only_m3u(data))

    def test_cid_none(self):
        """cid=None：旧版输出 tvg-id="None"，新版必须一致"""
        data = [{'name': '频道A', 'cid': None, 'video_streams': ['http://a/1.m3u8']}]
        self.assertEqual(self._run_new(data), _legacy_get_hntv_only_m3u(data))
        self.assertIn('tvg-id="None"', _legacy_get_hntv_only_m3u(data))

    def test_no_streams_skipped(self):
        """无可用流地址的条目跳过"""
        data = [
            {'name': 'A', 'cid': 1, 'video_streams': ['http://a/1.m3u8']},
            {'name': 'B', 'cid': 2, 'streams': []},
            {'name': 'C', 'cid': 3},
        ]
        self.assertEqual(self._run_new(data), _legacy_get_hntv_only_m3u(data))

    def test_non_200(self):
        """接口非 200：返回错误占位文本"""
        data = []
        self.assertEqual(self._run_new(data, status=500),
                         "#EXTM3U\n# Error: Failed to fetch data")


class LegacyFetchM3uGroupsEquivalenceTest(unittest.TestCase):
    """fetch_m3u_groups：新版输出与旧版逐元素一致"""

    def _run_new(self, text):
        with mock.patch('monitoring.checks.requests.get') as m:
            resp = mock.Mock()
            resp.status_code = 200
            resp.text = text
            m.return_value = resp
            return CheckUtils.fetch_m3u_groups()

    def test_normal(self):
        """规范 m3u：EXTINF+group+URL 成对"""
        text = (
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="河南卫视",河南卫视\nhttp://a/1.m3u8\n'
            '#EXTINF:-1 group-title="央视",CCTV-1 综合\nhttp://b/2.m3u8\n'
        )
        self.assertEqual(self._run_new(text), _legacy_fetch_m3u_groups(text))

    def test_bare_url_line(self):
        """裸 http(s) URL 行（无 EXTINF）：旧版收集并归属当前组，新版必须一致"""
        text = (
            '#EXTINF:-1 group-title="卫视",北京卫视\nhttp://a/1.m3u8\n'
            "http://bare/2.m3u8\n"
        )
        self.assertEqual(self._run_new(text), _legacy_fetch_m3u_groups(text))

    def test_extinf_without_group(self):
        """EXTINF 缺 group-title：沿用上一组"""
        text = (
            '#EXTINF:-1 group-title="央视",CCTV-1 综合\nhttp://a/1.m3u8\n'
            "#EXTINF:-1,某频道\nhttp://b/2.m3u8\n"
        )
        self.assertEqual(self._run_new(text), _legacy_fetch_m3u_groups(text))

    def test_rtmp_excluded(self):
        """rtmp 流不计入（非 http(s)）"""
        text = (
            '#EXTINF:-1 group-title="卫视",某卫视\n'
            "rtmp://rtmp.tv/stream/1\nhttp://a/1.m3u8\n"
        )
        self.assertEqual(self._run_new(text), _legacy_fetch_m3u_groups(text))

    def test_empty_and_blank(self):
        """空文本与纯空白行"""
        self.assertEqual(self._run_new(""), _legacy_fetch_m3u_groups(""))
        self.assertEqual(self._run_new("\n\n  \n#EXTM3U\n\n"),
                         _legacy_fetch_m3u_groups("\n\n  \n#EXTM3U\n\n"))

    def test_group_reset_after_new_group(self):
        """新 EXTINF 带新组后，URL 归属更新"""
        text = (
            '#EXTINF:-1 group-title="卫视",北京卫视\nhttp://a/1.m3u8\n'
            '#EXTINF:-1 group-title="央视",CCTV-1 综合\nhttp://b/2.m3u8\n'
            "http://c/3.m3u8\n"
        )
        self.assertEqual(self._run_new(text), _legacy_fetch_m3u_groups(text))


if __name__ == '__main__':
    unittest.main()
