"""网易云接入层测试：全部走本地假 HTTP 服务，不访问真实网络。

覆盖点：
- 搜索解析 / 空结果；
- 直链正常、无直链、自动降级（含当前档位与全部失败）；
- 账号 TTL 缓存（注入可推进时钟）、强制刷新、未登录短路；
- 错误码映射（-110/-460 → 401 login_required，301 → 403 vip_required，其它 → 502）；
- 歌单列表 ``more`` 翻页、歌单曲目 1500 首分页；
- 扫码登录（unikey → PNG、801/803 轮询、Set-Cookie 写 MUSIC_U）；
- cookie 默认值 / 0600 权限 / 登出删除；5xx 与连接失败重试、JSON 解析失败。
"""
from __future__ import annotations

import json
import os
import socket
import stat
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
from xiaoai_hub import config, db, netease, prefs
from xiaoai_hub.errors import HubError

from tests.fakes import FakeClock

# ---------------------------------------------------------------------------
# 假网易云服务
# ---------------------------------------------------------------------------
Route = Callable[[Dict[str, Any]], Any]


def _normalise_response(result: Any) -> Tuple[int, Dict[str, Any], bytes]:
    """把路由返回值规整成 ``(status, headers, body)``。

    支持三种写法：``payload`` / ``(payload, extra_headers)`` /
    ``(status, payload, extra_headers)``；``payload`` 为 ``bytes`` 时原样返回。
    """
    status = 200
    payload = result
    extra: Dict[str, Any] = {}
    if isinstance(result, tuple):
        if len(result) == 2:
            payload, extra = result
        else:
            status, payload, extra = result
    headers: Dict[str, Any] = {"Content-Type": "application/json"}
    headers.update(extra)
    if isinstance(payload, bytes):
        body = payload
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return int(status), headers, body


class _FakeNeteaseHandler(BaseHTTPRequestHandler):
    """把请求交给 ``server.routes`` 注册的处理函数，并记录请求。"""

    server: _FakeNeteaseServer

    def do_GET(self) -> None:  # noqa: N802 - 覆盖标准库方法名
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - 覆盖标准库方法名
        self._dispatch("POST")

    def log_message(self, fmt: str, *args: Any) -> None:
        """静默访问日志，保持测试输出干净。"""

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length > 0 else b""
        record: Dict[str, Any] = {
            "method": method,
            "path": parsed.path,
            "query": dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)),
            "form": (
                dict(urllib.parse.parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True))
                if raw_body
                else {}
            ),
            "headers": {key.lower(): value for key, value in self.headers.items()},
        }
        self.server.requests.append(record)
        route = self.server.routes.get(parsed.path)
        if route is None:
            status, headers, body = 404, {"Content-Type": "application/json"}, b'{"code": 404}'
        else:
            status, headers, body = _normalise_response(route(record))
        self.send_response(status)
        for key, value in headers.items():
            if isinstance(value, (list, tuple)):
                for item in value:
                    self.send_header(key, str(item))
            else:
                self.send_header(key, str(value))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _FakeNeteaseServer(ThreadingHTTPServer):
    """线程化的假网易云服务（端口 0 自动分配）。"""

    daemon_threads = True

    def __init__(self, server_address: Tuple[str, int] = ("127.0.0.1", 0)) -> None:
        super().__init__(server_address, _FakeNeteaseHandler)
        self.routes: Dict[str, Route] = {}
        self.requests: List[Dict[str, Any]] = []

    def on(self, path: str, handler: Route) -> None:
        """注册某路径的处理函数。"""
        self.routes[path] = handler

    def requests_to(self, path: str) -> List[Dict[str, Any]]:
        """取某路径的全部请求记录（按发生顺序）。"""
        return [item for item in self.requests if item["path"] == path]

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_hub(tmp_path):
    """每个测试独立的数据目录 / SQLite / 配置单例。"""
    data_dir = tmp_path / "data"
    env = {
        "DATA_DIR": str(data_dir),
        "MUSIC_DIR": str(tmp_path / "music"),
        "DB_PATH": str(data_dir / "music.db"),
        "CACHE_DIR": str(data_dir / "cache"),
        "SECRETS_DIR": str(data_dir / "secrets"),
        "COOKIE_FILE": str(data_dir / "secrets" / "netease.cookie"),
        "LAN_IP": "192.168.0.10",
        "NETEASE_BASE": "http://127.0.0.1:9",
        "LOG_LEVEL": "WARNING",
        "XIAOAI_IP": "192.168.3.81",
        "UI_PASSWORD": "",
    }
    config.reload(env=env, base_dir=str(tmp_path))
    db.configure(env["DB_PATH"])
    db.init_schema()
    netease.configure()
    try:
        yield tmp_path
    finally:
        netease.configure()
        db.configure(None)
        config.reset()


