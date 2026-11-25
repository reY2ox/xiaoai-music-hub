"""DLNA / UPnP 渲染器控制层（v3）。

职责边界
--------
本模块**只跟音箱说话**：播放、暂停、停止、音量、Seek、状态查询、设备发现。
它不认识网易云、本地曲库、数据库，也不读 Web 层，因此可以用本地假 UPnP 设备完整测试。

设计要点（见 ``docs/DESIGN.md`` 4.6 / 5.3）
------------------------------------------
- :class:`Renderer` 默认按小爱音箱的约定使用 9999 端口；也可以传 ``host:port``，
  或显式给出 ``avt_control`` / ``rc_control`` 绝对 URL（:func:`discover` 的产物就是这种）。
- 每次 SOAP 调用都带超时；网络类错误重试 1 次，仍失败抛 503 ``device_unavailable``；
  HTTP 错误响应里若带 ``UPnPError``，解析 ``errorCode`` 后抛 502 ``dlna_error``。
- :meth:`Renderer.position_info` 合并 ``GetPositionInfo`` 与 ``GetTransportInfo``，
  元数据里的 ``dc:title`` / ``upnp:artist`` 做两次 HTML 反转义（设备常多转义一层）。
- :attr:`Renderer.supports_pause` / :attr:`Renderer.supports_seek` 是惰性能力缓存：
  对应动作第一次失败后就记住 False，避免每次都拿错误去试探设备。
- :func:`discover` 用 SSDP M-SEARCH 找 ``MediaRenderer:1``，按 UDN（无则 IP）去重；
  任何异常都只记日志并返回已发现的结果，绝不向调用方抛出。
"""
from __future__ import annotations

import html
import re
import socket
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import xml.sax.saxutils as saxutils
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

from . import config, logging_setup
from .errors import HubError

log = logging_setup.get_logger(__name__)

#: 小爱音箱等设备默认的 UPnP HTTP 端口
DEFAULT_PORT = 9999
#: SOAP 请求的 Content-Type
UA_HEADER = 'text/xml; charset="utf-8"'
#: AVTransport 服务类型（带版本号，SOAPACTION 里也是这个）
SERVICE_AVT = "AVTransport:1"
#: RenderingControl 服务类型
SERVICE_RC = "RenderingControl:1"
#: SSDP 多播地址
SSDP_ADDRESS: Tuple[str, int] = ("239.255.255.250", 1900)
#: M-SEARCH 的 ST（只要渲染器）
SSDP_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
#: SSDP 多播 TTL
MULTICAST_TTL = 2
#: 抓取 description.xml 的超时（秒）；discover 总耗时 ≈ timeout + 该值
DESC_TIMEOUT = 2.0
#: ping 的单次请求超时上限（秒），保证 /health 在离线设备上快速失败
PING_TIMEOUT = 2.0
#: 同一动作「设备不可达」WARNING 的最小间隔（秒），避免 watchdog 轮询刷屏
WARN_INTERVAL = 60.0
#: 单次发现最多处理的设备数
MAX_DEVICES = 16
#: 并发抓取 description 的线程数
MAX_FETCH_WORKERS = 8
#: 默认请求超时（config 取不到时兜底）
DEFAULT_TIMEOUT = 10.0
#: 配置异常时的兜底属性转义表（用于 XML 属性值）
_ATTR_ESCAPES = {'"': "&quot;"}
#: 一次 SSDP 接收的最长阻塞时间
_RECV_TIMEOUT = 0.5
#: 剥掉 XML 声明，避免 Unicode 串与声明里的 encoding 冲突
_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)
#: DIDL-Lite 元数据里判断是否已经反转义到标签层的标志
_METADATA_MARKERS = ("<dc:title", "<upnp:artist", "<DIDL", "<item")

_M_SEARCH_MESSAGE = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    f"ST: {SSDP_ST}\r\n"
    "\r\n"
).encode("utf-8")


