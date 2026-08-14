"""管理后台运行时设置初始化脚本：把 config 默认值种子写入 settings 表（幂等）

用法（在项目根目录执行，仅依赖标准库）：
    python scripts/seed_admin_settings.py           # 只写缺失键（已有值不动）
    python scripts/seed_admin_settings.py --reset   # 覆盖已有键，恢复 config 默认

设计说明：
    - 设置读取是「DB 优先、config 兜底」：脚本只负责把默认值落库，
      之后可在管理后台「设置」页或 PUT /api/admin/settings 修改，立即生效
      （消费点：monitoring/checks.py、core/aggregator.py、admin/db.py 清理策略）
    - 密钥类（API_TOKEN / HNTV_SECRET_KEY / ADMIN_PASSWORD / email / password /
      BILIBILI_COOKIE）不落库，保持 .env + --env-file 注入
    - min_channel_count 种子值取显式 env 覆盖或 30（不取测试模式自动联动值 1，
      避免正式模式下误用低阈值；测试模式建议保持未设置）
退出码：0=成功；1=初始化失败
"""
import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from admin import db  # noqa: E402  导入顺序：先修正 sys.path
from config import (GROUP_HEALTH_RATIOS, LOG_KEEP_DAYS, MONITOR_HISTORY_KEEP,
                    STREAM_FAIL_LIMIT, STREAM_HISTORY_KEEP)  # noqa: E402

# key -> (描述, 默认值, 存储格式)
DEFAULTS = [
    ('min_channel_count', '监控频道数阈值（低于即告警）',
     int(os.environ.get('MIN_CHANNEL_COUNT', '30')), 'int'),
    ('stream_fail_limit', '聚合探测连续失败轮数（达到即丢弃频道）',
     STREAM_FAIL_LIMIT, 'int'),
    ('group_health_ratios', '分组健康率阈值（组名 -> 0~1 数值）',
     GROUP_HEALTH_RATIOS, 'json'),
    ('monitor_history_keep', '常规健康检测历史保留轮数',
     MONITOR_HISTORY_KEEP, 'int'),
    ('stream_history_keep', '流探测历史保留条数',
     STREAM_HISTORY_KEEP, 'int'),
    ('log_keep_days', '日志保留天数', LOG_KEEP_DAYS, 'int'),
]


def main():
    parser = argparse.ArgumentParser(description='把 config 默认值初始化到管理库 settings 表')
    parser.add_argument('--reset', action='store_true',
                        help='覆盖已存在的键（恢复 config 默认值）')
    args = parser.parse_args()

    if not db.init_db():
        print('错误：管理库初始化失败，请检查 ADMIN_DB_PATH 目录权限', file=sys.stderr)
        return 1

    written = kept = 0
    for key, desc, default, kind in DEFAULTS:
        value = json.dumps(default, ensure_ascii=False) if kind == 'json' else str(default)
        existing = db.get_setting(key)
        if existing is not None and not args.reset:
            print(f'跳过（已存在）: {key} = {existing}')
            kept += 1
            continue
        db.set_setting(key, value)
        print(f'{"覆盖" if existing is not None else "写入"}: {key} = {value}'
              f'（{desc}）')
        written += 1

    print(f'完成：写入 {written} 个，保持 {kept} 个；'
          '有效值见管理后台「设置」页或 GET /api/admin/settings')
    return 0


if __name__ == '__main__':
    sys.exit(main())
