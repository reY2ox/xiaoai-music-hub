"""鉴权与安全工具：媒体令牌、Basic Auth、同源校验。

- 媒体令牌：DLNA 渲染器取流时无法携带 Basic Auth，用 ``sha256(password:track_id)`` 派生短令牌。
- Basic Auth：仅当配置了 ``UI_PASSWORD`` 时启用；比较使用 :func:`hmac.compare_digest`。
- 同源校验：变更类请求检查 ``Origin`` / ``Referer``，降低 GET/POST 被跨站触发的风险。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from typing import Mapping, Optional
from urllib.parse import urlsplit


def media_token(track_id: int, password: Optional[str]) -> str:
    """派生媒体令牌；未设置密码时返回空串（不校验）。"""
    if not password:
        return ""
    digest = hashlib.sha256(f"{password}:{int(track_id)}".encode("utf-8")).hexdigest()
    return digest[:16]


def verify_media_token(track_id: int, token: Optional[str], password: Optional[str]) -> bool:
    expected = media_token(track_id, password)
    if not expected:
        return True
    return hmac.compare_digest(str(token or ""), expected)


def basic_auth_credentials(header: Optional[str]) -> Optional[tuple]:
    """解析 ``Authorization: Basic ...``，返回 ``(user, password)``。"""
    if not header or not header.startswith("Basic "):
        return None
    try:
        raw = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None
    user, _, password = raw.partition(":")
    return user, password


def basic_auth_ok(header: Optional[str], password: Optional[str]) -> bool:
    """未配置密码时视为通过；配置后校验密码（用户名不限制）。"""
    if not password:
        return True
    credentials = basic_auth_credentials(header)
    if not credentials:
        return False
    return hmac.compare_digest(credentials[1], password)


def _host_only(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else f"//{value}")
    return (parsed.netloc or "").lower()


def same_origin(headers: Mapping[str, str], host: Optional[str]) -> bool:
    """变更请求的同源校验。

    无 ``Origin`` 且无 ``Referer``（curl / 脚本 / DLNA）视为通过；
    有其中任意一个时必须与 ``Host`` 一致。
    """
    if not host:
        return True
    host = host.lower()
    origin = headers.get("Origin") or headers.get("origin") or ""
    referer = headers.get("Referer") or headers.get("referer") or ""
    for value in (origin, referer):
        if not value or value == "null":
            continue
        if _host_only(value) != host:
            return False
    return True
