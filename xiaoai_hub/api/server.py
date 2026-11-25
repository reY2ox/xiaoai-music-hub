"""HTTP 服务器：请求分发、鉴权、同源校验、安全响应头、访问日志。

``HubApp`` 是纯逻辑分发器（可单测）；``HubRequestHandler`` 只负责 HTTP 细节。
"""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from .. import errors, logging_setup, security
from .router import Request, Response, Router

log = logging_setup.get_logger(__name__)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class HubApp:
    """把 Router + 鉴权 + 错误映射组合起来的分发器。"""

    def __init__(self, ctx: Any, router: Router) -> None:
        self.ctx = ctx
        self.router = router

    def handle(self, request: Request) -> Response:
        route, params = self.router.match(request.method, request.path)
        if route is None:
            return Response.json(
                {"ok": False, "error": "接口不存在", "code": "not_found"}, 404
            )
        request.params = params
        try:
            if route.auth:
                denied = self._check_auth(request)
                if denied is not None:
                    return denied
            if route.mutation and not security.same_origin(request.headers, request.host):
                return Response.json(
                    {"ok": False, "error": "跨站请求被拒绝", "code": "forbidden"}, 403
                )
            response = route.handler(request)
            return response if response is not None else Response.json({"ok": True})
        except errors.HubError as exc:
            log.info("接口错误 %s %s → %s %s", request.method, request.path,
                     exc.status, exc.message)
            return Response.json(exc.to_dict(), exc.status)
        except ValueError as exc:
            log.info("参数错误 %s %s: %s", request.method, request.path, exc)
            return Response.json(
                {"ok": False, "error": str(exc) or "参数错误", "code": "bad_request"}, 400
            )
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception:
            log.exception("接口异常 %s %s", request.method, request.path)
            return Response.json(
                {"ok": False, "error": "服务器内部错误", "code": "internal"}, 500
            )

    def _check_auth(self, request: Request) -> Optional[Response]:
        password = self.ctx.prefs.ui_password()
        if not password:
            return None
        if security.basic_auth_ok(request.header("authorization"), password):
            return None
        return Response.json(
            {"ok": False, "error": "需要登录", "code": "unauthorized"},
            401,
            headers={"WWW-Authenticate": 'Basic realm="xiaoai-music-hub", charset="UTF-8"'},
        )


class HubRequestHandler(BaseHTTPRequestHandler):
    """把 HTTP 请求翻译成 :class:`Request`，把 :class:`Response` 写回。"""

    protocol_version = "HTTP/1.1"
    server_version = "xiaoai-hub/3.0"
    sys_version = ""

    # ---------- HTTP 方法 ----------
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, PUT, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- 核心 ----------
    def _dispatch(self, method: str) -> None:
        started = time.monotonic()
        status = 500
        try:
            request = self._build_request(method)
            response = self.server.app.handle(request)  # type: ignore[attr-defined]
            status = response.status
            self._write_response(response, head_only=(method == "HEAD"))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:  # pragma: no cover - 兜底，避免连接悬挂
            log.exception("HTTP 处理异常")
            try:
                self._write_response(
                    Response.json(
                        {"ok": False, "error": "服务器内部错误", "code": "internal"}, 500
                    ),
                    head_only=False,
                )
            except OSError:
                return
        finally:
            elapsed = (time.monotonic() - started) * 1000
            log.info(
                "%s %s %s %s %.1fms",
                self.client_address[0] if self.client_address else "-",
                method, self.path, status, elapsed,
            )

    def _build_request(self, method: str) -> Request:
        parsed = urlsplit(self.path)
        query = {
            key: values[-1]
            for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        }
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        headers = {key: value for key, value in self.headers.items()}
        return Request(
            method=method,
            path=parsed.path,
            query=query,
            headers=headers,
            body=body,
            client_ip=self.client_address[0] if self.client_address else "",
            host=self.headers.get("Host", ""),
        )

    def _write_response(self, response: Response, *, head_only: bool) -> None:
        if response.stream is not None:
            response.stream(self)
            return
        self.send_response(response.status)
        if response.status not in (204, 304):
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if head_only or not response.body:
            return
        self.wfile.write(response.body)

    def log_message(self, *args: Any) -> None:
        """由 :meth:`_dispatch` 统一记录访问日志，屏蔽默认实现。"""


class HubHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address: Tuple[str, int], app: HubApp) -> None:
        super().__init__(address, HubRequestHandler)
        self.app = app


def make_server(ctx: Any, router: Router, host: str = "0.0.0.0", port: int = 8010) -> HubHTTPServer:
    app = HubApp(ctx, router)
    return HubHTTPServer((host, int(port)), app)


def serve_forever(server: HubHTTPServer, stop_event: Optional[threading.Event] = None) -> None:
    """阻塞运行，支持通过 ``stop_event`` 触发优雅退出。"""
    if stop_event is None:
        server.serve_forever(poll_interval=0.2)
        return
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.2},
        name="http-server", daemon=True,
    )
    thread.start()
    try:
        while thread.is_alive():
            if stop_event.wait(0.2):
                break
    finally:
        server.shutdown()
        thread.join(timeout=3.0)
        server.server_close()


def build_router(ctx: Any) -> Router:
    """注册所有路由。"""
    from .routes import download, local, netease, player, playlists, system

    router = Router()
    system.register(router, ctx)
    player.register(router, ctx)
    playlists.register(router, ctx)
    netease.register(router, ctx)
    local.register(router, ctx)
    download.register(router, ctx)
    return router


def build_app(ctx: Any) -> HubApp:
    return HubApp(ctx, build_router(ctx))


__all__ = [
    "HubApp", "HubRequestHandler", "HubHTTPServer",
    "make_server", "serve_forever", "build_router", "build_app", "Request", "Response",
]
