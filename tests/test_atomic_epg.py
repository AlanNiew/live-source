"""原子写入与 EPG 生成测试"""
import gzip
import os
import tempfile
import unittest
from unittest import mock

from core.atomic_io import atomic_write_gzip, atomic_write_text
from core.epg import XmlUtils


class AtomicIoTest(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def _path(self, name):
        return os.path.join(self.tmp_dir, name)

    def test_atomic_write_text(self):
        """文本原子写入：内容正确、无 .tmp 残留"""
        path = self._path('a.txt')
        atomic_write_text(path, '你好 hello')
        with open(path, encoding='utf-8') as f:
            self.assertEqual(f.read(), '你好 hello')
        self.assertFalse(os.path.exists(path + '.tmp'))
        # 覆盖已有文件
        atomic_write_text(path, '第二次')
        with open(path, encoding='utf-8') as f:
            self.assertEqual(f.read(), '第二次')

    def test_atomic_write_gzip(self):
        """gzip 原子写入：解压内容正确"""
        path = self._path('a.gz')
        atomic_write_gzip(path, '压缩内容测试')
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            self.assertEqual(f.read(), '压缩内容测试')
        self.assertFalse(os.path.exists(path + '.tmp'))

    def test_atomic_write_creates_dir(self):
        """父目录不存在时自动创建"""
        path = os.path.join(self.tmp_dir, 'sub', 'deep', 'a.txt')
        atomic_write_text(path, 'x')
        self.assertTrue(os.path.exists(path))


class EpgXmlTest(unittest.TestCase):
    """EPG 生成逻辑（mock 官方接口，验证输出格式与降级）"""

    def test_channel_block_format(self):
        """单频道块：channel + programme 结构正确，时间 GMT+8 格式"""
        item = {'name': '河南卫视', 'cid': 145}
        with mock.patch('core.epg.ApiUtils.get_hntv_epg_data') as m:
            resp = mock.Mock()
            resp.status_code = 200
            resp.json.return_value = {'programs': [
                {'title': '梨园春', 'beginTime': '1786511621', 'endTime': '1786515221'},
            ]}
            m.return_value = resp
            block = XmlUtils._build_channel_block(item)

        self.assertIn('<channel id="145">', block)
        self.assertIn('<display-name lang="zh">河南卫视</display-name>', block)
        self.assertIn('<programme start="', block)
        self.assertIn(' +0800"', block)  # GMT+8 时区格式
        self.assertIn('<title lang="zh">梨园春</title>', block)

    def test_channel_block_no_cid(self):
        """无 cid 的频道返回空块"""
        self.assertEqual(XmlUtils._build_channel_block({'name': 'X'}), "")

    def test_channel_block_epg_fail(self):
        """EPG 接口非 200：仍返回 channel 块（不带 programme）"""
        item = {'name': '河南卫视', 'cid': 145}
        with mock.patch('core.epg.ApiUtils.get_hntv_epg_data') as m:
            resp = mock.Mock()
            resp.status_code = 500
            m.return_value = resp
            block = XmlUtils._build_channel_block(item)
        self.assertIn('<channel id="145">', block)
        self.assertNotIn('<programme', block)

    def test_fetch_xml_non_200_returns_empty(self):
        """列表接口非 200：返回空 tv 默认"""
        with mock.patch('core.epg.ApiUtils.get_hntv_live_list') as m:
            resp = mock.Mock()
            resp.status_code = 500
            m.return_value = resp
            self.assertEqual(XmlUtils._fetch_xml_content(), XmlUtils.EMPTY_XML)

    def test_get_and_save_writes_files(self):
        """生成并原子落盘 xml 与 gz，内容一致"""
        tmp_dir = tempfile.mkdtemp()
        xml_path = os.path.join(tmp_dir, 'live.xml')
        gz_path = os.path.join(tmp_dir, 'live.xml.gz')
        with mock.patch('core.epg.XML_FILE_PATH', xml_path), \
             mock.patch('core.epg.GZ_FILE_PATH', gz_path), \
             mock.patch.object(XmlUtils, '_fetch_xml_content',
                               return_value='<?xml version="1.0" encoding="UTF-8"?><tv></tv>'):
            content = XmlUtils.get_and_save_xml_data()

        self.assertEqual(content, '<?xml version="1.0" encoding="UTF-8"?><tv></tv>')
        with open(xml_path, encoding='utf-8') as f:
            self.assertEqual(f.read(), content)
        with gzip.open(gz_path, 'rt', encoding='utf-8') as f:
            self.assertEqual(f.read(), content)


if __name__ == '__main__':
    unittest.main()
