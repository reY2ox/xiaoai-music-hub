"""HTTP 路由层：只负责解析请求、调用 core、返回结果。

业务逻辑不写在这里——这里越薄，越容易维护。
"""
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, dlna, downloader, library, netease, player as player_mod

LOGIN = {"status": "idle", "key": None, "png": "/tmp/netease_qr.png",
         "started": 0, "message": ""}
SYNC = {"running": False, "done": 0, "total": 0, "current": "", "added": 0,
        "errors": [], "finished_at": None}


# ---------- 工具 ----------
def media_token(tid):
    pw = config.get_setting("ui_password")
    if not pw:
        return ""
    import hashlib
    return hashlib.sha256(f"{pw}:{tid}".encode()).hexdigest()[:16]


def playlist_allowed(pid):
    c = config.db()
    try:
        row = c.execute("SELECT source,owner_uid FROM playlists WHERE id=?", (pid,)).fetchone()
    finally:
        c.close()
    if not row:
        return False
    if row["source"] != "netease":
        return True
    return row["owner_uid"] is not None and row["owner_uid"] == netease.current_uid()


# ---------- 同步 ----------
def import_netease_playlist(remote_id, name=None, cover=""):
    tracks = netease.playlist_tracks(remote_id)
    if name is None:
        info, _ = netease.playlists()
        for p in info.get("playlists", []):
            if p["id"] == remote_id:
                name, cover = p["name"], p.get("cover", "")
    owner = netease.current_uid()
    c = config.db()
    try:
        row = c.execute(
            "SELECT id,owner_uid FROM playlists WHERE source='netease' AND remote_id=?",
            (remote_id,),
        ).fetchone()
        if row:
            pid = row["id"]
            if owner and row["owner_uid"] != owner:
                c.execute("UPDATE playlists SET owner_uid=? WHERE id=?", (owner, pid))
        else:
            cur = c.execute(
                """INSERT INTO playlists(name,created_at,source,remote_id,cover,owner_uid)
                   VALUES(?,?,?,?,?,?)""",
                (name or "网易云歌单", int(__import__("time").time()), "netease",
                 remote_id, cover, owner),
            )
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
            c.execute(
                """INSERT INTO tracks(playlist_id,song_id,name,artist,album,pos)
                   VALUES(?,?,?,?,?,?)""",
                (pid, t["song_id"], t["name"], t["artist"], t["album"], pos),
            )
            added += 1
        c.commit()
    finally:
        c.close()
    return {"ok": True, "playlist_id": pid, "name": name,
            "added": added, "total": len(tracks)}


def sync_all_worker():
    SYNC.update({"running": True, "done": 0, "total": 0, "current": "",
                 "added": 0, "errors": [], "finished_at": None})
    try:
        info, _ = netease.playlists()
        pls = info.get("playlists", [])
        SYNC["total"] = len(pls)
        for i, pl in enumerate(pls):
            SYNC["current"] = pl["name"]
            try:
                r = import_netease_playlist(pl["id"], pl["name"], pl.get("cover", ""))
                SYNC["added"] += r.get("added", 0)
            except Exception as e:
                SYNC["errors"].append(f'{pl["name"]}: {e!r}')
            SYNC["done"] = i + 1
    finally:
        SYNC.update({"running": False, "current": "",
                     "finished_at": int(__import__("time").time())})


def qr_poll_worker(key):
    import time
    from . import netease as nc
    end = time.time() + 300
    headers = {"User-Agent": nc.UA, "Referer": nc.REFERER,
               "Cookie": "os=pc; appver=2.9.7"}
    while time.time() < end:
        try:
            code, saved = nc.qr_poll(key, headers)
            if code == 802:
                LOGIN.update({"status": "scanned", "message": "已扫码，请在手机上确认"})
            elif code == 803:
                if saved:
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


