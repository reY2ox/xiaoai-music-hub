#!/usr/bin/env python3
"""网易云扫码登录：生成二维码 + 轮询拿 MUSIC_U"""
import json, sys, time, urllib.request, urllib.parse, http.cookiejar

import random, string
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
_r = lambda n: ''.join(random.choices(string.ascii_letters + string.digits, k=n))
H = {"User-Agent": UA, "Referer": "https://music.163.com/",
     "Cookie": f"os=pc; _ntes_nuid={_r(32)}; NMTID={_r(20)}; appver=2.9.7"}

def get(url):
    req = urllib.request.Request(url, headers=H)
    r = urllib.request.urlopen(req, timeout=12)
    return r, json.loads(r.read().decode())

r, d = get("https://music.163.com/api/login/qrcode/unikey?type=1")
key = d.get("unikey")
if not key:
    print(json.dumps({"error": "no unikey", "resp": d}, ensure_ascii=False)); sys.exit(1)

url = f"https://music.163.com/login?codekey={key}"
import segno
segno.make(url, error="m").save("/tmp/netease_qr.png", scale=8, border=3)
print(json.dumps({"qr": "/tmp/netease_qr.png", "key": key, "url": url}, ensure_ascii=False))

deadline = time.time() + 300
last = None
while time.time() < deadline:
    try:
        rr, dd = get(f"https://music.163.com/api/login/qrcode/client/login?key={key}&type=1")
        code = dd.get("code")
        if code != last:
            print(f"[{time.strftime('%H:%M:%S')}] code={code} {dd.get('message','')}", flush=True)
            last = code
        if code == 803:
            cookies = rr.headers.get_all("Set-Cookie") or []
            MUSIC_U = None
            for c in cookies:
                if c.startswith("MUSIC_U="):
                    MUSIC_U = c.split(";")[0].split("=", 1)[1]
            if not MUSIC_U:
                # 部分实现把 cookie 放在 body
                MUSIC_U = (dd.get("cookie") or "")
            if MUSIC_U:
                open("/root/.openclaw/workspace/secrets/netease.cookie", "w").write(
                    f"MUSIC_U={MUSIC_U}; os=pc")
                print(json.dumps({"ok": True, "saved": True}, ensure_ascii=False))
                sys.exit(0)
            print(json.dumps({"ok": False, "error": "no MUSIC_U in response"}, ensure_ascii=False))
            sys.exit(1)
        if code == 800:
            print(json.dumps({"ok": False, "error": "qr expired"}, ensure_ascii=False)); sys.exit(1)
    except Exception as e:
        print(f"[warn] {e!r}", flush=True)
    time.sleep(2)
print(json.dumps({"ok": False, "error": "timeout"}, ensure_ascii=False)); sys.exit(1)
