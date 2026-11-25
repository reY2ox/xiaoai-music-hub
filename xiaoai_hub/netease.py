"""网易云接入层：搜索、播放直链、账号、歌单与扫码登录。

设计要点
--------
- **统一会话**：所有请求都经 :func:`_request_json` 发出，统一 UA/Referer/Cookie 与超时；
  连接失败、超时与 5xx 最多重试 2 次（0.5s、1.5s 指数退避）。
- **账号缓存**：:func:`account` 的结果按 TTL 缓存（默认 ``config.get().netease_account_ttl``），
  :func:`account_cached` 只读缓存、**不产生网络请求**，供 ``/state`` 之类高频接口使用；
  登录（写 cookie）与登出时自动失效。
- **错误体系**：统一抛 :class:`NeteaseError`，保留网易云返回码并映射为
  ``login_required``(401) / ``vip_required``(403) / ``upstream_error``(502)。
- **音质降级**：:func:`song_url_auto` 从用户当前档位起按 ``lossless → high → standard``
  依次尝试，返回首个可用直链并标注实际档位。
- **歌单**：列表接口按 ``more`` 翻页；曲目接口在 ``trackCount`` 超过单页时改用
  ``/api/playlist/track/all`` 分页拉全。
- **扫码登录**：:func:`qr_start` 生成 PNG 并记住扫码会话 cookie，:func:`qr_poll` 轮询状态，
  803 成功时从 ``Set-Cookie`` 取出 ``MUSIC_U`` 原子写入 cookie 文件。

模块级状态（``configure()`` 可覆盖）仅用于测试注入；生产使用 :func:`configure` 之外的默认路径。
"""
from __future__ import annotations

import http.client
import json
import os
import random
import string
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config, logging_setup
from .errors import HubError

log = logging_setup.get_logger(__name__)

__all__ = [
    "Account",
    "NeteaseError",
    "Playlist",
    "Song",
    "SongUrl",
    "account",
    "account_cached",
    "configure",
    "cookie",
    "current_uid",
    "logout",
    "playlist_tracks",
    "playlists",
    "qr_poll",
    "qr_start",
    "reset_cache",
    "save_cookie",
    "search",
    "song_url",
    "song_url_auto",
]

#: 与 v2 保持一致的浏览器 UA（网易云对 UA 敏感）
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
REFERER = "https://music.163.com/"

#: 重试退避秒数：最多重试 2 次
_RETRY_DELAYS: Tuple[float, ...] = (0.5, 1.5)

#: 歌单/曲目分页大小
_PAGE_SIZE = 1000
#: 歌单列表最多翻页数（防上游 ``more`` 异常导致死循环）
_MAX_PLAYLIST_PAGES = 20
#: 曲目分页最多页数（1000 首/页，20 页即 2 万首上限）
_MAX_TRACK_PAGES = 20

#: 音质降级顺序（高档 → 低档）
_QUALITY_DOWNGRADE: Tuple[str, ...] = ("lossless", "high", "standard")

#: 网易云「需要登录」返回码
_LOGIN_REQUIRED_CODES = (-110, -460)
#: 网易云「需要 VIP / 版权受限」返回码
_VIP_REQUIRED_CODES = (301,)

