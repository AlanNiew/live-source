import hashlib
import time
import requests
from flask import Flask, request as flask_request, jsonify, abort, send_file
from flask_caching import Cache
import os
from dotenv import load_dotenv
import gzip
import json
import threading
import datetime
import atexit
import signal

# 加载环境变量
load_dotenv()
# 创建Flask应用
app = Flask(__name__)

# 配置缓存
app.config['CACHE_TYPE'] = 'simple'  # 使用简单缓存
app.config['CACHE_DEFAULT_TIMEOUT'] = 600  # 默认10分钟缓存

# 创建缓存实例
cache = Cache(app)

# 定义XML数据存储路径
XML_DATA_DIR = os.path.join(os.path.dirname(__file__), 'xml_data')
XML_FILE_PATH = os.path.join(XML_DATA_DIR, 'live.xml')
GZ_FILE_PATH = os.path.join(XML_DATA_DIR, 'live.xml.gz')

# 确保目录存在
os.makedirs(XML_DATA_DIR, exist_ok=True)

# 设置认证令牌，可以从环境变量获取，也可以直接设置
API_TOKEN = os.environ.get('API_TOKEN', 'hntv-secret-token-2025')
SECRET_KEY = os.environ.get('HNTV_SECRET_KEY', '6ca114a836ac7d73')

def verify_token(token):
    """
    验证API令牌
    :param token: 提供的令牌
    :return: 令牌是否有效
    """
    return token == API_TOKEN

def calculate_sha256_with_timestamp(secret_key="6ca114a836ac7d73"):
    """
    将指定字符串与当前时间秒进行sha256计算
    :param secret_key: 默认为"6ca114a836ac7d73"
    :return: sha256哈希值
    """
    # 获取当前时间的秒数
    current_time_seconds = int(time.time())
    
    # 将字符串和时间拼接
    combined_string = secret_key + str(current_time_seconds)
    
    # 进行sha256计算
    sha256_hash = hashlib.sha256(combined_string.encode('utf-8')).hexdigest()
    print(f"当前时间戳: {current_time_seconds}")
    print(f"SHA256哈希值: {sha256_hash}")
    return sha256_hash


def get_hntv_live_list():
    """
    封装API请求，使用时间戳和签名
    """
    url = "https://pubmod.hntv.tv/program/getAuth/live/class/program/11/"
    
    # 生成当前时间戳和签名
    timestamp = str(int(time.time()))
    sign = calculate_sha256_with_timestamp(SECRET_KEY)
    
    headers = {
        'timestamp': timestamp,
        'sign': sign
    }
    
    response = requests.get(url, headers=headers)
    
    return response


def get_hntv_epg_data(cid, date_timestamp):
    """
    获取EPG节目数据
    :param cid: 频道ID
    :param date_timestamp: 日期时间戳（当天零点）
    """
    url = f"https://pubmod.hntv.tv/program/getAuth/vod/originStream/program/{cid}/{date_timestamp}"
    
    # 生成当前时间戳和签名
    timestamp = str(int(time.time()))
    sign = calculate_sha256_with_timestamp(SECRET_KEY)
    
    headers = {
        'timestamp': timestamp,
        'sign': sign
    }
    
    response = requests.get(url, headers=headers)
    return response

