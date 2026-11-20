#!/usr/bin/env python3
"""小爱音箱音乐中枢 v2
- 多设备注册与切换（SSDP 自动发现小米 MediaRenderer）
- 歌单 / 播放队列（SQLite 持久化）
- 网易云搜索取直链 -> DLNA 推送
接口见 README（/ 为网页控制台）
"""
import json, os, re, sqlite3, threading, time, urllib.parse, urllib.request
import xml.sax.saxutils as sx
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
COOKIE_FILE = "/root/.openclaw/workspace/secrets/netease.cookie"
DB_PATH = "/root/xiaoai-music/music.db"
DEFAULT_IP = os.environ.get("XIAOAI_IP", "192.168.3.81")
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/root/music")
CACHE_DIR = "/root/xiaoai-music/cache"
AUDIO_EXT = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac", ".ape", ".wma")
NEED_TRANSCODE = (".flac", ".m4a", ".wav", ".ape", ".wma", ".aac")


# ---------------- 存储 ----------------
def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS devices(
      ip TEXT PRIMARY KEY, name TEXT, model TEXT, udn TEXT,
      last_seen INTEGER, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS playlists(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
      created_at INTEGER);
    CREATE TABLE IF NOT EXISTS tracks(
      id INTEGER PRIMARY KEY AUTOINCREMENT, playlist_id INTEGER NOT NULL,
      song_id INTEGER, name TEXT, artist TEXT, album TEXT, pos INTEGER);
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS local_tracks(
      id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE, title TEXT,
      artist TEXT, album TEXT, size INTEGER, mtime INTEGER);
    """)
    for ddl in ("ALTER TABLE tracks ADD COLUMN local_id INTEGER",
                "ALTER TABLE playlists ADD COLUMN source TEXT DEFAULT 'local'",
                "ALTER TABLE playlists ADD COLUMN remote_id INTEGER",
                "ALTER TABLE playlists ADD COLUMN cover TEXT",
                "ALTER TABLE playlists ADD COLUMN owner_uid INTEGER"):
        try:
            c.execute(ddl)
        except Exception:
            pass
    if not c.execute("SELECT 1 FROM devices").fetchone():
        c.execute("INSERT INTO devices(ip,name,model,udn,last_seen,created_at) VALUES(?,?,?,?,?,?)",
                  (DEFAULT_IP, "小爱音箱", "S12", "", int(time.time()), int(time.time())))
    if not c.execute("SELECT 1 FROM playlists").fetchone():
        c.execute("INSERT INTO playlists(name,created_at) VALUES(?,?)",
                  ("我的歌单", int(time.time())))
    c.commit()
    c.close()


def get_setting(k, default=None):
    c = db(); r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone(); c.close()
    return r["v"] if r else default


def set_setting(k, v):
    c = db()
    c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    c.commit(); c.close()


def detect_lan_ip():
    import socket
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("192.168.3.1", 80))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except Exception:
        return "127.0.0.1"


LAN_IP = detect_lan_ip()


def active_ip():
    c = db()
    row = c.execute("SELECT ip FROM devices ORDER BY last_seen DESC LIMIT 1").fetchone()
    c.close()
    return get_setting("active_device") or (row["ip"] if row else DEFAULT_IP)


# ---------------- DLNA ----------------
def cookie():
    try:
        return open(COOKIE_FILE).read().strip()
    except Exception:
        return "os=pc"


def soap(ip, service, action, body, timeout=8):
    url = f"http://{ip}:9999/{service.split(':')[0]}/control"
    env = ('<?xml version="1.0" encoding="utf-8"?>'
           '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
           'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
           f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}">{body}</u:{action}>'
           '</s:Body></s:Envelope>')
    req = urllib.request.Request(url, data=env.encode(), method="POST")
    req.add_header("Content-Type", 'text/xml; charset="utf-8"')
    req.add_header("SOAPACTION", f'"urn:schemas-upnp-org:service:{service}#{action}"')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def av(ip, action, extra=""):
    return soap(ip, "AVTransport:1", action,
                f"<InstanceID>0</InstanceID>{extra}")


def transport(ip):
    st, txt = av(ip, "GetTransportInfo")
    m = re.search(r"<CurrentTransportState>([^<]*)<", txt)
    return m.group(1) if m else "UNKNOWN"


def _unesc(v):
    import html
    for _ in range(2):
        v = html.unescape(v)
    return v


def now_playing(ip):
    st, txt = av(ip, "GetPositionInfo")
    def grab(tag):
        m = re.search(rf"<{tag}>([^<]*)</{tag}>", txt)
        return _unesc(m.group(1)) if m else ""
    meta = grab("TrackMetaData")
    t = re.search(r"<dc:title>([^<]*)</dc:title>", meta)
    a = re.search(r"<upnp:artist>([^<]*)</upnp:artist>", meta)
    return {"device": ip, "state": transport(ip),
            "title": _unesc(t.group(1)) if t else "",
            "artist": _unesc(a.group(1)) if a else "",
            "duration": grab("TrackDuration"), "position": grab("RelTime"),
            "url": grab("TrackURI")}


def _secs(ts):
    try:
        h, m, s = [int(x) for x in ts.split(":")]
        return h * 3600 + m * 60 + s
    except Exception:
        return 0


def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def seek(ip, sec):
    return av(ip, "Seek", f"<Unit>REL_TIME</Unit><Target>{fmt_time(sec)}</Target>")


def play_url(ip, media, title="", artist=""):
    meta = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            f'<item id="1" parentID="0" restricted="1"><dc:title>{sx.escape(title)}</dc:title>'
            '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
            f'<upnp:artist>{sx.escape(artist)}</upnp:artist>'
            f'<res protocolInfo="http-get:*:audio/mpeg:*">{sx.escape(media)}</res></item></DIDL-Lite>')
    av(ip, "SetAVTransportURI",
       f"<CurrentURI>{sx.escape(media)}</CurrentURI>"
       f"<CurrentURIMetaData>{sx.escape(meta)}</CurrentURIMetaData>")
    av(ip, "Play", "<Speed>1</Speed>")
    return transport(ip)


# ---------------- 网易云 ----------------
def ncm_search(q, limit=10):
    h = {"User-Agent": UA, "Referer": "https://music.163.com/", "Cookie": cookie()}
    data = urllib.parse.urlencode({"s": q, "type": 1, "limit": limit, "offset": 0}).encode()
    req = urllib.request.Request("https://music.163.com/api/cloudsearch/pc", data=data, headers=h)
    d = json.load(urllib.request.urlopen(req, timeout=12))
    out = []
    for s in d.get("result", {}).get("songs", []):
        out.append({"song_id": s["id"], "name": s["name"],
                    "artist": "/".join(a["name"] for a in s.get("ar", [])),
                    "album": (s.get("al") or {}).get("name", ""),
                    "fee": s.get("fee")})
    return out


def ncm_url(song_id, br=320000):
    h = {"User-Agent": UA, "Referer": "https://music.163.com/", "Cookie": cookie()}
    data = urllib.parse.urlencode({"ids": f"[{song_id}]", "br": br}).encode()
    req = urllib.request.Request("https://music.163.com/api/song/enhance/player/url",
                                 data=data, headers=h)
    d = json.load(urllib.request.urlopen(req, timeout=12))
    x = (d.get("data") or [{}])[0]
    return x.get("url"), x.get("code"), x.get("fee")


# ---------------- 本地音乐 ----------------
def parse_name(path):
    base = os.path.splitext(os.path.basename(path))[0]
    for sep in (" - ", "-", "—"):
        if sep in base:
            a, b = base.split(sep, 1)
            return b.strip(), a.strip()
    return base, ""


def scan_local(root=None):
    root = root or MUSIC_DIR
    os.makedirs(root, exist_ok=True)
    c = db()
    added = 0
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if not fn.lower().endswith(AUDIO_EXT):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                stt = os.stat(fp)
            except Exception:
                continue
            title, artist = parse_name(fp)
            row = c.execute("SELECT id FROM local_tracks WHERE path=?", (fp,)).fetchone()
            if row:
                c.execute("UPDATE local_tracks SET size=?, mtime=? WHERE id=?",
                          (stt.st_size, int(stt.st_mtime), row["id"]))
            else:
                c.execute("""INSERT INTO local_tracks(path,title,artist,album,size,mtime)
                             VALUES(?,?,?,?,?,?)""",
                          (fp, title, artist, os.path.basename(dirpath), stt.st_size, int(stt.st_mtime)))
                added += 1
    c.commit(); c.close()
    return added


def list_local(q="", limit=200):
    c = db()
    if q:
        rows = c.execute("""SELECT * FROM local_tracks WHERE title LIKE ? OR artist LIKE ?
                            ORDER BY artist,title LIMIT ?""", (f"%{q}%", f"%{q}%", limit)).fetchall()
    else:
        rows = c.execute("SELECT * FROM local_tracks ORDER BY artist,title LIMIT ?", (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def local_media_path(track):
    """返回可直接给音箱播放的文件路径（必要时转码）"""
    src = track["path"]
    ext = os.path.splitext(src)[1].lower()
    if ext not in NEED_TRANSCODE:
        return src
    os.makedirs(CACHE_DIR, exist_ok=True)
    out = os.path.join(CACHE_DIR, f'{track["id"]}.mp3')
    if os.path.exists(out) and os.path.getmtime(out) >= track["mtime"]:
        return out
    import subprocess
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                    "-codec:a", "libmp3lame", "-b:a", "320k", out],
                   check=True, timeout=300)
    return out


def media_token(tid):
    pw = get_setting("ui_password")
    if not pw:
        return ""
    import hashlib
    return hashlib.sha256(f"{pw}:{tid}".encode()).hexdigest()[:16]


def local_url(track):
    tk = media_token(track["id"])
    q = f"?t={tk}" if tk else ""
    return f"http://{LAN_IP}:8010/media/{track['id']}{q}"


# ---------------- 网易云登录管理 ----------------
LOGIN = {"status": "idle", "key": None, "png": "/tmp/netease_qr.png",
         "started": 0, "message": ""}


def cookie_path():
    return COOKIE_FILE


def current_uid():
    try:
        a = ncm_get("https://music.163.com/api/nuser/account/get")
        return (a.get("profile") or {}).get("userId")
    except Exception:
        return None


def account_info():
    ck = cookie()
    if not ck or "MUSIC_U=" not in ck:
        return {"ok": False, "logged_in": False, "reason": "没有 cookie"}
    try:
        a = ncm_get("https://music.163.com/api/nuser/account/get")
    except Exception as e:
        return {"ok": False, "logged_in": False, "reason": f"请求失败 {e!r}"}
    prof = a.get("profile") or {}
    return {"ok": True, "logged_in": bool(prof.get("userId")),
            "nickname": prof.get("nickname"), "uid": prof.get("userId"),
            "avatar": prof.get("avatarUrl"), "vip": (a.get("account") or {}).get("vipType"),
            "reason": "" if prof.get("userId") else "cookie 已失效，请重新扫码"}


def qr_start():
    import random, string
    UA2 = UA
    h = {"User-Agent": UA2, "Referer": "https://music.163.com/",
         "Cookie": f"os=pc; _ntes_nuid={''.join(random.choices(string.ascii_letters+string.digits,k=32))}"
                   f"; NMTID={''.join(random.choices(string.ascii_letters+string.digits,k=20))}; appver=2.9.7"}
    d = json.load(urllib.request.urlopen(urllib.request.Request(
        "https://music.163.com/api/login/qrcode/unikey?type=1", headers=h), timeout=12))
    key = d.get("unikey")
    if not key:
        LOGIN.update({"status": "error", "message": d.get("message") or "取码失败"})
        return LOGIN
    import segno
    segno.make(f"https://music.163.com/login?codekey={key}", error="m").save(
        LOGIN["png"], scale=8, border=3)
    LOGIN.update({"status": "waiting", "key": key, "started": int(time.time()), "message": "等待扫码"})
    threading.Thread(target=qr_poll, args=(key, h), daemon=True).start()
    return LOGIN


def qr_poll(key, h):
    end = time.time() + 300
    while time.time() < end:
        try:
            req = urllib.request.Request(
                f"https://music.163.com/api/login/qrcode/client/login?key={key}&type=1", headers=h)
            r = urllib.request.urlopen(req, timeout=12)
            d = json.loads(r.read().decode())
            code = d.get("code")
            if code == 802:
                LOGIN["status"] = "scanned"; LOGIN["message"] = "已扫码，请在手机上确认"
            elif code == 803:
                mu = None
                for c in (r.headers.get_all("Set-Cookie") or []):
                    if c.startswith("MUSIC_U="):
                        mu = c.split(";")[0].split("=", 1)[1]
                if mu:
                    open(COOKIE_FILE, "w").write(f"MUSIC_U={mu}; os=pc")
                    LOGIN.update({"status": "success", "message": "登录成功"})
                else:
                    LOGIN.update({"status": "error", "message": "未拿到 MUSIC_U"})
                return
            elif code == 800:
                LOGIN.update({"status": "expired", "message": "二维码已过期"})
                return
        except Exception as e:
            LOGIN["message"] = f"轮询异常 {e!r}"
        time.sleep(2)
    LOGIN.update({"status": "expired", "message": "超时未扫码"})


# ---------------- 网易云歌单 ----------------
def ncm_get(url):
    h = {"User-Agent": UA, "Referer": "https://music.163.com/", "Cookie": cookie()}
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=15))


def ncm_uid():
    a = ncm_get("https://music.163.com/api/nuser/account/get")
    return (a.get("account") or {}).get("id"), (a.get("profile") or {}).get("nickname", "")


def ncm_playlists():
    uid, nick = ncm_uid()
    if not uid:
        return {"ok": False, "error": "cookie 失效，需要重新扫码"}, 401
    p = ncm_get(f"https://music.163.com/api/user/playlist?uid={uid}&limit=1000&offset=0")
    out = []
    for x in (p.get("playlist") or []):
        c = db()
        imported = c.execute("SELECT id FROM playlists WHERE source='netease' AND remote_id=?",
                             (x["id"],)).fetchone()
        c.close()
        out.append({"id": x["id"], "name": x["name"], "count": x.get("trackCount", 0),
                    "cover": x.get("coverImgUrl", ""),
                    "imported_id": imported["id"] if imported else None})
    return {"ok": True, "uid": uid, "nickname": nick, "playlists": out}, 200


def ncm_playlist_tracks(pid):
    d = ncm_get(f"https://music.163.com/api/v3/playlist/detail?id={pid}&n=1000")
    songs = (d.get("playlist") or {}).get("tracks") or []
    return [{"song_id": s["id"], "name": s["name"],
             "artist": "/".join(a["name"] for a in s.get("ar", [])),
             "album": (s.get("al") or {}).get("name", "")} for s in songs]


def import_netease_playlist(remote_id, name=None, cover=""):
    tracks = ncm_playlist_tracks(remote_id)
    if name is None:
        info, _ = ncm_playlists()
        for p in info.get("playlists", []):
            if p["id"] == remote_id:
                name, cover = p["name"], p.get("cover", "")
    owner = current_uid()
    c = db()
    row = c.execute("SELECT id,owner_uid FROM playlists WHERE source='netease' AND remote_id=?",
                    (remote_id,)).fetchone()
    if row:
        pid = row["id"]
        if owner and row["owner_uid"] != owner:
            c.execute("UPDATE playlists SET owner_uid=? WHERE id=?", (owner, pid))
    else:
        cur = c.execute("""INSERT INTO playlists(name,created_at,source,remote_id,cover,owner_uid)
                           VALUES(?,?,?,?,?,?)""",
                        (name or "网易云歌单", int(time.time()), "netease", remote_id, cover, owner))
        pid = cur.lastrowid
    have = {r["song_id"] for r in c.execute(
        "SELECT song_id FROM tracks WHERE playlist_id=?", (pid,)).fetchall()}
    pos = c.execute("SELECT COALESCE(MAX(pos),0) FROM tracks WHERE playlist_id=?",
                    (pid,)).fetchone()[0]
    added = 0
    for t in tracks:
        if t["song_id"] in have:
            continue
        pos += 1
        c.execute("""INSERT INTO tracks(playlist_id,song_id,name,artist,album,pos)
                     VALUES(?,?,?,?,?,?)""",
                  (pid, t["song_id"], t["name"], t["artist"], t["album"], pos))
        added += 1
    c.commit(); c.close()
    return {"ok": True, "playlist_id": pid, "name": name, "added": added, "total": len(tracks)}


DL = {"running": False, "done": 0, "total": 0, "current": "", "ok": 0,
      "skipped": 0, "errors": [], "finished_at": None, "dir": None}


def safe_name(x):
    import re as _re
    x = _re.sub(r'[\\/:*?"<>|]+', "_", (x or "").strip())
    return x[:120] or "unknown"


def download_song(song_id, name, artist, album=""):
    """下载单曲到 MUSIC_DIR，返回 (status, path)"""
    folder = os.path.join(MUSIC_DIR, safe_name(artist or "未知歌手"))
    os.makedirs(folder, exist_ok=True)
    base = safe_name(f"{artist} - {name}" if artist else name)
    final = os.path.join(folder, base + ".mp3")
    if os.path.exists(final) and os.path.getsize(final) > 10240:
        return "skipped", final
    url, code, fee = ncm_url(song_id)
    if not url:
        return f"no-url({code})", None
    tmp = final + ".part"
    h = {"User-Agent": UA, "Referer": "https://music.163.com/"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            f.write(chunk)
    if os.path.getsize(tmp) < 10240:
        os.remove(tmp)
        return "too-small", None
    try:
        import subprocess
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", tmp,
                        "-c", "copy", "-metadata", f"title={name}",
                        "-metadata", f"artist={artist or ''}",
                        "-metadata", f"album={album or ''}", final],
                       check=True, timeout=120)
        os.remove(tmp)
    except Exception:
        os.replace(tmp, final)
    return "ok", final


def download_worker(items, label=""):
    DL.update({"running": True, "done": 0, "total": len(items), "current": label,
               "ok": 0, "skipped": 0, "errors": [], "finished_at": None, "dir": MUSIC_DIR})
    for i, it in enumerate(items):
        DL["current"] = f'{it.get("name", "")}'
        try:
            st, path = download_song(it["song_id"], it.get("name", ""), it.get("artist", ""),
                                     it.get("album", ""))
            if st == "ok":
                DL["ok"] += 1
            elif st == "skipped":
                DL["skipped"] += 1
            else:
                DL["errors"].append(f'{it.get("name")}: {st}')
        except Exception as e:
            DL["errors"].append(f'{it.get("name")}: {e!r}')
        DL["done"] = i + 1
    try:
        scan_local()
    except Exception:
        pass
    DL.update({"running": False, "current": "", "finished_at": int(time.time())})


SYNC_STATE = {"running": False, "done": 0, "total": 0, "current": "",
              "added": 0, "errors": [], "finished_at": None}


def sync_all_worker():
    SYNC_STATE.update({"running": True, "done": 0, "total": 0, "current": "",
                       "added": 0, "errors": [], "finished_at": None})
    try:
        info, _ = ncm_playlists()
        pls = info.get("playlists", [])
        SYNC_STATE["total"] = len(pls)
        for i, pl in enumerate(pls):
            SYNC_STATE["current"] = pl["name"]
            try:
                r = import_netease_playlist(pl["id"], pl["name"], pl.get("cover", ""))
                SYNC_STATE["added"] += r.get("added", 0)
            except Exception as e:
                SYNC_STATE["errors"].append(f'{pl["name"]}: {e!r}')
            SYNC_STATE["done"] = i + 1
    finally:
        SYNC_STATE["running"] = False
        SYNC_STATE["current"] = ""
        SYNC_STATE["finished_at"] = int(time.time())


# ---------------- 设备发现 ----------------
def ssdp_scan(timeout=5):
    msg = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
           'MAN: "ssdp:discover"\r\nMX: 3\r\nST: ssdp:all\r\n\r\n').encode()
    s = __import__("socket").socket(__import__("socket").AF_INET, __import__("socket").SOCK_DGRAM)
    s.setsockopt(__import__("socket").IPPROTO_IP, __import__("socket").IP_MULTICAST_TTL, 2)
    s.settimeout(1.0)
    s.sendto(msg, ("239.255.255.250", 1900))
    found, end = {}, time.time() + timeout
    while time.time() < end:
        try:
            data, addr = s.recvfrom(65535)
        except Exception:
            continue
        txt = data.decode("utf-8", "ignore")
        loc = re.search(r"(?im)^location:\s*(\S+)", txt)
        if not loc or addr[0] in found:
            continue
        try:
            desc = urllib.request.urlopen(loc.group(1), timeout=3).read().decode("utf-8", "ignore")
        except Exception:
            continue
        if "MediaRenderer" not in desc:
            continue
        fn = re.search(r"<friendlyName>([^<]*)</friendlyName>", desc)
        mn = re.search(r"<modelName>([^<]*)</modelName>", desc)
        udn = re.search(r"<UDN>([^<]*)</UDN>", desc)
        name = _unesc(fn.group(1)).strip() if fn else addr[0]
        found[addr[0]] = {"ip": addr[0], "name": name,
                          "model": mn.group(1) if mn else "",
                          "udn": udn.group(1) if udn else ""}
    return list(found.values())


def save_device(d):
    c = db()
    c.execute("""INSERT INTO devices(ip,name,model,udn,last_seen,created_at)
                 VALUES(?,?,?,?,?,?)
                 ON CONFLICT(ip) DO UPDATE SET name=excluded.name, model=excluded.model,
                 udn=excluded.udn, last_seen=excluded.last_seen""",
              (d["ip"], d["name"], d.get("model", ""), d.get("udn", ""),
               int(time.time()), int(time.time())))
    c.commit(); c.close()


# ---------------- 播放队列 ----------------
QUEUE = {"playlist_id": None, "index": None, "device": None,
         "mode": "sequential", "history": [],
         "last_state": None, "last_pos": 0, "manual_stop": False,
         "paused": None, "last_playing_at": 0, "active": False}
MODES = ("sequential", "loop", "single", "shuffle")


def playlist_allowed(pid):
    c = db()
    row = c.execute("SELECT source,owner_uid FROM playlists WHERE id=?", (pid,)).fetchone()
    c.close()
    if not row:
        return False
    if row["source"] != "netease":
        return True
    return row["owner_uid"] is not None and row["owner_uid"] == current_uid()


def playlist_tracks(pid):
    c = db(); rows = c.execute(
        "SELECT * FROM tracks WHERE playlist_id=? ORDER BY pos, id", (pid,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def play_track(ip, tr):
    if tr.get("local_id"):
        c = db()
        loc = c.execute("SELECT * FROM local_tracks WHERE id=?", (tr["local_id"],)).fetchone()
        c.close()
        if not loc:
            return {"ok": False, "error": "local track missing"}
        loc = dict(loc)
        local_media_path(loc)
        st = play_url(ip, local_url(loc), loc["title"], loc["artist"] or "")
        return {"ok": True, "song": {"name": loc["title"], "artist": loc["artist"]},
                "state": st, "device": ip, "source": "local"}
    url, code, fee = ncm_url(tr["song_id"])
    if not url:
        return {"ok": False, "error": "no url", "code": code, "fee": fee, "track": tr["name"]}
    st = play_url(ip, url, tr["name"], tr["artist"] or "")
    return {"ok": True, "song": {"name": tr["name"], "artist": tr["artist"]},
            "state": st, "device": ip, "source": "netease"}


def play_index(pid, idx, ip=None):
    tracks = playlist_tracks(pid)
    if not tracks:
        return {"ok": False, "error": "empty playlist"}
    idx = max(0, min(idx, len(tracks) - 1))
    ip = ip or active_ip()
    r = play_track(ip, tracks[idx])
    if r.get("ok"):
        if QUEUE.get("index") is not None and QUEUE.get("index") != idx:
            QUEUE.setdefault("history", []).append(QUEUE["index"])
            QUEUE["history"] = QUEUE["history"][-50:]
        QUEUE.update({"playlist_id": pid, "index": idx, "device": ip,
                      "last_state": None, "last_pos": 0, "manual_stop": False,
         "paused": None, "last_playing_at": 0, "active": False})
    return r


def advance(delta=1, natural=False):
    pid, idx = QUEUE.get("playlist_id"), QUEUE.get("index")
    if pid is None or idx is None:
        return {"ok": False, "error": "no queue"}
    tracks = playlist_tracks(pid)
    if not tracks:
        return {"ok": False, "error": "empty playlist"}
    mode = QUEUE.get("mode", "sequential")
    n = len(tracks)

    if natural and mode == "single":
        return play_index(pid, idx, QUEUE.get("device"))

    if mode == "shuffle":
        if delta < 0 and QUEUE.get("history"):
            return play_index(pid, QUEUE["history"][-1], QUEUE.get("device"))
        import random
        if n == 1:
            return play_index(pid, 0, QUEUE.get("device"))
        nxt = idx
        while nxt == idx:
            nxt = random.randrange(n)
        return play_index(pid, nxt, QUEUE.get("device"))

    nxt = idx + delta
    if nxt >= n:
        if mode == "loop":
            nxt = 0
        else:
            return {"ok": False, "error": "end of playlist"}
    if nxt < 0:
        nxt = 0 if mode != "loop" else n - 1
    return play_index(pid, nxt, QUEUE.get("device"))


def quiet_range():
    """返回免打扰时间段 'HH:MM-HH:MM'，空字符串表示关闭"""
    return get_setting("quiet_hours", "23:30-08:00") or ""


def in_quiet_hours(now=None):
    rng = quiet_range()
    if not rng or "-" not in rng:
        return False
    import datetime as _dt
    now = now or _dt.datetime.now()
    try:
        a, b = rng.split("-", 1)
        sh, sm = [int(x) for x in a.split(":")]
        eh, em = [int(x) for x in b.split(":")]
    except Exception:
        return False
    cur = now.hour * 60 + now.minute
    start = sh * 60 + sm
    end = eh * 60 + em
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end      # 跨午夜


def auto_advance_loop():
    """曲目播完后自动下一首。

    三种判定，任一成立就切歌：
    1) 状态从播放态变成 STOPPED
    2) 有总时长时，进度到尾部 2 秒内
    3) 没总时长（部分本地文件/直播流），进度从 >2 秒突然回到 0
    """
    while True:
        try:
            if in_quiet_hours():
                QUEUE["last_state"] = None
                time.sleep(30)
                continue
            if QUEUE.get("playlist_id") is not None and not QUEUE.get("manual_stop"):
                ip = QUEUE.get("device") or active_ip()
                np = now_playing(ip)
                st = np["state"]
                dur = _secs(np.get("duration") or "")
                pos = _secs(np.get("position") or "")
                prev_state = QUEUE.get("last_state")
                last_pos = QUEUE.get("last_pos") or 0
                if st == "PLAYING":
                    QUEUE["last_playing_at"] = time.time()
                # 只有最近 2 分钟内确实在播、且队列处于活跃状态，才允许自动续播；
                # 防止音箱被别的音源占用/断网恢复后，半夜自己接着放
                fresh = (time.time() - (QUEUE.get("last_playing_at") or 0)) <= 120
                ended = False
                if st == "STOPPED" and prev_state in ("PLAYING", "TRANSITIONING", "PAUSED_PLAYBACK"):
                    ended = True
                elif st == "PLAYING":
                    if dur > 0 and pos >= max(dur - 2, 1):
                        ended = True
                    elif dur == 0 and last_pos >= 3 and pos <= 1:
                        ended = True
                if ended and prev_state != "ADVANCING" and fresh and QUEUE.get("active"):
                    QUEUE["last_state"] = "ADVANCING"
                    r = advance(1, natural=True)
                    with open("/root/xiaoai-music/advance.log", "a") as f:
                        f.write(f"{time.strftime('%H:%M:%S')} advance next={r.get('ok')} err={r.get('error','')}\n")
                    if not r.get("ok"):
                        QUEUE["last_state"] = "STOPPED"
                        if r.get("error") in ("end of playlist", "empty playlist"):
                            QUEUE["active"] = False
                elif st != "ADVANCING":
                    QUEUE["last_state"] = st
                    QUEUE["last_pos"] = pos
        except Exception as e:
            try:
                with open("/root/xiaoai-music/advance.log", "a") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} loop error {e!r}\n")
            except Exception:
                pass
        time.sleep(2)


# ---------------- Web ----------------
UI = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>小爱音乐中枢</title>
<style>
*{box-sizing:border-box;min-width:0}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;
background:#0f1115;color:#e8eaed;padding:18px}
.wrap{max-width:860px;width:100%;margin:0 auto}
header{display:flex;align-items:center;gap:12px;margin-bottom:16px}
h1{font-size:17px;font-weight:600;margin:0;flex:1}
select{background:#171a21;border:1px solid #232834;color:#e8eaed;border-radius:9px;padding:8px 10px;font-size:13px}
.card{background:#171a21;border:1px solid #232834;border-radius:14px;padding:16px;margin-bottom:12px;overflow:hidden}
.t{font-size:19px;font-weight:600;min-height:26px}
.a{color:#9aa3b2;font-size:13px}
.bar{height:4px;background:#232834;border-radius:3px;margin:12px 0 6px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#4f8cff,#7cc4ff);transition:width .4s linear}
.times{display:flex;justify-content:space-between;color:#6f7889;font-size:11px}
.state{display:inline-block;font-size:10px;padding:2px 7px;border-radius:99px;background:#1f2633;color:#7cc4ff;margin-left:8px;vertical-align:middle;font-weight:400}
.ctrl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px}
button{cursor:pointer;border:0;border-radius:9px;padding:9px 14px;font-size:13px;background:#232834;color:#e8eaed}
button:hover{background:#2c3342}button.primary{background:#3b6fe0;color:#fff}
button.ghost{background:transparent;border:1px solid #2a3040}
input[type=search]{flex:1;min-width:160px;background:#0f1115;border:1px solid #232834;color:#e8eaed;border-radius:9px;padding:10px 13px;font-size:14px;outline:none}
input[type=search]:focus{border-color:#3b6fe0}
.row{display:flex;gap:8px;align-items:center}
.item{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 2px;border-bottom:1px solid #1d222c}
.item:last-child{border-bottom:0}
.n{font-size:14px}.m{color:#7b8494;font-size:12px}
.vip{font-size:10px;color:#f2b34b;border:1px solid #4a3a18;padding:1px 5px;border-radius:5px;margin-left:6px}
.tabs{display:flex;gap:6px;margin-bottom:10px}
.tabs button{font-size:12px;padding:6px 12px;background:transparent;border:1px solid #2a3040;color:#9aa3b2;border-radius:99px}
.tabs button.on{background:#232834;color:#e8eaed;border-color:#2c3342}
input[type=range]{width:110px}
.volrow{display:flex;align-items:center;gap:10px;width:100%;margin-top:4px}
.volrow input[type=range]{flex:1;width:auto}
.volrow #vl{flex:0 0 26px;text-align:right}
.modewrap{margin-top:8px;border-top:1px solid #1d222c;padding-top:10px}
.modes{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:6px}
.modes button{padding:9px 4px;font-size:12px}
/* ---- 手机适配 ---- */
@media (max-width:600px){
  body{padding:12px 12px 24px}
  header{flex-wrap:wrap;gap:8px;margin-bottom:12px}
  header h1{flex:1 0 100%;font-size:16px;margin-bottom:2px}
  .acct{order:2;max-width:44px;padding:4px;justify-content:center}
  .acct span:not(.dot){display:none}
  .acct img{width:20px;height:20px}
  #dev{order:3;flex:1 1 auto;min-width:0;font-size:13px;padding:8px}
  header button{order:4;padding:8px 12px;font-size:12px}
  .card{padding:13px;border-radius:12px;margin-bottom:10px}
  .t{font-size:17px}
  .state{font-size:10px}
  .ctrl{gap:8px}
  .ctrl button{flex:1 1 28%;padding:10px 6px;font-size:13px}
  .ctrl button[id^=mo_]{flex:1 1 20%;padding:9px 2px;font-size:12px}
  .ctrl .mini{flex:0 0 auto}
  input[type=range]{height:28px}
  #vl{flex:0 0 auto}
  .row{flex-wrap:wrap}
  input[type=search]{font-size:16px;padding:12px 13px}
  .row button{flex:1 1 auto}
  .item{padding:11px 2px;gap:8px;flex-wrap:wrap}
  .item>div{min-width:0}
  .item .n,.item .m{overflow-wrap:anywhere}
  .item button{padding:9px 12px;font-size:12px}
  .n{font-size:15px}
  .tabs{overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
  .tabs::-webkit-scrollbar{display:none}
  .tabs button{flex:0 0 auto}
}
.mini{font-size:11px;color:#6f7889}
.acct{display:flex;align-items:center;gap:7px;background:#171a21;border:1px solid #232834;
border-radius:99px;padding:5px 11px 5px 5px;cursor:pointer;font-size:12px;color:#c8cdd6;max-width:180px}
.acct img{width:22px;height:22px;border-radius:50%;object-fit:cover;background:#232834}
.acct .dot{width:7px;height:7px;border-radius:50%;background:#e06c75;flex:0 0 auto}
.acct .dot.on{background:#57c785}
.mask{position:fixed;inset:0;background:rgba(0,0,0,.65);display:none;align-items:center;justify-content:center;padding:18px;z-index:9}
.mask.on{display:flex}
.modal{background:#171a21;border:1px solid #232834;border-radius:16px;padding:20px;max-width:340px;width:100%;text-align:center}
.modal h3{margin:0 0 10px;font-size:15px}
.modal img{width:100%;max-width:240px;border-radius:10px;background:#fff;padding:6px}
.modal p{color:#8b93a1;font-size:12px;margin:10px 0}
.modal .row{justify-content:center;margin-top:12px}
</style></head><body><div class=wrap>
<header><h1>小爱音乐中枢</h1>
<div id=acct class=acct onclick="openAcct()" title="网易云账号">…</div>
<select id=dev onchange="pick(this.value)"></select>
<button class=ghost onclick="scan()">发现设备</button>
</header>

<div class="card">
  <div class=t><span id=t>未在播放</span><span class=state id=st>—</span></div>
  <div class=a id=a></div>
  <div class=bar><i id=pr></i></div>
  <div class=times><span id=pos>0:00</span><span id=dur>0:00</span></div>
  <div class=ctrl>
    <button class=primary onclick=c('resume')>▶︎ 继续</button>
    <button onclick=c('pause')>⏸ 暂停</button>
    <button onclick=c('stop')>⏹ 停止</button>
    <button class=ghost onclick="nav('prev')">上一首</button>
    <button class=ghost onclick="nav('next')">下一首</button>
    <div class=volrow>
      <span class=mini>音量</span>
      <input type=range id=vol min=0 max=100 value=30 oninput="onVol(this.value)">
      <span id=vl class=mini>30</span>
    </div>
  </div>
  <div class=modewrap>
    <span class=mini>播放模式</span>
    <div class=modes>
      <button id=mo_seq class=ghost onclick="setMode('sequential')">顺序</button>
      <button id=mo_loop class=ghost onclick="setMode('loop')">列表循环</button>
      <button id=mo_single class=ghost onclick="setMode('single')">单曲循环</button>
      <button id=mo_shuffle class=ghost onclick="setMode('shuffle')">随机</button>
    </div>
  </div>
</div>

<div class=card>
  <div class=tabs>
    <button class=on id=tb1 onclick="tab(1)">搜索</button>
    <button id=tb2 onclick="tab(2)">歌单</button>
    <button id=tb3 onclick="tab(3)">队列</button>
    <button id=tb4 onclick="tab(4)">本地</button>
  </div>

  <div id=p1>
    <div class=row><input type=search id=q placeholder="搜索歌曲、歌手…" onkeydown="if(event.key==='Enter')s()">
    <button class=primary onclick=s()>搜索</button></div>
    <div id=res></div>
  </div>

  <div id=p2 style="display:none">
    <div class=row style="margin-bottom:8px">
      <select id=pl onchange="loadPl()"></select>
      <button onclick="newPl()">新建歌单</button>
      <button class=ghost onclick="ncmList()">网易云导入</button>
      <button class=ghost onclick="syncAll()">全部同步</button>
      <button class=primary onclick="playPl()">播放整个歌单</button>
      <button class=ghost onclick="dlPl()">下载整张</button>
    </div>
    <div id=ncmlist></div>
    <div id=tracks></div>
  </div>

  <div id=p4 style="display:none">
    <div class=row style="margin-bottom:8px">
      <input type=search id=ql placeholder="本地曲库搜索…" onkeydown="if(event.key==='Enter')loadLocal()">
      <button onclick="loadLocal()">搜索</button>
      <button class=ghost onclick="scanLocal()">扫描目录</button>
    </div>
    <div class=mini id=linfo style="margin-bottom:6px"></div>
    <div id=ltracks></div>
  </div>

  <div id=p3 style="display:none">
    <div class=mini style="margin-bottom:8px">正在播放的是歌单里的曲目；「下一首」按队列顺序走。</div>
    <div id=queued></div>
  </div>
</div>

<div class=mini style="text-align:center;margin:6px 0 20px">DLNA 直推 · 网易云音源 · 局域网内使用</div>

<div class=mask id=mask onclick="if(event.target===this)closeAcct()">
 <div class=modal>
  <h3 id=mTitle>网易云账号</h3>
  <div id=mBody></div>
  <div class=row id=mBtns></div>
 </div>
</div>
</div>
<script>
const $=id=>document.getElementById(id);
let DEV='', PLS=[], CUR_PL=null;

function f2(x){if(!x)return'0:00';const p=x.split(':');if(p.length<3)return x;return (+p[1])+':'+p[2].padStart(2,'0')}
function secs(x){const p=f2(x).split(':');return (+p[0])*60+(+p[1])}

async function boot(){
  const d=await (await fetch('/devices')).json();
  $('dev').innerHTML=d.devices.map(x=>`<option value="${x.ip}"${x.active?' selected':''}>${x.name} (${x.ip})</option>`).join('');
  DEV=d.active||'';
  await loadPls(); poll();
}
let ACCT=null,QR=null;
async function loadAccount(){
 try{const d=await (await fetch('/account')).json();ACCT=d;
  $('acct').innerHTML = d.logged_in
   ? `<img src="${d.avatar}"><span class=dot></span><span style="max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${d.nickname}${d.vip?' VIP':''}</span>`
   : `<span class=dot></span><span>未登录</span>`;
 }catch(e){$('acct').innerHTML='<span class=dot></span><span>登录异常</span>'}
}
function closeAcct(){$('mask').classList.remove('on');if(QR){clearInterval(QR);QR=null}}
async function openAcct(){
 $('mask').classList.add('on');
 if(ACCT&&ACCT.logged_in){
  $('mTitle').textContent='网易云账号';
  $('mBody').innerHTML=`<p>${ACCT.nickname}${ACCT.vip?' · VIP':''}<br>UID ${ACCT.uid}</p>`;
  $('mBtns').innerHTML='<button class=ghost onclick="startQR()">重新登录</button><button onclick="logout()">退出登录</button>';
 }else{ startQR(); }
}
async function startQR(){
 $('mTitle').textContent='扫码登录网易云';
 $('mBody').innerHTML='<p>请用网易云 App 扫一扫</p>';
 $('mBtns').innerHTML='<button class=ghost onclick="closeAcct()">关闭</button>';
 await fetch('/account/qr');
 const img=new Image();img.src='/qr.png?ts='+Date.now();img.style.width='100%';img.style.maxWidth='240px';
 img.style.borderRadius='10px';img.style.background='#fff';img.style.padding='6px';
 $('mBody').innerHTML='';$('mBody').appendChild(img);
 const tip=document.createElement('p');tip.id='qrtip';tip.textContent='等待扫码…';$('mBody').appendChild(tip);
 QR=setInterval(async()=>{
  const d=await (await fetch('/account/qr/status')).json();
  const t=$('qrtip');if(t)t.textContent=d.message||d.status;
  if(d.status==='success'){clearInterval(QR);QR=null;await loadAccount();setTimeout(()=>{closeAcct();ncmList()},700)}
  if(d.status==='expired'||d.status==='error'){clearInterval(QR);QR=null;
    $('mBtns').innerHTML='<button class=primary onclick="startQR()">刷新二维码</button><button class=ghost onclick="closeAcct()">关闭</button>'}
 },1500);
}
async function logout(){await fetch('/account/logout');await loadAccount();closeAcct()}

async function pick(ip){await fetch('/devices/select?ip='+encodeURIComponent(ip));DEV=ip;poll()}
async function scan(){$('dev').disabled=true;await fetch('/devices/scan');$('dev').disabled=false;boot()}
function tab(n){[1,2,3,4].forEach(i=>{$('tb'+i).className=i===n?'on':'';$('p'+i).style.display=i===n?'':'none'});if(n===2)loadPl();if(n===3)loadQueue();if(n===4)loadLocal()}

async function poll(){try{
  const d=await (await fetch('/now')).json();
  $('t').textContent=d.title||'未在播放';$('a').textContent=d.artist||'';
  $('st').textContent=d.title?d.state:'';
  $('pos').textContent=f2(d.position);$('dur').textContent=f2(d.duration);
  const cs=secs(d.position),ts=secs(d.duration);
  $('pr').style.width=ts?Math.min(100,cs/ts*100)+'%':'0%';
}catch(e){}}
async function c(a){await fetch('/'+a);poll()}
async function nav(d){const r=await (await fetch('/'+d)).json();if(!r.ok)alert(r.error||'失败');poll();loadQueue()}
let VOLT=null;
function onVol(v){$('vl').textContent=v;
 if(VOLT)clearTimeout(VOLT);
 VOLT=setTimeout(async()=>{VOLT=null;
   try{await fetch('/volume?level='+v)}catch(e){}
 },180);
}

async function s(){const q=$('q').value.trim();if(!q)return;
 $('res').innerHTML='<div class=mini style="padding:8px">搜索中…</div>';
 const d=await (await fetch('/search?q='+encodeURIComponent(q))).json();
 if(!d.songs||!d.songs.length){$('res').innerHTML='<div class=mini style="padding:8px">没找到</div>';return}
 window._cur=d.songs;
 $('res').innerHTML=d.songs.map((x,i)=>`<div class=item><div><div class=n>${x.name}${x.fee===1?'<span class=vip>VIP</span>':''}</div>
 <div class=m>${x.artist}${x.album?' · '+x.album:''}</div></div>
 <div class=row><button class=ghost onclick="addTo(${i},this)">+ 歌单</button>
 <button class=primary onclick="playNow(${i})">播放</button></div></div>`).join('')}
async function playNow(i){const d=await (await fetch('/play?q='+encodeURIComponent($('q').value)+'&i='+i)).json();
 if(!d.ok)alert(d.error||'播放失败');poll()}
async function addTo(i,btn){const s=window._cur[i];const pid=$('pl').value||(PLS[0]&&PLS[0].id);
 if(!pid){alert('先建个歌单');return}
 if(btn.disabled)return;
 btn.disabled=true;btn.textContent='…';
 try{
  const r=await (await fetch('/playlist/add?id='+pid+'&song_id='+s.song_id+'&name='+encodeURIComponent(s.name)+'&artist='+encodeURIComponent(s.artist)+'&album='+encodeURIComponent(s.album||''))).json();
  btn.textContent=r.added?'已加 ✓':'已在歌单';
  if(r.added)loadPls();
 }catch(e){btn.textContent='失败'}
 setTimeout(()=>{btn.textContent='+ 歌单';btn.disabled=false},1400)}

async function loadPls(){const d=await (await fetch('/playlists')).json();PLS=d.playlists;
 $('pl').innerHTML=PLS.map(p=>`<option value="${p.id}">${p.name} (${p.count})</option>`).join('');
 if(!CUR_PL&&PLS[0])CUR_PL=PLS[0].id;
 if(CUR_PL)$('pl').value=CUR_PL; loadPl()}
async function loadPl(){CUR_PL=$('pl').value;if(!CUR_PL)return;
 const d=await (await fetch('/playlist?id='+CUR_PL)).json();
 $('tracks').innerHTML=(d.tracks||[]).map((t,i)=>`<div class=item><div><div class=n>${i+1}. ${t.name}</div>
 <div class=m>${t.artist||''}</div></div><div class=row>
 <button class=primary onclick="playIdx(${i})">播放</button>
 <button class=ghost onclick="delTrack(${t.id})">删</button></div></div>`).join('')||'<div class=mini style="padding:8px">空歌单</div>'}
let DLT=null;
async function dlPl(){
 if(!CUR_PL)return;
 const r=await (await fetch('/local/download?id='+CUR_PL)).json();
 if(!r.ok){alert(r.error||'失败');return}
 $('ncmlist').innerHTML=`<div class=mini style="padding:8px">开始下载 ${r.total} 首…</div>`;
 if(DLT)clearInterval(DLT);
 DLT=setInterval(async()=>{
  const d=await (await fetch('/local/download/status')).json();
  $('ncmlist').innerHTML=`<div class=mini style="padding:8px">${d.running?'下载中':'完成'} ${d.done}/${d.total} · 成功 ${d.ok} · 跳过 ${d.skipped} · ${d.current||''}</div>`;
  if(!d.running){clearInterval(DLT);DLT=null;loadLocal()}
 },1500);
}
async function playPl(){if(!CUR_PL)return;const d=await (await fetch('/playlist/play?id='+CUR_PL+'&i=0&whole=1')).json();
 if(!d.ok)alert(d.error||'失败');poll()}
async function playIdx(i){const d=await (await fetch('/playlist/play?id='+CUR_PL+'&i='+i)).json();if(!d.ok)alert(d.error||'失败');poll()}
async function delTrack(id){await fetch('/playlist/remove?id='+CUR_PL+'&track='+id);loadPl();loadPls()}
let ST=null;
async function syncAll(){
 $('ncmlist').innerHTML='<div class=mini style="padding:8px">开始全量同步…</div>';
 await fetch('/netease/sync_all');
 if(ST)clearInterval(ST);
 ST=setInterval(async()=>{
   const d=await (await fetch('/netease/sync_all/status')).json();
   $('ncmlist').innerHTML=`<div class=mini style="padding:8px">${d.running?'同步中':'已完成'} ${d.done}/${d.total} · 新增 ${d.added} 首 · ${d.current||''}</div>`;
   if(!d.running){clearInterval(ST);ST=null;loadPls();}
 },1500);
}
async function ncmList(){
 $('ncmlist').innerHTML='<div class=mini style="padding:8px">读取网易云歌单…</div>';
 const d=await (await fetch('/netease/playlists')).json();
 if(!d.ok){$('ncmlist').innerHTML=`<div class=mini style="padding:8px;color:#e06c75">${d.error}</div>`;return}
 $('ncmlist').innerHTML='<div class=mini style="padding:4px 2px">账号：'+d.nickname+'</div>'+
 d.playlists.map(p=>`<div class=item><div><div class=n>${p.name}</div><div class=m>${p.count} 首</div></div>
 <div class=row><button class=ghost onclick="ncmDo(${p.id},'${p.name.replace(/'/g,'')}')">${p.imported_id?'同步':'导入'}</button></div></div>`).join('');
}
async function ncmDo(id,name){
 const pl=PLS.find(x=>x.remote_id==id);
 const url=pl?`/netease/sync?id=${pl.id}`:`/netease/import?id=${id}&name=`+encodeURIComponent(name);
 const d=await (await fetch(url)).json();
 if(!d.ok){alert(d.error||'失败');return}
 await loadPls();$('pl').value=d.playlist_id;loadPl();
 $('ncmlist').innerHTML=`<div class=mini style="padding:8px;color:#7cc4ff">已同步「${d.name||name}」，新增 ${d.added} 首</div>`;
}
async function newPl(){const n=prompt('歌单名');if(!n)return;
 const r=await (await fetch('/playlist/create?name='+encodeURIComponent(n))).json();CUR_PL=r.id;await loadPls();loadPl()}
async function loadQueue(){try{const d=await (await fetch('/queue')).json();
 $('queued').innerHTML=d.playlist?`<div class=item><div><div class=n>${d.playlist}</div>
 <div class=m>第 ${(d.index||0)+1} / ${d.total} 首</div></div><button class=ghost onclick="nav('next')">下一首</button></div>`
 :'<div class=mini style="padding:8px">还没在放歌单</div>'}catch(e){}}
async function loadMode(){try{const d=await (await fetch('/mode')).json();markMode(d.mode)}catch(e){}}
function markMode(m){['seq','loop','single','shuffle'].forEach(k=>{const b=$('mo_'+k);if(!b)return;b.className=k===m?'primary':'ghost'})}
async function setMode(m){await fetch('/mode?set='+m);markMode(m)}
async function loadLocal(){
 const q=$('ql').value.trim();
 const d=await (await fetch('/local/tracks?q='+encodeURIComponent(q))).json();
 $('linfo').textContent=(d.tracks||[]).length+' 首'+(q?'（搜索 "'.replace('"','')+q+'）':'');
 $('ltracks').innerHTML=(d.tracks||[]).map(t=>`<div class=item><div><div class=n>${t.title}</div>
 <div class=m>${t.artist||'未知'} · ${(t.size/1048576).toFixed(1)}MB</div></div>
 <div class=row><button class=ghost onclick="addLocal(${t.id},this)">+ 歌单</button>
 <button class=primary onclick="playLocal(${t.id})">播放</button></div></div>`).join('')||'<div class=mini style="padding:8px">没有本地音乐，把文件放进目录后点“扫描目录”</div>'}
async function playLocal(id){const d=await (await fetch('/local/play?id='+id)).json();if(!d.ok)alert(d.error||'失败');poll()}
async function addLocal(id,btn){const pid=$('pl').value||(PLS[0]&&PLS[0].id);
 if(!pid){alert('先建个歌单');return}
 if(btn&&btn.disabled)return;
 if(btn){btn.disabled=true;btn.textContent='…'}
 try{
  const r=await (await fetch('/local/add?id='+pid+'&track='+id)).json();
  if(btn)btn.textContent=r.added?'已加 ✓':'已在歌单';
  if(r.added)loadPls();
 }catch(e){if(btn)btn.textContent='失败'}
 if(btn)setTimeout(()=>{btn.textContent='+ 歌单';btn.disabled=false},1400)}
async function scanLocal(){$('ltracks').innerHTML='<div class=mini style="padding:8px">扫描中…</div>';
 const d=await (await fetch('/local/scan')).json();loadLocal()}
boot();loadMode();loadAccount();setInterval(poll,2000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _j(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _html(self, s):
        b = s.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        pw = get_setting("ui_password")
        if not pw:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        import base64
        try:
            user, _, given = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return False
        return given == pw

    def _ask_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="xiaoai"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if not u.path.startswith("/media/") and not self._authed():
            return self._ask_auth()
        if u.path.startswith("/media/"):
            tk = (qs.get("t") or [""])[0]
            want = media_token(int(u.path.split("/")[-1]))
            if want and tk != want:
                return self._j({"error": "forbidden"}, 403)
        g = lambda k, d="": qs.get(k, [d])[0]
        try:
            ip = g("ip") or active_ip()
            p = u.path

            if p == "/":
                return self._html(UI)

            # --- 播放控制 ---
            if p == "/now":
                return self._j(now_playing(ip))
            if p == "/status":
                return self._j(now_playing(ip))
            if p in ("/stop", "/pause", "/resume"):
                if p == "/stop":
                    QUEUE["manual_stop"] = True
                    QUEUE["paused"] = None
                    QUEUE["active"] = False
                    av(ip, "Stop")
                    return self._j({"ok": True, "state": transport(ip)})

                if p == "/pause":
                    QUEUE["manual_stop"] = True          # 暂停时不让自动续播
                    np = now_playing(ip)
                    if np.get("url"):
                        QUEUE["paused"] = {"ip": ip, "uri": np["url"],
                                           "title": np.get("title", ""),
                                           "artist": np.get("artist", ""),
                                           "pos": _secs(np.get("position", ""))}
                    av(ip, "Pause")
                    time.sleep(1.2)
                    st = transport(ip)
                    # 有些 DLNA 渲染器不支持 Pause，会直接停掉；这里如实返回
                    return self._j({"ok": True, "state": st,
                                    "fallback": QUEUE["paused"] is not None and st != "PAUSED_PLAYBACK"})

                # resume
                QUEUE["manual_stop"] = False
                st = transport(ip)
                if st == "PAUSED_PLAYBACK":
                    av(ip, "Play", "<Speed>1</Speed>")
                    QUEUE["paused"] = None
                    return self._j({"ok": True, "state": transport(ip), "resumed": "pause"})
                info = QUEUE.get("paused")
                if info and info.get("uri"):
                    play_url(info["ip"], info["uri"], info.get("title", ""), info.get("artist", ""))
                    if info.get("pos", 0) > 1:
                        time.sleep(1.5)   # 等渲染器真正开始拉流，Seek 才生效
                        seek(info["ip"], info["pos"])
                        time.sleep(0.5)
                    QUEUE["paused"] = None
                    return self._j({"ok": True, "state": transport(info["ip"]),
                                    "resumed": "replay", "position": info.get("pos", 0)})
                return self._j({"ok": False, "error": "没有可恢复的播放"}, 409)
            if p == "/volume":
                lv = int(g("level", "30"))
                soap(ip, "RenderingControl:1", "SetVolume",
                     f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{lv}</DesiredVolume>")
                return self._j({"ok": True, "volume": lv})

            # --- 搜索/播放 ---
            if p == "/search":
                return self._j({"songs": ncm_search(g("q"))})
            if p == "/play":
                q = g("q").strip()
                if not q:
                    return self._j({"error": "need q"}, 400)
                songs = ncm_search(q)
                if not songs:
                    return self._j({"error": "not found"}, 404)
                s = songs[min(int(g("i", "0")), len(songs) - 1)]
                url, code, fee = ncm_url(s["song_id"])
                if not url:
                    return self._j({"ok": False, "error": "VIP 曲目需要有效 cookie",
                                    "code": code, "fee": fee, "song": s}, 409)
                return self._j({"ok": True, "song": s,
                                "state": play_url(ip, url, s["name"], s["artist"]), "device": ip})

            # --- 本地音乐 ---
            if p == "/local/scan":
                return self._j({"ok": True, "count": scan_local()})
            if p == "/local/tracks":
                return self._j({"tracks": list_local(g("q"))})
            if p == "/local/play":
                tid = int(g("id", "0"))
                c = db(); tr = c.execute("SELECT * FROM local_tracks WHERE id=?", (tid,)).fetchone(); c.close()
                if not tr:
                    return self._j({"ok": False, "error": "not found"}, 404)
                tr = dict(tr)
                fp = local_media_path(tr)
                return self._j({"ok": True, "song": {"name": tr["title"], "artist": tr["artist"]},
                                "state": play_url(ip, local_url(tr), tr["title"], tr["artist"] or ""),
                                "device": ip})
            if p == "/local/add":
                pid = int(g("id", "0")); tid = int(g("track", "0"))
                c = db()
                tr = c.execute("SELECT * FROM local_tracks WHERE id=?", (tid,)).fetchone()
                if not tr:
                    c.close(); return self._j({"ok": False, "error": "not found"}, 404)
                dup = c.execute(
                    "SELECT id FROM tracks WHERE playlist_id=? AND local_id=?",
                    (pid, tid)).fetchone()
                if dup:
                    c.close()
                    return self._j({"ok": True, "added": False, "message": "已在歌单中"})
                pos = c.execute("SELECT COALESCE(MAX(pos),0)+1 FROM tracks WHERE playlist_id=?",
                                (pid,)).fetchone()[0]
                c.execute("""INSERT INTO tracks(playlist_id,song_id,name,artist,album,pos,local_id)
                             VALUES(?,?,?,?,?,?,?)""",
                          (pid, 0, tr["title"], tr["artist"], tr["album"], pos, tid))
                c.commit(); c.close()
                return self._j({"ok": True})
            if p.startswith("/media/"):
                tid = int(p.split("/")[-1])
                c = db(); tr = c.execute("SELECT * FROM local_tracks WHERE id=?", (tid,)).fetchone(); c.close()
                if not tr:
                    return self._j({"error": "not found"}, 404)
                fp = local_media_path(dict(tr))
                return self._stream_file(fp)
            # --- 下载 ---
            if p == "/local/download":
                pid = int(g("id", "0"))
                if not playlist_allowed(pid):
                    return self._j({"ok": False, "error": "歌单不可用"}, 403)
                if DL["running"]:
                    return self._j({"ok": False, "error": "已有下载任务在跑"}, 409)
                items = [t for t in playlist_tracks(pid) if not t.get("local_id") and t.get("song_id")]
                if not items:
                    return self._j({"ok": False, "error": "这个歌单没有可下载的在线曲目"}, 400)
                threading.Thread(target=download_worker, args=(items,), daemon=True).start()
                return self._j({"ok": True, "started": True, "total": len(items)})
            if p == "/local/download/song":
                if DL["running"]:
                    return self._j({"ok": False, "error": "已有下载任务在跑"}, 409)
                it = {"song_id": int(g("song_id", "0")), "name": g("name"),
                      "artist": g("artist"), "album": g("album")}
                threading.Thread(target=download_worker, args=([it],), daemon=True).start()
                return self._j({"ok": True, "started": True, "total": 1})
            if p == "/local/download/status":
                return self._j(dict(DL))
            # --- 设备 ---
            if p == "/devices":
                c = db()
                rows = [dict(r) for r in c.execute(
                    "SELECT * FROM devices ORDER BY last_seen DESC").fetchall()]
                c.close()
                act = active_ip()
                for r in rows:
                    r["active"] = (r["ip"] == act)
                return self._j({"devices": rows, "active": act})
            if p == "/devices/select":
                set_setting("active_device", g("ip"))
                return self._j({"ok": True, "active": g("ip")})
            if p == "/devices/scan":
                found = ssdp_scan()
                for d in found:
                    save_device(d)
                return self._j({"ok": True, "found": found})

            # --- 网易云账号 ---
            if p == "/account":
                return self._j(account_info())
            if p == "/account/qr":
                return self._j(qr_start())
            if p == "/account/qr/status":
                return self._j(dict(LOGIN))
            if p == "/qr.png":
                if os.path.exists(LOGIN["png"]):
                    return self._stream_file(LOGIN["png"], mime="image/png")
                return self._j({"error": "no qr"}, 404)
            if p == "/account/logout":
                try:
                    open(COOKIE_FILE, "w").write("")
                except Exception:
                    pass
                LOGIN.update({"status": "idle", "message": "已退出"})
                return self._j({"ok": True, "logged_in": False})

            # --- 网易云歌单 ---
            if p == "/netease/playlists":
                data, code = ncm_playlists()
                return self._j(data, code)
            if p == "/netease/import":
                return self._j(import_netease_playlist(int(g("id", "0")), g("name") or None))
            if p == "/netease/sync_all":
                if not SYNC_STATE["running"]:
                    threading.Thread(target=sync_all_worker, daemon=True).start()
                return self._j({"ok": True, "started": True})
            if p == "/netease/sync_all/status":
                return self._j(dict(SYNC_STATE))
            if p == "/netease/sync":
                pid = int(g("id", "0"))
                c = db()
                row = c.execute("SELECT remote_id FROM playlists WHERE id=?", (pid,)).fetchone()
                c.close()
                if not row or not row["remote_id"]:
                    return self._j({"ok": False, "error": "该歌单不是从网易云导入的"}, 400)
                return self._j(import_netease_playlist(row["remote_id"]))

            # --- 歌单 ---
            if p == "/playlists":
                uid = current_uid()
                c = db()
                rows = [dict(r) for r in c.execute(
                    """SELECT p.id,p.name,p.source,p.remote_id,p.owner_uid,COUNT(t.id) AS count
                       FROM playlists p LEFT JOIN tracks t ON t.playlist_id=p.id
                       WHERE p.source='local' OR (p.source='netease' AND p.owner_uid IS ?)
                       GROUP BY p.id ORDER BY p.id""", (uid,)).fetchall()]
                c.close()
                return self._j({"playlists": rows})
            if p == "/playlist/create":
                c = db()
                cur = c.execute("INSERT INTO playlists(name,created_at) VALUES(?,?)",
                                (g("name") or "新歌单", int(time.time())))
                c.commit(); pid = cur.lastrowid; c.close()
                return self._j({"ok": True, "id": pid})
            if p == "/playlist":
                pid = int(g("id", "0"))
                if not playlist_allowed(pid):
                    return self._j({"ok": False, "error": "该歌单属于其他账号或账号已退出"}, 403)
                return self._j({"tracks": playlist_tracks(pid)})
            if p == "/playlist/add":
                pid = int(g("id", "0"))
                sid = int(g("song_id", "0"))
                c = db()
                dup = c.execute(
                    "SELECT id FROM tracks WHERE playlist_id=? AND song_id=? AND COALESCE(local_id,0)=0",
                    (pid, sid)).fetchone()
                if dup:
                    c.close()
                    return self._j({"ok": True, "added": False, "message": "已在歌单中"})
                pos = c.execute("SELECT COALESCE(MAX(pos),0)+1 FROM tracks WHERE playlist_id=?",
                                (pid,)).fetchone()[0]
                c.execute("""INSERT INTO tracks(playlist_id,song_id,name,artist,album,pos)
                             VALUES(?,?,?,?,?,?)""",
                          (pid, sid, g("name"), g("artist"), g("album"), pos))
                c.commit(); c.close()
                return self._j({"ok": True, "added": True})
            if p == "/playlist/remove":
                c = db()
                c.execute("DELETE FROM tracks WHERE id=? AND playlist_id=?",
                          (int(g("track", "0")), int(g("id", "0"))))
                c.commit(); c.close()
                return self._j({"ok": True})
            if p == "/playlist/play":
                _pid = int(g("id", "0"))
                if not playlist_allowed(_pid):
                    return self._j({"ok": False, "error": "该歌单属于其他账号或账号已退出"}, 403)
                idx = int(g("i", "0"))
                # 整个歌单播放且当前是随机模式时，第一首也随机
                if g("whole") and QUEUE.get("mode") == "shuffle":
                    import random as _r
                    total = len(playlist_tracks(_pid))
                    if total > 1:
                        idx = _r.randrange(total)
                return self._j(play_index(_pid, idx, ip))
            if p == "/playlist/delete":
                c = db()
                c.execute("DELETE FROM tracks WHERE playlist_id=?", (int(g("id", "0")),))
                c.execute("DELETE FROM playlists WHERE id=?", (int(g("id", "0")),))
                c.commit(); c.close()
                return self._j({"ok": True})

            # --- 队列 ---
            if p == "/next":
                return self._j(advance(1, natural=True))
            if p == "/prev":
                return self._j(advance(-1))
            if p == "/auth":
                if self.client_address[0] not in ("127.0.0.1", "::1") and get_setting("ui_password"):
                    return self._j({"ok": False, "error": "forbidden"}, 403)
                set_setting("ui_password", g("set"))
                return self._j({"ok": True, "auth": bool(g("set"))})
            if p == "/quiet":
                if "set" in qs:
                    set_setting("quiet_hours", g("set"))
                elif "off" in qs:
                    set_setting("quiet_hours", "")
                return self._j({"ok": True, "quiet_hours": quiet_range(),
                                "active": in_quiet_hours()})
            if p == "/mode":
                m = g("set") or g("m")
                if m in MODES:
                    QUEUE["mode"] = m
                    set_setting("play_mode", m)
                return self._j({"ok": True, "mode": QUEUE["mode"]})
            if p == "/queue":
                pid, idx = QUEUE.get("playlist_id"), QUEUE.get("index")
                if pid is None:
                    return self._j({"playlist": None})
                c = db()
                nm = c.execute("SELECT name FROM playlists WHERE id=?", (pid,)).fetchone()
                c.close()
                tracks = playlist_tracks(pid)
                cur = tracks[idx] if idx is not None and idx < len(tracks) else {}
                return self._j({"playlist": nm["name"] if nm else "",
                                "index": idx or 0, "total": len(tracks),
                                "mode": QUEUE.get("mode", "sequential"),
                                "total_duration": 0,
                                "track": {"name": cur.get("name"), "artist": cur.get("artist")}})

            return self._j({"error": "unknown"}, 404)
        except Exception as e:
            return self._j({"error": repr(e)}, 500)

    def _stream_file(self, path, mime="audio/mpeg"):
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            parts = rng[6:].split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
        end = min(end, size - 1)
        length = max(0, end - start + 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except Exception:
                    break
                left -= len(chunk)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    init_db()
    QUEUE["mode"] = get_setting("play_mode", "sequential")
    threading.Thread(target=auto_advance_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8010), H).serve_forever()
