"""Flask 应用工厂与全部路由（薄层，业务逻辑在 core/ 与 monitoring/）"""
import os
import time

from flask import Flask, abort, jsonify, request as flask_request, send_file
from flask_caching import Cache

from config import GZ_FILE_PATH, SECRET_KEY
from core.aggregator import AggregatorUtils
from core.epg import XmlUtils
from core.hntv_client import CryptoUtils, TokenUtils


def create_app():
    """创建 Flask 应用（含缓存配置与路由）"""
    app = Flask(__name__)

    # 简单内存缓存（默认 10 分钟）
    app.config['CACHE_TYPE'] = 'simple'
    app.config['CACHE_DEFAULT_TIMEOUT'] = 600
    cache = Cache(app)

    @cache.cached(timeout=600, key_prefix='transList2M3U')
    def trans_list_to_m3u_cached():
        """直播列表（带 10 分钟缓存；底层已读磁盘聚合缓存，开销极小）"""
        return AggregatorUtils.trans_list_to_m3u()

    @app.route('/api/proxy', methods=['GET'])
    def proxy_api():
        """API 代理端点（需 Bearer token），封装 HNTV 官方直播列表"""
        token = _extract_token()
        if not token or not TokenUtils.verify_token(token):
            abort(401, description="Missing or invalid token")

        try:
            from core.hntv_client import ApiUtils
            response = ApiUtils.get_hntv_live_list()
            return jsonify({
                'status': 'success',
                'data': response.json(),
                'status_code': response.status_code,
            })
        except Exception as e:
            return jsonify({'status': 'error', 'message': str(e)}), 500

    @app.route('/api/generate-sign', methods=['GET'])
    def generate_sign():
        """生成上游签名端点（需 Bearer token）"""
        token = _extract_token()
        if not token or not TokenUtils.verify_token(token):
            abort(401, description="Missing or invalid token")

        try:
            timestamp = str(int(time.time()))
            sign = CryptoUtils.calculate_sha256_with_timestamp(SECRET_KEY, timestamp)
            return jsonify({'timestamp': timestamp, 'sign': sign})
        except Exception as e:
            return jsonify({'status': 'error', 'message': str(e)}), 500

    @app.route('/api/live.m3u8', methods=['GET'])
    def generate_m3u():
        """生成 M3U 格式的直播列表（多源聚合结果）"""
        try:
            m3u_content = trans_list_to_m3u_cached()
            return m3u_content, 200, {'Content-Type': 'application/x-mpegURL'}
        except Exception as e:
            return f"#EXTM3U\n# Error: {str(e)}", 500, {'Content-Type': 'application/x-mpegURL'}

    @app.route('/api/live.xml', methods=['GET'])
    def generate_xml():
        """生成 EPG XML 节目单（读磁盘缓存，每天 02:30 刷新）"""
        try:
            xml_content = XmlUtils.trans_list_to_xml()
            return xml_content, 200, {'Content-Type': 'application/xml'}
        except Exception as e:
            return f'<?xml version="1.0" encoding="UTF-8"?>\n<error>{str(e)}</error>', 500, {
                'Content-Type': 'application/xml'}

    @app.route('/api/live.xml.gz', methods=['GET'])
    def generate_compressed_xml():
        """生成压缩的 EPG XML（文件不存在才现场生成）"""
        try:
            if not os.path.exists(GZ_FILE_PATH):
                XmlUtils.get_and_save_xml_data()
            return send_file(GZ_FILE_PATH, as_attachment=True, download_name='live.xml.gz',
                             mimetype='application/gzip')
        except Exception as e:
            return f'<?xml version="1.0" encoding="UTF-8"?>\n<error>{str(e)}</error>', 500, {
                'Content-Type': 'application/xml'}

    @app.route('/health', methods=['GET'])
    def health_check():
        """健康检查端点（无认证）"""
        return jsonify({'status': 'healthy'})

    return app


def _extract_token():
    """从请求头或查询参数中提取 Bearer token"""
    token = flask_request.headers.get('Authorization') or flask_request.args.get('token')
    if token:
        token = token.replace('Bearer ', '')
    return token or None
