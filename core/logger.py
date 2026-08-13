"""统一日志模块：替代散落的 print，双输出（stdout + 滚动文件 + SQLite WARNING+）

用法：
    from core.logger import get_logger
    log = get_logger('aggregator')
    log.info("...")
    log.warning("...")

特性：
- 每个模块独立 logger（module 名进日志行与 DB）
- stdout：INFO+（开发即时可见，保留原 print 体验）
- 文件：滚动 10MB × 3（xml_data/app.log）
- SQLite logs 表：仅 WARNING+（防膨胀，7 天清理由 admin.db.save_log 处理）
- 线程名前缀：[线程名]，保留聚合日志的可读性
"""
import datetime
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from config import GMT8, LOG_FILE_PATH

# 已初始化的 logger 缓存（同模块名复用同一实例）
_loggers = {}


class SqliteHandler(logging.Handler):
    """把 WARNING+ 日志写入 SQLite logs 表（失败静默，不影响业务）"""

    def emit(self, record):
        try:
            from admin import db
            # 注意：record.asctime 仅在 Formatter 调用后才存在，这里须用 record.created 自行格式化
            ts = datetime.datetime.fromtimestamp(
                record.created, tz=GMT8).strftime('%Y-%m-%d %H:%M:%S')
            db.save_log(
                ts,
                record.levelname,
                record.name,
                record.getMessage(),
            )
        except Exception:
            pass


def _build_logger(name):
    """构建带三个 handler 的 logger"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:  # 防重复初始化
        return logger

    fmt = logging.Formatter(
        '%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S')

    # 1. stdout（开发可见）
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    stream.setLevel(logging.INFO)
    logger.addHandler(stream)

    # 2. 滚动文件
    try:
        os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE_PATH, maxBytes=10 * 1024 * 1024, backupCount=3,
            encoding='utf-8')
        file_handler.setFormatter(fmt)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
    except Exception:
        pass

    # 3. SQLite（WARNING+）
    sqlite_handler = SqliteHandler()
    sqlite_handler.setLevel(logging.WARNING)
    logger.addHandler(sqlite_handler)

    return logger


def get_logger(name):
    """获取模块 logger（同名复用）"""
    if name not in _loggers:
        _loggers[name] = _build_logger(name)
    return _loggers[name]
