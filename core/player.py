"""播放调度：当前设备、队列、播放模式、自动续播、暂停恢复。

这是唯一的播放状态持有者。所有「播哪个、下一首是谁」的判断都在这里。
"""
import datetime as _dt
import random
import threading
import time

from . import config, dlna, netease


class Player:
    def __init__(self):
        self.playlist_id = None
        self.index = None
        self.device = None
        self.mode = config.get_setting("play_mode", "sequential") or "sequential"
        self.history = []
        self.last_state = None
        self.last_pos = 0
        self.last_playing_at = 0
        self.manual_stop = False
        self.paused = None
        self.active = False
        self._lock = threading.Lock()

    # ---------- 设备 ----------
    def active_ip(self):
        if self.device:
            return self.device
        c = config.db()
        try:
            row = c.execute("SELECT ip FROM devices ORDER BY last_seen DESC LIMIT 1").fetchone()
        finally:
            c.close()
        return (config.get_setting("active_device")
                or (row["ip"] if row else config.DEFAULT_SPEAKER_IP))

    # ---------- 队列 ----------
    def tracks(self, pid=None):
        pid = pid if pid is not None else self.playlist_id
        if pid is None:
            return []
        c = config.db()
        try:
            rows = c.execute(
                "SELECT * FROM tracks WHERE playlist_id=? ORDER BY pos,id", (pid,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def set_mode(self, mode):
        if mode in config.PLAY_MODES:
            self.mode = mode
            config.set_setting("play_mode", mode)
        return self.mode

    # ---------- 播放 ----------
    def play_track(self, tr, ip=None):
        """播放单个曲目 dict（需含 name/artist，二选一：local_id 或 song_id）"""
        ip = ip or self.active_ip()
        if tr.get("local_id"):
            from . import library
            c = config.db()
            try:
                loc = c.execute("SELECT * FROM local_tracks WHERE id=?",
                                (tr["local_id"],)).fetchone()
            finally:
                c.close()
            if not loc:
                return {"ok": False, "error": "本地文件不存在"}
            loc = dict(loc)
            library.media_path(loc)
            state = dlna.play_url(ip, library.media_url(loc), loc["title"],
                                  loc["artist"] or "", mime="audio/mpeg")
            return {"ok": True, "song": {"name": loc["title"], "artist": loc["artist"]},
                    "state": state, "device": ip, "source": "local"}

        quality = config.get_setting("quality", config.DEFAULT_QUALITY)
        url, code, br, ext = netease.song_url(tr["song_id"], quality)
        if not url:
            return {"ok": False, "error": "no url", "code": code, "track": tr.get("name")}
        mime = "audio/flac" if ext == "flac" else "audio/mpeg"
        state = dlna.play_url(ip, url, tr.get("name", ""), tr.get("artist", ""), mime=mime)
        return {"ok": True, "song": {"name": tr.get("name"), "artist": tr.get("artist")},
                "state": state, "device": ip, "source": "netease", "quality": quality}

    def play_index(self, pid, idx, ip=None):
        tracks = self.tracks(pid)
        if not tracks:
            return {"ok": False, "error": "empty playlist"}
        idx = max(0, min(idx, len(tracks) - 1))
        ip = ip or self.active_ip()
        r = self.play_track(tracks[idx], ip)
        if r.get("ok"):
            if self.index is not None and self.index != idx:
                self.history.append(self.index)
                self.history = self.history[-50:]
            self.playlist_id, self.index, self.device = pid, idx, ip
            self.last_state = None
            self.last_pos = 0
            self.last_playing_at = time.time()
            self.manual_stop = False
            self.active = True
        return r

    def play_url_direct(self, ip, url, title, artist, mime="audio/mpeg"):
        """直接把一条地址推给音箱（外部音频、临时播放）"""
        state = dlna.play_url(ip, url, title, artist, mime=mime)
        return {"ok": True, "state": state, "device": ip}

    # ---------- 推进 ----------
    def advance(self, delta=1, natural=False):
        if self.playlist_id is None or self.index is None:
            return {"ok": False, "error": "no queue"}
        tracks = self.tracks()
        if not tracks:
            return {"ok": False, "error": "empty playlist"}
        n = len(tracks)

        if natural and self.mode == "single":
            return self.play_index(self.playlist_id, self.index)

        if self.mode == "shuffle":
            if delta < 0 and self.history:
                return self.play_index(self.playlist_id, self.history[-1])
            if n == 1:
                return self.play_index(self.playlist_id, 0)
            nxt = self.index
            while nxt == self.index:
                nxt = random.randrange(n)
            return self.play_index(self.playlist_id, nxt)

        nxt = self.index + delta
        if nxt >= n:
            if self.mode == "loop":
                nxt = 0
            else:
                self.active = False
                return {"ok": False, "error": "end of playlist"}
        if nxt < 0:
            nxt = 0 if self.mode != "loop" else n - 1
        return self.play_index(self.playlist_id, nxt)

    # ---------- 静音时段 ----------
    @staticmethod
    def in_quiet_hours():
        rng = config.get_setting("quiet_hours", config.QUIET_DEFAULT) or ""
        if not rng or "-" not in rng:
            return False
        try:
            a, b = rng.split("-", 1)
            sh, sm = [int(x) for x in a.split(":")]
            eh, em = [int(x) for x in b.split(":")]
        except Exception:
            return False
        now = _dt.datetime.now()
        cur, start, end = now.hour * 60 + now.minute, sh * 60 + sm, eh * 60 + em
        if start == end:
            return False
        return (start <= cur < end) if start < end else (cur >= start or cur < end)

    # ---------- 自动续播循环 ----------
    def run_auto_advance(self):
        while True:
            try:
                if self.in_quiet_hours():
                    self.last_state = None
                    time.sleep(30)
                    continue
                if self.playlist_id is not None and not self.manual_stop and self.active:
                    ip = self.active_ip()
                    np = dlna.now_playing(ip)
                    st = np["state"]
                    dur = dlna.secs(np.get("duration") or "")
                    pos = dlna.secs(np.get("position") or "")
                    if st == "PLAYING":
                        self.last_playing_at = time.time()
                    fresh = (time.time() - self.last_playing_at) <= config.AUTO_ADVANCE_FRESH_SECONDS

                    ended = False
                    if st == "STOPPED" and self.last_state in ("PLAYING", "TRANSITIONING", "PAUSED_PLAYBACK"):
                        ended = True
                    elif st == "PLAYING":
                        if dur > 0 and pos >= max(dur - 2, 1):
                            ended = True
                        elif dur == 0 and self.last_pos >= 3 and pos <= 1:
                            ended = True

                    if ended and self.last_state != "ADVANCING" and fresh:
                        self.last_state = "ADVANCING"
                        self.advance(1, natural=True)
                    elif st != "ADVANCING":
                        self.last_state = st
                        self.last_pos = pos
            except Exception:
                pass
            time.sleep(config.AUTO_ADVANCE_INTERVAL)

    # ---------- 暂停/恢复（设备不支持 Pause 时降级） ----------
    def pause(self):
        ip = self.active_ip()
        self.manual_stop = True
        np = dlna.now_playing(ip)
        if np.get("url"):
            self.paused = {
                "ip": ip, "uri": np["url"], "title": np.get("title", ""),
                "artist": np.get("artist", ""), "pos": dlna.secs(np.get("position", "")),
            }
        dlna.pause(ip)
        time.sleep(1.2)
        st = dlna.transport(ip)
        return {"ok": True, "state": st,
                "fallback": self.paused is not None and st != "PAUSED_PLAYBACK"}

    def resume(self):
        ip = self.active_ip()
        self.manual_stop = False
        st = dlna.transport(ip)
        if st == "PAUSED_PLAYBACK":
            dlna.resume(ip)
            self.paused = None
            return {"ok": True, "state": dlna.transport(ip), "resumed": "pause"}
        info = self.paused
        if info and info.get("uri"):
            dlna.play_url(info["ip"], info["uri"], info.get("title", ""), info.get("artist", ""))
            if info.get("pos", 0) > 1:
                time.sleep(1.5)
                dlna.seek(info["ip"], info["pos"])
                time.sleep(0.5)
            self.paused = None
            return {"ok": True, "state": dlna.transport(info["ip"]),
                    "resumed": "replay", "position": info.get("pos", 0)}
        return {"ok": False, "error": "没有可恢复的播放"}

    def stop(self):
        ip = self.active_ip()
        self.manual_stop = True
        self.paused = None
        self.active = False
        dlna.stop(ip)
        return {"ok": True, "state": dlna.transport(ip)}

    def now(self):
        return dlna.now_playing(self.active_ip())


player = Player()
