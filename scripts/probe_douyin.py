"""抖音直播接入可行性探测脚本 v2（POC，不写任何文件）

v1 结论：页面 cookie 可拿（ttwid），但 enter API 返回未开播房间、无流地址。
v2 依据 DouyinLiveWebFetcher（2025 版 liveMan.py）修正调用方式：
  - GET 请求 + web_rid + room_id_str（页面内部房间号）双参数
  - 生成随机 msToken（107 位）+ 从 www.douyin.com 取 __ac_nonce
  - 关键验证点：**不带 __ac_signature 与 a_bogus 时能否拿到流地址**（决定接入成本）

用法：python scripts/probe_douyin.py [房间号]
输出：每一步 ✓/✗ + 关键信息；全程只发网络请求。
"""
import argparse
import json
import random
import re
import string
import sys

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BASE_HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}


def gen_ms_token(length=107):
    """随机 msToken（开源实现：纯随机字符串即可）"""
    base = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(base) for _ in range(length))


def get_ttwid_and_nonce(session):
    """ttwid 来自 live.douyin.com，__ac_nonce 来自 www.douyin.com"""
    cookies = {}
    try:
        r1 = session.get("https://live.douyin.com/", headers=BASE_HEADERS, timeout=15)
        cookies.update({k: v for k, v in r1.cookies.items()})
    except Exception as e:
        return cookies, f"live 首页失败: {type(e).__name__} {str(e)[:100]}"
    try:
        r2 = session.get("https://www.douyin.com/", headers=BASE_HEADERS, timeout=15)
        cookies.update({k: v for k, v in r2.cookies.items()})
    except Exception as e:
        return cookies, f"www 首页失败: {type(e).__name__} {str(e)[:100]}"
    return cookies, f"live+www 首页完成，cookie={sorted(cookies.keys())}"


def extract_room_id(session, web_rid, cookies):
    """从直播间页面 HTML 提取内部 room_id"""
    headers = dict(BASE_HEADERS)
    headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    try:
        r = session.get(f"https://live.douyin.com/{web_rid}", headers=headers, timeout=15)
    except Exception as e:
        return None, f"页面请求失败: {type(e).__name__} {str(e)[:100]}"
    patterns = [
        r'roomId\\":\\"(\d+)\\"',
        r'"roomId":"(\d+)"',
        r'"room_id":"(\d+)"',
        r'roomId[":= ]+(\d+)',
    ]
    for p in patterns:
        m = re.search(p, r.text)
        if m:
            return m.group(1), f"页面 {r.status_code}，提取到内部 room_id={m.group(1)}"
    # 失败诊断：打印 roomId/room_id 出现处上下文，便于调整正则
    ctx = []
    for kw in ("roomId", "room_id"):
        for m in list(re.finditer(kw, r.text))[:2]:
            s = max(0, m.start() - 40)
            ctx.append(f"{kw}...{r.text[s:m.start() + 60]!r}")
    return None, f"页面 {r.status_code}，未找到内部 room_id（len={len(r.text)}）；上下文: {' | '.join(ctx) or '无'}"


