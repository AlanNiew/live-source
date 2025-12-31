import os
import time

from dotenv import load_dotenv
from flask import Flask, request as flask_request, jsonify, abort, send_file
from flask_caching import Cache

from utils import TokenUtils, CryptoUtils, ApiUtils, XmlUtils, M3uUtils, SchedulerUtils, TimeUtils

# 加载环境变量
load_dotenv()

# 创建Flask应用
app = Flask(__name__)

# 配置缓存
app.config['CACHE_TYPE'] = 'simple'  # 使用简单缓存
app.config['CACHE_DEFAULT_TIMEOUT'] = 600  # 默认10分钟缓存

# 创建缓存实例
cache = Cache(app)

# 从utils模块获取配置
from utils import GZ_FILE_PATH, SECRET_KEY

def verify_token(token):
    """
    验证API令牌
    :param token: 提供的令牌
    :return: 令牌是否有效
    """
    return TokenUtils.verify_token(token)

def calculate_sha256_with_timestamp(secret_key="6ca114a836ac7d73",timestamp=None):
    """
    将指定字符串与当前时间秒进行sha256计算
    :param timestamp:
    :param secret_key: 默认为"6ca114a836ac7d73"
    :return: sha256哈希值
    """
    return CryptoUtils.calculate_sha256_with_timestamp(secret_key,timestamp)

def get_hntv_live_list():
    """
    封装API请求，使用时间戳和签名
    """
    return ApiUtils.get_hntv_live_list()

def get_hntv_epg_data(cid, date_timestamp):
    """
    获取EPG节目数据
    :param cid: 频道ID
    :param date_timestamp: 日期时间戳（当天零点）
    """
    return ApiUtils.get_hntv_epg_data(cid, date_timestamp)

# 新增函数：获取XML数据并保存为压缩文件
def get_and_save_xml_data():
    """
    获取XML数据并保存到文件，同时生成压缩文件
    """
    return XmlUtils.get_and_save_xml_data()

def load_xml_from_file():
    """
    从文件加载XML数据
    """
    return XmlUtils.load_xml_from_file()

def schedule_daily_xml_update():
    """
    定时每天更新XML数据
    """
    return SchedulerUtils.schedule_daily_xml_update()

def transList2XML():
    # 使用从文件加载的XML数据
    return XmlUtils.transList2XML()

def format_timestamp_for_epg(timestamp_str):
    """
    将时间戳格式化为EPG格式 (YYYYMMDDHHMMSS +0800)
    :param timestamp_str: 时间戳字符串
    :return: 格式化后的时间字符串
    """
    return TimeUtils.format_timestamp_for_epg(timestamp_str)

@cache.cached(timeout=600, key_prefix='transList2M3U')
def transList2M3U():
    return M3uUtils.transList2M3U()


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
    生成M3U格式的直播列表
    """
    try:
        m3u_content = transList2M3U()
        
        # 返回M3U格式内容
        return m3u_content, 200, {'Content-Type': 'application/x-mpegURL'}
    except Exception as e:
        return f"#EXTM3U\n# Error: {str(e)}", 500, {'Content-Type': 'application/x-mpegURL'}

@app.route('/api/live.xml', methods=['GET'])
def generate_xml():
    """
    生成XML格式的直播列表
    """
    try:
        xml_content = transList2XML()

        # 返回XML格式内容
        return xml_content, 200, {'Content-Type': 'application/xml'}
    except Exception as e:
        return f'<?xml version="1.0" encoding="UTF-8"?>\n<error>{str(e)}</error>', 500, {
            'Content-Type': 'application/xml'}


@app.route('/api/live.xml.gz', methods=['GET'])
def generate_compressed_xml():
    """
    生成压缩的XML格式的直播列表
    """
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
        timestamp = str(int(time.time()))
        sign = calculate_sha256_with_timestamp(SECRET_KEY,timestamp)

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

    # 启动Flask应用 - 仅在直接运行脚本时使用（开发环境）
    print("\n启动Web API服务...")
    app.run(debug=False, host='0.0.0.0', port=5002)
else:
    # 在WSGI服务器环境下启动定时任务
    schedule_daily_xml_update()
    print("定时XML更新任务已启动")