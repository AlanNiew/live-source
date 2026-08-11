"""流地址可达性探测（monitor 严格版与聚合宽松版共用单实现）"""
import requests

from config import STREAM_PROBE_TIMEOUT, STREAM_USER_AGENT


def probe_stream(url, accept_403=False):
    """
    探测单个流地址可达性：GET + Range 请求读少量字节即断开
    （HEAD 对直播源不可靠，很多返回 404；Range 206 也算成功）
    :param url: 流地址
    :param accept_403: True 时 403 也视为可达（聚合过滤用宽松判定——
                      403 可能是探测特征被拒但播放器能放）；
                      False 时仅 200/206 算可达（监控告警口径）
    :return: True 可达 / False 不可达
    """
    r = None
    try:
        r = requests.get(
            url, timeout=STREAM_PROBE_TIMEOUT, stream=True,
            headers={'Range': 'bytes=0-1024', 'User-Agent': STREAM_USER_AGENT},
        )
        if r.status_code in (200, 206) or (accept_403 and r.status_code == 403):
            try:
                return bool(next(r.iter_content(1024)))
            except StopIteration:
                return False
        return False
    except Exception:
        return False
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
