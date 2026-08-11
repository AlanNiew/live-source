"""EPG 节目单：XML 生成/读取/时间格式化（全部按 GMT+8）"""
import datetime
import gzip
import os

from config import GMT8, GZ_FILE_PATH, XML_DATA_DIR, XML_FILE_PATH
from core.hntv_client import ApiUtils


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

    @staticmethod
    def get_and_save_xml_data():
        """
        获取XML数据并保存到文件，同时生成压缩文件
        :return: XML 文本内容
        """
        try:
            response = ApiUtils.get_hntv_live_list()
            if response.status_code != 200:
                xml_content = '<?xml version="1.0" encoding="UTF-8"?><tv generator-info-name="hntv-live" generator-info-url="https://github.com/AlanNiew"></tv>'
            else:
                data = response.json()

                # 构建EPG XML内容
                xml_content = '<?xml version="1.0" encoding="UTF-8"?><tv generator-info-name="hntv-live" generator-info-url="https://github.com/AlanNiew">\n'

                # 如果数据结构不同，直接遍历响应数据
                if isinstance(data, list):
                    for item in data:
                        name = item.get('name', 'Unknown')
                        cid = item.get('cid')
                        if cid is not None:
                            # 添加频道信息
                            xml_content += f'<channel id="{cid}">\n<display-name lang="zh">{name}</display-name>\n</channel>\n'

                            # 获取EPG节目数据
                            today = datetime.datetime.now(tz=GMT8).date()
                            zero_time = datetime.datetime.combine(today, datetime.time.min, tzinfo=GMT8)
                            date_timestamp = int(zero_time.timestamp())

                            epg_response = ApiUtils.get_hntv_epg_data(cid, date_timestamp)
                            if epg_response.status_code == 200:
                                epg_data = epg_response.json()
                                if 'programs' in epg_data and isinstance(epg_data['programs'], list):
                                    for program in epg_data['programs']:
                                        title = program.get('title', 'Unknown')
                                        begin_time = program.get('beginTime', '')
                                        end_time = program.get('endTime', '')

                                        # 将时间戳转换为EPG格式 (YYYYMMDDHHMMSS +0800)
                                        begin_time_formatted = TimeUtils.format_timestamp_for_epg(begin_time)
                                        end_time_formatted = TimeUtils.format_timestamp_for_epg(end_time)

                                        xml_content += f'<programme start="{begin_time_formatted}" stop="{end_time_formatted}" channel="{cid}">\n<title lang="zh">{title}</title>\n</programme>\n'

                xml_content += '</tv>'

            # 保存原始XML文件
            with open(XML_FILE_PATH, 'w', encoding='utf-8') as f:
                f.write(xml_content)

            # 生成并保存压缩文件
            with gzip.open(GZ_FILE_PATH, 'wt', encoding='utf-8') as f:
                f.write(xml_content)

            print(f"XML数据已保存到 {XML_FILE_PATH} 和 {GZ_FILE_PATH}")
            return xml_content
        except Exception as e:
            print(f"获取并保存XML数据时出错: {str(e)}")
            # 返回默认XML内容
            return '<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hntv-live" generator-info-url="https://github.com/AlanNiew"></tv>'

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
            print(f"从文件加载XML数据时出错: {str(e)}")
            return '<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hntv-live" generator-info-url="https://github.com/AlanNiew"></tv>'

    @staticmethod
    def trans_list_to_xml():
        """读取缓存 XML（接口用），无缓存时现场生成"""
        return XmlUtils.load_xml_from_file()