# 新增函数：获取XML数据并保存为压缩文件
def get_and_save_xml_data():
    """
    获取XML数据并保存到文件，同时生成压缩文件
    """
    try:
        response = get_hntv_live_list()
        if response.status_code != 200:
            xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hntv-api" generator-info-url="https://pubmod.hntv.tv"></tv>'
        else:
            data = response.json()
            
            # 构建EPG XML内容
            xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hntv-api" generator-info-url="https://pubmod.hntv.tv">\n'
            
            # 如果数据结构不同，直接遍历响应数据
            if isinstance(data, list):
                for item in data:
                    name = item.get('name', 'Unknown')
                    cid = item.get('cid')
                    if cid is not None:
                        # 添加频道信息
                        xml_content += f'  <channel id="{cid}">\n    <display-name lang="zh">{name}</display-name>\n   </channel>\n'

                        # 获取EPG节目数据
                        import datetime
                        today = datetime.date.today()
                        zero_time = datetime.datetime.combine(today, datetime.time.min)
                        date_timestamp = int(zero_time.timestamp())

                        epg_response = get_hntv_epg_data(cid, date_timestamp)
                        if epg_response.status_code == 200:
                            epg_data = epg_response.json()
                            if 'programs' in epg_data and isinstance(epg_data['programs'], list):
                                for program in epg_data['programs']:
                                    title = program.get('title', 'Unknown')
                                    begin_time = program.get('beginTime', '')
                                    end_time = program.get('endTime', '')

                                    # 将时间戳转换为EPG格式 (YYYYMMDDHHMMSS +0800)
                                    begin_time_formatted = format_timestamp_for_epg(begin_time)
                                    end_time_formatted = format_timestamp_for_epg(end_time)

                                    xml_content += f'  <programme start="{begin_time_formatted}" stop="{end_time_formatted}" channel="{cid}">\n    <title lang="zh">{title}</title>\n  </programme>\n'

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
        return '<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hntv-api" generator-info-url="https://pubmod.hntv.tv"></tv>'

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
            return get_and_save_xml_data()
    except Exception as e:
        print(f"从文件加载XML数据时出错: {str(e)}")
        return '<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hntv-api" generator-info-url="https://pubmod.hntv.tv"></tv>'

def schedule_daily_xml_update():
    """
    定时每天更新XML数据
    """
    def update_xml_daily():
        while True:
            try:
                # 计算到明天零点的时间间隔（秒）
                now = datetime.datetime.now()
                tomorrow = now + datetime.timedelta(days=1)
                next_midnight = tomorrow.replace(hour=3, minute=30, second=0, microsecond=0)
                time_to_wait = (next_midnight - now).total_seconds()
                
                print(f"等待 {time_to_wait} 秒后更新XML数据...")
                time.sleep(time_to_wait)
                
                # 获取并保存XML数据
                get_and_save_xml_data()
                print("XML数据已更新")
                
            except Exception as e:
                print(f"定时更新XML数据时出错: {str(e)}")
    
    # 启动定时更新线程
    scheduler_thread = threading.Thread(target=update_xml_daily, daemon=True)
    scheduler_thread.start()

@cache.cached(timeout=600, key_prefix='transList2XML')
def transList2XML():
    # 使用从文件加载的XML数据
    return load_xml_from_file()

def format_timestamp_for_epg(timestamp_str):
    """
    将时间戳格式化为EPG格式 (YYYYMMDDHHMMSS +0800)
    :param timestamp_str: 时间戳字符串
    :return: 格式化后的时间字符串
    """
    import datetime
    
    try:
        timestamp = int(timestamp_str)
        dt = datetime.datetime.fromtimestamp(timestamp)
        return dt.strftime('%Y%m%d%H%M%S +0800')
    except (ValueError, TypeError):
        # 如果转换失败，返回当前时间的格式化字符串
        return datetime.datetime.now().strftime('%Y%m%d%H%M%S +0800')

@cache.cached(timeout=600, key_prefix='transList2M3U')
def transList2M3U():
    response = get_hntv_live_list()
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


