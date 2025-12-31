import hashlib
import time
import requests
import os
import gzip
import datetime
import threading
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 定义GMT+8时区
GMT8 = datetime.timezone(datetime.timedelta(hours=8))

# 设置认证令牌，可以从环境变量获取，也可以直接设置
API_TOKEN = os.environ.get('API_TOKEN', 'hntv-secret-token-2025')
SECRET_KEY = os.environ.get('HNTV_SECRET_KEY', '6ca114a836ac7d73')

# 定义XML数据存储路径
XML_DATA_DIR = os.path.join(os.path.dirname(__file__), 'xml_data')
XML_FILE_PATH = os.path.join(XML_DATA_DIR, 'live.xml')
GZ_FILE_PATH = os.path.join(XML_DATA_DIR, 'live.xml.gz')

# 确保目录存在
os.makedirs(XML_DATA_DIR, exist_ok=True)


class TokenUtils:
    """令牌验证工具类"""
    
    @staticmethod
    def verify_token(token):
        """
        验证API令牌
        :param token: 提供的令牌
        :return: 令牌是否有效
        """
        return token == API_TOKEN


class CryptoUtils:
    """加密工具类"""
    
    @staticmethod
    def calculate_sha256_with_timestamp(secret_key="6ca114a836ac7d73",timestamp=str(int(time.time()))):
        """
        将指定字符串与当前时间秒进行sha256计算
        :param timestamp:
        :param secret_key: 默认为"6ca114a836ac7d73"
        :return: sha256哈希值
        """
        # 获取当前时间的秒数

        # 将字符串和时间拼接
        combined_string = secret_key + str(timestamp)
        
        # 进行sha256计算
        sha256_hash = hashlib.sha256(combined_string.encode('utf-8')).hexdigest()
        print(f"加密时间戳: {timestamp}")
        print(f"SHA256哈希值: {sha256_hash}")
        return sha256_hash


class ApiUtils:
    """API请求工具类"""
    
    @staticmethod
    def get_hntv_live_list():
        """
        封装API请求，使用时间戳和签名
        """
        url = "https://pubmod.hntv.tv/program/getAuth/live/class/program/11/"
        
        # 生成当前时间戳和签名
        timestamp = str(int(time.time()))
        sign = CryptoUtils.calculate_sha256_with_timestamp(SECRET_KEY,timestamp)
        
        headers = {
            'timestamp': timestamp,
            'sign': sign
        }
        
        response = requests.get(url, headers=headers)
        
        return response

    @staticmethod
    def get_hntv_epg_data(cid, date_timestamp):
        """
        获取EPG节目数据
        :param cid: 频道ID
        :param date_timestamp: 日期时间戳（当天零点）
        """
        url = f"https://pubmod.hntv.tv/program/getAuth/vod/originStream/program/{cid}/{date_timestamp}"
        
        # 生成当前时间戳和签名
        timestamp = str(int(time.time()))
        sign = CryptoUtils.calculate_sha256_with_timestamp(SECRET_KEY,timestamp)
        
        headers = {
            'timestamp': timestamp,
            'sign': sign
        }
        
        response = requests.get(url, headers=headers)
        return response


class XmlUtils:
    """XML处理工具类"""
    
    @staticmethod
    def get_and_save_xml_data():
        """
        获取XML数据并保存到文件，同时生成压缩文件
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
        从文件加载XML数据
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
    def transList2XML():
        # 使用从文件加载的XML数据
        return XmlUtils.load_xml_from_file()


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


class M3uUtils:
    """M3U处理工具类"""
    
    @staticmethod
    def transList2M3U():
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


class SchedulerUtils:
    """调度工具类"""
    
    @staticmethod
    def schedule_daily_xml_update():
        """
        定时每天更新XML数据
        """
        def update_xml_daily():
            while True:
                try:
                    # 计算到明天零点的时间间隔（秒），使用GMT+8时区
                    now = datetime.datetime.now(tz=GMT8)
                    tomorrow = now + datetime.timedelta(days=1)
                    next_midnight = tomorrow.replace(hour=2, minute=30, second=0, microsecond=0)
                    time_to_wait = (next_midnight - now).total_seconds()
                    
                    print(f"等待 {time_to_wait} 秒后更新XML数据...")
                    time.sleep(time_to_wait)
                    
                    # 获取并保存XML数据
                    XmlUtils.get_and_save_xml_data()
                    print("XML数据已更新")
                    
                except Exception as e:
                    print(f"定时更新XML数据时出错: {str(e)}")
        
        # 启动定时更新线程
        scheduler_thread = threading.Thread(target=update_xml_daily, daemon=True)
        scheduler_thread.start()