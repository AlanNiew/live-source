"""原子文件写入：先写临时文件再 os.replace 原子替换

避免写一半崩溃（断电/被杀）留下半文件被后续读取。
崩溃最多残留 .tmp 文件（下次写入自动覆盖，无害）。
"""
import gzip
import os


def atomic_write_text(path, content, encoding='utf-8'):
    """文本原子写入（自动创建父目录）"""
    _ensure_dir(path)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, 'w', encoding=encoding) as f:
        f.write(content)
    os.replace(tmp_path, path)


def atomic_write_gzip(path, text, encoding='utf-8'):
    """gzip 压缩文本原子写入（自动创建父目录）"""
    _ensure_dir(path)
    tmp_path = f"{path}.tmp"
    with gzip.open(tmp_path, 'wt', encoding=encoding) as f:
        f.write(text)
    os.replace(tmp_path, path)


def _ensure_dir(path):
    """确保目标文件父目录存在"""
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