def call_enter_api(session, web_rid, room_id_str, cookies, with_signatures=False):
    """GET 调 enter API；with_signatures=False 时缺 __ac_signature/a_bogus"""
    params = {
        "aid": "6383",
        "app_name": "douyin_web",
        "live_id": "1",
        "device_platform": "web",
        "language": "zh-CN",
        "enter_from": "page_refresh",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "126.0.0.0",
        "web_rid": web_rid,
        "room_id_str": room_id_str,
        "enter_source": "",
        "is_need_double_stream": "false",
        "insert_task_id": "",
        "live_reason": "",
        "msToken": gen_ms_token(),
    }
    headers = dict(BASE_HEADERS)
    headers.update({
        "Referer": f"https://live.douyin.com/{web_rid}",
        "Accept": "application/json, text/plain, */*",
        "cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    })
    if with_signatures:
        params["a_bogus"] = "PLACEHOLDER"  # 本 POC 未实现签名算法，仅用于对照
    try:
        r = session.get("https://live.douyin.com/webcast/room/web/enter/",
                        params=params, headers=headers, timeout=15)
    except Exception as e:
        return None, f"API 请求失败: {type(e).__name__} {str(e)[:100]}"
    try:
        return r.json(), f"HTTP {r.status_code}"
    except Exception:
        return None, f"响应非 JSON（HTTP {r.status_code}，{r.text[:150]}）"


def analyze_response(data):
    """解析 enter 响应：直播状态 / 流地址 / 风控迹象"""
    try:
        room_info = data["data"]["room_info"]
        status = room_info.get("room_status")
        stream_url = room_info.get("stream_url", {})
        hls_map = stream_url.get("hls_pull_url_map") or {}
        flv_map = stream_url.get("flv_pull_url_map") or {}
        return {
            "live": status == 0,
            "status": status,
            "hls": hls_map,
            "flv": flv_map,
        }
    except (KeyError, TypeError):
        pass
    # 兜底：列表结构（data.data[0]）
    try:
        item = data["data"]["data"][0]
        return {"live": item.get("status") == 2, "status": item.get("status"),
                "hls": {}, "flv": {}, "raw_tail": json.dumps(item, ensure_ascii=False)[:200]}
    except (KeyError, TypeError, IndexError):
        return {"live": False, "status": None, "hls": {}, "flv": {},
                "raw_tail": json.dumps(data, ensure_ascii=False)[:200]}


def fetch_with_referer(url, referer, cookies):
    headers = dict(BASE_HEADERS)
    headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, cookies=cookies, timeout=15)
        return r.status_code, len(r.content), r.text[:150].replace("\n", " ")
    except Exception as e:
        return None, 0, f"请求失败: {type(e).__name__} {str(e)[:100]}"


def probe_room(web_rid):
    print(f"\n========== 探测房间 {web_rid} ==========")
    session = requests.Session()

    # 1. ttwid + __ac_nonce
    cookies, note = get_ttwid_and_nonce(session)
    if not cookies.get("ttwid"):
        print(f"[✗] 1. ttwid/__ac_nonce: {note}")
        return False
    print(f"[✓] 1. ttwid/__ac_nonce: {note}")

    # 2. 内部 room_id
    room_id_str, note = extract_room_id(session, web_rid, cookies)
    if not room_id_str:
        # 兜底：页面未注入 roomId（可能未开播/风控降级页），直接用 web_rid 当 room_id_str 尝试
        print(f"[△] 2. 内部 room_id: {note}")
        room_id_str = web_rid
        print(f"    兜底：以 web_rid 充当 room_id_str={room_id_str} 继续尝试")
    else:
        print(f"[✓] 2. 内部 room_id: {note}")

    # 3. enter API（无 __ac_signature / a_bogus）
    data, note = call_enter_api(session, web_rid, room_id_str, cookies)
    if data is None:
        print(f"[✗] 3. enter API: {note}")
        return False
    print(f"[✓] 3. enter API 调用成功: {note}")
    info = analyze_response(data)
    print(f"    房间状态: {'直播中' if info['live'] else '未直播/其他'}（status={info['status']}）")
    if not info["hls"] and not info["flv"]:
        print("[✗] 4. 无签名（缺 __ac_signature/a_bogus）时未拿到流地址")
        print(f"    响应片段: {info.get('raw_tail', '')}")
        return False
    print(f"[✓] 4. 无签名即拿到流地址！hls 档位={list(info['hls'].keys())} flv 档位={list(info['flv'].keys())}")

    # 5. m3u8 可播性
    hls_url = None
    for q in ("FULL_HD1", "HD1", "SD1"):
        if q in info["hls"]:
            hls_url = info["hls"][q].get("url")
            break
    if not hls_url and info["hls"]:
        hls_url = next(iter(info["hls"].values())).get("url")
    if not hls_url:
        flv_url = next(iter(info["flv"].values())).get("url")
        print(f"[△] 5. 仅 FLV 无 HLS: {flv_url[:120]}")
        return False
    referer = f"https://live.douyin.com/{web_rid}"
    code, size, head = fetch_with_referer(hls_url, referer, cookies)
    is_m3u8 = code == 200 and "#EXTM3U" in head
    print(f"[{'✓' if is_m3u8 else '✗'}] 5. m3u8 主清单: HTTP {code}，{size} 字节"
          + ("" if is_m3u8 else f"，头: {head}"))
    if not is_m3u8:
        return False
    ts_match = re.findall(r"https?://[^\s\"']+\.ts[^\s\"']*", head)
    if ts_match:
        code, size, head2 = fetch_with_referer(ts_match[0], referer, cookies)
        ok = code == 200 and size > 1000
        print(f"[{'✓' if ok else '✗'}] 6. TS 分片: HTTP {code}，{size} 字节"
              + ("" if ok else f"，头: {head2[:100]}"))
        if not ok:
            return False
    print("\n========== 结论：无签名方案可行（可接入） ==========")
    return True


def discover_rooms():
    """从直播首页解析候选房间号（_ROUTER_DATA 内嵌 JSON + 正则兜底）"""
    try:
        r = requests.get("https://live.douyin.com/", headers=BASE_HEADERS, timeout=15)
    except Exception as e:
        print(f"首页请求失败: {type(e).__name__} {str(e)[:120]}")
        return []
    ids = []
    # 内嵌 JSON 中的 web_rid / id_str
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", r.text, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            text = json.dumps(data, ensure_ascii=False)
            ids += re.findall(r'"web_rid"\s*:\s*"(\d+)"', text)
            ids += re.findall(r'"id_str"\s*:\s*"(\d+)"', text)
            ids += re.findall(r'"roomId"\s*:\s*"(\d+)"', text)
        except Exception:
            pass
    # 正则兜底
    ids += re.findall(r"live\.douyin\.com/(\d+)", r.text)
    ids += re.findall(r"web_rid[\"':= ]+(\d+)", r.text)
    return list(dict.fromkeys(ids))[:12]


def debug_home():
    """打印直播首页的房间相关数据分布（排查发现不到直播房间时用）"""
    try:
        r = requests.get("https://live.douyin.com/", headers=BASE_HEADERS, timeout=15)
    except Exception as e:
        print(f"首页请求失败: {type(e).__name__} {str(e)[:120]}")
        return
    t = r.text
    print(f"len={len(t)} _ROUTER_DATA={'_ROUTER_DATA' in t} "
          f"web_rid={len(re.findall(r'web_rid', t))} "
          f"roomId={len(re.findall(r'roomId', t))} "
          f"id_str={len(re.findall(r'id_str', t))}")
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", t, re.S)
    target = m.group(1) if m else t
    for pat in (r"web_rid.{0,25}", r'"status":\s*\d+', r"live\.douyin\.com/\d+"):
        found = re.findall(pat, target)
        print(f"{pat} -> {len(found)} 个: {found[:8]}")


def main():
    parser = argparse.ArgumentParser(description="抖音直播接入可行性探测 v2（无签名对照）")
    parser.add_argument("room_id", nargs="?", help="抖音房间号（缺省自动发现）")
    parser.add_argument("--debug-home", action="store_true", help="打印首页房间数据分布后退出")
    args = parser.parse_args()

    if args.debug_home:
        debug_home()
        return 0

    rooms = [args.room_id] if args.room_id else discover_rooms()
    if not rooms:
        print("未找到候选房间号，请手动指定：python scripts/probe_douyin.py <房间号>")
        return 1

    ok = 0
    for rid in rooms:
        try:
            if probe_room(rid):
                ok += 1
                break  # 命中一个可接入即停止（其余房间用于候选）
        except Exception as e:
            print(f"[✗] 探测 {rid} 异常: {type(e).__name__} {str(e)[:150]}")
    print(f"\n汇总: 共 {len(rooms)} 个候选房间，无签名可接入 {ok} 个")
    if ok == 0:
        print("提示：若候选房间均未开播，可稍后重试或手动指定直播中的房间号验证")
    return 0


if __name__ == "__main__":
    sys.exit(main())