@pytest.fixture()
def fake_server(isolated_hub):
    """启动假网易云服务，并把 netease 模块指向它。"""
    server = _FakeNeteaseServer()
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    _point_at(server, isolated_hub)
    try:
        yield server
    finally:
        try:
            server.shutdown()
            server.server_close()
        finally:
            thread.join(timeout=5.0)


def _point_at(server: _FakeNeteaseServer, tmp_path, **overrides: Any) -> None:
    """把 netease 的 base / cookie 文件 / 超时指向测试资源。"""
    options: Dict[str, Any] = {
        "base": server.base_url,
        "cookie_file": str(tmp_path / "cookie"),
        "timeout": 2.0,
    }
    options.update(overrides)
    netease.configure(**options)


def _unused_port() -> int:
    """拿一个当前无人监听的端口（用于连接失败测试）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _song(
    song_id: int,
    name: Optional[str] = None,
    artists: Optional[List[str]] = None,
    album: str = "专辑",
    fee: Optional[int] = 0,
    dt: int = 269000,
    cover: str = "",
) -> Dict[str, Any]:
    """构造网易云歌曲 JSON。"""
    return {
        "id": song_id,
        "name": name or f"歌曲{song_id}",
        "ar": [{"name": item} for item in (artists or ["测试歌手"])],
        "al": {"name": album, "picUrl": cover},
        "fee": fee,
        "dt": dt,
    }


def _account_payload(
    uid: int = 42, nickname: str = "测试用户", vip_type: int = 11
) -> Dict[str, Any]:
    """构造网易云账号 JSON。"""
    return {
        "code": 200,
        "account": {"vipType": vip_type},
        "profile": {
            "userId": uid,
            "nickname": nickname,
            "avatarUrl": "http://avatar.example/u.jpg",
        },
    }


def _login(fake_server: _FakeNeteaseServer) -> None:
    """写入假 cookie 并让账号接口返回已登录。"""
    fake_server.on("/api/nuser/account/get", lambda request: _account_payload())
    netease.save_cookie("MUSIC_U=abc; os=pc")


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
def test_search_parses_songs(fake_server):
    fake_server.on(
        "/api/cloudsearch/pc",
        lambda request: {
            "code": 200,
            "result": {
                "songs": [
                    _song(
                        1001,
                        "晴天",
                        ["周杰伦"],
                        "叶惠美",
                        fee=0,
                        dt=269000,
                        cover="http://cover/1.jpg",
                    ),
                    _song(1002, "七里香", ["周杰伦", "方文山"], "七里香", fee=1, dt=299000),
                ]
            },
        },
    )
    songs = netease.search("周杰伦", limit=5, offset=10)
    assert [song.song_id for song in songs] == [1001, 1002]
    first = songs[0]
    assert (first.name, first.artist, first.album) == ("晴天", "周杰伦", "叶惠美")
    assert (first.fee, first.duration, first.cover) == (0, 269, "http://cover/1.jpg")
    assert songs[1].artist == "周杰伦/方文山"
    assert songs[1].duration == 299
    request = fake_server.requests_to("/api/cloudsearch/pc")[0]
    assert request["method"] == "POST"
    assert request["form"] == {"s": "周杰伦", "type": "1", "limit": "5", "offset": "10"}


def test_search_empty_result_and_blank_query(fake_server):
    fake_server.on("/api/cloudsearch/pc", lambda request: {"code": 200, "result": {}})
    assert netease.search("不存在的歌") == []
    assert len(fake_server.requests_to("/api/cloudsearch/pc")) == 1
    # 空白关键词直接返回空列表，不发请求
    assert netease.search("   ") == []
    assert len(fake_server.requests_to("/api/cloudsearch/pc")) == 1


# ---------------------------------------------------------------------------
# 播放直链
# ---------------------------------------------------------------------------
def test_song_url_ok(fake_server):
    fake_server.on(
        "/api/song/enhance/player/url",
        lambda request: {
            "code": 200,
            "data": [{"id": 1001, "url": "http://music/1.mp3", "br": 320000,
                      "code": 200, "type": "mp3"}],
        },
    )
    result = netease.song_url(1001)
    assert result.ok is True
    assert result.url == "http://music/1.mp3"
    assert (result.code, result.br, result.ext, result.quality) == (200, 320000, "mp3", "high")
    request = fake_server.requests_to("/api/song/enhance/player/url")[0]
    assert request["form"] == {"ids": "[1001]", "br": "320000"}


def test_song_url_without_url_keeps_code(fake_server):
    fake_server.on(
        "/api/song/enhance/player/url",
        lambda request: {
            "code": 200,
            "data": [{"id": 1001, "url": None, "br": 0, "code": 404, "type": None}],
        },
    )
    result = netease.song_url(1001)
    assert result.ok is False
    assert result.url == ""
    assert result.code == 404
    assert result.quality == "high"  # 无可用码率时沿用请求档位


def test_song_url_auto_falls_back_to_lower_quality(fake_server):
    prefs.set_quality("lossless")
    requested: List[int] = []

    def route(request: Dict[str, Any]) -> Dict[str, Any]:
        br = int(request["form"]["br"])
        requested.append(br)
        if br == 999000:
            return {"code": 200, "data": [{"url": None, "br": 0, "code": 404}]}
        if br == 320000:
            return {"code": 200, "data": [{"url": None, "br": 0, "code": 404}]}
        return {"code": 200, "data": [{"url": "http://music/1.mp3", "br": 192000,
                                       "code": 200, "type": "mp3"}]}

    fake_server.on("/api/song/enhance/player/url", route)
    result = netease.song_url_auto(1001)
    assert requested == [999000, 320000, 192000]
    assert result.ok is True
    assert result.quality == "standard"
    assert result.ext == "mp3"


def test_song_url_auto_starts_from_current_quality_without_duplicates(fake_server):
    prefs.set_quality("high")
    requested: List[int] = []

    def route(request: Dict[str, Any]) -> Dict[str, Any]:
        requested.append(int(request["form"]["br"]))
        return {"code": 200, "data": [{"url": "http://music/1.mp3", "br": 320000,
                                       "code": 200, "type": "mp3"}]}

    fake_server.on("/api/song/enhance/player/url", route)
    result = netease.song_url_auto(1001)
    assert requested == [320000]  # 当前 high 可用就不再尝试低档，也不重复尝试 lossless
    assert result.ok is True
    assert result.quality == "high"


def test_song_url_auto_all_fail_returns_last(fake_server):
    prefs.set_quality("high")
    requested: List[int] = []

    def route(request: Dict[str, Any]) -> Dict[str, Any]:
        requested.append(int(request["form"]["br"]))
        return {"code": 200, "data": [{"url": None, "br": 0, "code": 404}]}

    fake_server.on("/api/song/enhance/player/url", route)
    result = netease.song_url_auto(1001)
    assert requested == [320000, 192000]
    assert result.ok is False
    assert result.code == 404
    assert result.quality == "standard"  # 最后一次尝试的档位


# ---------------------------------------------------------------------------
# 账号与缓存
# ---------------------------------------------------------------------------
def test_account_cached_only_one_request(fake_server):
    fake_server.on("/api/nuser/account/get", lambda request: _account_payload())
    netease.save_cookie("MUSIC_U=abc; os=pc")
    first = netease.account()
    second = netease.account()
    assert first is second
    assert first.logged_in is True
    assert (first.uid, first.nickname, first.vip) == (42, "测试用户", True)
    assert netease.account_cached() is first
    assert netease.current_uid() == 42
    requests = fake_server.requests_to("/api/nuser/account/get")
    assert len(requests) == 1
    assert requests[0]["method"] == "GET"


def test_account_ttl_and_force_refresh(fake_server, isolated_hub, monkeypatch):
    calls = {"count": 0}

    def route(request: Dict[str, Any]) -> Dict[str, Any]:
        calls["count"] += 1
        return _account_payload(uid=calls["count"])

    fake_server.on("/api/nuser/account/get", route)
    netease.save_cookie("MUSIC_U=abc; os=pc")
    clock = FakeClock()
    monkeypatch.setattr(netease, "_clock", clock)
    _point_at(fake_server, isolated_hub, account_ttl=60.0)

    assert netease.account().uid == 1
    assert netease.account().uid == 1  # 命中缓存
    assert calls["count"] == 1

    clock.advance(61)
    assert netease.account().uid == 2  # 过期后重新请求
    assert calls["count"] == 2

    assert netease.account(force=True).uid == 3
    assert calls["count"] == 3

    # TTL=0 时始终视为过期（不依赖真实 sleep）
    _point_at(fake_server, isolated_hub, account_ttl=0.0)
    assert netease.account().uid == 4
    assert netease.account().uid == 5
    assert calls["count"] == 5


def test_account_without_cookie_skips_network(fake_server):
    info = netease.account()
    assert info.logged_in is False
    assert info.uid is None
    assert "扫码" in info.reason
    assert fake_server.requests == []
    assert netease.account_cached() is info
    assert netease.current_uid() is None


def test_account_login_invalid_maps_to_401(fake_server):
    fake_server.on("/api/nuser/account/get", lambda request: {"code": -110, "message": "未登录"})
    netease.save_cookie("MUSIC_U=bad; os=pc")
    with pytest.raises(netease.NeteaseError) as caught:
        netease.account()
    exc = caught.value
    assert isinstance(exc, HubError)
    assert (exc.code, exc.status, exc.error_code) == (-110, 401, "login_required")
    assert exc.to_dict() == {"ok": False, "error": "未登录", "code": "login_required"}
    assert netease.account_cached() is None


@pytest.mark.parametrize(
    ("code", "status", "error_code", "keyword"),
    [
        (-110, 401, "login_required", "登录"),
        (-460, 401, "login_required", "登录"),
        (301, 403, "vip_required", "VIP"),
        (-999, 502, "upstream_error", "-999"),
    ],
)
def test_error_code_mapping(fake_server, code, status, error_code, keyword):
    fake_server.on("/api/cloudsearch/pc", lambda request: {"code": code})
    with pytest.raises(netease.NeteaseError) as caught:
        netease.search("晴天")
    exc = caught.value
    assert (exc.code, exc.status, exc.error_code) == (code, status, error_code)
    assert keyword in exc.message
    assert exc.to_dict()["code"] == error_code


# ---------------------------------------------------------------------------
# 歌单
# ---------------------------------------------------------------------------
def test_playlists_paginates(fake_server):
    _login(fake_server)

    def route(request: Dict[str, Any]) -> Dict[str, Any]:
        if request["query"]["offset"] == "0":
            return {
                "code": 200,
                "more": True,
                "playlist": [
                    {"id": 9001, "name": "我喜欢的音乐", "trackCount": 2,
                     "coverImgUrl": "http://cover/9.jpg"},
                    {"id": 9002, "name": "通勤", "trackCount": 5, "coverImgUrl": ""},
                ],
            }
        return {
            "code": 200,
            "more": False,
            "playlist": [{"id": 9003, "name": "收藏", "trackCount": 8}],
        }

    fake_server.on("/api/user/playlist", route)
    playlists = netease.playlists()
    assert [item.remote_id for item in playlists] == [9001, 9002, 9003]
    assert playlists[0].to_dict() == {
        "id": 9001,
        "name": "我喜欢的音乐",
        "count": 2,
        "cover": "http://cover/9.jpg",
    }
    requests = fake_server.requests_to("/api/user/playlist")
    # offset 是「已获取条数」：假服务首页只返回 2 条，因此第二次翻页 offset=2
    # （真实接口 limit=1000 且满页返回时即为 1000，两种实现结果等价）
    assert [request["query"]["offset"] for request in requests] == ["0", "2"]
    assert {request["query"]["uid"] for request in requests} == {"42"}
    assert len(fake_server.requests_to("/api/nuser/account/get")) == 1


def test_playlists_requires_login(fake_server):
    with pytest.raises(netease.NeteaseError) as caught:
        netease.playlists()
    assert caught.value.status == 401
    assert caught.value.error_code == "login_required"
    assert fake_server.requests_to("/api/user/playlist") == []


def test_playlist_tracks_paginates_beyond_first_page(fake_server):
    first_page = [_song(song_id, f"歌{song_id}") for song_id in range(1, 1001)]
    rest = [_song(song_id, f"歌{song_id}") for song_id in range(1001, 1501)]

    def detail_route(request: Dict[str, Any]) -> Dict[str, Any]:
        assert request["query"] == {"id": "9001", "n": "1000"}
        return {
            "code": 200,
            "playlist": {"trackCount": 1500, "tracks": first_page},
        }

    def all_route(request: Dict[str, Any]) -> Dict[str, Any]:
        offset = int(request["query"]["offset"])
        assert request["query"]["limit"] == "1000"
        assert request["query"]["id"] == "9001"
        return {"code": 200, "songs": first_page if offset == 0 else rest}

    fake_server.on("/api/v3/playlist/detail", detail_route)
    fake_server.on("/api/playlist/track/all", all_route)
    tracks = netease.playlist_tracks(9001)
    assert len(tracks) == 1500
    assert tracks[0].song_id == 1
    assert tracks[-1].song_id == 1500
    offsets = [
        int(request["query"]["offset"])
        for request in fake_server.requests_to("/api/playlist/track/all")
    ]
    assert offsets == [0, 1000]


def test_playlist_tracks_uses_detail_when_complete(fake_server):
    fake_server.on(
        "/api/v3/playlist/detail",
        lambda request: {
            "code": 200,
            "playlist": {"trackCount": 2, "tracks": [_song(1), _song(2)]},
        },
    )
    tracks = netease.playlist_tracks(9001)
    assert [track.song_id for track in tracks] == [1, 2]
    assert fake_server.requests_to("/api/playlist/track/all") == []


# ---------------------------------------------------------------------------
# 扫码登录
# ---------------------------------------------------------------------------
def test_qr_start_and_poll_saves_cookie(fake_server, isolated_hub):
    poll_state = {"count": 0}

    def unikey_route(request: Dict[str, Any]) -> Dict[str, Any]:
        return {"code": 200, "unikey": "KEY-123"}

    def poll_route(request: Dict[str, Any]) -> Any:
        poll_state["count"] += 1
        if poll_state["count"] == 1:
            return {"code": 801, "message": "等待扫码"}
        return (
            {"code": 803, "message": "授权成功"},
            {"Set-Cookie": "MUSIC_U=QR-TOKEN; Path=/; HttpOnly"},
        )

    fake_server.on("/api/login/qrcode/unikey", unikey_route)
    fake_server.on("/api/login/qrcode/client/login", poll_route)

    started = netease.qr_start()
    assert started["key"] == "KEY-123"
    assert started["png_path"] == str(isolated_hub / "data" / "cache" / "netease_qr.png")
    with open(started["png_path"], "rb") as handle:
        assert handle.read(8) == b"\x89PNG\r\n\x1a\n"

    assert netease.qr_poll("KEY-123") == (801, False)
    assert netease.cookie() == "os=pc"  # 未成功不写 cookie

    assert netease.qr_poll("KEY-123") == (803, True)
    assert netease.cookie() == "MUSIC_U=QR-TOKEN; os=pc"
    assert netease.account_cached() is None  # 登录后账号缓存已失效

    unikey_request = fake_server.requests_to("/api/login/qrcode/unikey")[0]
    poll_request = fake_server.requests_to("/api/login/qrcode/client/login")[0]
    assert poll_request["query"] == {"key": "KEY-123", "type": "1"}
    # 扫码会话 cookie 与轮询 cookie 保持一致
    assert unikey_request["headers"]["cookie"] == poll_request["headers"]["cookie"]
    assert "_ntes_nuid=" in unikey_request["headers"]["cookie"]
    assert "NMTID=" in unikey_request["headers"]["cookie"]


def test_qr_start_without_segno(fake_server, monkeypatch):
    fake_server.on("/api/login/qrcode/unikey", lambda request: {"code": 200, "unikey": "KEY-456"})
    monkeypatch.setitem(sys.modules, "segno", None)
    started = netease.qr_start()
    assert started == {"error": "缺少 segno 依赖，请 pip install segno", "key": "KEY-456"}


# ---------------------------------------------------------------------------
# cookie 与登出
# ---------------------------------------------------------------------------
def test_cookie_default_and_roundtrip(isolated_hub):
    cookie_path = isolated_hub / "cookie"
    netease.configure(cookie_file=str(cookie_path))
    assert netease.cookie() == "os=pc"
    netease.save_cookie("  MUSIC_U=abc; os=pc  ")
    assert netease.cookie() == "MUSIC_U=abc; os=pc"
    assert stat.S_IMODE(os.stat(cookie_path).st_mode) == 0o600
    # 原子写不留下临时文件
    leftovers = [item.name for item in isolated_hub.iterdir()
                 if item.name.startswith(".netease-cookie-")]
    assert leftovers == []


def test_cookie_empty_file_returns_default(isolated_hub):
    cookie_path = isolated_hub / "cookie"
    cookie_path.write_text("   \n", encoding="utf-8")
    netease.configure(cookie_file=str(cookie_path))
    assert netease.cookie() == "os=pc"


def test_logout_deletes_cookie_and_clears_cache(fake_server, isolated_hub):
    _login(fake_server)
    assert netease.account().logged_in is True
    assert netease.account_cached() is not None
    cookie_path = isolated_hub / "cookie"
    assert cookie_path.exists()

    netease.logout()
    assert not cookie_path.exists()
    assert netease.cookie() == "os=pc"
    assert netease.account_cached() is None
    netease.logout()  # 幂等：文件不存在也不抛异常


# ---------------------------------------------------------------------------
# 网络重试与异常
# ---------------------------------------------------------------------------
def test_request_retries_on_5xx(fake_server, monkeypatch):
    delays: List[float] = []
    monkeypatch.setattr(netease, "_sleep", lambda seconds: delays.append(seconds))
    calls = {"count": 0}

    def route(request: Dict[str, Any]) -> Any:
        calls["count"] += 1
        if calls["count"] <= 2:
            return (500, {"code": 500}, {})
        return {"code": 200, "result": {"songs": [_song(1)]}}

    fake_server.on("/api/cloudsearch/pc", route)
    songs = netease.search("晴天")
    assert [song.song_id for song in songs] == [1]
    assert calls["count"] == 3
    assert delays == [0.5, 1.5]


def test_request_retries_on_connection_error(isolated_hub, monkeypatch):
    delays: List[float] = []
    monkeypatch.setattr(netease, "_sleep", lambda seconds: delays.append(seconds))
    netease.configure(
        base=f"http://127.0.0.1:{_unused_port()}",
        cookie_file=str(isolated_hub / "cookie"),
        timeout=0.5,
    )
    with pytest.raises(netease.NeteaseError) as caught:
        netease.search("晴天")
    exc = caught.value
    assert (exc.status, exc.error_code) == (502, "upstream_error")
    assert "无法连接网易云" in exc.message
    assert delays == [0.5, 1.5]


def test_json_parse_failure_raises_upstream_error(fake_server, monkeypatch):
    delays: List[float] = []
    monkeypatch.setattr(netease, "_sleep", lambda seconds: delays.append(seconds))
    fake_server.on("/api/cloudsearch/pc", lambda request: b"<html>not json</html>")
    with pytest.raises(netease.NeteaseError) as caught:
        netease.search("晴天")
    assert caught.value.status == 502
    assert "解析失败" in caught.value.message
    assert delays == []  # JSON 解析失败不重试


# ---------------------------------------------------------------------------
# 数据类契约
# ---------------------------------------------------------------------------
def test_to_dict_contract():
    song = netease.Song(
        1001, "晴天", "周杰伦", "叶惠美", fee=0, duration=269, cover="c"
    )
    assert song.to_dict() == {
        "song_id": 1001,
        "name": "晴天",
        "artist": "周杰伦",
        "album": "叶惠美",
        "fee": 0,
        "duration": 269,
        "cover": "c",
    }
    assert netease.SongUrl("http://u", 200, 320000, "mp3", "high", True).to_dict() == {
        "url": "http://u",
        "code": 200,
        "br": 320000,
        "ext": "mp3",
        "quality": "high",
        "ok": True,
    }
    account = netease.Account(True, 42, "小明", "http://a", True, "")
    assert account.to_dict()["ok"] is True
    assert account.to_dict()["logged_in"] is True
    assert account.to_dict()["uid"] == 42
    assert netease.Playlist(9001, "我喜欢的音乐", 2, "http://c").to_dict() == {
        "id": 9001,
        "name": "我喜欢的音乐",
        "count": 2,
        "cover": "http://c",
    }