class DLNAError(HubError):
    """DLNA / UPnP 交互失败。

    ``upnp_code`` 是设备 SOAP 错误里的 ``UPnPError/errorCode``（取不到为 ``None``）；
    ``status`` / ``code`` 供接口层直接映射：

    - 设备不可达（连接失败、超时、重试后仍失败）→ 503 ``device_unavailable``；
    - 设备返回错误（HTTP 500 / UPnPError）→ 502 ``dlna_error``。
    """

    status = 502
    code = "dlna_error"

    def __init__(
        self,
        message: str = "",
        *,
        upnp_code: Optional[int] = None,
        status: Optional[int] = None,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, status=status, code=code, details=details)
        self.upnp_code = upnp_code


@dataclass
class Device:
    """一台 DLNA 渲染器的静态信息（设备发现结果）。"""

    ip: str
    name: str = ""
    model: str = ""
    udn: str = ""
    avt_control: str = ""
    rc_control: str = ""

    def to_dict(self) -> Dict[str, str]:
        """API 层直接可用的字典。"""
        return {
            "ip": self.ip,
            "name": self.name,
            "model": self.model,
            "udn": self.udn,
            "avt_control": self.avt_control,
            "rc_control": self.rc_control,
        }


@dataclass
class PositionInfo:
    """一次播放状态快照（全部来自设备回报，不做推测）。"""

    state: str = "UNKNOWN"
    title: str = ""
    artist: str = ""
    duration: float = 0.0
    position: float = 0.0
    uri: str = ""
    duration_raw: str = ""
    position_raw: str = ""

    @property
    def playing(self) -> bool:
        """设备是否正在播放。"""
        return self.state == "PLAYING"

    @property
    def paused(self) -> bool:
        """设备是否处于暂停态。"""
        return self.state == "PAUSED_PLAYBACK"

    @property
    def stopped(self) -> bool:
        """设备是否停止（含无媒体）。"""
        return self.state in ("STOPPED", "NO_MEDIA_PRESENT")

    def to_dict(self) -> Dict[str, Any]:
        """API 层直接可用的字典。"""
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


# ---------------------------------------------------------------------------
# 数值与时间工具
# ---------------------------------------------------------------------------
def _to_float(value: Any, default: float = 0.0) -> float:
    """尽量把任意值转成 float，失败返回 ``default``。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """尽量把任意值转成 int，失败返回 ``default``。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _clamp_non_negative(value: Any) -> float:
    """转 float 并 clamp 到非负（非法值按 0 处理）。"""
    return max(0.0, _to_float(value, 0.0))


def _clamp_volume(level: Any) -> int:
    """音量 clamp 到 0-100（非法值按 0）。"""
    return max(0, min(100, int(_to_float(level, 0.0))))


def secs(ts: Any) -> float:
    """把设备回报的时间串转成秒。

    支持 ``"HH:MM:SS"``、``"HH:MM:SS.mmm"``、纯数字（如 ``"12"`` / ``12.5``）；
    ``"NOT_IMPLEMENTED"``、空串、``None`` 以及非法输入统一返回 ``0.0``。
    """
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return _clamp_non_negative(ts)
    text = str(ts).strip()
    if not text:
        return 0.0
    if text.count(":") == 2:
        hours, minutes, seconds = text.split(":")
        try:
            total = float(hours) * 3600 + float(minutes) * 60 + float(seconds)
        except ValueError:
            return 0.0
        return max(0.0, total)
    try:
        return max(0.0, float(text))
    except ValueError:
        return 0.0


