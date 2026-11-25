"""播放调度：队列、模式、设备、暂停恢复，以及 watchdog 状态机。

这是整个项目**唯一的播放状态持有者**。所有「播哪首、下一首是谁、是否该续播」的判断都在这里。

核心难点：DLNA 渲染器只有 ``PLAYING / PAUSED_PLAYBACK / TRANSITIONING / STOPPED`` 状态，
不区分「一首歌自然播完」和「用户对小爱说暂停/停止」。v2 只要看到 STOPPED 就播下一首，
于是用户语音暂停后中枢会"抢回"播放。v3 的判定规则：

自然播完（允许续播）必须有**正证据**
    1. 已知时长，且最后一次播放位置到达 ``duration - END_TOLERANCE``；
    2. 或设备播完后仍报 PLAYING，位置到达末尾 2 秒内；
    3. 或显式开启 ``AUTO_ADVANCE_UNKNOWN_DURATION`` 时，用「位置从 ≥3s 跳到 0」兜底。

外部控制（禁止续播，进入中断态）
    1. ``PLAYING → PAUSED_PLAYBACK``；
    2. ``PLAYING → STOPPED`` 但不满足自然播完的正证据（位置离末尾还远 / 时长未知）；
    3. 先记录候选，**下一次轮询仍为 STOPPED** 才落锤，避免瞬时抖动误判；
    4. 本机刚下发指令后的 ``GRACE_AFTER_COMMAND`` 秒内忽略 STOPPED。

外部中断后：不推进、不抢播，记录 ``interrupted`` 供 UI 展示；用户点"继续"时重新推流并 Seek；
若用户直接对小爱说"继续播放"，下一次轮询看到 PLAYING 就自动清除中断态。
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from . import config, db, dlna, errors, logging_setup, netease, prefs, security
from . import library as library_mod

log = logging_setup.get_logger(__name__)

MODE_SEQUENTIAL = "sequential"
MODE_LOOP = "loop"
MODE_SINGLE = "single"
MODE_SHUFFLE = "shuffle"

#: 本机下发指令后的静默期（秒），期间忽略 STOPPED，避免刚 SetAVTransportURI 就被误判
GRACE_AFTER_COMMAND = 3.0
#: 暂停后等待设备进入 PAUSED_PLAYBACK 的最长时间（秒）
PAUSE_VERIFY_TIMEOUT = 0.8
#: 重推流后等待设备就绪再 Seek 的秒数
REPLAY_SEEK_DELAY = 1.2
#: 历史栈上限
HISTORY_LIMIT = 50

_EXT_MIME = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".ape": "audio/x-ape",
    ".wma": "audio/x-ms-wma",
}


@dataclass
class Poll:
    """一次渲染器状态轮询。"""

    at: float
    state: str
    position: float = 0.0
    duration: float = 0.0
    uri: str = ""
    title: str = ""
    artist: str = ""


def _mime_for(path_or_url: str) -> str:
    try:
        suffix = "." + path_or_url.split("?")[0].rsplit(".", 1)[-1].lower()
    except Exception:  # pragma: no cover - 极端输入
        return "audio/mpeg"
    return _EXT_MIME.get(suffix, "audio/mpeg")


class Player:
    """播放状态机（线程安全：所有状态变更走 ``self._lock``）。"""

    def __init__(
        self,
        *,
        renderer_factory: Optional[Callable[..., Any]] = None,
        netease_api: Any = None,
        library_api: Any = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._renderer_factory = renderer_factory or dlna.Renderer
        self._netease = netease_api if netease_api is not None else netease
        self._library = library_api if library_api is not None else library_mod
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep

        self.playlist_id: Optional[int] = None
        self.index: Optional[int] = None
        self.mode: str = prefs.play_mode()
        self.device: Optional[str] = None
        self.history: List[int] = []

        self._last: Optional[Poll] = None
        self._position_cache: tuple = (0.0, None)
        self._last_playing_pos: float = 0.0
        self._last_playing_duration: float = 0.0
        self._last_playing_at: float = 0.0
        self._stop_candidate: Optional[Poll] = None
        self._interrupted: Optional[Dict[str, Any]] = None
        self._paused: Optional[Dict[str, Any]] = None
        self._command_grace_until: float = 0.0
        self._manual_stop: bool = False
        self._active: bool = False

        self._lock = threading.RLock()
        self._renderers: Dict[str, Any] = {}
        self._watchdog: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # 设备
    # ------------------------------------------------------------------
    def active_ip(self) -> str:
        if self.device:
            return self.device
        stored = prefs.active_device()
        if stored:
            return stored
        row = db.query_one("SELECT ip FROM devices ORDER BY last_seen DESC LIMIT 1")
        return row["ip"] if row else config.get().speaker_ip

    def set_device(self, ip: str) -> str:
        ip = (ip or "").strip()
        if not ip:
            raise errors.BadRequest("设备 IP 不能为空")
        prefs.set_active_device(ip)
        with self._lock:
            self.device = ip
            self._last = None
            self._stop_candidate = None
            self._interrupted = None
            self._paused = None
        return ip

    def renderer(self, ip: Optional[str] = None) -> Any:
        """按 IP 取（并缓存）渲染器实例，控制 URL 优先用设备发现结果。"""
        target = ip or self.active_ip()
        with self._lock:
            cached = self._renderers.get(target)
            if cached is not None:
                return cached
        kwargs: Dict[str, str] = {}
        row = db.query_one(
            "SELECT avt_control, rc_control FROM devices WHERE ip=?", (target,)
        )
        if row:
            kwargs["avt_control"] = row["avt_control"] or ""
            kwargs["rc_control"] = row["rc_control"] or ""
        instance = self._renderer_factory(target, **kwargs)
        with self._lock:
            self._renderers[target] = instance
        return instance

    def reset_renderers(self) -> None:
        with self._lock:
            self._renderers.clear()

    # ------------------------------------------------------------------
    # 队列
    # ------------------------------------------------------------------
    def tracks(self, pid: Optional[int] = None) -> List[Dict[str, Any]]:
        target = self.playlist_id if pid is None else pid
        if target is None:
            return []
        rows = db.query(
            "SELECT * FROM tracks WHERE playlist_id=? ORDER BY pos, id", (int(target),)
        )
        return [dict(row) for row in rows]

    def playlist_name(self, pid: Optional[int] = None) -> str:
        target = self.playlist_id if pid is None else pid
        if target is None:
            return ""
        row = db.query_one("SELECT name FROM playlists WHERE id=?", (int(target),))
        return row["name"] if row else ""

    def set_mode(self, mode: str) -> str:
        if mode not in config.PLAY_MODES:
            raise errors.BadRequest(f"未知播放模式: {mode}")
        prefs.set_play_mode(mode)
        with self._lock:
            self.mode = mode
        return mode

    def clear_queue(self, playlist_id: Optional[int] = None) -> None:
        """歌单被删除时清空队列（不影响音箱当前正在放的内容）。"""
        with self._lock:
            if playlist_id is None or self.playlist_id == playlist_id:
                self.playlist_id = None
                self.index = None
                self._active = False
                self.history.clear()

    def ping(self) -> bool:
        """设备是否可达（/health 使用）。"""
        try:
            return bool(self.renderer().ping())
        except errors.HubError:
            return False

    def _history_push(self, index: Optional[int]) -> None:
        if index is None:
            return
        if not self.history or self.history[-1] != index:
            self.history.append(index)
            del self.history[:-HISTORY_LIMIT]

    # ------------------------------------------------------------------
    # 播放
    # ------------------------------------------------------------------
    def play_track(
        self,
        track: Dict[str, Any],
        ip: Optional[str] = None,
        *,
        playlist_id: Optional[int] = None,
        index: Optional[int] = None,
        record_history: bool = True,
    ) -> Dict[str, Any]:
        """播放单个曲目 dict（``local_id`` 或 ``song_id`` 二选一）。"""
        target_ip = ip or self.active_ip()
        renderer = self.renderer(target_ip)

        local = self._resolve_local(track)
        if local is not None:
            try:
                path = self._library.media_path(local)
            except errors.HubError:
                raise
            token = security.media_token(local["id"], prefs.ui_password())
            url = self._library.media_url(local, token)
            mime = _mime_for(path)
            try:
                state = renderer.play_url(
                    url, local.get("title") or "", local.get("artist") or "", mime
                )
            except errors.HubError as exc:
                return {"ok": False, "error": str(exc), "code": exc.code}
            self._after_play(
                playlist_id, index, target_ip,
                title=local.get("title") or "", artist=local.get("artist") or "",
                source="local", song_id=local.get("song_key"), local_id=local.get("id"),
                record_history=record_history,
            )
            return {
                "ok": True,
                "song": {"name": local.get("title"), "artist": local.get("artist")},
                "state": state,
                "device": target_ip,
                "source": "local",
            }

        song_id = track.get("song_id")
        if not song_id:
            return {"ok": False, "error": "缺少 song_id 或 local_id"}
        try:
            result = self._netease.song_url_auto(int(song_id))
        except errors.HubError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        if not getattr(result, "ok", False) or not getattr(result, "url", None):
            return {
                "ok": False,
                "error": "no url",
                "code": getattr(result, "code", None),
                "track": track.get("name"),
            }
        mime = "audio/flac" if getattr(result, "ext", "mp3") == "flac" else "audio/mpeg"
        try:
            state = renderer.play_url(
                result.url, track.get("name") or "", track.get("artist") or "", mime
            )
        except errors.HubError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        self._after_play(
            playlist_id, index, target_ip,
            title=track.get("name") or "", artist=track.get("artist") or "",
            source="netease", song_id=song_id, local_id=None,
            record_history=record_history,
        )
        return {
            "ok": True,
            "song": {"name": track.get("name"), "artist": track.get("artist")},
            "state": state,
            "device": target_ip,
            "source": "netease",
            "quality": getattr(result, "quality", None),
        }

    def _resolve_local(self, track: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """本地优先：先按 local_id，再按 song_id 绑定，最后按命名约定兜底。"""
        local_id = track.get("local_id")
        if local_id:
            row = db.query_one("SELECT * FROM local_tracks WHERE id=?", (int(local_id),))
            return dict(row) if row else None
        song_id = track.get("song_id")
        if not song_id:
            return None
        try:
            found = self._library.local_for_song(
                int(song_id), track.get("name"), track.get("artist")
            )
        except Exception as exc:  # 曲库查询失败不应阻断在线播放
            log.warning("本地曲库查询失败: %r", exc)
            return None
        return found

    def _after_play(
        self,
        playlist_id: Optional[int],
        index: Optional[int],
        ip: str,
        *,
        title: str,
        artist: str,
        source: str,
        song_id: Optional[int],
        local_id: Optional[int],
        record_history: bool = True,
    ) -> None:
        now = self._clock()
        with self._lock:
            if playlist_id is not None and index is not None:
                if (
                    record_history
                    and self.index is not None
                    and self.index != index
                    and self.playlist_id == playlist_id
                ):
                    self._history_push(self.index)
                self.playlist_id, self.index = int(playlist_id), int(index)
                self._active = True
            else:
                self.playlist_id, self.index = None, None
                self._active = False
            self.device = ip
            self._last = None
            self._last_playing_pos = 0.0
            self._last_playing_duration = 0.0
            self._last_playing_at = now
            self._stop_candidate = None
            self._interrupted = None
            self._paused = None
            self._manual_stop = False
            self._command_grace_until = now + GRACE_AFTER_COMMAND
        self._record_history(song_id, local_id, title, artist, source)

    def _record_history(
        self,
        song_id: Optional[int],
        local_id: Optional[int],
        title: str,
        artist: str,
        source: str,
    ) -> None:
        try:
            db.execute(
                """INSERT INTO play_history(song_id,local_id,name,artist,source,played_at)
                   VALUES(?,?,?,?,?,?)""",
                (song_id, local_id, title, artist, source, int(time.time())),
            )
        except Exception as exc:  # 历史记录失败不影响播放
            log.debug("写入播放历史失败: %r", exc)

    def play_index(self, pid: int, idx: int, ip: Optional[str] = None) -> Dict[str, Any]:
        tracks = self.tracks(pid)
        if not tracks:
            return {"ok": False, "error": "empty playlist"}
        idx = max(0, min(int(idx), len(tracks) - 1))
        return self.play_track(tracks[idx], ip, playlist_id=int(pid), index=idx)

    def play_url(
        self,
        url: str,
        title: str = "",
        artist: str = "",
        mime: str = "audio/mpeg",
        ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把任意音频直链推给音箱（不挂队列）。"""
        target_ip = ip or self.active_ip()
        try:
            state = self.renderer(target_ip).play_url(url, title, artist, mime)
        except errors.HubError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        self._after_play(None, None, target_ip, title=title, artist=artist,
                         source="external", song_id=None, local_id=None)
        return {"ok": True, "state": state, "device": target_ip, "url": url}

    # ------------------------------------------------------------------
    # 推进
    # ------------------------------------------------------------------
    def advance(self, delta: int = 1, *, natural: bool = False) -> Dict[str, Any]:
        with self._lock:
            pid, index, mode = self.playlist_id, self.index, self.mode
        if pid is None or index is None:
            return {"ok": False, "error": "no queue"}
        tracks = self.tracks(pid)
        total = len(tracks)
        if not tracks:
            return {"ok": False, "error": "empty playlist"}

        if natural and mode == MODE_SINGLE:
            return self.play_track(tracks[index], playlist_id=pid, index=index)

        if mode == MODE_SHUFFLE:
            if delta < 0:
                with self._lock:
                    target = self.history.pop() if self.history else None
                if target is None:
                    return {"ok": False, "error": "没有上一首"}
                tracks_target = self.tracks(pid)
                target = max(0, min(int(target), len(tracks_target) - 1))
                return self.play_track(
                    tracks_target[target], playlist_id=pid, index=target,
                    record_history=False,
                )
            if total == 1:
                return self.play_track(tracks[0], playlist_id=pid, index=0)
            candidate = index
            while candidate == index:
                candidate = random.randrange(total)
            return self.play_track(tracks[candidate], playlist_id=pid, index=candidate)

        nxt = index + delta
        if nxt >= total:
            if mode == MODE_LOOP:
                nxt = 0
            else:
                with self._lock:
                    self._active = False
                log.info("队列播放结束（playlist=%s, index=%s）", pid, index)
                return {"ok": False, "error": "end of playlist"}
        if nxt < 0:
            nxt = total - 1 if mode == MODE_LOOP else 0
        return self.play_track(tracks[nxt], playlist_id=pid, index=nxt)

    # ------------------------------------------------------------------
    # 暂停 / 恢复 / 停止 / Seek / 音量
    # ------------------------------------------------------------------
    def pause(self) -> Dict[str, Any]:
        ip = self.active_ip()
        renderer = self.renderer(ip)
        with self._lock:
            self._manual_stop = True
        info = self._safe_position(renderer)
        paused_info = None
        if info is not None:
            paused_info = {
                "ip": ip, "uri": info.uri, "title": info.title,
                "artist": info.artist, "position": info.position,
            }
        try:
            renderer.pause()
        except errors.HubError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}

        state = self._safe_state(renderer)
        deadline = self._clock() + PAUSE_VERIFY_TIMEOUT
        while state == "PLAYING" and self._clock() < deadline:
            self._sleep(0.1)
            state = self._safe_state(renderer)
        fallback = state != "PAUSED_PLAYBACK"
        if fallback:
            # 设备不支持 Pause：Stop 保证静音，位置已记录，恢复时重推流
            try:
                renderer.stop()
            except errors.HubError as exc:
                log.debug("暂停降级 stop 失败: %s", exc)
        with self._lock:
            # 设备支持 Pause 时保留设备自身状态；不支持时才记录重推流信息
            self._paused = None if not fallback else paused_info
            self._last = None
            self._stop_candidate = None
        position = paused_info["position"] if fallback and paused_info else 0.0
        return {"ok": True, "state": state, "fallback": fallback, "position": position}

    def resume(self) -> Dict[str, Any]:
        ip = self.active_ip()
        renderer = self.renderer(ip)
        with self._lock:
            self._manual_stop = False
            interrupted = dict(self._interrupted) if self._interrupted else None
            paused = dict(self._paused) if self._paused else None
        restore = interrupted or paused
        if restore and restore.get("uri"):
            return self._replay(renderer, restore)

        state = self._safe_state(renderer)
        if state == "PAUSED_PLAYBACK":
            try:
                renderer.resume()
            except errors.HubError as exc:
                return {"ok": False, "error": str(exc), "code": exc.code}
            with self._lock:
                self._paused = None
                self._interrupted = None
                self._last = None
                if self.playlist_id is not None:
                    self._active = True
                self._command_grace_until = self._clock() + GRACE_AFTER_COMMAND
            return {"ok": True, "resumed": "pause", "state": self._safe_state(renderer)}
        if state == "PLAYING":
            return {"ok": True, "resumed": "already", "state": state}
        return {"ok": False, "error": "没有可恢复的播放", "state": state}

    def _replay(self, renderer: Any, info: Dict[str, Any]) -> Dict[str, Any]:
        mime = _mime_for(str(info.get("uri") or ""))
        try:
            renderer.play_url(
                info["uri"], info.get("title", ""), info.get("artist", ""), mime
            )
        except errors.HubError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        position = float(info.get("position") or 0.0)
        if position > 1:
            self._sleep(REPLAY_SEEK_DELAY)
            try:
                renderer.seek(position)
            except errors.HubError as exc:
                log.debug("恢复位置 Seek 失败: %s", exc)
            self._sleep(0.3)
        with self._lock:
            self._interrupted = None
            self._paused = None
            self._last = None
            self._stop_candidate = None
            self._command_grace_until = self._clock() + GRACE_AFTER_COMMAND
            if self.playlist_id is not None:
                self._active = True
        return {
            "ok": True,
            "resumed": "replay",
            "position": position,
            "state": self._safe_state(renderer),
        }

    def stop(self) -> Dict[str, Any]:
        ip = self.active_ip()
        renderer = self.renderer(ip)
        try:
            renderer.stop()
        except errors.HubError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        with self._lock:
            self._manual_stop = True
            self._active = False
            self._paused = None
            self._interrupted = None
            self._last = None
            self._stop_candidate = None
        return {"ok": True, "state": self._safe_state(renderer)}

    def seek(self, seconds: float) -> Dict[str, Any]:
        seconds = max(0.0, float(seconds))
        renderer = self.renderer()
        try:
            ok = renderer.seek(seconds)
        except errors.HubError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        if ok:
            with self._lock:
                self._last = None
                self._command_grace_until = self._clock() + 1.0
        return {"ok": bool(ok), "position": seconds}

    def set_volume(self, level: int) -> Dict[str, Any]:
        try:
            volume = self.renderer().set_volume(int(level))
        except errors.HubError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        return {"ok": True, "volume": volume}

    def get_volume(self) -> Optional[int]:
        try:
            return self.renderer().get_volume()
        except errors.HubError:
            return None

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """``/state`` 的主体：一次调用拿到播放器全部状态。"""
        ip = self.active_ip()
        renderer = self.renderer(ip)
        info = self._cached_position(renderer)
        reachable = info is not None
        with self._lock:
            interrupted = dict(self._interrupted) if self._interrupted else None
            manual_stop = self._manual_stop
            active = self._active
            pid, index, mode = self.playlist_id, self.index, self.mode
            history = list(self.history)
        device = db.query_one("SELECT name, model FROM devices WHERE ip=?", (ip,))
        total = 0
        if pid is not None:
            row = db.query_one(
                "SELECT COUNT(*) AS n FROM tracks WHERE playlist_id=?", (int(pid),)
            )
            total = int(row["n"]) if row else 0
        state = info.state if info else "UNKNOWN"
        return {
            "ok": True,
            "device": {
                "ip": ip,
                "name": (device["name"] if device else "") or "",
                "reachable": reachable,
            },
            "player": {
                "state": state,
                "title": info.title if info else "",
                "artist": info.artist if info else "",
                "position": info.position if info else 0.0,
                "duration": info.duration if info else 0.0,
                "position_text": dlna.fmt_time(info.position if info else 0),
                "duration_text": dlna.fmt_time(info.duration if info else 0),
                "uri": info.uri if info else "",
                "active": active,
                "manual_stop": manual_stop,
                "interrupted": interrupted,
                "queue": {
                    "playlist_id": pid,
                    "playlist_name": self.playlist_name(pid),
                    "index": index if index is not None else 0,
                    "total": total,
                    "mode": mode,
                    "history": history,
                },
            },
        }

    def now(self) -> Dict[str, Any]:
        """``/now``：兼容 v2 字段 + v3 扩展字段。"""
        snapshot = self.snapshot()
        player = snapshot["player"]
        return {
            "ok": True,
            "device": snapshot["device"]["ip"],
            "state": player["state"],
            "title": player["title"],
            "artist": player["artist"],
            "duration": player["duration_text"],
            "position": player["position_text"],
            "duration_seconds": player["duration"],
            "position_seconds": player["position"],
            "url": player["uri"],
            "active": player["active"],
            "manual_stop": player["manual_stop"],
            "interrupted": player["interrupted"],
            "queue": player["queue"],
        }

    def queue(self) -> Dict[str, Any]:
        pid = self.playlist_id
        if pid is None:
            return {"ok": True, "playlist_id": None, "playlist_name": "",
                    "index": 0, "mode": self.mode, "tracks": []}
        tracks = self.tracks(pid)
        return {
            "ok": True,
            "playlist_id": pid,
            "playlist_name": self.playlist_name(pid),
            "index": self.index if self.index is not None else 0,
            "mode": self.mode,
            "tracks": tracks,
        }

    def in_quiet_hours(self) -> bool:
        return prefs.in_quiet_hours()

    # ------------------------------------------------------------------
    # watchdog
    # ------------------------------------------------------------------
    def poll_once(self) -> None:
        """单步轮询：真实逻辑入口，测试里直接调用。"""
        if prefs.in_quiet_hours():
            return
        ip = self.active_ip()
        try:
            info = self.renderer(ip).position_info()
        except errors.HubError as exc:
            log.debug("轮询失败（设备不可达？）: %s", exc)
            return
        now = self._clock()
        current = Poll(
            at=now, state=info.state, position=info.position,
            duration=info.duration, uri=info.uri,
            title=info.title, artist=info.artist,
        )
        with self._lock:
            previous = self._last
            self._last = current
            if current.state == "PLAYING":
                self._last_playing_at = now
                self._last_playing_pos = current.position
                self._last_playing_duration = current.duration

            # 1) 外部中断后的自动恢复：用户对小爱说"继续播放"
            if self._interrupted and current.state == "PLAYING":
                same_uri = not current.uri or current.uri == self._interrupted.get("uri")
                if same_uri:
                    log.info("检测到外部恢复播放，清除中断态")
                    self._interrupted = None
                    self._paused = None
                    if self.playlist_id is not None and not self._manual_stop:
                        self._active = True
                else:
                    log.info("检测到外部接管播放（URI 变化），停止队列监测")
                    self._interrupted = None
                    self._paused = None
                    self._active = False
                self._stop_candidate = None
                return

            # 2) 本机暂停 / 外部中断 / 未挂队列 / 已停止推进：只更新快照
            if (self._manual_stop or self._interrupted
                    or self.playlist_id is None or self.index is None):
                self._stop_candidate = None
                return
            if not self._active:
                self._stop_candidate = None
                return
            if now < self._command_grace_until:
                self._stop_candidate = None
                return

            if current.state == "PLAYING":
                self._stop_candidate = None
                if (
                    current.duration > 0
                    and current.position >= max(current.duration - 2.0, 1.0)
                    and self._fresh(now)
                ):
                    self._advance_natural(current)
                return

            if current.state == "PAUSED_PLAYBACK":
                self._stop_candidate = None
                if previous is not None and previous.state in ("PLAYING", "TRANSITIONING"):
                    self._mark_interrupted("external_pause", previous, current, now)
                return

            if current.state in ("STOPPED", "NO_MEDIA_PRESENT"):
                if previous is not None and previous.state in ("PLAYING", "TRANSITIONING"):
                    # 第一次看到停止：先记候选，下一次轮询仍停止才落锤
                    self._stop_candidate = current
                    return
                if self._stop_candidate is not None:
                    candidate = self._stop_candidate
                    self._stop_candidate = None
                    if self._is_natural_end(candidate):
                        self._advance_natural(current)
                    else:
                        self._mark_interrupted(
                            "external_stop", previous or candidate, candidate, now
                        )
                return

            self._stop_candidate = None

    def _fresh(self, now: float) -> bool:
        return (now - self._last_playing_at) <= config.get().auto_advance_fresh

    def _is_natural_end(self, candidate: Poll) -> bool:
        """是否有"确实播到末尾"的正证据。"""
        duration = max(candidate.duration, self._last_playing_duration)
        position = max(candidate.position, self._last_playing_pos)
        if duration > 0:
            return position >= duration - config.get().end_tolerance
        if config.get().auto_advance_unknown_duration:
            return self._last_playing_pos >= 3 and candidate.position <= 1
        return False

    def _advance_natural(self, current: Poll) -> None:
        with self._lock:
            self._stop_candidate = None
            self._command_grace_until = self._clock() + GRACE_AFTER_COMMAND
        log.info("曲目播完，自动续播（position=%.1fs / duration=%.1fs）",
                 current.position, current.duration)
        result = self.advance(1, natural=True)
        if not result.get("ok"):
            with self._lock:
                self._active = False
            log.info("自动续播结束：%s", result.get("error"))

    def _mark_interrupted(self, reason: str, previous: Poll, current: Poll, now: float) -> None:
        position = max(previous.position, current.position)
        if reason == "external_stop":
            position = max(position, self._last_playing_pos)
        uri = current.uri or previous.uri
        interrupted = {
            "reason": reason,
            "title": previous.title or current.title,
            "artist": previous.artist or current.artist,
            "uri": uri,
            "position": round(position, 1),
            "at": int(time.time()),
        }
        with self._lock:
            self._interrupted = interrupted
            self._paused = dict(interrupted, ip=self.active_ip())
            self._active = False
            self._stop_candidate = None
        log.info(
            "检测到外部控制：%s（%.1fs，%s - %s），不自动续播",
            reason, position, interrupted["title"], interrupted["artist"],
        )

    def _safe_position(self, renderer: Any) -> Optional[Any]:
        try:
            return renderer.position_info()
        except errors.HubError as exc:
            log.debug("读取播放状态失败: %s", exc)
            return None

    def _cached_position(self, renderer: Any, ttl: float = 1.0) -> Optional[Any]:
        """短 TTL 缓存：/state 高频轮询时不至于把设备打爆。"""
        now = self._clock()
        cached_at, cached = self._position_cache
        if cached is not None and (now - cached_at) < ttl:
            return cached
        info = self._safe_position(renderer)
        self._position_cache = (now, info)
        return info

    def _safe_state(self, renderer: Any) -> str:
        try:
            return renderer.transport_state()
        except errors.HubError:
            return "UNKNOWN"

    # ------------------------------------------------------------------
    # 线程生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._watchdog is not None and self._watchdog.is_alive():
                return
            self._stop_event.clear()
            self._watchdog = threading.Thread(
                target=self._watchdog_loop, name="player-watchdog", daemon=True
            )
            self._watchdog.start()

    def _watchdog_loop(self) -> None:
        interval = config.get().auto_advance_interval
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - 守护线程不允许崩溃
                log.exception("watchdog 轮询异常")
            self._stop_event.wait(interval)

    def shutdown(self) -> None:
        self._stop_event.set()
        thread = self._watchdog
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)


_PLAYER: Optional[Player] = None


def get_player() -> Player:
    """懒加载单例（避免 import 期访问数据库，修复 v2 启动崩溃）。"""
    global _PLAYER
    if _PLAYER is None:
        _PLAYER = Player()
    return _PLAYER


def reset_player() -> None:
    global _PLAYER
    if _PLAYER is not None:
        _PLAYER.shutdown()
    _PLAYER = None
