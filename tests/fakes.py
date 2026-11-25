"""测试替身：可脚本化的假 DLNA 渲染器、假网易云、假曲库。

设计目标
--------
- 不依赖网络、不依赖真实音箱，核心状态机可以 100% 确定性测试；
- 通过 ``FakeClock`` 控制时间，watchdog 的宽限期/新鲜度判定完全可复现；
- 假对象用「鸭子类型」模拟真实模块，避免测试与被测模块的 import 顺序耦合。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class FakePositionInfo:
    """与 :class:`xiaoai_hub.dlna.PositionInfo` 同构。"""

    def __init__(
        self,
        state: str = "STOPPED",
        title: str = "",
        artist: str = "",
        duration: float = 0.0,
        position: float = 0.0,
        uri: str = "",
        duration_raw: str = "",
        position_raw: str = "",
    ) -> None:
        self.state = state
        self.title = title
        self.artist = artist
        self.duration = duration
        self.position = position
        self.uri = uri
        self.duration_raw = duration_raw
        self.position_raw = position_raw

    @property
    def playing(self) -> bool:
        return self.state == "PLAYING"

    @property
    def paused(self) -> bool:
        return self.state == "PAUSED_PLAYBACK"

    @property
    def stopped(self) -> bool:
        return self.state in ("STOPPED", "NO_MEDIA_PRESENT")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "title": self.title,
            "artist": self.artist,
            "duration": self.duration,
            "position": self.position,
            "duration_raw": self.duration_raw,
            "position_raw": self.position_raw,
            "uri": self.uri,
        }


class FakeRenderer:
    """实现 :class:`xiaoai_hub.dlna.Renderer` 公开方法的替身。

    测试里用 :meth:`tick` / :meth:`finish` / :meth:`voice_pause` / :meth:`voice_stop`
    模拟真实音箱行为，然后调用 ``player.poll_once()`` 验证状态机。
    """

    def __init__(self, ip: str = "127.0.0.1", *, clock: Optional[FakeClock] = None) -> None:
        self.ip = ip
        self.state = "STOPPED"
        self.position = 0.0
        self.duration = 180.0
        self.uri = ""
        self.title = ""
        self.artist = ""
        self.volume = 30
        self.commands: List[Dict[str, Any]] = []
        self.fail_with: Optional[BaseException] = None
        self.supports_pause = True
        self.supports_seek = True
        self.reachable = True
        self._clock = clock

    # ---------- 记录 ----------
    def _record(self, action: str, **kwargs: Any) -> None:
        entry = {"action": action, "at": self._clock() if self._clock else time.monotonic()}
        entry.update(kwargs)
        self.commands.append(entry)

    def actions(self) -> List[str]:
        return [c["action"] for c in self.commands]

    def last(self, action: Optional[str] = None) -> Optional[Dict[str, Any]]:
        for entry in reversed(self.commands):
            if action is None or entry["action"] == action:
                return entry
        return None

    def clear(self) -> None:
        self.commands.clear()

    # ---------- Renderer 接口 ----------
    def _check(self) -> None:
        if self.fail_with:
            raise self.fail_with

    def ping(self) -> bool:
        return self.reachable and not self.fail_with

    def transport_state(self) -> str:
        self._check()
        return self.state

    def position_info(self) -> FakePositionInfo:
        self._check()
        return FakePositionInfo(
            state=self.state,
            title=self.title,
            artist=self.artist,
            duration=self.duration,
            position=self.position,
            uri=self.uri,
            duration_raw=_fmt(self.duration),
            position_raw=_fmt(self.position),
        )

    def play_url(self, uri: str, title: str = "", artist: str = "",
                 mime: str = "audio/mpeg") -> str:
        self._check()
        self._record("play_url", uri=uri, title=title, artist=artist, mime=mime)
        self.uri, self.title, self.artist = uri, title, artist
        self.state = "PLAYING"
        self.position = 0.0
        return self.state

    def stop(self) -> str:
        self._check()
        self._record("stop")
        self.state = "STOPPED"
        return self.state

    def pause(self) -> str:
        self._check()
        self._record("pause")
        if self.supports_pause:
            self.state = "PAUSED_PLAYBACK"
        else:
            self.state = "STOPPED"
        return self.state

    def resume(self) -> str:
        self._check()
        self._record("resume")
        self.state = "PLAYING"
        return self.state

    def seek(self, seconds: float) -> bool:
        self._check()
        self._record("seek", seconds=seconds)
        if not self.supports_seek:
            return False
        self.position = max(0.0, min(float(seconds), self.duration))
        return True

    def set_volume(self, level: int) -> int:
        self._check()
        self._record("set_volume", level=level)
        self.volume = max(0, min(100, int(level)))
        return self.volume

    def get_volume(self) -> Optional[int]:
        self._check()
        return self.volume

    # ---------- 脚本化音箱行为 ----------
    def tick(self, seconds: float) -> None:
        """播放推进 N 秒（仅 PLAYING 时）。"""
        if self.state == "PLAYING":
            self.position = min(self.duration, self.position + float(seconds))

    def finish(self) -> None:
        """自然播完：位置停在末尾，状态 STOPPED。"""
        self.position = self.duration
        self.state = "STOPPED"

    def voice_pause(self) -> None:
        """小爱语音暂停（设备支持 Pause 时的表现）。"""
        self.state = "PAUSED_PLAYBACK"

    def voice_resume(self) -> None:
        self.state = "PLAYING"

    def voice_stop(self, position: Optional[float] = None, *, clear_uri: bool = False) -> None:
        """小爱语音停止（多数小米音箱表现为 STOPPED，位置可能归零）。"""
        if position is not None:
            self.position = float(position)
        self.state = "STOPPED"
        if clear_uri:
            self.uri = ""

    def takeover(self) -> None:
        """音箱被语音助手切到自有内容：DLNA 会话被顶掉。"""
        self.state = "NO_MEDIA_PRESENT"
        self.uri = ""
        self.title = ""
        self.artist = ""


class FakeSongUrl:
    def __init__(self, url: Optional[str], *, code: int = 200, br: int = 320000,
                 ext: str = "mp3", quality: str = "high", ok: bool = True) -> None:
        self.url = url
        self.code = code
        self.br = br
        self.ext = ext
        self.quality = quality
        self.ok = ok and bool(url)

    def to_dict(self) -> Dict[str, Any]:
        return {"url": self.url, "code": self.code, "br": self.br,
                "ext": self.ext, "quality": self.quality, "ok": self.ok}


class FakeSong:
    def __init__(self, song_id: int, name: str, artist: str = "", album: str = "",
                 fee: Optional[int] = None, duration: int = 0, cover: str = "") -> None:
        self.song_id = song_id
        self.name = name
        self.artist = artist
        self.album = album
        self.fee = fee
        self.duration = duration
        self.cover = cover

    def to_dict(self) -> Dict[str, Any]:
        return {"song_id": self.song_id, "name": self.name, "artist": self.artist,
                "album": self.album, "fee": self.fee, "duration": self.duration,
                "cover": self.cover}


class FakeAccount:
    def __init__(self, logged_in: bool = True, uid: Optional[int] = 42,
                 nickname: str = "测试用户", avatar: str = "", vip: bool = True,
                 reason: str = "") -> None:
        self.logged_in = logged_in
        self.uid = uid
        self.nickname = nickname
        self.avatar = avatar
        self.vip = vip
        self.reason = reason

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.logged_in, "logged_in": self.logged_in, "uid": self.uid,
                "nickname": self.nickname, "avatar": self.avatar, "vip": self.vip,
                "reason": self.reason}


class FakePlaylist:
    def __init__(self, remote_id: int, name: str, count: int = 0, cover: str = "") -> None:
        self.remote_id = remote_id
        self.name = name
        self.count = count
        self.cover = cover

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.remote_id, "name": self.name,
                "count": self.count, "cover": self.cover}


class FakeNetease:
    """网易云模块替身：只实现播放器/接口层用到的函数。"""

    def __init__(self) -> None:
        self.songs = [FakeSong(1001, "晴天", "周杰伦", "叶惠美", fee=0, duration=269),
                      FakeSong(1002, "七里香", "周杰伦", "七里香", fee=1, duration=299)]
        self.song_urls: Dict[int, FakeSongUrl] = {}
        self.default_url = "http://fake.local/audio.mp3"
        self.default_ext = "mp3"
        self.account_info = FakeAccount()
        self.playlists_data = [FakePlaylist(9001, "我喜欢的音乐", 2)]
        self.tracks_data = [FakeSong(1001, "晴天", "周杰伦", "叶惠美"),
                            FakeSong(1002, "七里香", "周杰伦", "七里香")]
        self.calls: List[str] = []
        self.fail_with: Optional[BaseException] = None
        # 扫码登录可编程：qr_codes 依次弹出 801/802/803/800
        self.qr_codes: List[int] = [803]
        self.qr_payload: Dict[str, Any] = {"key": "FAKE-KEY", "png_path": ""}

    # ---------- 网易云接口 ----------
    def search(self, query: str, limit: int = 10, offset: int = 0) -> List[FakeSong]:
        self.calls.append(f"search:{query}")
        if self.fail_with:
            raise self.fail_with
        text = (query or "").strip()
        return [s for s in self.songs if text in s.name or text in s.artist][:limit]

    def song_url(self, song_id: int, quality: Optional[str] = None) -> FakeSongUrl:
        self.calls.append(f"song_url:{song_id}")
        if self.fail_with:
            raise self.fail_with
        if int(song_id) in self.song_urls:
            return self.song_urls[int(song_id)]
        return FakeSongUrl(self.default_url, ext=self.default_ext, quality=quality or "high")

    def song_url_auto(self, song_id: int) -> FakeSongUrl:
        return self.song_url(song_id)

    def account(self, force: bool = False) -> FakeAccount:
        return self.account_info

    def account_cached(self) -> Optional[FakeAccount]:
        return self.account_info if self.account_info.logged_in else None

    def current_uid(self, force: bool = False) -> Optional[int]:
        return self.account_info.uid if self.account_info.logged_in else None

    def playlists(self) -> List[FakePlaylist]:
        self.calls.append("playlists")
        return list(self.playlists_data)

    def playlist_tracks(self, remote_id: int) -> List[FakeSong]:
        self.calls.append(f"playlist_tracks:{remote_id}")
        return list(self.tracks_data)

    def qr_start(self) -> Dict[str, Any]:
        self.calls.append("qr_start")
        return dict(self.qr_payload)

    def qr_poll(self, key: str, headers: Optional[Dict[str, str]] = None):
        self.calls.append(f"qr_poll:{key}")
        code = self.qr_codes.pop(0) if self.qr_codes else 801
        return code, code == 803

    def save_cookie(self, text: str) -> None:
        self.calls.append("save_cookie")

    def logout(self) -> None:
        self.calls.append("logout")
        self.account_info = FakeAccount(logged_in=False, uid=None)


class FakeLibrary:
    """曲库替身：默认没有本地文件（强制走在线直链）。"""

    def __init__(self) -> None:
        self.local_tracks: Dict[int, Dict[str, Any]] = {}
        self.song_map: Dict[int, Dict[str, Any]] = {}
        self.calls: List[str] = []

    def local_for_song(self, song_id: Optional[int], name: Optional[str] = None,
                       artist: Optional[str] = None) -> Optional[Dict[str, Any]]:
        self.calls.append(f"local_for_song:{song_id}")
        if song_id and int(song_id) in self.song_map:
            return dict(self.song_map[int(song_id)])
        return None

    def media_path(self, track: Dict[str, Any]) -> str:
        return track.get("path") or "/tmp/fake.mp3"

    def media_url(self, track: Dict[str, Any], token: str = "") -> str:
        suffix = f"?t={token}" if token else ""
        return f"http://192.168.0.10:8010/media/{track.get('id', 0)}{suffix}"

    def scan(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"added": 0, "updated": 0, "removed": 0, "total": 0, "elapsed": 0.0}

    def scan_file(self, path: str) -> Optional[Dict[str, Any]]:
        return None

    def list_tracks(self, query: str = "", limit: int = 200,
                    offset: int = 0) -> List[Dict[str, Any]]:
        return list(self.local_tracks.values())[offset:offset + limit]

    def stats(self) -> Dict[str, Any]:
        return {"tracks": len(self.local_tracks), "bytes": 0}


def make_track(track_id: int = 1, name: str = "晴天", artist: str = "周杰伦",
               song_id: Optional[int] = 1001, local_id: Optional[int] = None,
               duration: int = 269) -> Dict[str, Any]:
    """构造一条 ``tracks`` 行（dict 形态）。"""
    return {"id": track_id, "playlist_id": 1, "song_id": song_id, "name": name,
            "artist": artist, "album": "", "pos": track_id, "local_id": local_id,
            "duration": duration}


def _fmt(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