def fmt_time(seconds: Any) -> str:
    """秒数 → ``"HH:MM:SS"``（小时可超过两位，非法值按 0）。"""
    total = int(_clamp_non_negative(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, sec = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}"


# ---------------------------------------------------------------------------
# XML 解析工具（命名空间不敏感）
# ---------------------------------------------------------------------------
def _local_name(tag: Any) -> str:
    """``{uri}Local`` / ``prefix:Local`` → ``Local``。"""
    if not isinstance(tag, str):
        return ""
    name = tag.rsplit("}", 1)[-1]
    if ":" in name:
        name = name.rsplit(":", 1)[-1]
    return name


def _parse_xml(text: str) -> Optional[ET.Element]:
    """解析 XML 文本（容忍 XML 声明与首尾空白）；失败返回 ``None``。"""
    if not text:
        return None
    cleaned = _XML_DECL_RE.sub("", text, count=1).strip()
    if not cleaned:
        return None
    try:
        return ET.fromstring(cleaned)
    except (ET.ParseError, ValueError):
        return None


def _iter_local(root: ET.Element, name: str) -> Iterator[ET.Element]:
    """遍历所有局部名等于 ``name`` 的元素（忽略命名空间）。"""
    for element in root.iter():
        if _local_name(element.tag) == name:
            yield element


def _child_elements(element: ET.Element, name: str) -> List[ET.Element]:
    """直接子元素中局部名等于 ``name`` 的元素。"""
    return [child for child in element if _local_name(child.tag) == name]


def _child_text(element: ET.Element, name: str) -> str:
    """取直接子元素（找不到再退化为后代）的文本，去首尾空白。"""
    for child in element:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    for descendant in _iter_local(element, name):
        return (descendant.text or "").strip()
    return ""


def _first_text(xml_text: str, name: str) -> str:
    """取第一个指定局部名元素的文本；XML 解析失败时退回正则。"""
    root = _parse_xml(xml_text)
    if root is not None:
        for element in _iter_local(root, name):
            return element.text or ""
    match = re.search(
        rf"<(?:\w+:)?{re.escape(name)}\b[^>]*>(.*?)</(?:\w+:)?{re.escape(name)}\s*>",
        xml_text or "",
        re.S,
    )
    return html.unescape(match.group(1)) if match else ""


def _unescape_twice(value: str) -> str:
    """连续两次 HTML 反转义（设备常把元数据多转义一层）。"""
    text = value or ""
    for _ in range(2):
        text = html.unescape(text)
    return text


def _decode_metadata(metadata: str) -> str:
    """逐层反转义 ``TrackMetaData``，直到露出 DIDL-Lite 标签为止。"""
    text = metadata or ""
    for _ in range(3):
        if not text or any(marker in text for marker in _METADATA_MARKERS):
            return text
        decoded = html.unescape(text)
        if decoded == text:
            return text
        text = decoded
    return text


def _metadata_field(metadata: str, tag: str) -> str:
    """从 DIDL-Lite 元数据里取一个标签的文本（反转义两次）。"""
    match = re.search(
        rf"<{re.escape(tag)}\b[^>]*>(.*?)</{re.escape(tag)}\s*>",
        metadata or "",
        re.S,
    )
    return _unescape_twice(match.group(1)) if match else ""


def _position_metadata(xml_text: str) -> Tuple[str, str]:
    """从 ``GetPositionInfo`` 响应里取 ``(title, artist)``。"""
    metadata = _decode_metadata(_first_text(xml_text, "TrackMetaData"))
    return _metadata_field(metadata, "dc:title"), _metadata_field(metadata, "upnp:artist")


def _upnp_error(xml_text: str) -> Tuple[Optional[int], str]:
    """从 UPnPError 响应里取 ``(errorCode, errorDescription)``。"""
    code = _to_int(_first_text(xml_text, "errorCode"))
    description = _first_text(xml_text, "errorDescription").strip()
    return code, description


def _build_soap_envelope(service: str, action: str, body: str = "") -> str:
    """拼 SOAP 1.1 请求信封。"""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}">'
        f"{body}</u:{action}></s:Body></s:Envelope>"
    )


