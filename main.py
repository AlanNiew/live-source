import hashlib
import time
import requests
from flask import Flask, request as flask_request, jsonify, abort
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()
# 创建Flask应用
app = Flask(__name__)

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

    # 启动Flask应用
    print("\n启动Web API服务...")
    app.run(debug=False, host='0.0.0.0', port=5002)
