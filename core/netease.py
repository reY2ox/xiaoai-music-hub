"""网易云接口层。

搜歌、取播放直链、账号、歌单。所有对 music.163.com 的请求都在这。
"""
import json
import os
import random
import string
import urllib.parse
import urllib.request

from . import config

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
REFERER = "https://music.163.com/"


def cookie():
    try:
        return open(config.COOKIE_FILE).read().strip()
    except Exception:
        return "os=pc"


def save_cookie(text):
    os.makedirs(config.SECRETS_DIR, exist_ok=True)
    with open(config.COOKIE_FILE, "w") as f:
        f.write(text)
    os.chmod(config.COOKIE_FILE, 0o600)


def _headers(extra=None):
    h = {"User-Agent": UA, "Referer": REFERER, "Cookie": cookie()}
    if extra:
        h.update(extra)
    return h


def _json(url, data=None, headers=None, timeout=15):
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode() if data else None,
        headers=_headers(headers),
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------- 搜索 ----------
def search(query, limit=10):
    """返回 [{song_id, name, artist, album, fee}]"""
    d = _json(
        "https://music.163.com/api/cloudsearch/pc",
        {"s": query, "type": 1, "limit": limit, "offset": 0},
    )
    out = []
    for s in d.get("result", {}).get("songs", []):
        out.append({
            "song_id": s["id"],
            "name": s["name"],
            "artist": "/".join(a["name"] for a in s.get("ar", [])),
            "album": (s.get("al") or {}).get("name", ""),
            "fee": s.get("fee"),
        })
    return out


# ---------- 取直链 ----------
def song_url(song_id, quality=None):
    """返回 (url, code, br, ext)。url 为 None 表示拿不到（VIP/版权）"""
    br = config.quality_br(quality)
    ext = config.quality_ext(quality)
    d = _json(
        "https://music.163.com/api/song/enhance/player/url",
        {"ids": f"[{song_id}]", "br": br},
    )
    item = (d.get("data") or [{}])[0]
    url = item.get("url")
    real_ext = (item.get("type") or ext) if url else ext
    return url, item.get("code"), item.get("br"), real_ext


# ---------- 账号 ----------
def account():
    try:
        a = _json("https://music.163.com/api/nuser/account/get")
    except Exception as e:
        return {"ok": False, "logged_in": False, "reason": f"请求失败 {e!r}"}
    prof = a.get("profile") or {}
    if not prof.get("userId"):
        return {"ok": False, "logged_in": False, "reason": "cookie 已失效，请重新扫码"}
    return {
        "ok": True,
        "logged_in": True,
        "nickname": prof.get("nickname"),
        "uid": prof.get("userId"),
        "avatar": prof.get("avatarUrl"),
        "vip": (a.get("account") or {}).get("vipType"),
        "reason": "",
    }


def current_uid():
    try:
        return (account() or {}).get("uid")
    except Exception:
        return None


# ---------- 扫码登录 ----------
def qr_start():
    """生成登录二维码，返回 {key, png_path}；失败返回 {error}"""
    h = {
        "User-Agent": UA,
        "Referer": REFERER,
        "Cookie": (
            f"os=pc; _ntes_nuid={''.join(random.choices(string.ascii_letters + string.digits, k=32))}"
            f"; NMTID={''.join(random.choices(string.ascii_letters + string.digits, k=20))}"
            "; appver=2.9.7"
        ),
    }
    try:
        d = _json("https://music.163.com/api/login/qrcode/unikey?type=1", headers=h)
    except Exception as e:
        return {"error": f"取码失败 {e!r}"}
    key = d.get("unikey")
    if not key:
        return {"error": d.get("message") or "取码失败"}
    try:
        import segno
        png = "/tmp/netease_qr.png"
        segno.make(f"https://music.163.com/login?codekey={key}", error="m").save(
            png, scale=8, border=3
        )
    except Exception as e:
        return {"error": f"生成二维码失败 {e!r}", "key": key}
    return {"key": key, "png_path": png}


def qr_poll(key, headers=None):
    """查询扫码状态：801 等待 / 802 已扫 / 803 成功(写 cookie) / 800 过期"""
    h = headers or _headers({"Cookie": f"os=pc; appver=2.9.7"})
    req = urllib.request.Request(
        f"https://music.163.com/api/login/qrcode/client/login?key={key}&type=1", headers=h
    )
    r = urllib.request.urlopen(req, timeout=12)
    d = json.loads(r.read().decode())
    code = d.get("code")
    if code == 803:
        music_u = None
        for c in (r.headers.get_all("Set-Cookie") or []):
            if c.startswith("MUSIC_U="):
                music_u = c.split(";")[0].split("=", 1)[1]
        if music_u:
            save_cookie(f"MUSIC_U={music_u}; os=pc")
            return 803, True
        return 803, False
    return code, False


# ---------- 歌单 ----------
def playlists():
    uid = current_uid()
    if not uid:
        return {"ok": False, "error": "cookie 失效，需要重新扫码"}, 401
    p = _json(f"https://music.163.com/api/user/playlist?uid={uid}&limit=1000&offset=0")
    return {
        "ok": True,
        "uid": uid,
        "playlists": [
            {
                "id": x["id"],
                "name": x["name"],
                "count": x.get("trackCount", 0),
                "cover": x.get("coverImgUrl", ""),
            }
            for x in (p.get("playlist") or [])
        ],
    }, 200


def playlist_tracks(remote_id):
    d = _json(f"https://music.163.com/api/v3/playlist/detail?id={remote_id}&n=1000")
    songs = (d.get("playlist") or {}).get("tracks") or []
    return [
        {
            "song_id": s["id"],
            "name": s["name"],
            "artist": "/".join(a["name"] for a in s.get("ar", [])),
            "album": (s.get("al") or {}).get("name", ""),
        }
        for s in songs
    ]
