"""HTTP 接口层测试：路由、鉴权、安全、静态资源、媒体流、业务接口。

测试启动真实 :class:`ThreadingHTTPServer`（随机端口），但依赖全部是替身：
FakeRenderer / FakeNetease / FakeDownloader / FakeDlna，数据库是临时目录，
因此完全离线、可重复。
"""
from __future__ import annotations

import base64
import dataclasses
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest
from xiaoai_hub import config, db, errors, library, prefs, security
from xiaoai_hub.api import build_router, make_server
from xiaoai_hub.api.context import AppContext
from xiaoai_hub.api.router import Request, Router
from xiaoai_hub.player import Player


# ---------------------------------------------------------------------------
# 替身与工具
# ---------------------------------------------------------------------------
class FakeDownloader:
    def __init__(self) -> None:
        self.jobs_data = []
        self.stats_data = {"pending": 1, "running": 0, "done": 2, "failed": 0,
                           "canceled": 0, "running_workers": 0}
        self.calls = []

    def init(self) -> None: ...
    def recover(self) -> None: ...
    def kick(self) -> None: ...
    def shutdown(self) -> None: ...
    def jobs(self, limit=100, status=None):
        return list(self.jobs_data)[:limit]

    def stats(self):
        return dict(self.stats_data)

    def add_job(self, song, quality=None):
        self.calls.append(("add_job", song.get("song_id")))
        return 42

    def add_playlist(self, playlist_id):
        self.calls.append(("add_playlist", playlist_id))
        return 3

    def retry_failed(self, job_id=None):
        self.calls.append(("retry_failed", job_id))
        return 2

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        return True


class FakeDevice:
    def __init__(self, ip="192.168.3.50", name="小爱音箱 Pro") -> None:
        self.ip = ip
        self.name = name
        self.model = "LX06"
        self.udn = "uuid:test"
        self.avt_control = "http://192.168.3.50:9999/AVTransport/control"
        self.rc_control = "http://192.168.3.50:9999/RenderingControl/control"

    def to_dict(self):
        return {"ip": self.ip, "name": self.name, "model": self.model,
                "udn": self.udn, "avt_control": self.avt_control,
                "rc_control": self.rc_control}


class FakeDlna:
    def __init__(self) -> None:
        self.devices = [FakeDevice()]

    def discover(self, timeout=5.0):
        return list(self.devices)


