"""HNTV 官方 API 客户端：令牌校验、签名、直播列表/EPG 接口封装"""
import hashlib
import time

import requests

from config import API_TOKEN, SECRET_KEY


class TokenUtils:
    """API 令牌验证工具类"""

    @staticmethod
    def verify_token(token):
        """
        验证 API 令牌
        :param token: 提供的令牌
        :return: 令牌是否有效
        """
        return token == API_TOKEN


class CryptoUtils:
    """加密工具类（上游签名 = sha256(SECRET_KEY + timestamp)）"""

    @staticmethod
    def calculate_sha256_with_timestamp(secret_key=SECRET_KEY, timestamp=None):
        """
        将指定字符串与当前时间秒进行 sha256 计算
        :param secret_key: 签名密钥（默认取环境变量 HNTV_SECRET_KEY）
        :param timestamp: 时间戳（默认当前秒）
        :return: sha256哈希值
        """
        if timestamp is None:
            timestamp = str(int(time.time()))
        combined_string = secret_key + str(timestamp)
        sha256_hash = hashlib.sha256(combined_string.encode('utf-8')).hexdigest()
        print(f"加密时间戳: {timestamp}")
        print(f"SHA256哈希值: {sha256_hash}")
        return sha256_hash

    @staticmethod
    def _auth_headers():
        """生成上游 HNTV API 鉴权请求头（timestamp + sign）"""
        timestamp = str(int(time.time()))
        sign = CryptoUtils.calculate_sha256_with_timestamp(SECRET_KEY, timestamp)
        return {'timestamp': timestamp, 'sign': sign}


class ApiUtils:
    """HNTV 官方接口请求工具类"""

    @staticmethod
    def get_hntv_live_list():
        """
        获取 hntv 官方直播频道列表
        :return: requests.Response
        """
        url = "https://pubmod.hntv.tv/program/getAuth/live/class/program/11/"
        return requests.get(url, headers=CryptoUtils._auth_headers())

    @staticmethod
    def get_hntv_epg_data(cid, date_timestamp):
        """
        获取单频道当日 EPG 节目数据
        :param cid: 频道ID
        :param date_timestamp: 日期时间戳（当天零点）
        :return: requests.Response
        """
        url = f"https://pubmod.hntv.tv/program/getAuth/vod/originStream/program/{cid}/{date_timestamp}"
        return requests.get(url, headers=CryptoUtils._auth_headers())