# ---------- 请求处理 ----------
class Handler(BaseHTTPRequestHandler):
    def _j(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        pw = config.get_setting("ui_password")
        if not pw:
            return True
        import base64
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            _, _, given = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return False
        return given == pw

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        g = lambda k, d="": qs.get(k, [d])[0]
        p = u.path
        pl = player_mod.player

        try:
            # 静态页
            if p == "/":
                idx = os.path.join(config.WEB_DIR, "index.html")
                if not os.path.exists(idx):
                    return self._j({"error": "web/index.html 缺失"}, 500)
                return self._send_file(idx, "text/html; charset=utf-8")

            if p.startswith("/media/"):
                return self._media(u, qs)

            if not self._authed():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="xiaoai"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            ip = g("ip") or pl.active_ip()

            # --- 播放控制 ---
            if p == "/now":
                return self._j(pl.now())
            if p == "/stop":
                return self._j(pl.stop())
            if p == "/pause":
                return self._j(pl.pause())
            if p == "/resume":
                return self._j(pl.resume())
            if p == "/next":
                return self._j(pl.advance(1))
            if p == "/prev":
                return self._j(pl.advance(-1))
            if p == "/volume":
                return self._j({"ok": True, "volume": dlna.set_volume(ip, int(g("level", "30")))})
            if p == "/mode":
                return self._j({"ok": True, "mode": pl.set_mode(g("set") or pl.mode)})

            # --- 音质 ---
            if p == "/quality":
                if "set" in qs:
                    q = g("set")
                    if q in config.QUALITY_LEVELS:
                        config.set_setting("quality", q)
                    else:
                        return self._j({"ok": False, "error": "未知音质档位"}, 400)
                cur = config.get_setting("quality", config.DEFAULT_QUALITY)
                return self._j({"ok": True, "quality": cur,
                                "levels": {k: v["label"] for k, v in config.QUALITY_LEVELS.items()}})

            # --- 搜索/点歌 ---
            if p == "/search":
                return self._j({"songs": netease.search(g("q"))})
            if p == "/play":
                q = g("q").strip()
                if not q:
                    return self._j({"error": "need q"}, 400)
                songs = netease.search(q)
                if not songs:
                    return self._j({"error": "not found"}, 404)
                s = songs[min(int(g("i", "0")), len(songs) - 1)]
                r = pl.play_track(s, ip)
                if not r.get("ok"):
                    return self._j({**r, "error": "VIP 曲目需要有效 cookie",
                                    "song": s}, 409)
                pl.active = False
                return self._j(r)

            # --- 账号 ---
            if p == "/account":
                return self._j(netease.account())
            if p == "/account/qr":
                r = netease.qr_start()
                if "error" in r:
                    LOGIN.update({"status": "error", "message": r["error"]})
                    return self._j(r, 500)
                LOGIN.update({"status": "waiting", "key": r["key"], "png": r["png_path"],
                              "started": int(__import__("time").time()),
                              "message": "等待扫码"})
                threading.Thread(target=qr_poll_worker, args=(r["key"],), daemon=True).start()
                return self._j(dict(LOGIN))
            if p == "/account/qr/status":
                return self._j(dict(LOGIN))
            if p == "/qr.png":
                return self._send_file(LOGIN["png"], "image/png") if os.path.exists(LOGIN["png"]) \
                    else self._j({"error": "no qr"}, 404)
            if p == "/account/logout":
                netease.save_cookie("")
                LOGIN.update({"status": "idle", "message": "已退出"})
                return self._j({"ok": True, "logged_in": False})

            # --- 网易云歌单 ---
            if p == "/netease/playlists":
                data, code = netease.playlists()
                return self._j(data, code)
            if p == "/netease/import":
                return self._j(import_netease_playlist(int(g("id", "0")), g("name") or None))
            if p == "/netease/sync_all":
                if not SYNC["running"]:
                    threading.Thread(target=sync_all_worker, daemon=True).start()
                return self._j({"ok": True, "started": True})
            if p == "/netease/sync_all/status":
                return self._j(dict(SYNC))
            if p == "/netease/sync":
                pid = int(g("id", "0"))
                c = config.db()
                try:
                    row = c.execute("SELECT remote_id FROM playlists WHERE id=?", (pid,)).fetchone()
                finally:
                    c.close()
                if not row or not row["remote_id"]:
                    return self._j({"ok": False, "error": "该歌单不是从网易云导入的"}, 400)
                return self._j(import_netease_playlist(row["remote_id"]))

            # --- 歌单 ---
            if p == "/playlists":
                uid = netease.current_uid()
                c = config.db()
                try:
                    rows = [dict(r) for r in c.execute(
                        """SELECT p.id,p.name,p.source,p.remote_id,p.owner_uid,COUNT(t.id) AS count
                           FROM playlists p LEFT JOIN tracks t ON t.playlist_id=p.id
                           WHERE p.source='local' OR (p.source='netease' AND p.owner_uid IS ?)
                           GROUP BY p.id ORDER BY p.id""", (uid,)).fetchall()]
                finally:
                    c.close()
                return self._j({"playlists": rows})
            if p == "/playlist/create":
                c = config.db()
                try:
                    cur = c.execute("INSERT INTO playlists(name,created_at) VALUES(?,?)",
                                    (g("name") or "新歌单", int(__import__("time").time())))
                    c.commit()
                    return self._j({"ok": True, "id": cur.lastrowid})
                finally:
                    c.close()
            if p == "/playlist":
                pid = int(g("id", "0"))
                if not playlist_allowed(pid):
                    return self._j({"ok": False, "error": "该歌单属于其他账号或账号已退出"}, 403)
                return self._j({"tracks": pl.tracks(pid)})
            if p == "/playlist/add":
                return self._playlist_add(g)
            if p == "/playlist/remove":
                c = config.db()
                try:
                    c.execute("DELETE FROM tracks WHERE id=? AND playlist_id=?",
                              (int(g("track", "0")), int(g("id", "0"))))
                    c.commit()
                finally:
                    c.close()
                return self._j({"ok": True})
            if p == "/playlist/delete":
                c = config.db()
                try:
                    c.execute("DELETE FROM tracks WHERE playlist_id=?", (int(g("id", "0")),))
                    c.execute("DELETE FROM playlists WHERE id=?", (int(g("id", "0")),))
                    c.commit()
                finally:
                    c.close()
                return self._j({"ok": True})
            if p == "/playlist/play":
                pid = int(g("id", "0"))
                if not playlist_allowed(pid):
                    return self._j({"ok": False, "error": "该歌单属于其他账号或账号已退出"}, 403)
                idx = int(g("i", "0"))
                if g("whole") and pl.mode == "shuffle":
                    import random as _r
                    total = len(pl.tracks(pid))
                    if total > 1:
                        idx = _r.randrange(total)
                return self._j(pl.play_index(pid, idx, ip))

            # --- 队列 ---
            if p == "/queue":
                if pl.playlist_id is None:
                    return self._j({"playlist": None, "mode": pl.mode})
                c = config.db()
                try:
                    nm = c.execute("SELECT name FROM playlists WHERE id=?",
                                   (pl.playlist_id,)).fetchone()
                finally:
                    c.close()
                tracks = pl.tracks()
                cur = tracks[pl.index] if pl.index is not None and pl.index < len(tracks) else {}
                return self._j({"playlist": nm["name"] if nm else "",
                                "index": pl.index or 0, "total": len(tracks),
                                "mode": pl.mode,
                                "track": {"name": cur.get("name"), "artist": cur.get("artist")}})

            # --- 免打扰 ---
            if p == "/quiet":
                if "set" in qs:
                    config.set_setting("quiet_hours", g("set"))
                elif "off" in qs:
                    config.set_setting("quiet_hours", "")
                return self._j({"ok": True,
                                "quiet_hours": config.get_setting("quiet_hours",
                                                                  config.QUIET_DEFAULT),
                                "active": pl.in_quiet_hours()})

            # --- 本地曲库 ---
            if p == "/local/scan":
                return self._j({"ok": True, "count": library.scan()})
            if p == "/local/tracks":
                return self._j({"tracks": library.list_tracks(g("q"))})
            if p == "/local/play":
                tid = int(g("id", "0"))
                c = config.db()
                try:
                    tr = c.execute("SELECT * FROM local_tracks WHERE id=?", (tid,)).fetchone()
                finally:
                    c.close()
                if not tr:
                    return self._j({"ok": False, "error": "not found"}, 404)
                return self._j(pl.play_track({"local_id": tid}, ip))
            if p == "/local/add":
                return self._local_add(g)

            # --- 下载队列 ---
            if p == "/download/playlist":
                pid = int(g("id", "0"))
                if not playlist_allowed(pid):
                    return self._j({"ok": False, "error": "歌单不可用"}, 403)
                n = downloader.add_playlist(pid)
                downloader.kick()
                return self._j({"ok": True, "queued": n})
            if p == "/download/song":
                jid = downloader.add_job({
                    "song_id": int(g("song_id", "0")),
                    "name": g("name"), "artist": g("artist"), "album": g("album"),
                }, g("quality") or None)
                downloader.kick()
                return self._j({"ok": True, "job": jid})
            if p == "/download/jobs":
                return self._j({"jobs": downloader.jobs(int(g("limit", "100"))),
                                "stats": downloader.stats()})
            if p == "/download/retry":
                downloader.retry_failed(int(g("id", "0")) or None)
                downloader.kick()
                return self._j({"ok": True})
            if p == "/download/cancel":
                downloader.cancel(int(g("id", "0")))
                return self._j({"ok": True})
            if p == "/download/rescan":
                return self._j({"ok": True, "count": library.scan()})

            # --- 设备 ---
            if p == "/devices":
                c = config.db()
                try:
                    rows = [dict(r) for r in c.execute(
                        "SELECT * FROM devices ORDER BY last_seen DESC").fetchall()]
                finally:
                    c.close()
                act = pl.active_ip()
                for r in rows:
                    r["active"] = (r["ip"] == act)
                return self._j({"devices": rows, "active": act})
            if p == "/devices/select":
                config.set_setting("active_device", g("ip"))
                pl.device = g("ip")
                return self._j({"ok": True, "active": g("ip")})
            if p == "/devices/scan":
                found = dlna.ssdp_scan()
                c = config.db()
                try:
                    for d in found:
                        c.execute(
                            """INSERT INTO devices(ip,name,model,udn,last_seen,created_at)
                               VALUES(?,?,?,?,?,?)
                               ON CONFLICT(ip) DO UPDATE SET name=excluded.name,
                               model=excluded.model, udn=excluded.udn, last_seen=excluded.last_seen""",
                            (d["ip"], d["name"], d.get("model", ""), d.get("udn", ""),
                             int(__import__("time").time()), int(__import__("time").time())),
                        )
                    c.commit()
                finally:
                    c.close()
                return self._j({"ok": True, "found": found})

            return self._j({"error": "unknown"}, 404)
        except Exception as e:
            return self._j({"error": repr(e)}, 500)

    # ---------- 子处理 ----------
    def _playlist_add(self, g):
        pid, sid = int(g("id", "0")), int(g("song_id", "0"))
        c = config.db()
        try:
            dup = c.execute(
                "SELECT id FROM tracks WHERE playlist_id=? AND song_id=? AND COALESCE(local_id,0)=0",
                (pid, sid)).fetchone()
            if dup:
                return self._j({"ok": True, "added": False, "message": "已在歌单中"})
            pos = c.execute("SELECT COALESCE(MAX(pos),0)+1 FROM tracks WHERE playlist_id=?",
                            (pid,)).fetchone()[0]
            c.execute(
                """INSERT INTO tracks(playlist_id,song_id,name,artist,album,pos)
                   VALUES(?,?,?,?,?,?)""",
                (pid, sid, g("name"), g("artist"), g("album"), pos))
            c.commit()
            return self._j({"ok": True, "added": True})
        finally:
            c.close()

    def _local_add(self, g):
        pid, tid = int(g("id", "0")), int(g("track", "0"))
        c = config.db()
        try:
            tr = c.execute("SELECT * FROM local_tracks WHERE id=?", (tid,)).fetchone()
            if not tr:
                return self._j({"ok": False, "error": "not found"}, 404)
            dup = c.execute("SELECT id FROM tracks WHERE playlist_id=? AND local_id=?",
                            (pid, tid)).fetchone()
            if dup:
                return self._j({"ok": True, "added": False, "message": "已在歌单中"})
            pos = c.execute("SELECT COALESCE(MAX(pos),0)+1 FROM tracks WHERE playlist_id=?",
                            (pid,)).fetchone()[0]
            c.execute(
                """INSERT INTO tracks(playlist_id,song_id,name,artist,album,pos,local_id)
                   VALUES(?,?,?,?,?,?,?)""",
                (pid, 0, tr["title"], tr["artist"], tr["album"], pos, tid))
            c.commit()
            return self._j({"ok": True, "added": True})
        finally:
            c.close()

    def _media(self, u, qs):
        tid = int(u.path.split("/")[-1])
        want = media_token(tid)
        if want and (qs.get("t") or [""])[0] != want:
            return self._j({"error": "forbidden"}, 403)
        c = config.db()
        try:
            tr = c.execute("SELECT * FROM local_tracks WHERE id=?", (tid,)).fetchone()
        finally:
            c.close()
        if not tr:
            return self._j({"error": "not found"}, 404)
        return library.stream_file(self, library.media_path(dict(tr)))

    def _send_file(self, path, mime):
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass
