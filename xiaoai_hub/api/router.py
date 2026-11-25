"""极简 HTTP 路由：Request / Response / Router。

不追求通用框架，只解决本项目的问题：
- 路径占位符（``/playlist/{id}``）；
- GET 查询 / POST 变更的统一参数读取（query + JSON + form）；
- 统一的 JSON 错误响应；
- 媒体流式输出通过 ``Response.stream`` 回调交给底层 handler。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple
from urllib.parse import parse_qs, unquote

from .. import errors

_UNSET = object()

_PATH_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class Request:
    """一次 HTTP 请求的只读视图。"""

    def __init__(
        self,
        method: str,
        path: str,
        query: Dict[str, str],
        headers: Dict[str, str],
        body: bytes,
        client_ip: str = "",
        host: str = "",
    ) -> None:
        self.method = method.upper()
        self.path = path
        self.query = dict(query)
        self.headers = {str(k).lower(): str(v) for k, v in headers.items()}
        self.body = body or b""
        self.client_ip = client_ip
        self.host = host
        self.params: Dict[str, str] = {}
        self._data: Any = _UNSET

    # ---------- 参数 ----------
    @property
    def is_mutation(self) -> bool:
        return self.method in ("POST", "PUT", "PATCH", "DELETE")

    def data(self) -> Dict[str, Any]:
        """请求体解析结果（JSON 或 form），只读缓存。"""
        if self._data is not _UNSET:
            return self._data if isinstance(self._data, dict) else {}
        parsed: Dict[str, Any] = {}
        content_type = self.headers.get("content-type", "")
        if self.body:
            if "application/json" in content_type:
                try:
                    loaded = json.loads(self.body.decode("utf-8"))
                    if isinstance(loaded, dict):
                        parsed = loaded
                except (UnicodeDecodeError, ValueError):
                    parsed = {}
            elif "application/x-www-form-urlencoded" in content_type:
                try:
                    parsed = {
                        key: values[-1]
                        for key, values in parse_qs(
                            self.body.decode("utf-8", "replace"), keep_blank_values=True
                        ).items()
                    }
                except ValueError:
                    parsed = {}
        self._data = parsed
        return parsed

    def param(self, name: str, default: str = "") -> str:
        if name in self.params:
            return self.params[name]
        if name in self.query:
            return self.query[name]
        value = self.data().get(name)
        if value is None or isinstance(value, (dict, list)):
            return default
        return str(value)

    def int_param(self, name: str, default: int = 0) -> int:
        try:
            raw = self.param(name, "")
            return int(float(raw)) if raw != "" else default
        except (TypeError, ValueError):
            return default

    def float_param(self, name: str, default: float = 0.0) -> float:
        try:
            raw = self.param(name, "")
            return float(raw) if raw != "" else default
        except (TypeError, ValueError):
            return default

    def flag(self, name: str) -> bool:
        return self.param(name, "").strip().lower() in {"1", "true", "yes", "on"}

    def has(self, name: str) -> bool:
        return bool(self.param(name, "")) or name in self.query or name in self.data()

    def require(self, name: str) -> str:
        value = self.param(name, "")
        if value == "":
            raise errors.BadRequest(f"缺少参数 {name}")
        return value

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"Request({self.method} {self.path})"


@dataclass
class Response:
    """统一响应对象。"""

    status: int = 200
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)
    content_type: str = "application/json; charset=utf-8"
    stream: Optional[Callable[[Any], None]] = None

    @classmethod
    def json(cls, payload: Any, status: int = 200,
             headers: Optional[Dict[str, str]] = None) -> "Response":
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return cls(status=status, body=body, headers=dict(headers or {}))

    @classmethod
    def text(cls, text: str, status: int = 200,
             content_type: str = "text/plain; charset=utf-8") -> "Response":
        return cls(status=status, body=text.encode("utf-8"), content_type=content_type)

    @classmethod
    def raw(cls, body: bytes, content_type: str, status: int = 200,
            headers: Optional[Dict[str, str]] = None) -> "Response":
        return cls(status=status, body=body, content_type=content_type,
                   headers=dict(headers or {}))

    @classmethod
    def no_content(cls, status: int = 204) -> "Response":
        return cls(status=status, body=b"")

    @classmethod
    def streamed(cls, callback: Callable[[Any], None]) -> "Response":
        return cls(status=200, body=b"", stream=callback)


@dataclass
class Route:
    method: str
    pattern: str
    regex: Pattern
    handler: Callable[[Request], Response]
    auth: bool = True
    mutation: bool = False


class Router:
    """顺序匹配的简单路由表。"""

    def __init__(self) -> None:
        self._routes: List[Route] = []

    def add(
        self,
        method: str,
        pattern: str,
        handler: Callable[[Request], Response],
        *,
        auth: bool = True,
        mutation: Optional[bool] = None,
    ) -> None:
        method = method.upper()
        if mutation is None:
            mutation = method in ("POST", "PUT", "PATCH", "DELETE")
        self._routes.append(
            Route(
                method=method,
                pattern=pattern,
                regex=_compile_pattern(pattern),
                handler=handler,
                auth=auth,
                mutation=mutation,
            )
        )

    def get(self, pattern: str, handler: Callable[[Request], Response], **kwargs: Any) -> None:
        self.add("GET", pattern, handler, **kwargs)

    def post(self, pattern: str, handler: Callable[[Request], Response], **kwargs: Any) -> None:
        self.add("POST", pattern, handler, **kwargs)

    def match(self, method: str, path: str) -> Tuple[Optional[Route], Dict[str, str]]:
        method = "GET" if method.upper() == "HEAD" else method.upper()
        for route in self._routes:
            if route.method != method:
                continue
            matched = route.regex.match(path)
            if matched:
                params = {
                    key: unquote(value)
                    for key, value in matched.groupdict().items()
                    if value is not None
                }
                return route, params
        return None, {}

    @property
    def routes(self) -> List[Route]:
        return list(self._routes)


def _compile_pattern(pattern: str) -> Pattern:
    parts = _PATH_PARAM.split(pattern)
    out: List[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            out.append(rf"(?P<{part}>[^/]+)")
        else:
            out.append(re.escape(part))
    suffix = "" if pattern.endswith("/") else "/?"
    return re.compile("^" + "".join(out) + suffix + "$")