#: 网易云返回码 → 面向用户的中文提示
_DEFAULT_MESSAGES: Dict[int, str] = {
    -110: "网易云登录状态已失效，请重新扫码登录",
    -460: "网易云登录状态异常，请重新扫码登录",
    301: "该歌曲需要网易云 VIP 或受版权限制",
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Song:
    """一首网易云歌曲（搜索结果 / 歌单曲目的统一形态）。"""

    song_id: int
    name: str
    artist: str = ""
    album: str = ""
    fee: Optional[int] = None
    duration: int = 0
    cover: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转成接口层 JSON（字段名见 docs/API.md）。"""
        return {
            "song_id": self.song_id,
            "name": self.name,
            "artist": self.artist,
            "album": self.album,
            "fee": self.fee,
            "duration": self.duration,
            "cover": self.cover,
        }


@dataclass
class SongUrl:
    """播放直链查询结果；``ok=False`` 表示无可用直链（VIP / 版权 / 下架）。"""

    url: str = ""
    code: Optional[int] = None
    br: int = 0
    ext: str = "mp3"
    quality: str = ""
    ok: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """转成接口层 JSON。"""
        return {
            "url": self.url,
            "code": self.code,
            "br": self.br,
            "ext": self.ext,
            "quality": self.quality,
            "ok": self.ok,
        }


@dataclass
class Account:
    """网易云账号信息。"""

    logged_in: bool = False
    uid: Optional[int] = None
    nickname: str = ""
    avatar: str = ""
    vip: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转成接口层 JSON（``ok`` 与 ``logged_in`` 同义，兼容 v2）。"""
        return {
            "ok": self.logged_in,
            "logged_in": self.logged_in,
            "uid": self.uid,
            "nickname": self.nickname,
            "avatar": self.avatar,
            "vip": self.vip,
            "reason": self.reason,
        }


@dataclass
class Playlist:
    """网易云歌单（远端）。"""

    remote_id: int
    name: str
    count: int = 0
    cover: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转成接口层 JSON；键为 ``id/name/count/cover``（见 docs/API.md）。"""
        return {
            "id": self.remote_id,
            "name": self.name,
            "count": self.count,
            "cover": self.cover,
        }


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
def _to_int(value: Any) -> Optional[int]:
    """尽力转 int，失败返回 ``None``。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _classify_code(code: Optional[int]) -> Tuple[int, str]:
    """网易云返回码 → ``(HTTP 状态码, 机器可读错误码)``。"""
    if code in _LOGIN_REQUIRED_CODES:
        return 401, "login_required"
    if code in _VIP_REQUIRED_CODES:
        return 403, "vip_required"
    return 502, "upstream_error"


def _default_message(code: Optional[int]) -> str:
    if code is None:
        return "网易云接口请求失败"
    return _DEFAULT_MESSAGES.get(code, f"网易云接口返回错误（code={code}）")


class NeteaseError(HubError):
    """网易云接口异常。

    Attributes:
        code: 网易云返回码（无返回码时为 ``None``）。
        error_code: 对外的机器可读错误码（``login_required`` / ``vip_required`` /
            ``upstream_error``），用于 HTTP 响应体的 ``code`` 字段。
        status: 建议的 HTTP 状态码（401 / 403 / 502）。
        message: 面向用户的中文提示，可直接展示。
    """

    def __init__(
        self,
        code: Optional[int] = None,
        message: str = "",
        *,
        status: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        numeric = _to_int(code)
        mapped_status, error_code = _classify_code(numeric)
        super().__init__(message or _default_message(numeric),
                         status=status or mapped_status, details=details)
        self.code = numeric
        self.error_code = error_code

    def to_dict(self) -> Dict[str, Any]:
        """对外 JSON：``code`` 使用机器可读字符串（网易云原始返回码保留在属性上）。"""
        payload = super().to_dict()
        payload["code"] = self.error_code
        return payload


# ---------------------------------------------------------------------------
# 模块级会话状态（测试用 configure() 覆盖）
# ---------------------------------------------------------------------------
_base: Optional[str] = None
_cookie_file: Optional[str] = None
_timeout: Optional[float] = None
_account_ttl: Optional[float] = None

#: 账号信息缓存（TTL 由 config / configure 决定）
_account_cache: Optional[Account] = None
_account_cached_at: float = 0.0
#: 扫码登录会话 cookie（qr_start 记录，qr_poll 复用，保证两次请求同一会话）
_qr_session_cookie: Optional[str] = None

#: 账号缓存锁：并发调用只发一次网络请求
_state_lock = threading.Lock()

#: 可注入的时钟与休眠（测试替换，避免真实等待）
_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep


def configure(
    base: Optional[str] = None,
    cookie_file: Optional[str] = None,
    timeout: Optional[float] = None,
    account_ttl: Optional[float] = None,
) -> None:
    """覆盖会话配置并清空缓存（测试用；参数传 ``None`` 表示恢复 config 默认值）。

    Args:
        base: 网易云 API 根地址，如 ``http://127.0.0.1:12345``。
        cookie_file: cookie 文件路径。
        timeout: 单次请求超时秒数。
        account_ttl: 账号信息缓存秒数；传 ``0`` 表示每次都重新请求。
    """
    global _base, _cookie_file, _timeout, _account_ttl
    _base = base.rstrip("/") if base else None
    _cookie_file = cookie_file
    _timeout = timeout
    _account_ttl = account_ttl
    log.debug("网易云配置已重置：base=%s cookie_file=%s", _base, _cookie_file)
    reset_cache()


def reset_cache() -> None:
    """清空账号缓存与扫码会话（登录/登出/测试使用）。"""
    global _qr_session_cookie
    _invalidate_account_cache()
    _qr_session_cookie = None


def _invalidate_account_cache() -> None:
    """只清账号缓存（写新 cookie 时调用，保留当前扫码会话）。"""
    global _account_cache, _account_cached_at
    with _state_lock:
        _account_cache = None
        _account_cached_at = 0.0


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------
def _base_url() -> str:
    return (_base or config.get().netease_base).rstrip("/")


def _cookie_path() -> str:
    return _cookie_file or config.get().paths.cookie_file


def _request_timeout() -> float:
    if _timeout is not None:
        return float(_timeout)
    return float(config.get().netease_timeout)


def _account_ttl_value() -> float:
    if _account_ttl is not None:
        return float(_account_ttl)
    return float(config.get().netease_account_ttl)


def _current_quality() -> str:
    """用户当前音质档位；读取失败（如数据库未就绪）时回退默认档。"""
    settings = config.get()
    name = ""
    try:
        # 延迟导入：prefs 位于领域层，能力层只在运行时取偏好，避免模块级层级倒置
        from . import prefs

        name = str(prefs.quality() or "")
    except Exception as exc:  # pragma: no cover - 仅在数据库异常时触发
        log.debug("读取音质偏好失败，回退默认档：%s", exc)
    if name not in settings.quality_levels:
        name = settings.default_quality
    return name


def _normalise_quality(name: Optional[str]) -> str:
    settings = config.get()
    if name and name in settings.quality_levels:
        return name
    return _current_quality()


# ---------------------------------------------------------------------------
# Cookie
# ---------------------------------------------------------------------------
def cookie() -> str:
    """读取 cookie 文本；文件不存在或内容为空时返回 ``"os=pc"``。"""
    path = _cookie_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return "os=pc"
    return text or "os=pc"


def save_cookie(text: str) -> None:
    """原子写入 cookie 文件（临时文件 + ``os.replace``），权限 ``0600``。

    写入成功后立即失效账号缓存，保证下一次 :func:`account` 重新拉取。
    """
    content = (text or "").strip()
    path = _cookie_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(prefix=".netease-cookie-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:  # pragma: no cover - 清理失败不影响主流程
                pass
    log.info("网易云 cookie 已保存（%d 字节）", len(content))
    _invalidate_account_cache()


def logout() -> None:
    """登出：删除 cookie 文件并清空所有缓存（不在文件里写空串）。"""
    path = _cookie_path()
    try:
        os.remove(path)
        log.info("网易云 cookie 已删除：%s", path)
    except FileNotFoundError:
        log.debug("网易云 cookie 不存在，无需删除：%s", path)
    except OSError as exc:
        log.warning("删除网易云 cookie 失败：%s", exc)
    reset_cache()


def _has_login_cookie() -> bool:
    """是否持有可用于请求账号信息的 cookie（默认 ``os=pc`` 视为未登录）。"""
    text = cookie()
    return bool(text) and text != "os=pc"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {"User-Agent": UA, "Referer": REFERER, "Cookie": cookie()}
    if extra:
        headers.update(extra)
    return headers


def _request_json(
    path: str,
    data: Optional[Dict[str, Any]] = None,
    method: str = "POST",
    headers: Optional[Dict[str, str]] = None,
    *,
    capture_headers: bool = False,
) -> Any:
    """发起一次网易云请求并解析 JSON。

    Args:
        path: 以 ``/`` 开头的接口路径，或完整 URL。
        data: 表单参数；``GET`` 时拼到 query，其它方法作为 form body。
        method: HTTP 方法（默认 ``POST``）。
        headers: 追加/覆盖的请求头。
        capture_headers: 为 ``True`` 时返回 ``(json, set_cookie_headers)``，
            供扫码轮询读取 ``Set-Cookie``（其余调用只返回 JSON）。

    Returns:
        解析后的 JSON 对象；``capture_headers=True`` 时返回二元组。

    Raises:
        NeteaseError: 连接失败（已重试）、HTTP 5xx/4xx 或 JSON 解析失败。
    """
    url = path if "://" in path else _base_url() + path
    payload: Optional[bytes] = None
    if method.upper() == "GET":
        if data:
            query = urllib.parse.urlencode(data, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
    elif data:
        payload = urllib.parse.urlencode(data, doseq=True).encode("utf-8")

    request_headers = _headers(headers)
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    body = b""
    set_cookies: List[str] = []
    attempt = 0
    while True:
        try:
            request = urllib.request.Request(
                url, data=payload, headers=request_headers, method=method.upper()
            )
            with urllib.request.urlopen(request, timeout=_request_timeout()) as response:
                body = response.read()
                set_cookies = list(response.headers.get_all("Set-Cookie") or [])
            break
        except urllib.error.HTTPError as exc:
            retryable = 500 <= int(exc.code) < 600 and attempt < len(_RETRY_DELAYS)
            if not retryable:
                message = f"网易云接口异常（HTTP {exc.code}）"
                exc.close()
                raise NeteaseError(None, message) from exc
            delay = _RETRY_DELAYS[attempt]
            log.warning(
                "网易云接口 HTTP %s，%.1fs 后重试（第 %s 次）",
                exc.code,
                delay,
                attempt + 1,
            )
            exc.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            if attempt >= len(_RETRY_DELAYS):
                raise NeteaseError(None, "无法连接网易云，请检查网络后重试") from exc
            delay = _RETRY_DELAYS[attempt]
            log.warning("网易云连接异常，%.1fs 后重试（第 %s 次）：%s", delay, attempt + 1, exc)
        _sleep(delay)
        attempt += 1

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise NeteaseError(None, "网易云返回数据解析失败") from exc
    if capture_headers:
        return parsed, set_cookies
    return parsed


def _require_ok(payload: Any) -> Dict[str, Any]:
    """校验网易云顶层返回码，非 200 抛映射后的 :class:`NeteaseError`。"""
    if not isinstance(payload, dict):
        raise NeteaseError(None, "网易云返回数据格式异常")
    code = _to_int(payload.get("code"))
    if code is None or code == 200:
        return payload
    message = str(payload.get("message") or payload.get("msg") or "")
    raise NeteaseError(code, message)


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------
def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value > 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_song(raw: Dict[str, Any]) -> Song:
    """解析网易云歌曲对象（``ar`` 歌手列表 / ``al`` 专辑 / ``dt`` 毫秒时长）。"""
    artists = raw.get("ar") or []
    artist = "/".join(
        str(item.get("name") or "") for item in artists if isinstance(item, dict)
    )
    album_raw = raw.get("al") or {}
    album = album_raw if isinstance(album_raw, dict) else {}
    duration_ms = _to_int(raw.get("dt")) or 0
    return Song(
        song_id=_to_int(raw.get("id")) or 0,
        name=str(raw.get("name") or ""),
        artist=artist,
        album=str(album.get("name") or ""),
        fee=_to_int(raw.get("fee")),
        duration=duration_ms // 1000,
        cover=str(album.get("picUrl") or ""),
    )


def _parse_playlist(raw: Dict[str, Any]) -> Playlist:
    """解析网易云歌单对象。"""
    return Playlist(
        remote_id=_to_int(raw.get("id")) or 0,
        name=str(raw.get("name") or ""),
        count=_to_int(raw.get("trackCount")) or 0,
        cover=str(raw.get("coverImgUrl") or ""),
    )


def _quality_from_br(br: int, fallback: str) -> str:
    """根据返回码率推断实际音质档位；无法判断时使用请求档位。"""
    levels = config.get().quality_levels
    lossless_br = _to_int((levels.get("lossless") or {}).get("br")) or 999000
    high_br = _to_int((levels.get("high") or {}).get("br")) or 320000
    if br <= 0:
        return fallback
    if br >= int(lossless_br * 0.9):
        return "lossless"
    if br >= int(high_br * 0.9):
        return "high"
    return "standard"


def _quality_sequence(start: str) -> List[str]:
    """从当前档位开始的降级序列，如 ``high → [high, standard]``。"""
    order = list(_QUALITY_DOWNGRADE)
    if start in order:
        order = order[order.index(start):]
    return order


def _extract_music_u(set_cookies: List[str]) -> str:
    """从 ``Set-Cookie`` 头列表里取出 ``MUSIC_U`` 的值。"""
    for raw in set_cookies:
        for part in str(raw).split(";"):
            piece = part.strip()
            if piece.startswith("MUSIC_U="):
                return piece.split("=", 1)[1].strip().strip('"')
    return ""


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
def search(query: str, limit: int = 10, offset: int = 0) -> List[Song]:
    """搜索歌曲。

    Args:
        query: 关键词（歌名 / 歌手 / 专辑）；空白时直接返回 ``[]``，不发请求。
        limit: 返回条数，默认 10。
        offset: 偏移量，默认 0。

    Returns:
        :class:`Song` 列表；无结果时为 ``[]``。

    Raises:
        NeteaseError: 上游返回错误码或网络失败。
    """
    text = (query or "").strip()
    if not text:
        return []
    payload = _require_ok(
        _request_json(
            "/api/cloudsearch/pc",
            {"s": text, "type": 1, "limit": int(limit), "offset": int(offset)},
        )
    )
    result = payload.get("result") or {}
    raw_songs = result.get("songs") or []
    return [_parse_song(item) for item in raw_songs if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# 播放直链
# ---------------------------------------------------------------------------
def song_url(song_id: int, quality: Optional[str] = None) -> SongUrl:
    """查询单曲播放直链。

    Args:
        song_id: 网易云歌曲 ID。
        quality: 音质档位（``standard`` / ``high`` / ``lossless``）；
            缺省用用户当前档位。

    Returns:
        :class:`SongUrl`；无直链时 ``ok=False``，并保留网易云返回的 ``code``。

    Raises:
        NeteaseError: 上游错误码（如 301 需要 VIP）或网络失败。
    """
    quality_name = _normalise_quality(quality)
    br = int(config.quality_br(quality_name))
    fallback_ext = str(config.quality_ext(quality_name))
    payload = _require_ok(
        _request_json(
            "/api/song/enhance/player/url",
            {"ids": f"[{int(song_id)}]", "br": br},
        )
    )
    items = payload.get("data") or []
    item = items[0] if items and isinstance(items[0], dict) else {}
    url = str(item.get("url") or "")
    code = _to_int(item.get("code"))
    if code is None:
        code = _to_int(payload.get("code"))
    actual_br = _to_int(item.get("br")) or (br if url else 0)
    ext = str(item.get("type") or fallback_ext) if url else fallback_ext
    return SongUrl(
        url=url,
        code=code,
        br=int(actual_br),
        ext=ext,
        quality=_quality_from_br(int(actual_br), quality_name),
        ok=bool(url),
    )


def song_url_auto(song_id: int) -> SongUrl:
    """自动降级取直链：从用户当前档位起依次尝试更低档位，返回首个可用结果。

    尝试顺序示例：当前 ``lossless`` → lossless / high / standard；
    当前 ``high`` → high / standard；当前 ``standard`` → standard。
    全部失败时返回最后一次尝试的结果。
    """
    attempts = _quality_sequence(_current_quality())
    last = SongUrl(quality=attempts[-1])
    for name in attempts:
        result = song_url(song_id, name)
        if result.ok:
            return result
        last = result
    return last


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------
def _fetch_account() -> Account:
    """请求 ``/api/nuser/account/get`` 并解析账号信息。"""
    payload = _require_ok(_request_json("/api/nuser/account/get", method="GET"))
    profile = payload.get("profile") or {}
    uid = _to_int(profile.get("userId"))
    if not uid:
        return Account(logged_in=False, reason="cookie 已失效，请重新扫码登录")
    info = payload.get("account") or {}
    vip = _as_bool(info.get("vipType")) or _as_bool(profile.get("vipType"))
    return Account(
        logged_in=True,
        uid=uid,
        nickname=str(profile.get("nickname") or ""),
        avatar=str(profile.get("avatarUrl") or ""),
        vip=vip,
        reason="",
    )


def account(force: bool = False) -> Account:
    """获取当前账号信息（带 TTL 缓存，线程安全）。

    Args:
        force: 为 ``True`` 时绕过缓存强制请求。

    Returns:
        :class:`Account`；未登录时 ``logged_in=False`` 并给出 ``reason``。

    Raises:
        NeteaseError: 上游返回错误码（如 -110 登录失效 → 401 ``login_required``）。
    """
    global _account_cache, _account_cached_at
    with _state_lock:
        if not force and _account_cache is not None:
            if _clock() - _account_cached_at < _account_ttl_value():
                return _account_cache
        if not _has_login_cookie():
            info = Account(logged_in=False, reason="未登录，请扫码登录")
        else:
            info = _fetch_account()
        _account_cache = info
        _account_cached_at = _clock()
        return info


def account_cached() -> Optional[Account]:
    """读取账号缓存（**不产生网络请求**，可能返回 ``None`` 或过期快照）。"""
    with _state_lock:
        return _account_cache


def current_uid(force: bool = False) -> Optional[int]:
    """获取当前登录用户 ID；未登录或请求失败返回 ``None``（不抛异常）。"""
    try:
        info = account(force=force)
    except NeteaseError as exc:
        log.warning("获取网易云账号失败：%s", exc.message)
        return None
    return info.uid if info.logged_in else None


# ---------------------------------------------------------------------------
# 歌单
# ---------------------------------------------------------------------------
def playlists() -> List[Playlist]:
    """获取当前账号的全部歌单（按 ``more`` 自动翻页）。

    Raises:
        NeteaseError: 未登录（401 ``login_required``）或上游错误。
    """
    info = account()
    if not info.logged_in or not info.uid:
        raise NeteaseError(-110, "需要登录网易云后才能读取歌单")
    uid = int(info.uid)
    result: List[Playlist] = []
    offset = 0
    for _page in range(_MAX_PLAYLIST_PAGES):
        payload = _require_ok(
            _request_json(
                "/api/user/playlist",
                {"uid": uid, "limit": _PAGE_SIZE, "offset": offset},
                method="GET",
            )
        )
        items = payload.get("playlist") or []
        result.extend(_parse_playlist(item) for item in items if isinstance(item, dict))
        if not items or not _as_bool(payload.get("more")):
            break
        # offset 语义是「已获取条数」：按实际返回条数推进，服务端返回不足一页时也不会跳过数据
        offset += len(items)
    else:
        log.warning("网易云歌单超过 %s 页，已截断", _MAX_PLAYLIST_PAGES)
    return result


def _fetch_all_tracks(remote_id: int, track_count: int) -> List[Song]:
    """用 ``/api/playlist/track/all`` 分页拉取完整曲目（每页 1000，最多 20 页）。"""
    songs: List[Song] = []
    offset = 0
    for _page in range(_MAX_TRACK_PAGES):
        payload = _require_ok(
            _request_json(
                "/api/playlist/track/all",
                {"id": remote_id, "limit": _PAGE_SIZE, "offset": offset},
                method="GET",
            )
        )
        items = payload.get("songs") or []
        songs.extend(_parse_song(item) for item in items if isinstance(item, dict))
        if not items:
            break
        offset += len(items)
        if len(songs) >= track_count:
            break
    else:
        log.warning("歌单 %s 曲目超过 %s 页，已截断", remote_id, _MAX_TRACK_PAGES)
    return songs


def playlist_tracks(remote_id: int) -> List[Song]:
    """获取远端歌单的全部曲目。

    先用 ``/api/v3/playlist/detail`` 取首屏；若 ``trackCount`` 大于实际返回条数，
    改用 ``/api/playlist/track/all`` 分页拉全（防止 1000 首以上的歌单被截断）。

    Raises:
        NeteaseError: 上游错误码或网络失败。
    """
    rid = int(remote_id)
    payload = _require_ok(
        _request_json(
            "/api/v3/playlist/detail",
            {"id": rid, "n": _PAGE_SIZE},
            method="GET",
        )
    )
    detail = payload.get("playlist") or {}
    raw_tracks = detail.get("tracks") or []
    songs = [_parse_song(item) for item in raw_tracks if isinstance(item, dict)]
    track_count = _to_int(detail.get("trackCount")) or 0
    if track_count <= len(songs):
        return songs
    log.debug("歌单 %s 共 %s 首、首屏 %s 首，改用分页接口", rid, track_count, len(songs))
    return _fetch_all_tracks(rid, track_count)


# ---------------------------------------------------------------------------
# 扫码登录
# ---------------------------------------------------------------------------
def _random_token(length: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def qr_start() -> Dict[str, Any]:
    """开始扫码登录：取 ``unikey`` 并生成二维码 PNG。

    Returns:
        成功 ``{"key": ..., "png_path": ...}``；失败 ``{"error": "...", "key": ...}``。
        缺少 segno 依赖时返回 ``{"error": "缺少 segno 依赖，请 pip install segno", "key": ...}``。
    """
    global _qr_session_cookie
    session_cookie = (
        f"os=pc; _ntes_nuid={_random_token(32)}; "
        f"NMTID={_random_token(20)}; appver=2.9.7"
    )
    try:
        payload = _request_json(
            "/api/login/qrcode/unikey?type=1",
            method="GET",
            headers={"Cookie": session_cookie},
        )
    except NeteaseError as exc:
        log.warning("获取网易云登录二维码失败：%s", exc.message)
        return {"error": exc.message}
    if not isinstance(payload, dict):
        return {"error": "网易云返回数据格式异常"}
    code = _to_int(payload.get("code"))
    key = payload.get("unikey") or payload.get("key")
    if code not in (None, 200) or not key:
        return {"error": str(payload.get("message") or "获取登录二维码失败")}
    key_text = str(key)
    # 记住本次扫码会话 cookie，轮询时复用，保证与生成 unikey 时同一会话
    _qr_session_cookie = session_cookie
    png_path = os.path.join(config.get().paths.cache_dir, "netease_qr.png")
    try:
        import segno
    except ImportError:
        log.warning("缺少 segno 依赖，无法生成网易云登录二维码")
        return {"error": "缺少 segno 依赖，请 pip install segno", "key": key_text}
    try:
        os.makedirs(os.path.dirname(png_path), exist_ok=True)
        segno.make(f"https://music.163.com/login?codekey={key_text}", error="m").save(
            png_path, scale=8, border=3
        )
    except Exception as exc:  # 生成失败时至少把 key 返回给前端展示登录链接
        log.warning("生成网易云登录二维码失败：%s", exc)
        return {"error": f"生成二维码失败：{exc}", "key": key_text}
    log.info("网易云登录二维码已生成：%s", png_path)
    return {"key": key_text, "png_path": png_path}


def qr_poll(key: str, headers: Optional[Dict[str, str]] = None) -> Tuple[int, bool]:
    """轮询扫码状态。

    Args:
        key: :func:`qr_start` 返回的 ``unikey``。
        headers: 额外请求头；不传时复用扫码会话 cookie。

    Returns:
        ``(code, saved)``：``code`` 为 800/801/802/803；``saved=True`` 表示
        803 成功且已从 ``Set-Cookie`` 取出 ``MUSIC_U`` 写入 cookie 文件。
    """
    default_cookie = _qr_session_cookie or "os=pc; appver=2.9.7"
    request_headers: Dict[str, str] = {"Cookie": default_cookie}
    if headers:
        request_headers.update(headers)
    payload, set_cookies = _request_json(
        "/api/login/qrcode/client/login",
        {"key": key, "type": 1},
        method="GET",
        headers=request_headers,
        capture_headers=True,
    )
    code = _to_int(payload.get("code")) or 0
    if code != 803:
        return code, False
    music_u = _extract_music_u(set_cookies)
    if not music_u:
        log.warning("扫码登录成功但响应未携带 MUSIC_U cookie")
        return code, False
    save_cookie(f"MUSIC_U={music_u}; os=pc")
    log.info("网易云扫码登录成功，cookie 已保存")
    return code, True
