"""MCP stdio server 集成测试。

用本地假音乐中枢（HTTP）验证：协议握手、工具清单、工具调用、错误文案友好化、
stdout 只输出合法 JSON-RPC。
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="依赖 select 读子进程 stdout")


class FakeHub:
    """可编程的假音乐中枢。"""

    def __init__(self) -> None:
        self.routes = {}
        self.requests = []
        self.server = None
        self.thread = None

    def route(self, method, path, status, payload):
        self.routes[(method.upper(), path)] = (status, payload)

    def start(self) -> str:
        hub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                hub.requests.append({
                    "method": self.command,
                    "path": parsed.path,
                    "query": dict(urllib.parse.parse_qsl(parsed.query)),
                    "body": body.decode("utf-8", "replace"),
                    "headers": dict(self.headers),
                })
                status, payload = hub.routes.get(
                    (self.command, parsed.path), (404, {"ok": False, "error": "not found"})
                )
                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802
                self._handle()

            def do_POST(self) -> None:  # noqa: N802
                self._handle()

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)


class McpSession:
    def __init__(self, base_url: str) -> None:
        env = dict(os.environ)
        env["MUSIC_HUB_URL"] = base_url
        env["MUSIC_HUB_TIMEOUT"] = "5"
        env.pop("MUSIC_HUB_PASSWORD", None)
        self.process = subprocess.Popen(
            [PYTHON, "mcp_server.py"],
            cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._id = 0

    def send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def read(self, timeout: float = 10.0) -> dict:
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not ready:
            stderr = ""
            if self.process.poll() is not None and self.process.stderr is not None:
                stderr = self.process.stderr.read()
            raise AssertionError(f"MCP 无响应（进程状态 {self.process.poll()}）\n{stderr}")
        line = self.process.stdout.readline()
        return json.loads(line)

    def request(self, method: str, params=None, timeout: float = 10.0) -> dict:
        self._id += 1
        message = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        response = self.read(timeout)
        assert response.get("id") == self._id
        return response

    def initialize(self) -> dict:
        response = self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        })
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def call_tool(self, name: str, arguments=None, timeout: float = 10.0) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()


@pytest.fixture()
def hub():
    fake = FakeHub()
    base = fake.start()
    try:
        yield fake, base
    finally:
        fake.stop()


@pytest.fixture()
def session(hub):
    _, base = hub
    mcp = McpSession(base)
    try:
        yield mcp
    finally:
        mcp.close()


def tool_names(payload) -> set:
    return {tool["name"] for tool in payload["result"]["tools"]}


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------
def test_initialize_and_tools_list(session):
    init = session.initialize()
    result = init["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "xiaoai-music-hub"
    assert "tools" in result["capabilities"]

    listed = session.request("tools/list")["result"]["tools"]
    names = tool_names({"result": {"tools": listed}})
    for expected in ("play_music", "pause_music", "resume_music", "stop_music",
                     "next_music", "prev_music", "music_status", "search_music",
                     "list_playlists", "play_playlist", "set_volume",
                     "set_quality", "set_mode"):
        assert expected in names
    for tool in listed:
        assert tool["inputSchema"]["type"] == "object"


def test_unknown_method_returns_jsonrpc_error(session):
    session.initialize()
    response = session.request("no/such/method")
    assert response["error"]["code"] == -32601


def test_ping(session):
    session.initialize()
    response = session.request("ping")
    assert response.get("result") == {}


# ---------------------------------------------------------------------------
# 工具调用
# ---------------------------------------------------------------------------
def test_play_music_success(hub, session):
    fake, _ = hub
    fake.route("POST", "/play", 200, {
        "ok": True, "song": {"name": "晴天", "artist": "周杰伦"}, "state": "PLAYING",
    })
    session.initialize()
    response = session.call_tool("play_music", {"query": "晴天"})
    result = response["result"]
    assert result["isError"] is False
    assert "晴天" in result["content"][0]["text"]
    play_request = [item for item in fake.requests if item["path"] == "/play"]
    assert play_request and "晴天" in play_request[0]["body"]


def test_play_music_vip_conflict_is_friendly(hub, session):
    """v2 bug：/play 返回 409 时 MCP 直接抛 HTTPError。"""
    fake, _ = hub
    fake.route("POST", "/play", 409, {
        "ok": False, "error": "VIP 曲目需要有效 cookie", "code": "no_url",
    })
    session.initialize()
    result = session.call_tool("play_music", {"query": "晴天"})["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "HTTP Error" not in text
    assert "播放地址" in text or "VIP" in text


def test_device_unreachable_is_friendly(hub, session):
    fake, _ = hub
    fake.route("POST", "/play", 503, {
        "ok": False, "error": "设备不可达", "code": "device_unavailable",
    })
    session.initialize()
    text = session.call_tool("play_music", {"query": "晴天"})["result"]["content"][0]["text"]
    assert "HTTP Error" not in text
    assert "音箱" in text


def test_music_status(hub, session):
    fake, _ = hub
    fake.route("GET", "/now", 200, {
        "ok": True, "state": "PLAYING", "title": "晴天", "artist": "周杰伦",
        "position": "00:12", "duration": "04:29",
    })
    session.initialize()
    result = session.call_tool("music_status")["result"]
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "晴天" in text and "周杰伦" in text


def test_search_music(hub, session):
    fake, _ = hub
    fake.route("GET", "/search", 200, {
        "ok": True,
        "songs": [
            {"song_id": 1, "name": "晴天", "artist": "周杰伦", "album": "叶惠美"},
            {"song_id": 2, "name": "七里香", "artist": "周杰伦", "album": "七里香"},
        ],
    })
    session.initialize()
    result = session.call_tool("search_music", {"query": "周杰伦", "limit": 2})["result"]
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "晴天" in text and "七里香" in text


def test_stop_and_volume_and_mode(hub, session):
    fake, _ = hub
    for method, path in (("POST", "/stop"), ("POST", "/volume"), ("POST", "/mode")):
        fake.route(method, path, 200, {"ok": True})
    session.initialize()
    assert session.call_tool("stop_music")["result"]["isError"] is False
    assert session.call_tool("set_volume", {"level": 30})["result"]["isError"] is False
    assert session.call_tool("set_mode", {"mode": "loop"})["result"]["isError"] is False
    paths = [item["path"] for item in fake.requests]
    assert "/stop" in paths and "/volume" in paths and "/mode" in paths


def test_play_playlist_by_name(hub, session):
    fake, _ = hub
    fake.route("GET", "/playlists", 200, {
        "ok": True,
        "playlists": [{"id": 3, "name": "通勤歌单", "count": 10}],
    })
    fake.route("POST", "/playlist/play", 200, {"ok": True})
    session.initialize()
    result = session.call_tool("play_playlist", {"name": "通勤"})["result"]
    assert result["isError"] is False
    assert any(item["path"] == "/playlist/play" for item in fake.requests)


def test_unknown_tool_returns_error(session):
    session.initialize()
    result = session.call_tool("no_such_tool", {})["result"]
    assert result["isError"] is True
