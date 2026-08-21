"""EPG 节目单：XML 生成/读取/时间格式化（全部按 GMT+8）"""
import datetime
import gzip
import os

from config import GMT8, GZ_FILE_PATH, XML_FILE_PATH
from core.atomic_io import atomic_write_gzip, atomic_write_text
from core.hntv_client import ApiUtils
from core.logger import get_logger

_logger = get_logger('epg')


class TimeUtils:
    """时间处理工具类"""

    @staticmethod
    def format_timestamp_for_epg(timestamp_str):
        """
        将时间戳格式化为EPG格式 (YYYYMMDDHHMMSS +0800)
        :param timestamp_str: 时间戳字符串
        :return: 格式化后的时间字符串
        """
        try:
            timestamp = int(timestamp_str)
            dt = datetime.datetime.fromtimestamp(timestamp, tz=GMT8)
            return dt.strftime('%Y%m%d%H%M%S +0800')
        except (ValueError, TypeError):
            # 如果转换失败，返回当前时间的格式化字符串（使用GMT+8时区）
            return datetime.datetime.now(tz=GMT8).strftime('%Y%m%d%H%M%S +0800')


class XmlUtils:
    """EPG XML 处理工具类"""

    # 空 tv 默认 XML（接口失败/异常时的兜底返回，同时落盘）
    EMPTY_XML = ('<?xml version="1.0" encoding="UTF-8"?>'
                 '<tv generator-info-name="hntv-live" '
                 'generator-info-url="https://github.com/AlanNiew"></tv>')

    # XML 头（channel/programme 均追加在此之后，结束于 </tv>）
    XML_HEADER = ('<?xml version="1.0" encoding="UTF-8"?>'
                  '<tv generator-info-name="hntv-live" '
                  'generator-info-url="https://github.com/AlanNiew">\n')

    @staticmethod
    def _build_channel_block(item):
        """
        构建单个频道的 XML 块（channel 定义 + 当日 programme 列表）
        :param item: 官方接口返回的单个频道 dict
        :return: XML 块字符串（无 cid 时返回空串）
        """
        name = item.get('name', 'Unknown')
        cid = item.get('cid')
        if cid is None:
            return ""

        block = f'<channel id="{cid}">\n' \
                f'<display-name lang="zh">{name}</display-name>\n' \
                f'</channel>\n'

        # 拉取当日 EPG 节目数据（当天零点时间戳）
        today = datetime.datetime.now(tz=GMT8).date()
        zero_time = datetime.datetime.combine(today, datetime.time.min, tzinfo=GMT8)
        epg_response = ApiUtils.get_hntv_epg_data(cid, int(zero_time.timestamp()))
        if epg_response.status_code != 200:
            return block
        epg_data = epg_response.json()
        if not isinstance(epg_data, dict) or not isinstance(epg_data.get('programs'), list):
            return block

        for program in epg_data['programs']:
            title = program.get('title', 'Unknown')
            begin_time = TimeUtils.format_timestamp_for_epg(program.get('beginTime', ''))
            end_time = TimeUtils.format_timestamp_for_epg(program.get('endTime', ''))
            block += f'<programme start="{begin_time}" stop="{end_time}" channel="{cid}">\n' \
                     f'<title lang="zh">{title}</title>\n' \
                     f'</programme>\n'
        return block

    @staticmethod
    def _fetch_xml_content():
        """
        拉取频道列表并构建完整 XML 内容
        :return: XML 文本（接口非 200 时返回空 tv 默认）
        """
        response = ApiUtils.get_hntv_live_list()
        if response.status_code != 200:
            return XmlUtils.EMPTY_XML

        data = response.json()
        xml_content = XmlUtils.XML_HEADER
        if isinstance(data, list):
            for item in data:
                xml_content += XmlUtils._build_channel_block(item)
        xml_content += '</tv>'
        return xml_content

    @staticmethod
    def get_and_save_xml_data():
        """
        获取XML数据并保存到文件（原子写入），同时生成压缩文件
        :return: XML 文本内容
        """
        try:
            xml_content = XmlUtils._fetch_xml_content()
            atomic_write_text(XML_FILE_PATH, xml_content)
            atomic_write_gzip(GZ_FILE_PATH, xml_content)
            _logger.info(f"XML数据已保存到 {XML_FILE_PATH} 和 {GZ_FILE_PATH}")
            return xml_content
        except Exception as e:
            _logger.warning(f"获取并保存XML数据时出错: {str(e)}")
            return XmlUtils.EMPTY_XML

    @staticmethod
    def load_xml_from_file():
        """
        从文件加载XML数据（优先 gz，其次 xml，都没有则现场生成）
        :return: XML 文本内容
        """
        try:
            if os.path.exists(GZ_FILE_PATH):
                with gzip.open(GZ_FILE_PATH, 'rt', encoding='utf-8') as f:
                    return f.read()
            elif os.path.exists(XML_FILE_PATH):
                with open(XML_FILE_PATH, 'r', encoding='utf-8') as f:
                    return f.read()
            else:
                # 如果文件不存在，获取并保存数据
                return XmlUtils.get_and_save_xml_data()
        except Exception as e:
            _logger.warning(f"从文件加载XML数据时出错: {str(e)}")
            return XmlUtils.EMPTY_XML

    @staticmethod
    def trans_list_to_xml():
        """读取缓存 XML（接口用），无缓存时现场生成"""
        return XmlUtils.load_xml_from_file()
