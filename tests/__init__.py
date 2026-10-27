"""测试包：python -m unittest discover -s tests -t . 运行（标准库 unittest，无外部依赖）

隔离策略（务必用上面的命令，`-t .` 才会先导入本包）：
整个测试进程把 config 里的**落盘路径**统一重定向到一次性临时目录，
让测试完全不碰真实 xml_data（app.log / admin.db / bilibili_rooms.json /
music_playlists.json / live.xml / aggregated.m3u 等），避免测试污染运行数据。

注意：core/admin 各模块在导入时用 `from config import XXX` 绑定路径常量，
因此必须在**任何业务模块导入之前**完成重定向——这也是把逻辑放在包 __init__
（先于 test_*.py 执行）而不是各测试用例 setUp 的原因。
"""
import atexit
import os
import shutil
import tempfile

_TEST_XML_DATA_DIR = tempfile.mkdtemp(prefix='hntv-api-test-')
atexit.register(shutil.rmtree, _TEST_XML_DATA_DIR, ignore_errors=True)

import config  # noqa: E402  （必须在重定向逻辑之前导入，才能改写其常量）

# config 中所有落在 xml_data 下的路径常量：文件名保持不变，目录换到临时目录
_XML_DATA_PATHS = (
    'XML_DATA_DIR',
    'XML_FILE_PATH', 'GZ_FILE_PATH',
    'AGGREGATED_M3U_PATH', 'STREAM_FAILURES_PATH', 'PUBLIC_CHANNELS_CACHE_PATH',
    'BILIBILI_CACHE_PATH', 'BILIBILI_CUSTOM_ROOMS_PATH',
    'MUSIC_PLAYLISTS_PATH',
    'ADMIN_DB_PATH', 'LOG_FILE_PATH',
)

for _name in _XML_DATA_PATHS:
    _old = getattr(config, _name, '')
    _basename = os.path.basename(_old) if _old else ''
    if _name == 'XML_DATA_DIR' or not _basename:
        config.__dict__[_name] = _TEST_XML_DATA_DIR
    else:
        config.__dict__[_name] = os.path.join(_TEST_XML_DATA_DIR, _basename)

del _name, _old, _basename