def _build_didl_metadata(
    uri: str,
    title: str = "",
    artist: str = "",
    mime: str = "audio/mpeg",
) -> str:
    """构造 DIDL-Lite 元数据（标题/歌手/URL 都做 XML 转义）。"""
    protocol_info = f"http-get:*:{mime}:*"
    escaped_protocol = saxutils.escape(protocol_info, _ATTR_ESCAPES)
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="1" parentID="0" restricted="1">'
        f"<dc:title>{saxutils.escape(title)}</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f"<upnp:artist>{saxutils.escape(artist)}</upnp:artist>"
        f'<res protocolInfo="{escaped_protocol}">{saxutils.escape(uri)}</res>'
        "</item></DIDL-Lite>"
    )


# ---------------------------------------------------------------------------
# 控制 URL / 超时
# ---------------------------------------------------------------------------
def _split_endpoint(ip: str) -> Tuple[str, int]:
    """``"192.168.1.9"`` / ``"192.168.1.9:49152"`` → ``(host, port)``。

    只支持 IPv4（UPnP 局域网场景）；显式 ``http://host:port`` 也容忍。
    """
    text = str(ip or "").strip()
    if "://" in text:
        parsed = urlsplit(text)
        try:
            port = parsed.port or DEFAULT_PORT
        except ValueError:  # pragma: no cover - 非法端口
            port = DEFAULT_PORT
        return parsed.hostname or "", int(port)
    host, separator, port_text = text.rpartition(":")
    if separator and port_text.isdigit():
        return host or text, int(port_text)
    return text, DEFAULT_PORT


def _absolute_url(origin: str, path: str) -> str:
    """把 ``controlURL`` 拼成绝对 URL；已经是绝对 URL 时原样返回。"""
    text = (path or "").strip()
    if not text:
        return ""
    lower = text.lower()
    if lower.startswith(("http://", "https://")):
        return text
    if not origin:
        return text
    if text.startswith("//"):
        scheme = urlsplit(origin).scheme or "http"
        return f"{scheme}:{text}"
    return f"{origin.rstrip('/')}/{text.lstrip('/')}"


def _as_timeout(value: Any, default: float = DEFAULT_TIMEOUT) -> float:
    """把超时值转成不小于 0.1 秒的 float。"""
    return max(0.1, _to_float(value, default))