class Client:
    """极简 HTTP 客户端，返回 ``(status, body_bytes, headers)``。"""

    def __init__(self, base: str) -> None:
        self.base = base

    def request(self, method, path, *, data=None, payload=None, headers=None):
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            request_headers.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        encoded = urllib.parse.quote(path, safe="/?=&%")
        request = urllib.request.Request(
            self.base + encoded, data=body, method=method, headers=request_headers
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def head(self, path, **kwargs):
        return self.request("HEAD", path, **kwargs)

    def json(self, method, path, **kwargs):
        status, body, headers = self.request(method, path, **kwargs)
        try:
            return status, json.loads(body or b"{}"), headers
        except ValueError:
            return status, {"raw": body.decode("utf-8", "replace")}, headers


def make_playlist(count: int = 2, name: str = "接口测试歌单") -> int:
    playlist_id = db.execute(
        "INSERT INTO playlists(name,created_at,source) VALUES(?,?,?)",
        (name, 0, "local"),
    )
    for index in range(count):
        db.execute(
            """INSERT INTO tracks(playlist_id,song_id,name,artist,album,pos)
               VALUES(?,?,?,?,?,?)""",
            (playlist_id, 1001 + index, f"歌曲{index + 1}", "测试歌手", "专辑", index + 1),
        )
    return playlist_id


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture()
def api(tmp_hub, tmp_path, fake_clock, fake_renderer, fake_netease):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<!doctype html><title>hub</title>", encoding="utf-8")
    (web_dir / "app.js").write_text("console.log('hub');", encoding="utf-8")
    (web_dir / "style.css").write_text("body{color:#fff}", encoding="utf-8")
    (web_dir / "favicon.svg").write_text("<svg/>", encoding="utf-8")

    settings = dataclasses.replace(
        config.get(),
        paths=dataclasses.replace(config.get().paths, web_dir=str(web_dir)),
    )
    player = Player(
        renderer_factory=lambda ip, **kwargs: fake_renderer,
        netease_api=fake_netease,
        library_api=library,
        clock=fake_clock,
        sleep=lambda seconds: fake_clock.advance(seconds),
    )
    downloader = FakeDownloader()
    ctx = AppContext(
        settings=settings,
        db=db,
        prefs=prefs,
        player=player,
        netease=fake_netease,
        library=library,
        downloader=downloader,
        dlna=FakeDlna(),
        version="3.0.0-test",
    )
    server = make_server(ctx, build_router(ctx), "127.0.0.1", 0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        yield SimpleNamespace(
            ctx=ctx, client=client, player=player, renderer=fake_renderer,
            netease=fake_netease, downloader=downloader, clock=fake_clock,
            web_dir=web_dir, base=client.base, markup=tmp_hub,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


# ---------------------------------------------------------------------------
# 基础与静态资源
# ---------------------------------------------------------------------------
def test_health_and_version(api):
    status, payload, _ = api.client.json("GET", "/health")
    assert status == 200 and payload["ok"] is True
    assert payload["version"] == "3.0.0-test"
    assert payload["device"]["ip"]
    assert payload["db"]["ok"] is True
    assert "downloads" in payload and "netease" in payload

    status, payload, _ = api.client.json("GET", "/version")
    assert status == 200 and payload["api"] == 1


def test_static_index_and_assets(api):
    status, body, headers = api.client.get("/")
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert b"<title>hub</title>" in body

    status, body, headers = api.client.get("/app.js")
    assert status == 200 and "javascript" in headers.get("Content-Type", "")
    assert body.startswith(b"console.log")

    status, _, headers = api.client.get("/style.css")
    assert status == 200 and "text/css" in headers.get("Content-Type", "")


def test_static_etag_returns_304(api):
    _, _, headers = api.client.get("/app.js")
    etag = headers.get("ETag")
    assert etag
    status, body, _ = api.client.get("/app.js", headers={"If-None-Match": etag})
    assert status == 304
    assert body == b""


def test_head_request(api):
    status, body, headers = api.client.head("/")
    assert status == 200
    assert body == b""
    assert int(headers.get("Content-Length", 0)) > 0


def test_unknown_route_404(api):
    status, payload, _ = api.client.json("GET", "/no-such-endpoint")
    assert status == 404
    assert payload["code"] == "not_found"


# ---------------------------------------------------------------------------
# 鉴权与 CSRF
# ---------------------------------------------------------------------------
def test_basic_auth_when_password_set(api):
    prefs.set_ui_password("secret")
    status, payload, headers = api.client.json("GET", "/state")
    assert status == 401
    assert "WWW-Authenticate" in headers
    assert payload["code"] == "unauthorized"

    token = base64.b64encode(b"anyone:secret").decode()
    status, payload, _ = api.client.json(
        "GET", "/state", headers={"Authorization": f"Basic {token}"}
    )
    assert status == 200 and payload["ok"] is True

    # 健康检查与静态资源免鉴权
    assert api.client.json("GET", "/health")[0] == 200
    assert api.client.get("/")[0] == 200


def test_csrf_origin_check(api):
    evil = {"Origin": "http://evil.example.com"}
    status, payload, _ = api.client.json(
        "POST", "/playlist/create", data={"name": "x"}, headers=evil
    )
    assert status == 403
    assert payload["code"] == "forbidden"

    same = {"Origin": api.base}
    status, payload, _ = api.client.json(
        "POST", "/playlist/create", data={"name": "ok"}, headers=same
    )
    assert status == 200 and payload["ok"] is True


def test_media_token(api):
    track_path = api.markup.music_dir / "测试歌手 - 晴天.mp3"
    track_path.write_bytes(b"ID3" + b"\x00" * 2048)
    row = library.scan_file(str(track_path))
    assert row is not None
    track_id = int(row["id"])
    prefs.set_ui_password("secret")
    token = security.media_token(track_id, "secret")

    status, payload, _ = api.client.json("GET", f"/media/{track_id}")
    assert status == 403

    status, body, headers = api.client.get(f"/media/{track_id}?t={token}")
    assert status == 200
    assert headers.get("Accept-Ranges") == "bytes"
    assert len(body) == track_path.stat().st_size

    status, _, headers = api.client.head(f"/media/{track_id}?t={token}")
    assert status == 200 and int(headers.get("Content-Length", 0)) > 0

    status, body, headers = api.client.get(
        f"/media/{track_id}?t={token}", headers={"Range": "bytes=0-99"}
    )
    assert status == 206 and len(body) == 100
    assert headers.get("Content-Range", "").startswith("bytes 0-99/")

    status, _, _ = api.client.get(
        f"/media/{track_id}?t={token}", headers={"Range": "bytes=999999-"}
    )
    assert status == 416

    prefs.set_ui_password("")


# ---------------------------------------------------------------------------
# 状态、设置
# ---------------------------------------------------------------------------
def test_state_payload(api):
    status, payload, _ = api.client.json("GET", "/state")
    assert status == 200
    for key in ("version", "server", "device", "player", "settings", "download", "account"):
        assert key in payload
    assert payload["player"]["queue"]["playlist_id"] is None
    assert payload["server"]["port"] == config.get().port


def test_settings_update(api):
    status, payload, _ = api.client.json("POST", "/settings", payload={"quality": "lossless"})
    assert status == 200
    assert payload["settings"]["quality"] == "lossless"

    status, payload, _ = api.client.json("GET", "/quality")
    assert payload["quality"] == "lossless"

    status, payload, _ = api.client.json("POST", "/settings", payload={"play_mode": "shuffle"})
    assert payload["settings"]["play_mode"] == "shuffle"


def test_quiet_hours(api):
    status, payload, _ = api.client.json("POST", "/quiet", payload={"set": "22:00-07:00"})
    assert status == 200 and payload["quiet_hours"] == "22:00-07:00"
    status, payload, _ = api.client.json("POST", "/quiet", payload={"off": 1})
    assert payload["quiet_hours"] == ""


def test_invalid_quality_rejected(api):
    status, payload, _ = api.client.json("POST", "/settings", payload={"quality": "ultra"})
    assert status == 400 and payload["code"] == "bad_request"


# ---------------------------------------------------------------------------
# 设备
# ---------------------------------------------------------------------------
def test_devices_scan_and_select(api):
    status, payload, _ = api.client.json("POST", "/devices/scan", payload={})
    assert status == 200 and payload["found"][0]["ip"] == "192.168.3.50"
    rows = db.query("SELECT * FROM devices WHERE ip=?", ("192.168.3.50",))
    assert rows and rows[0]["name"] == "小爱音箱 Pro"

    status, payload, _ = api.client.json("POST", "/devices/select", payload={"ip": "192.168.3.50"})
    assert status == 200 and payload["active"] == "192.168.3.50"

    status, payload, _ = api.client.json("GET", "/devices")
    assert status == 200 and payload["active"] == "192.168.3.50"
    assert any(item["active"] for item in payload["devices"])


# ---------------------------------------------------------------------------
# 播放
# ---------------------------------------------------------------------------
def test_search_endpoint(api):
    status, payload, _ = api.client.json("GET", "/search?q=晴天&limit=1")
    assert status == 200
    assert len(payload["songs"]) == 1
    assert payload["songs"][0]["song_id"] == 1001
    assert payload["songs"][0]["name"] == "晴天"


def test_play_search_result(api):
    status, payload, _ = api.client.json("POST", "/play?q=晴天")
    assert status == 200 and payload["ok"] is True
    assert api.renderer.state == "PLAYING"
    assert payload["song"]["name"] == "晴天"

    status, payload, _ = api.client.json("POST", "/pause")
    assert status == 200 and payload["ok"] is True
    status, payload, _ = api.client.json("POST", "/resume")
    assert status == 200 and payload["ok"] is True


def test_play_playlist_and_now(api):
    playlist_id = make_playlist(2)
    status, payload, _ = api.client.json(
        "POST", "/playlist/play", payload={"id": playlist_id, "i": 1}
    )
    assert status == 200 and payload["ok"] is True
    assert api.player.index == 1

    status, payload, _ = api.client.json("GET", "/now")
    assert status == 200 and payload["queue"]["playlist_id"] == playlist_id
    assert payload["queue"]["index"] == 1

    status, payload, _ = api.client.json("GET", "/queue")
    assert status == 200 and len(payload["tracks"]) == 2


def test_next_prev_and_seek(api):
    playlist_id = make_playlist(3)
    api.client.json("POST", "/playlist/play", payload={"id": playlist_id, "i": 0})
    status, payload, _ = api.client.json("POST", "/next")
    assert status == 200 and api.player.index == 1
    status, payload, _ = api.client.json("POST", "/prev")
    assert status == 200 and api.player.index == 0
    status, payload, _ = api.client.json("POST", "/seek", payload={"pos": 30})
    assert status == 200 and payload["ok"] is True
    status, payload, _ = api.client.json("POST", "/volume", payload={"level": 40})
    assert payload["volume"] == 40


def test_pause_without_queue_returns_conflict(api):
    status, payload, _ = api.client.json("POST", "/pause")
    assert status in (200, 409)
    if status == 409:
        assert payload["ok"] is False


# ---------------------------------------------------------------------------
# 歌单
# ---------------------------------------------------------------------------
def test_playlist_crud(api):
    status, payload, _ = api.client.json("POST", "/playlist/create", payload={"name": "新歌单"})
    assert status == 200
    playlist_id = payload["id"]

    status, payload, _ = api.client.json("GET", "/playlists")
    assert status == 200
    assert any(item["id"] == playlist_id for item in payload["playlists"])

    status, payload, _ = api.client.json(
        "POST", "/playlist/add",
        payload={"id": playlist_id, "song_id": 1001, "name": "晴天",
                 "artist": "周杰伦", "album": "叶惠美"},
    )
    assert status == 200 and payload["added"] is True

    # 重复添加幂等
    status, payload, _ = api.client.json(
        "POST", "/playlist/add",
        payload={"id": playlist_id, "song_id": 1001, "name": "晴天"},
    )
    assert payload["added"] is False

    status, payload, _ = api.client.json("GET", f"/playlist?id={playlist_id}")
    assert status == 200 and len(payload["tracks"]) == 1
    track_id = payload["tracks"][0]["id"]

    status, payload, _ = api.client.json(
        "POST", "/playlist/rename", payload={"id": playlist_id, "name": "改名"}
    )
    assert status == 200

    status, payload, _ = api.client.json(
        "POST", "/playlist/remove", payload={"id": playlist_id, "track": track_id}
    )
    assert status == 200
    assert api.client.json("GET", f"/playlist?id={playlist_id}")[1]["tracks"] == []

    status, payload, _ = api.client.json("POST", "/playlist/delete", payload={"id": playlist_id})
    assert status == 200
    assert db.query_one("SELECT id FROM playlists WHERE id=?", (playlist_id,)) is None


def test_playlist_reorder(api):
    playlist_id = make_playlist(3)
    tracks = api.client.json("GET", f"/playlist?id={playlist_id}")[1]["tracks"]
    ids = [track["id"] for track in tracks]
    new_order = list(reversed(ids))
    status, payload, _ = api.client.json(
        "POST", "/playlist/reorder", payload={"id": playlist_id, "order": new_order}
    )
    assert status == 200
    tracks_after = api.client.json("GET", f"/playlist?id={playlist_id}")[1]["tracks"]
    assert [track["id"] for track in tracks_after] == new_order


def test_playlist_add_local(api):
    track_path = api.markup.music_dir / "本地歌手 - 本地歌.mp3"
    track_path.write_bytes(b"ID3" + b"\x00" * 1024)
    local = library.scan_file(str(track_path))
    playlist_id = make_playlist(0)
    status, payload, _ = api.client.json(
        "POST", "/playlist/add_local", payload={"id": playlist_id, "track": local["id"]}
    )
    assert status == 200 and payload["added"] is True
    tracks = api.client.json("GET", f"/playlist?id={playlist_id}")[1]["tracks"]
    assert tracks[0]["local_id"] == local["id"]


# ---------------------------------------------------------------------------
# 本地曲库
# ---------------------------------------------------------------------------
def test_local_scan_and_tracks(api):
    (api.markup.music_dir / "歌手A - 歌曲A.mp3").write_bytes(b"ID3" + b"\x00" * 512)
    status, payload, _ = api.client.json("POST", "/local/scan")
    assert status == 200 and payload["added"] == 1

    status, payload, _ = api.client.json("GET", "/local/tracks?q=歌曲A")
    assert status == 200 and len(payload["tracks"]) == 1

    local_id = payload["tracks"][0]["id"]
    status, payload, _ = api.client.json("POST", "/local/play", payload={"id": local_id})
    assert status == 200 and payload["ok"] is True
    assert payload["source"] == "local"


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------
def test_download_endpoints(api):
    playlist_id = make_playlist(2)
    status, payload, _ = api.client.json("POST", "/download/playlist", payload={"id": playlist_id})
    assert status == 200 and payload["queued"] == 3
    assert ("add_playlist", playlist_id) in api.downloader.calls

    status, payload, _ = api.client.json(
        "POST", "/download/song",
        payload={"song_id": 1001, "name": "晴天", "artist": "周杰伦"},
    )
    assert status == 200 and payload["job"] == 42

    status, payload, _ = api.client.json("GET", "/download/jobs")
    assert status == 200 and "stats" in payload

    status, payload, _ = api.client.json("POST", "/download/retry", payload={})
    assert status == 200 and payload["retried"] == 2

    status, payload, _ = api.client.json("POST", "/download/cancel", payload={"id": 7})
    assert status == 200 and payload["ok"] is True


# ---------------------------------------------------------------------------
# 错误映射
# ---------------------------------------------------------------------------
def test_upstream_error_mapping(api):
    api.netease.fail_with = errors.UpstreamError("网易云不可用")
    status, payload, _ = api.client.json("GET", "/search?q=晴天")
    assert status == 502 and payload["code"] == "upstream_error"
    api.netease.fail_with = None


def test_bad_request_mapping(api):
    status, payload, _ = api.client.json("POST", "/play", payload={})
    assert status == 400 and payload["code"] == "bad_request"
    status, payload, _ = api.client.json("GET", "/local/tracks?limit=abc")
    assert status == 200


# ---------------------------------------------------------------------------
# 路由单元测试（不经过 HTTP）
# ---------------------------------------------------------------------------
def test_router_pattern_matching():
    router = Router()
    router.get("/media/{id}", lambda req: None)
    router.get("/playlist", lambda req: None)

    route, params = router.match("GET", "/media/42")
    assert route is not None and params == {"id": "42"}

    route, params = router.match("HEAD", "/media/42")  # HEAD 复用 GET
    assert route is not None and params == {"id": "42"}

    route, _ = router.match("GET", "/playlist/")
    assert route is not None

    route, _ = router.match("GET", "/media")
    assert route is None


def test_request_parameter_sources():
    request = Request(
        "POST", "/x", {"q": "query"}, {"content-type": "application/json"},
        json.dumps({"offset": 5, "name": "n"}).encode(),
    )
    assert request.param("q") == "query"
    assert request.int_param("offset") == 5
    assert request.param("name") == "n"
    with pytest.raises(errors.BadRequest):
        request.require("missing")

    form = Request(
        "POST", "/x", {}, {"content-type": "application/x-www-form-urlencoded"},
        b"level=30&flag=1",
    )
    assert form.int_param("level") == 30
    assert form.flag("flag") is True


# ---------------------------------------------------------------------------
# 网易云账号 / 扫码 / 歌单同步
# ---------------------------------------------------------------------------
def _wait_until(predicate, timeout=6.0, interval=0.05):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_account_endpoint(api):
    status, payload, _ = api.client.json("GET", "/account")
    assert status == 200
    assert payload["logged_in"] is True
    assert payload["uid"] == 42
    assert payload["nickname"] == "测试用户"

    api.netease.account_info.logged_in = False
    api.netease.account_info.uid = None
    status, payload, _ = api.client.json("GET", "/account")
    assert status == 200 and payload["logged_in"] is False


def test_qr_login_flow(api, tmp_path):
    png = tmp_path / "qr.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    api.netease.qr_payload = {"key": "TESTKEY", "png_path": str(png)}
    api.netease.qr_codes = [802, 803]

    status, payload, _ = api.client.json("POST", "/account/qr")
    assert status == 200 and payload["status"] in ("waiting", "scanned")

    assert _wait_until(lambda: api.ctx.login.get("status") == "success")

    status, payload, _ = api.client.json("GET", "/account/qr/status")
    assert payload["status"] == "success"

    status, body, headers = api.client.get("/qr.png")
    assert status == 200 and headers["Content-Type"] == "image/png"
    assert body.startswith(b"\x89PNG")


def test_qr_png_missing_returns_404(api):
    api.ctx.login["png"] = None
    status, payload, _ = api.client.json("GET", "/qr.png")
    assert status == 404


def test_logout(api):
    status, payload, _ = api.client.json("POST", "/account/logout")
    assert status == 200 and payload["logged_in"] is False
    assert "logout" in api.netease.calls
    assert api.ctx.login["status"] == "idle"


def test_netease_playlists(api):
    status, payload, _ = api.client.json("GET", "/netease/playlists")
    assert status == 200
    assert payload["uid"] == 42
    assert payload["playlists"][0]["id"] == 9001
    assert payload["playlists"][0]["name"] == "我喜欢的音乐"


def test_netease_import_and_incremental(api):
    status, payload, _ = api.client.json("POST", "/netease/import", payload={"id": 9001})
    assert status == 200
    assert payload["added"] == 2
    playlist_id = payload["playlist_id"]
    rows = db.query("SELECT * FROM playlists WHERE id=?", (playlist_id,))
    assert rows and rows[0]["source"] == "netease" and rows[0]["owner_uid"] == 42

    # 再次导入：幂等，不重复插入
    status, payload, _ = api.client.json("POST", "/netease/import", payload={"id": 9001})
    assert payload["added"] == 0
    assert len(db.query("SELECT * FROM tracks WHERE playlist_id=?", (playlist_id,))) == 2


def test_netease_sync_requires_remote_playlist(api):
    local_id = make_playlist(1)
    status, payload, _ = api.client.json("POST", "/netease/sync", payload={"id": local_id})
    assert status == 400 and payload["code"] == "bad_request"


def test_netease_sync_remote_playlist(api):
    imported = api.client.json("POST", "/netease/import", payload={"id": 9001})[1]
    status, payload, _ = api.client.json(
        "POST", "/netease/sync", payload={"id": imported["playlist_id"]}
    )
    assert status == 200 and payload["added"] == 0


def test_netease_sync_all(api):
    status, payload, _ = api.client.json("POST", "/netease/sync_all", payload={})
    assert status == 200 and payload["started"] is True
    assert _wait_until(lambda: api.ctx.sync.get("running") is False)
    status, payload, _ = api.client.json("GET", "/netease/sync_all/status")
    assert payload["running"] is False
    assert payload["total"] == 1
    assert payload["done"] == 1
    assert payload["finished_at"]


def test_local_add_endpoint_errors(api):
    status, payload, _ = api.client.json("POST", "/local/add", payload={"id": 999, "track": 1})
    assert status == 404
    status, payload, _ = api.client.json("POST", "/local/play", payload={"id": 999})
    assert status == 404
    status, payload, _ = api.client.json("GET", "/local/scan")
    assert status == 200
