"""管理数据层：SQLite 单文件（源配置/频道覆盖/监控历史/日志/设置）

- 零依赖（只 import sqlite3/os/threading/config），被 core/monitoring 单向引用，避免循环导入
- 每操作短连接（check_same_thread=False + 独立连接），模块级写锁串行化并发写
- 所有写操作 try/except 兜底：数据层失败不影响核心业务（聚合/监控照常跑）
"""
import datetime
import os
import sqlite3
import threading

from config import (ADMIN_DB_PATH, GMT8, LOG_KEEP_DAYS, MONITOR_HISTORY_KEEP,
                    STREAM_HISTORY_KEEP)

# 模块级写锁：SQLite 并发写串行化（监控线程 + API 线程）
_db_lock = threading.Lock()

# 表结构定义（首次建库时执行）
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  url TEXT,
  name TEXT NOT NULL,
  enabled INTEGER DEFAULT 1,
  sort_order INTEGER DEFAULT 0,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS channel_overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_key TEXT UNIQUE NOT NULL,
  display_name TEXT,
  group_title TEXT,
  enabled INTEGER DEFAULT 1,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS monitor_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  health_ok INTEGER, m3u_ok INTEGER, epg_ok INTEGER,
  channel_count INTEGER, epg_size INTEGER,
  overall INTEGER
);
CREATE TABLE IF NOT EXISTS stream_check_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  group_name TEXT, channel_name TEXT, url TEXT,
  ok INTEGER,
  round_id TEXT
);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT, module TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE INDEX IF NOT EXISTS idx_monitor_ts ON monitor_history(ts);
CREATE INDEX IF NOT EXISTS idx_stream_ts ON stream_check_history(ts);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
"""


def _now():
    """当前 GMT+8 时间字符串（与项目其他时间一致）"""
    return datetime.datetime.now(tz=GMT8).strftime('%Y-%m-%d %H:%M:%S')


def _connect():
    """新建数据库连接（每操作短连接）"""
    conn = sqlite3.connect(ADMIN_DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _execute(sql, params=()):
    """执行写操作（加锁串行化），返回受影响行数"""
    with _db_lock:
        try:
            conn = _connect()
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
        except Exception as e:
            print(f"管理数据写入失败: {str(e)}")
            return 0


def _query(sql, params=()):
    """执行查询，返回 dict 列表（只读不加锁）"""
    try:
        conn = _connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        print(f"管理数据读取失败: {str(e)}")
        return []


def init_db():
    """建库建表（首次自动创建；目录不存在自动创建）"""
    try:
        os.makedirs(os.path.dirname(ADMIN_DB_PATH), exist_ok=True)
        with _db_lock:
            conn = _connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception as e:
        print(f"管理数据库初始化失败: {str(e)}")
        return False


# ---------------------------------------------------------------- 源配置

def get_sources(source_type=None):
    """查询源列表；type 为 None 返回全部，否则按类型过滤"""
    if source_type:
        return _query("SELECT * FROM sources WHERE type=? ORDER BY sort_order, id",
                      (source_type,))
    return _query("SELECT * FROM sources ORDER BY type, sort_order, id")


def get_enabled_public_urls():
    """已启用的公开源 url 列表（供聚合；空则调用方回退 config）"""
    rows = _query("SELECT url FROM sources WHERE type='public' AND enabled=1 "
                  "AND url IS NOT NULL AND url!='' ORDER BY sort_order, id")
    return [r['url'] for r in rows]


def get_enabled_bilibili_rooms():
    """已启用的 B 站房间配置列表 [{name, room_id} 或 {name, uid}]（供聚合；空则回退 config）"""
    rows = _query("SELECT * FROM sources WHERE type='bilibili' AND enabled=1 "
                  "ORDER BY sort_order, id")
    result = []
    for r in rows:
        item = {"name": r['name']}
        # url 字段存 room_id 或 uid（P2 定义：url 存 room_id）
        if r['url'] and str(r['url']).isdigit():
            item['room_id'] = int(r['url'])
        result.append(item)
    return result


def add_source(source_type, name, url=None, enabled=1, sort_order=0):
    """新增源；返回新 id 或 None"""
    ts = _now()
    _execute(
        "INSERT INTO sources (type, url, name, enabled, sort_order, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (source_type, url, name, 1 if enabled else 0, sort_order, ts, ts))
    rows = _query("SELECT id FROM sources WHERE type=? AND name=? ORDER BY id DESC LIMIT 1",
                  (source_type, name))
    return rows[0]['id'] if rows else None


def update_source(source_id, name=None, url=None, enabled=None, sort_order=None):
    """更新源；字段为 None 表示不改"""
    sets, params = [], []
    if name is not None:
        sets.append("name=?")
        params.append(name)
    if url is not None:
        sets.append("url=?")
        params.append(url)
    if enabled is not None:
        sets.append("enabled=?")
        params.append(1 if enabled else 0)
    if sort_order is not None:
        sets.append("sort_order=?")
        params.append(sort_order)
    if not sets:
        return 0
    sets.append("updated_at=?")
    params.append(_now())
    params.append(source_id)
    return _execute(f"UPDATE sources SET {', '.join(sets)} WHERE id=?", params)


def delete_source(source_id):
    """删除源"""
    return _execute("DELETE FROM sources WHERE id=?", (source_id,))


# ---------------------------------------------------------------- 频道覆盖

def get_channel_overrides():
    """全部频道覆盖 {channel_key: dict}"""
    rows = _query("SELECT * FROM channel_overrides")
    return {r['channel_key']: r for r in rows}


def get_channel_override(channel_key):
    """单个频道覆盖 dict 或 None"""
    rows = _query("SELECT * FROM channel_overrides WHERE channel_key=?", (channel_key,))
    return rows[0] if rows else None


def upsert_channel_override(channel_key, display_name=None, group_title=None, enabled=None):
    """新增或更新频道覆盖（key 唯一）"""
    existing = get_channel_override(channel_key)
    if existing:
        sets, params = [], []
        if display_name is not None:
            sets.append("display_name=?")
            params.append(display_name)
        if group_title is not None:
            sets.append("group_title=?")
            params.append(group_title)
        if enabled is not None:
            sets.append("enabled=?")
            params.append(1 if enabled else 0)
        if not sets:
            return 0
        sets.append("updated_at=?")
        params.append(_now())
        params.append(channel_key)
        return _execute(f"UPDATE channel_overrides SET {', '.join(sets)} "
                        "WHERE channel_key=?", params)
    _execute(
        "INSERT INTO channel_overrides (channel_key, display_name, group_title, enabled, updated_at) "
        "VALUES (?,?,?,?,?)",
        (channel_key, display_name, group_title, 1 if enabled is None else (1 if enabled else 0),
         _now()))
    return 1


def delete_channel_override(channel_key):
    """删除频道覆盖（恢复默认）"""
    return _execute("DELETE FROM channel_overrides WHERE channel_key=?", (channel_key,))


# ---------------------------------------------------------------- 监控历史

def save_monitor_history(health_ok, m3u_ok, epg_ok, channel_count, epg_size, overall):
    """保存一轮常规健康检测结果，并清理超量旧数据"""
    _execute(
        "INSERT INTO monitor_history (ts, health_ok, m3u_ok, epg_ok, channel_count, epg_size, overall) "
        "VALUES (?,?,?,?,?,?,?)",
        (_now(), 1 if health_ok else 0, 1 if m3u_ok else 0, 1 if epg_ok else 0,
         channel_count, epg_size, 1 if overall else 0))
    # 清理：只保留最近 N 轮
    _execute("DELETE FROM monitor_history WHERE id NOT IN "
             "(SELECT id FROM monitor_history ORDER BY id DESC LIMIT ?)",
             (MONITOR_HISTORY_KEEP,))


def get_monitor_history(limit=100):
    """最近 N 轮健康检测历史（新→旧）"""
    return _query("SELECT * FROM monitor_history ORDER BY id DESC LIMIT ?", (limit,))


def save_stream_history(ts, group_name, channel_name, url, ok, round_id):
    """保存一条流探测记录"""
    _execute(
        "INSERT INTO stream_check_history (ts, group_name, channel_name, url, ok, round_id) "
        "VALUES (?,?,?,?,?,?)",
        (ts, group_name, channel_name, url, 1 if ok else 0, round_id))
    _execute("DELETE FROM stream_check_history WHERE id NOT IN "
             "(SELECT id FROM stream_check_history ORDER BY id DESC LIMIT ?)",
             (STREAM_HISTORY_KEEP,))


def get_stream_history(limit=200, unreachable_only=False):
    """最近流探测记录；unreachable_only=True 只取不可达"""
    if unreachable_only:
        return _query("SELECT * FROM stream_check_history WHERE ok=0 "
                      "ORDER BY id DESC LIMIT ?", (limit,))
    return _query("SELECT * FROM stream_check_history ORDER BY id DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------- 日志

def save_log(ts, level, module, message):
    """写一条日志（WARNING+ 才由 logger 调用，防膨胀）"""
    _execute("INSERT INTO logs (ts, level, module, message) VALUES (?,?,?,?)",
             (ts, level, module, message[:2000]))
    # 定期清理超期日志（每天首次写时清理一次）
    _execute("DELETE FROM logs WHERE ts < datetime('now', 'localtime', ?)",
             (f'-{LOG_KEEP_DAYS} days',))


def get_logs(limit=200, level=None, keyword=None):
    """查询日志；level/keyword 可选过滤"""
    sql = "SELECT * FROM logs WHERE 1=1"
    params = []
    if level:
        sql += " AND level=?"
        params.append(level.upper())
    if keyword:
        sql += " AND message LIKE ?"
        params.append(f"%{keyword}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return _query(sql, params)


# ---------------------------------------------------------------- 设置

def get_setting(key, default=None):
    """读设置项"""
    rows = _query("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]['value'] if rows else default


def set_setting(key, value):
    """写设置项（UPSERT）"""
    existing = get_setting(key)
    if existing is None:
        _execute("INSERT INTO settings (key, value) VALUES (?,?)", (key, value))
    else:
        _execute("UPDATE settings SET value=? WHERE key=?", (value, key))