def _read_http_error(exc: urllib.error.HTTPError) -> str:
    """读 HTTP 错误响应的文本体（读不到就当空串）。"""
    try:
        return exc.read().decode("utf-8", "ignore")
    except (OSError, ValueError):  # pragma: no cover - 错误体读取失败
        return ""


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------
class Renderer:
    """单台 DLNA 渲染器（AVTransport + RenderingControl）。

    参数
    ----
    ip:
        纯 IP（默认端口 9999），或 ``host:port``。
    avt_control / rc_control:
        控制 URL。为空时按小爱约定用 ``http://<ip>:<port>/{AVTransport,RenderingControl}/control``；
        给了绝对 URL 时直接用（设备发现拿到的控制 URL 走这条路径）。
    timeout:
        单次 SOAP 请求超时；为 ``None`` 时用 ``config.get().request_timeout``。

    注意：实例只保存少量惰性能力缓存，不是线程安全的，并发控制应放在上层。
    """

    def __init__(
        self,
        ip: str,
        *,
        avt_control: str = "",
        rc_control: str = "",
        timeout: Optional[float] = None,
    ) -> None:
        self.ip, self.port = _split_endpoint(ip)
        self.base_url = f"http://{self.ip}:{self.port}"
        self.avt_control = _absolute_url(self.base_url, avt_control) or (
            f"{self.base_url}/{SERVICE_AVT.split(':', 1)[0]}/control"
        )
        self.rc_control = _absolute_url(self.base_url, rc_control) or (
            f"{self.base_url}/{SERVICE_RC.split(':', 1)[0]}/control"
        )
        self._timeout = timeout
        self._supports_pause: Optional[bool] = None
        self._supports_seek: Optional[bool] = None
        #: action → 上次「设备不可达」WARNING 的单调时间，用于日志节流
        self._warned_at: Dict[str, float] = {}

    # ---------- 能力（惰性缓存） ----------
    @property
    def supports_pause(self) -> bool:
        """是否支持 Pause：未知时乐观返回 True，Pause 失败后记住 False。"""
        return self._supports_pause is not False

    @property
    def supports_seek(self) -> bool:
        """是否支持 Seek：未知时乐观返回 True，Seek 失败后记住 False。"""
        return self._supports_seek is not False

    # ---------- 底层 SOAP ----------
    def _effective_timeout(self, timeout: Optional[float] = None) -> float:
        """``参数 > 实例 > config`` 的超时优先级。"""
        if timeout is None:
            timeout = self._timeout
        if timeout is None:
            timeout = getattr(config.get(), "request_timeout", DEFAULT_TIMEOUT)
        return _as_timeout(timeout)

    def _soap(
        self,
        control_url: str,
        service: str,
        action: str,
        body: str = "",
        timeout: Optional[float] = None,
        retry: bool = True,
    ) -> str:
        """发一个 SOAP 动作并返回响应文本。

        网络类错误自动重试 1 次（``retry=False`` 可关闭，供 :meth:`ping` 快速失败）；
        仍失败抛 :class:`DLNAError`（503 ``device_unavailable``）。

        日志策略：重试过程只打 DEBUG，最终失败按动作节流（同一动作
        :data:`WARN_INTERVAL` 秒内最多一条 WARNING），避免离线设备被 watchdog
        轮询时刷屏；调用方（如 player 的 ``_safe_position``）仍有自己的 debug 记录。

        HTTP 错误响应里的 ``UPnPError`` 会被解析成 :class:`DLNAError`
        （502 ``dlna_error``，带 ``upnp_code``）。
        """
        payload = _build_soap_envelope(service, action, body).encode("utf-8")
        action_header = f'"urn:schemas-upnp-org:service:{service}#{action}"'
        effective = self._effective_timeout(timeout)
        attempts = 2 if retry else 1
        last_error: Optional[BaseException] = None
        for attempt in range(attempts):
            request = urllib.request.Request(control_url, data=payload, method="POST")
            request.add_header("Content-Type", UA_HEADER)
            request.add_header("SOAPACTION", action_header)
            try:
                with urllib.request.urlopen(request, timeout=effective) as response:
                    return response.read().decode("utf-8", "ignore")
            except urllib.error.HTTPError as exc:
                raise self._http_error(action, exc.code, _read_http_error(exc)) from exc
            except (urllib.error.URLError, OSError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    log.debug("SOAP %s#%s 网络错误，准备重试: %s", service, action, exc)
                continue
        error = DLNAError(
            f"设备不可达: {self.ip}:{self.port}",
            status=503,
            code="device_unavailable",
            details={
                "ip": self.ip,
                "port": self.port,
                "action": action,
                "error": str(last_error or ""),
            },
        )
        self._log_unreachable(action, error)
        raise error from last_error

    def _log_unreachable(self, action: str, error: DLNAError) -> None:
        """设备不可达日志：同一动作 60s 内只 WARNING 一次，其余降为 DEBUG。"""
        now = time.monotonic()
        last = self._warned_at.get(action)
        if last is None or now - last >= WARN_INTERVAL:
            self._warned_at[action] = now
            log.warning("设备不可达（%s:%s，%s）: %s", self.ip, self.port, action, error.details)
        else:
            log.debug("设备不可达（%s:%s，%s）: %s", self.ip, self.port, action, error.details)

    def _http_error(self, action: str, http_status: int, text: str) -> DLNAError:
        """HTTP 错误响应 → :class:`DLNAError`（尽量带上 UPnP errorCode）。"""
        upnp_code, description = _upnp_error(text)
        details: Dict[str, Any] = {
            "ip": self.ip,
            "port": self.port,
            "action": action,
            "http_status": http_status,
        }
        if upnp_code is None:
            return DLNAError(
                f"设备返回 HTTP {http_status}（{action}）",
                status=502,
                code="dlna_error",
                details=details,
            )
        message = f"设备返回 SOAP 错误 {upnp_code}（{action}）"
        details["upnp_code"] = upnp_code
        if description:
            message = f"{message}: {description}"
            details["upnp_description"] = description
        return DLNAError(
            message,
            upnp_code=upnp_code,
            status=502,
            code="dlna_error",
            details=details,
        )

    def _av(self, action: str, extra: str = "") -> str:
        """AVTransport 动作（自动带 ``InstanceID=0``）。"""
        return self._soap(
            self.avt_control,
            SERVICE_AVT,
            action,
            f"<InstanceID>0</InstanceID>{extra}",
        )

    def _rc(self, action: str, extra: str = "") -> str:
        """RenderingControl 动作（自动带 ``InstanceID=0`` / ``Channel=Master``）。"""
        return self._soap(
            self.rc_control,
            SERVICE_RC,
            action,
            f"<InstanceID>0</InstanceID><Channel>Master</Channel>{extra}",
        )

    # ---------- 状态查询 ----------
    def transport_state(self) -> str:
        """当前传输状态：``PLAYING`` / ``PAUSED_PLAYBACK`` / ``STOPPED`` / ... 。

        设备不可达或返回错误时抛 :class:`DLNAError`；响应里取不到状态字段则返回
        ``"UNKNOWN"``。
        """
        text = self._av("GetTransportInfo")
        state = _first_text(text, "CurrentTransportState").strip()
        return state or "UNKNOWN"

    def position_info(self) -> PositionInfo:
        """合并 ``GetPositionInfo`` + ``GetTransportInfo`` 得到状态快照。

        任何一步 SOAP 失败都向上抛 :class:`DLNAError`；时间字段无法解析时按 0 处理。
        """
        text = self._av("GetPositionInfo")
        duration_raw = _first_text(text, "TrackDuration").strip()
        position_raw = _first_text(text, "RelTime").strip()
        uri = _first_text(text, "TrackURI").strip()
        title, artist = _position_metadata(text)
        return PositionInfo(
            state=self.transport_state(),
            title=title,
            artist=artist,
            duration=secs(duration_raw),
            position=secs(position_raw),
            uri=uri,
            duration_raw=duration_raw,
            position_raw=position_raw,
        )

    def ping(self) -> bool:
        """设备是否可达（``GetTransportInfo`` 成功即 True）。

        使用 ``min(超时, PING_TIMEOUT)`` 且**不重试**，保证设备离线时快速失败
        （``/health`` 依赖它，不能等完整请求超时）；任何失败都返回 False。
        """
        timeout = min(self._effective_timeout(), PING_TIMEOUT)
        try:
            self._soap(
                self.avt_control,
                SERVICE_AVT,
                "GetTransportInfo",
                "<InstanceID>0</InstanceID>",
                timeout=timeout,
                retry=False,
            )
        except (DLNAError, OSError):
            return False
        return True

    # ---------- 播放控制 ----------
    def play_url(
        self,
        uri: str,
        title: str = "",
        artist: str = "",
        mime: str = "audio/mpeg",
    ) -> str:
        """推流播放：``SetAVTransportURI``（带 DIDL-Lite 元数据）+ ``Play``。

        返回执行后的传输状态。
        """
        if not uri:
            raise DLNAError("播放地址为空", status=400, code="bad_request")
        metadata = _build_didl_metadata(uri, title=title, artist=artist, mime=mime)
        self._av(
            "SetAVTransportURI",
            f"<CurrentURI>{saxutils.escape(uri)}</CurrentURI>"
            f"<CurrentURIMetaData>{saxutils.escape(metadata)}</CurrentURIMetaData>",
        )
        self._av("Play", "<Speed>1</Speed>")
        return self.transport_state()

    def stop(self) -> str:
        """停止播放，返回最新状态。"""
        self._av("Stop")
        return self.transport_state()

    def pause(self) -> str:
        """暂停，返回最新状态。

        - 设备不可达（503）时抛 :class:`DLNAError`，便于接口层返回 ``device_unavailable``；
        - 设备可达但拒绝 ``Pause``（UPnPError 等 SOAP 错误）时不算致命：记住能力为 False，
          返回当前传输状态，交给上层走「Stop + 记录位置」的降级路径；
        - 已知不支持后不再重复发送 ``Pause``，只回读状态，避免反复试错。
        """
        if self._supports_pause is False:
            return self.transport_state()
        try:
            self._av("Pause")
        except DLNAError as exc:
            self._supports_pause = False
            if exc.status == 503:
                raise
            log.warning("设备拒绝 Pause（%s），交给上层降级: %s", self.ip, exc)
            return self.transport_state()
        self._supports_pause = True
        return self.transport_state()

    def resume(self) -> str:
        """继续播放（``Play``），返回最新状态。"""
        self._av("Play", "<Speed>1</Speed>")
        return self.transport_state()

    def seek(self, seconds: float) -> bool:
        """Seek 到指定秒数；失败返回 ``False``（不抛异常）并记住不支持 Seek。"""
        if self._supports_seek is False:
            return False
        try:
            target = fmt_time(max(0.0, _to_float(seconds, 0.0)))
            self._av("Seek", f"<Unit>REL_TIME</Unit><Target>{target}</Target>")
        except DLNAError as exc:
            self._supports_seek = False
            log.warning("Seek 失败（%s）: %s", self.ip, exc)
            return False
        self._supports_seek = True
        return True

    # ---------- 音量 ----------
    def set_volume(self, level: int) -> int:
        """设置主音量（clamp 到 0-100），返回实际发送的值。"""
        value = _clamp_volume(level)
        self._rc("SetVolume", f"<DesiredVolume>{value}</DesiredVolume>")
        return value

    def get_volume(self) -> Optional[int]:
        """读取主音量；字段解析失败返回 ``None``，网络/SOAP 错误抛 :class:`DLNAError`。"""
        text = self._rc("GetVolume")
        return _to_int(_first_text(text, "CurrentVolume"))


# ---------------------------------------------------------------------------
# 设备描述解析
# ---------------------------------------------------------------------------
def _find_renderer_device(root: ET.Element) -> Optional[ET.Element]:
    """在描述树里找 ``deviceType`` 含 ``MediaRenderer`` 的 device 元素。"""
    for element in _iter_local(root, "device"):
        if "MediaRenderer" in _child_text(element, "deviceType"):
            return element
    return None


def _service_control_url(device: ET.Element, service_type: str, origin: str) -> str:
    """在设备的 serviceList 里找指定服务类型的 controlURL（拼成绝对 URL）。"""
    for service_list in _child_elements(device, "serviceList"):
        for service in _child_elements(service_list, "service"):
            if service_type in _child_text(service, "serviceType"):
                return _absolute_url(origin, _child_text(service, "controlURL"))
    for service in _iter_local(device, "service"):
        if service_type in _child_text(service, "serviceType"):
            return _absolute_url(origin, _child_text(service, "controlURL"))
    return ""


def parse_device_description(xml_text: str, location: str) -> Optional[Device]:
    """解析 UPnP ``description.xml``，返回 MediaRenderer 设备。

    ``location`` 是该描述的 URL（SSDP 响应里的 ``LOCATION``）；相对的 ``controlURL``
    以它的 ``scheme://netloc`` 为基准拼成绝对 URL。

    非 MediaRenderer（``deviceType`` 不含该字样）或 XML 非法时返回 ``None``。
    """
    root = _parse_xml(xml_text or "")
    if root is None:
        log.warning("设备描述 XML 解析失败: %s", location)
        return None
    device = _find_renderer_device(root)
    if device is None:
        log.debug("跳过非 MediaRenderer 设备: %s", location)
        return None
    parts = urlsplit(location or "")
    origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
    return Device(
        ip=parts.hostname or "",
        name=_child_text(device, "friendlyName"),
        model=_child_text(device, "modelName"),
        udn=_child_text(device, "UDN"),
        avt_control=_service_control_url(device, "AVTransport", origin),
        rc_control=_service_control_url(device, "RenderingControl", origin),
    )


# ---------------------------------------------------------------------------
# SSDP 发现
# ---------------------------------------------------------------------------
def _ssdp_location(data: bytes) -> str:
    """从 SSDP 响应报文里取 ``LOCATION`` 头。"""
    text = data.decode("utf-8", "ignore")
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().upper() == "LOCATION":
            return value.strip()
    return ""


def _fetch_device(location: str) -> Optional[Device]:
    """抓一台设备的 description.xml 并解析；失败返回 ``None``（只记日志）。"""
    try:
        with urllib.request.urlopen(location, timeout=DESC_TIMEOUT) as response:
            text = response.read().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001 - 单台设备失败不能影响整体发现
        log.debug("抓取设备描述失败 %s: %s", location, exc)
        return None
    device = parse_device_description(text, location)
    if device is not None and not device.name:
        device.name = device.ip
    return device


def _dedupe_devices(devices: Iterable[Device]) -> List[Device]:
    """按 UDN（无则 IP）去重，按 key 排序保证结果稳定。"""
    found: Dict[str, Device] = {}
    for index, device in enumerate(devices):
        key = device.udn or device.ip or f"unknown-{index}"
        found.setdefault(key, device)
    return [found[key] for key in sorted(found)]


def _fetch_descriptions(locations: List[str]) -> List[Device]:
    """并发抓取所有 description，返回解析成功且保序的设备列表。"""
    devices: List[Optional[Device]] = []
    workers = min(MAX_FETCH_WORKERS, len(locations))
    if workers <= 1:
        for location in locations:
            devices.append(_fetch_device(location))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_fetch_device, location) for location in locations]
            for future in futures:
                try:
                    devices.append(future.result())
                except Exception as exc:  # pragma: no cover - _fetch_device 已兜底
                    log.debug("抓取设备描述异常: %s", exc)
                    devices.append(None)
    return [device for device in devices if device is not None]


