"""统一异常体系：领域层抛出，接口层映射为 HTTP 状态码。

约定：异常消息面向用户，可直接展示；内部细节放 ``details``，只写日志。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class HubError(Exception):
    """所有业务异常的基类。"""

    status: int = 500
    code: str = "internal"

    def __init__(
        self,
        message: str = "",
        *,
        status: Optional[int] = None,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": False,
            "error": self.message,
            "code": self.code,
        }
        if self.details:
            payload["details"] = self.details
        return payload

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"{self.__class__.__name__}({self.message!r}, "
            f"status={self.status}, code={self.code!r})"
        )


class BadRequest(HubError):
    status = 400
    code = "bad_request"


class Unauthorized(HubError):
    status = 401
    code = "unauthorized"


class Forbidden(HubError):
    status = 403
    code = "forbidden"


class NotFound(HubError):
    status = 404
    code = "not_found"


class Conflict(HubError):
    status = 409
    code = "conflict"


class DeviceUnavailable(HubError):
    status = 503
    code = "device_unavailable"


class UpstreamError(HubError):
    """上游（网易云等）不可用。"""

    status = 502
    code = "upstream_error"


class LoginRequired(HubError):
    status = 401
    code = "login_required"


def to_http(exc: BaseException) -> Dict[str, Any]:
    """异常 → HTTP 响应体（状态码由调用方从 ``status`` 取）。"""
    if isinstance(exc, HubError):
        return exc.to_dict()
    return {
        "ok": False,
        "error": "服务器内部错误",
        "code": "internal",
        "details": {"exception": exc.__class__.__name__},
    }


def http_status(exc: BaseException) -> int:
    return exc.status if isinstance(exc, HubError) else 500