@app.route('/api/proxy', methods=['GET'])
def proxy_api():
    """
    API代理端点，需要令牌认证
    """
    # 从请求头或查询参数中获取令牌
    token = flask_request.headers.get('Authorization') or flask_request.args.get('token')
    
    if not token:
        abort(401, description="Missing token")
    
    # 验证令牌
    if not verify_token(token.replace('Bearer ', '')):
        abort(401, description="Invalid token")
    
    try:
        # 调用API请求函数
        response = get_hntv_live_list()
        
        # 返回响应
        return jsonify({
            'status': 'success',
            'data': response.json(),
            'status_code': response.status_code
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/api/live.m3u8', methods=['GET'])
def generate_m3u():
    """
    生成M3U格式的直播列表，需要令牌认证
    """
    # 从请求头或查询参数中获取令牌
    token = flask_request.headers.get('Authorization') or flask_request.args.get('token')
    
    if not token:
        abort(401, description="Missing token")
    
    # 验证令牌
    if not verify_token(token.replace('Bearer ', '')):
        abort(401, description="Invalid token")
    
    try:
        m3u_content = transList2M3U()
        
        # 返回M3U格式内容
        return m3u_content, 200, {'Content-Type': 'application/x-mpegURL'}
    except Exception as e:
        return f"#EXTM3U\n# Error: {str(e)}", 500, {'Content-Type': 'application/x-mpegURL'}

@app.route('/api/live.xml', methods=['GET'])
def generate_xml():
    """
    生成XML格式的直播列表，需要令牌认证
    """
    # 从请求头或查询参数中获取令牌
    token = flask_request.headers.get('Authorization') or flask_request.args.get('token')

    if not token:
        abort(401, description="Missing token")

    # 验证令牌
    if not verify_token(token.replace('Bearer ', '')):
        abort(401, description="Invalid token")

    try:
        xml_content = transList2XML()

        # 返回XML格式内容
        return xml_content, 200, {'Content-Type': 'application/xml'}
    except Exception as e:
        return f'<?xml version="1.0" encoding="UTF-8"?>\n<error>{str(e)}</error>', 500, {
            'Content-Type': 'application/xml'}


@app.route('/api/live_compressed.xml', methods=['GET'])
def generate_compressed_xml():
    """
    生成压缩的XML格式的直播列表，需要令牌认证
    """
    # 从请求头或查询参数中获取令牌
    token = flask_request.headers.get('Authorization') or flask_request.args.get('token')

    if not token:
        abort(401, description="Missing token")

    # 验证令牌
    if not verify_token(token.replace('Bearer ', '')):
        abort(401, description="Invalid token")

    try:
        # 检查压缩文件是否存在，如果不存在则生成
        if not os.path.exists(GZ_FILE_PATH):
            get_and_save_xml_data()
        
        # 返回压缩的XML文件
        return send_file(GZ_FILE_PATH, as_attachment=True, download_name='live.xml.gz', mimetype='application/gzip')
    except Exception as e:
        return f'<?xml version="1.0" encoding="UTF-8"?>\n<error>{str(e)}</error>', 500, {
            'Content-Type': 'application/xml'}

@app.route('/api/generate-sign', methods=['GET'])
def generate_sign():
    """
    生成签名的端点，需要令牌认证
    """
    # 从请求头或查询参数中获取令牌
    token = flask_request.headers.get('Authorization') or flask_request.args.get('token')
    
    if not token:
        abort(401, description="Missing token")
    
    # 验证令牌
    if not verify_token(token.replace('Bearer ', '')):
        abort(401, description="Invalid token")
    
    try:
        # 生成签名
        sign = calculate_sha256_with_timestamp(SECRET_KEY)
        timestamp = str(int(time.time()))
        
        return jsonify({
            'timestamp': timestamp,
            'sign': sign
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/health', methods=['GET'])
def health_check():
    """
    健康检查端点
    """
    return jsonify({'status': 'healthy'})


# 按装订区域中的绿色按钮以运行脚本。
if __name__ == '__main__':

    # 启动定时XML更新任务
    schedule_daily_xml_update()
    print("定时XML更新任务已启动")
    
    # 在应用启动时获取一次XML数据
    print("正在获取初始XML数据...")
    get_and_save_xml_data()
    
    # 启动Flask应用
    print("\n启动Web API服务...")
    app.run(debug=True, host='0.0.0.0', port=5002)