def _discover(timeout: float) -> List[Device]:
    """``discover`` 的实际实现（异常处理在最外层）。"""
    deadline = time.monotonic() + max(0.1, _to_float(timeout, 5.0))
    locations: List[str] = []
    seen = set()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL)
        sock.settimeout(_RECV_TIMEOUT)
        sock.sendto(_M_SEARCH_MESSAGE, SSDP_ADDRESS)
        while len(locations) < MAX_DEVICES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(_RECV_TIMEOUT, max(0.05, remaining)))
            try:
                data, _address = sock.recvfrom(65535)
            except OSError as exc:
                if locations:
                    break
                if time.monotonic() >= deadline:
                    break
                log.debug("SSDP 接收超时: %s", exc)
                continue
            location = _ssdp_location(data)
            if location and location not in seen:
                seen.add(location)
                locations.append(location)
    except OSError as exc:
        log.warning("SSDP 发现失败: %s", exc)
        return []
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover - 关闭失败无需处理
                log.debug("关闭 SSDP socket 失败")
    if not locations:
        return []
    return _dedupe_devices(_fetch_descriptions(locations))


def discover(timeout: float = 5.0) -> List[Device]:
    """SSDP M-SEARCH 发现局域网内的 MediaRenderer。

    使用 ``ST=MediaRenderer:1``、``MX=2``、多播 TTL=2；收集 ``LOCATION`` 后抓取并解析
    ``description.xml``，按 UDN（无则 IP）去重。单台设备失败只跳过该设备，
    任何异常都不会抛给调用方，最坏返回 ``[]``。
    """
    try:
        return _discover(timeout)
    except Exception as exc:  # noqa: BLE001 - 发现失败绝不影响主流程
        log.warning("设备发现失败: %s", exc)
        return []


__all__ = [
    "DLNAError",
    "Device",
    "PositionInfo",
    "Renderer",
    "discover",
    "fmt_time",
    "parse_device_description",
    "secs",
]
