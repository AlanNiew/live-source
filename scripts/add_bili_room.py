"""B 站直播房间校验与添加脚本

验证房间号是否有效、是否在播，并可直接调 API 动态添加到服务（无需改配置重启）。

用法（在项目根目录执行，仅依赖标准库）：
    python scripts/add_bili_room.py 123456
    python scripts/add_bili_room.py 123456 "XX卫视"
    python scripts/add_bili_room.py 123456 "XX卫视" --api http://192.168.1.107:5002 --token <API_TOKEN>

说明：
    - 不带 --api：只验证并打印可直接粘贴进 config.py 的配置行
    - 带 --api --token：直接 POST 到服务动态添加（服务需已运行且 token 正确）
退出码：0=成功；1=房间不存在或失败
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# B 站防盗链要求 Referer/UA
BILI_REFERER = "https://live.bilibili.com/"
BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _get(url):
    """带 Referer/UA 的 GET 请求，返回 JSON dict"""
    req = urllib.request.Request(
        url, headers={'Referer': BILI_REFERER, 'User-Agent': BILI_UA})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def verify_room(room_id):
    """
    验证房间：room_init 查 uid + 开播状态，getRoomInfoOld 查标题
    :return: dict(uid, live, title) 或 None（房间不存在）
    """
    data = _get(f"https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}")
    d = data.get('data') or {}
    uid = d.get('uid')
    if not uid:
        return None
    live = d.get('live_status', 0)

    title = ''
    try:
        info = _get(
            f"https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid={uid}")
        title = (info.get('data') or {}).get('title') or ''
    except Exception:
        pass

    return {'uid': uid, 'live': live, 'title': title}


def load_token():
    """优先取命令行 token，否则读 .env 的 API_TOKEN"""
    env = {}
    env_path = os.path.join(BASE_DIR, '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                env[key.strip()] = value.strip()
    return env.get('API_TOKEN', '')


def post_room(api, token, name, room_id):
    """调用 POST /api/bilibili/rooms 动态添加"""
    url = api.rstrip('/') + '/api/bilibili/rooms'
    payload = json.dumps({'name': name, 'room_id': room_id}).encode('utf-8')
    req = urllib.request.Request(
        url, data=payload, method='POST',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser(description='B 站直播房间校验与添加')
    parser.add_argument('room_id', type=int, help='直播间房间号（live.bilibili.com/<房间号> 的数字）')
    parser.add_argument('name', nargs='?', default=None, help='频道名（缺省用 B 站标题）')
    parser.add_argument('--api', default=None, help='服务地址，如 http://192.168.1.107:5002')
    parser.add_argument('--token', default=None, help='API 令牌（缺省读 .env 的 API_TOKEN）')
    args = parser.parse_args()

    print(f"验证房间 {args.room_id} ...")
    info = verify_room(args.room_id)
    if info is None:
        print(f"错误: 房间 {args.room_id} 不存在或无法访问")
        return 1

    name = args.name or info['title'] or f'房间{args.room_id}'
    status = '在播' if info['live'] else '未开播'
    print(f"  频道名: {name}")
    print(f"  uid:    {info['uid']}")
    print(f"  状态:   {status}")
    if info['title']:
        print(f"  标题:   {info['title']}")

    # 配置行
    print()
    print("可直接粘贴进 config.py BILIBILI_ROOMS 的配置行：")
    print(f'    {{"name": "{name}", "room_id": {args.room_id}}},')

    # 调 API 动态添加
    if args.api:
        token = args.token or load_token()
        if not token:
            print("\n错误: 未提供 token（--token 或 .env 的 API_TOKEN）")
            return 1
        print(f"\n正在 POST 到 {args.api} ...")
        try:
            result = post_room(args.api, token, name, args.room_id)
            print(f"添加成功: {result.get('message', result)}")
        except Exception as e:
            print(f"添加失败: {type(e).__name__}: {e}")
            return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
