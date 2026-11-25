"""``xiaoai_hub.dlna`` 单元测试。

测试策略
--------
- 用 ``ThreadingHTTPServer`` 在 127.0.0.1 的随机端口上起一个**假 UPnP 渲染器**，
  SOAP 请求/响应走真实 HTTP（覆盖请求头、XML 拼装、响应解析、错误码）；
- SSDP 发现用 monkeypatch 替换 ``dlna.socket`` 与 ``urllib.request.urlopen``，
  不发真实多播、不依赖外网，结果完全确定；
- 无需真实音箱、无需联网，单文件运行应在 20s 内完成。
"""
from __future__ import annotations

import http.server
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import xml.sax.saxutils as saxutils
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytest
from xiaoai_hub import dlna
from xiaoai_hub.errors import HubError

SERVICE_AVT_URN = "urn:schemas-upnp-org:service:AVTransport:1"
SERVICE_RC_URN = "urn:schemas-upnp-org:service:RenderingControl:1"
RENDERER_UDN = "uuid:fake-renderer-0001"


# ---------------------------------------------------------------------------
# XML 构造工具
# ---------------------------------------------------------------------------
def esc(text: Any) -> str:
    """XML 文本转义。"""
    return saxutils.escape(str(text))


def soap_response(action: str, fields: str = "", service: str = SERVICE_AVT_URN) -> str:
    """构造一个成功的 SOAP 响应信封。"""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action}Response xmlns:u="{service}">'
        f"{fields}</u:{action}Response></s:Body></s:Envelope>"
    )


def upnp_error_response(code: int, description: str = "") -> str:
    """构造一个 HTTP 500 + UPnPError 的响应。"""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body><s:Fault><faultcode>s:Client</faultcode>"
        "<faultstring>UPnPError</faultstring><detail>"
        '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f"<errorCode>{code}</errorCode>"
        f"<errorDescription>{esc(description)}</errorDescription>"
        "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
    )


def didl_metadata(title: str, artist: str, uri: str, mime: str = "audio/mpeg") -> str:
    """构造设备回报的 DIDL-Lite 元数据（模拟真实设备的单层转义）。"""
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="1" parentID="0" restricted="1">'
        f"<dc:title>{esc(title)}</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f"<upnp:artist>{esc(artist)}</upnp:artist>"
        f'<res protocolInfo="http-get:*:{esc(mime)}:*">{esc(uri)}</res>'
        "</item></DIDL-Lite>"
    )


def description_xml(
    *,
    friendly_name: str = "假小爱音箱",
    model_name: str = "FakeSpeaker",
    udn: str = RENDERER_UDN,
    device_type: str = "urn:schemas-upnp-org:device:MediaRenderer:1",
    avt_control: str = "/AVTransport/control",
    rc_control: str = "/RenderingControl/control",
) -> str:
    """构造一份 description.xml（默认 controlURL 为相对路径）。"""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion>"
        "<device>"
        f"<deviceType>{esc(device_type)}</deviceType>"
        f"<friendlyName>{esc(friendly_name)}</friendlyName>"
        "<manufacturer>Xiaoai</manufacturer>"
        f"<modelName>{esc(model_name)}</modelName>"
        f"<UDN>{esc(udn)}</UDN>"
        "<serviceList>"
        "<service>"
        f"<serviceType>{SERVICE_AVT_URN}</serviceType>"
        "<serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>"
        f"<controlURL>{esc(avt_control)}</controlURL>"
        "<eventSubURL>/AVTransport/event</eventSubURL>"
        "<SCPDURL>/AVTransport/scpd.xml</SCPDURL>"
        "</service>"
        "<service>"
        f"<serviceType>{SERVICE_RC_URN}</serviceType>"
        "<serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>"
        f"<controlURL>{esc(rc_control)}</controlURL>"
        "<eventSubURL>/RenderingControl/event</eventSubURL>"
        "<SCPDURL>/RenderingControl/scpd.xml</SCPDURL>"
        "</service>"
        "</serviceList>"
        "</device></root>"
    )


# ---------------------------------------------------------------------------
# 假 UPnP 设备（状态 + SOAP 响应）
# ---------------------------------------------------------------------------
class FakeUPnPDevice:
    """可编程的假渲染器：记录请求、维护状态、按 SOAPACTION 返回响应。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: List[Dict[str, Any]] = []
        self.state = "PLAYING"
        self.duration_raw = "00:03:21"
        self.position_raw = "00:00:11"
        self.uri = "http://192.168.0.10:8010/media/1.mp3"
        self.title = "晴天 & 你"
        self.artist = "周杰伦 <Jay>"
        self.volume = 30
        self.double_escape_metadata = False
        #: action → (HTTP 状态, 响应体)，用于覆盖默认处理（模拟错误/畸形响应）
        self.overrides: Dict[str, Tuple[int, str]] = {}

    # ---------- 请求分发 ----------
    def handle_soap(
        self,
        path: str,
        action: str,
        headers: Dict[str, str],
        body: str,
    ) -> Tuple[int, str]:
        """处理一个 SOAP 请求，返回 ``(HTTP 状态, 响应体)``。"""
        with self.lock:
            self.requests.append(
                {"path": path, "action": action, "headers": dict(headers), "body": body}
            )
        if action in self.overrides:
            return self.overrides[action]
        handler = getattr(self, f"_do_{action.lower()}", None)
        if handler is None:
            return 500, upnp_error_response(401, f"Invalid Action {action}")
        return 200, handler(body)

    def actions(self) -> List[str]:
        """已收到的动作名（按到达顺序）。"""
        with self.lock:
            return [request["action"] for request in self.requests]

    def of(self, action: str) -> List[Dict[str, Any]]:
        """取出某个动作的全部请求。"""
        with self.lock:
            return [request for request in self.requests if request["action"] == action]

    # ---------- AVTransport ----------
    def _do_gettransportinfo(self, body: str) -> str:
        return soap_response(
            "GetTransportInfo",
            f"<CurrentTransportState>{self.state}</CurrentTransportState>"
            "<CurrentTransportStatus>OK</CurrentTransportStatus>"
            "<CurrentSpeed>1</CurrentSpeed>",
        )

    def _do_getpositioninfo(self, body: str) -> str:
        encoded = esc(didl_metadata(self.title, self.artist, self.uri))
        if self.double_escape_metadata:
            encoded = esc(encoded)
        fields = (
            "<Track>1</Track>"
            f"<TrackDuration>{self.duration_raw}</TrackDuration>"
            f"<TrackMetaData>{encoded}</TrackMetaData>"
            f"<TrackURI>{esc(self.uri)}</TrackURI>"
            f"<RelTime>{self.position_raw}</RelTime>"
            "<AbsTime>00:00:11</AbsTime>"
            "<RelCount>2147483647</RelCount>"
            "<AbsCount>2147483647</AbsCount>"
        )
        return soap_response("GetPositionInfo", fields)

    def _do_setavtransporturi(self, body: str) -> str:
        match = re.search(r"<CurrentURI>(.*?)</CurrentURI>", body, re.S)
        if match:
            self.uri = match.group(1)
        return soap_response("SetAVTransportURI")

    def _do_play(self, body: str) -> str:
        self.state = "PLAYING"
        return soap_response("Play")

    def _do_stop(self, body: str) -> str:
        self.state = "STOPPED"
        return soap_response("Stop")

    def _do_pause(self, body: str) -> str:
        self.state = "PAUSED_PLAYBACK"
        return soap_response("Pause")

    def _do_seek(self, body: str) -> str:
        match = re.search(r"<Target>(.*?)</Target>", body, re.S)
        if match:
            self.position_raw = match.group(1)
        return soap_response("Seek")

    # ---------- RenderingControl ----------
    def _do_setvolume(self, body: str) -> str:
        match = re.search(r"<DesiredVolume>(\d+)</DesiredVolume>", body)
        if match:
            self.volume = int(match.group(1))
        return soap_response("SetVolume", service=SERVICE_RC_URN)

    def _do_getvolume(self, body: str) -> str:
        return soap_response(
            "GetVolume",
            f"<CurrentVolume>{self.volume}</CurrentVolume>",
            service=SERVICE_RC_URN,
        )


# ---------------------------------------------------------------------------
# 假 UPnP HTTP 服务
# ---------------------------------------------------------------------------
class FakeUPnPServer(http.server.ThreadingHTTPServer):
    """承载 :class:`FakeUPnPDevice` 的本地 HTTP 服务。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, device: FakeUPnPDevice, descriptions: Dict[str, str]) -> None:
        self.device = device
        self.descriptions = descriptions
        super().__init__(("127.0.0.1", 0), FakeUPnPRequestHandler)

    @property
    def base_url(self) -> str:
        """``http://127.0.0.1:<随机端口>``。"""
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


class FakeUPnPRequestHandler(http.server.BaseHTTPRequestHandler):
    """按路径分发 description / SOAP 请求。"""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 基类签名
        """静默访问日志。"""

    def do_GET(self) -> None:  # noqa: N802 - 基类约定
        path = self.path.split("?", 1)[0]
        text = self.server.descriptions.get(path)  # type: ignore[attr-defined]
        if text is None:
            self._send(404, b"not found")
            return
        self._send(200, text.encode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802 - 基类约定
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
        headers = {key.upper(): value for key, value in self.headers.items()}
        soap_action = (headers.get("SOAPACTION") or "").strip().strip('"')
        action = soap_action.rsplit("#", 1)[-1] if "#" in soap_action else ""
        status, text = self.server.device.handle_soap(  # type: ignore[attr-defined]
            self.path, action, headers, body
        )
        self._send(status, text.encode("utf-8"))

    def _send(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture()
def upnp() -> Iterable[SimpleNamespace]:
    """起一个本地假 UPnP 设备，测试结束关闭。"""
    device = FakeUPnPDevice()
    descriptions = {"/description.xml": description_xml()}
    server = FakeUPnPServer(device, descriptions)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, name="fake-upnp", daemon=True
    )
    thread.start()
    try:
        yield SimpleNamespace(
            device=device,
            server=server,
            descriptions=descriptions,
            base=server.base_url,
            port=server.server_address[1],
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


@pytest.fixture()
def renderer(upnp: SimpleNamespace) -> dlna.Renderer:
    """按 ``host:port`` 构造，验证 Renderer 自己拼默认控制 URL。"""
    return dlna.Renderer(f"127.0.0.1:{upnp.port}", timeout=2.0)


def _soap_fields(body: str) -> Dict[str, str]:
    """把 SOAP 请求体解析成 ``{局部标签名: 文本}``。"""
    root = ET.fromstring(body)
    fields: Dict[str, str] = {}
    for element in root.iter():
        fields[element.tag.rsplit("}", 1)[-1]] = element.text or ""
    return fields


class _FakeHTTPResponse:
    """假的 urlopen 返回值（支持 with 语法）。"""

    def __init__(self, text: str, status: int = 200) -> None:
        self._data = text.encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class FakeSSDPSocket:
    """假的 UDP socket：按脚本吐出 SSDP 响应，并记录发送内容/选项。"""

    def __init__(self, responses: Iterable[bytes] = (), *, fail: bool = False) -> None:
        self.responses = list(responses)
        self.fail = fail
        self.sent: List[Tuple[bytes, Tuple[str, int]]] = []
        self.options: List[Tuple[int, int, int]] = []
        self.timeouts: List[float] = []
        self.closed = False

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def sendto(self, data: bytes, address: Tuple[str, int]) -> int:
        if self.fail:
            raise OSError("send failed")
        self.sent.append((data, address))
        return len(data)

    def recvfrom(self, size: int) -> Tuple[bytes, Tuple[str, int]]:
        if self.fail:
            raise OSError("recv failed")
        if not self.responses:
            raise OSError("timeout")
        return self.responses.pop(0), ("192.168.1.50", 1900)

    def close(self) -> None:
        self.closed = True


def _patch_socket(monkeypatch: pytest.MonkeyPatch, fake: FakeSSDPSocket) -> None:
    """把 dlna 模块看到的 socket 换成假实现（不动全局 socket 模块）。"""
    stub = SimpleNamespace(
        AF_INET=socket.AF_INET,
        SOCK_DGRAM=socket.SOCK_DGRAM,
        IPPROTO_IP=socket.IPPROTO_IP,
        IP_MULTICAST_TTL=socket.IP_MULTICAST_TTL,
        socket=lambda *args, **kwargs: fake,
    )
    monkeypatch.setattr(dlna, "socket", stub)


def _ssdp_response(location: str) -> bytes:
    """构造一条 SSDP 200 响应。"""
    return (
        "HTTP/1.1 200 OK\r\n"
        "CACHE-CONTROL: max-age=1800\r\n"
        "EXT:\r\n"
        f"LOCATION: {location}\r\n"
        "SERVER: Linux/3.14 UPnP/1.0 Fake/1.0\r\n"
        f"ST: {dlna.SSDP_ST}\r\n"
        f"USN: {RENDERER_UDN}::{dlna.SSDP_ST}\r\n"
        "\r\n"
    ).encode("utf-8")


def _closed_port() -> int:
    """占一个端口再释放，得到一个大概率无人监听的本地端口。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


# ---------------------------------------------------------------------------
# Renderer：构造与控制 URL
# ---------------------------------------------------------------------------
def test_renderer_default_control_urls() -> None:
    renderer = dlna.Renderer("192.168.3.81")
    assert renderer.ip == "192.168.3.81"
    assert renderer.port == 9999
    assert renderer.avt_control == "http://192.168.3.81:9999/AVTransport/control"
    assert renderer.rc_control == "http://192.168.3.81:9999/RenderingControl/control"


def test_renderer_host_port_and_explicit_control_urls() -> None:
    renderer = dlna.Renderer("192.168.3.81:49152")
    assert renderer.port == 49152
    assert renderer.avt_control == "http://192.168.3.81:49152/AVTransport/control"

    explicit = dlna.Renderer(
        "192.168.3.81",
        avt_control="http://192.168.3.81:1234/custom/avt",
        rc_control="http://192.168.3.81:1234/custom/rc",
    )
    assert explicit.avt_control == "http://192.168.3.81:1234/custom/avt"
    assert explicit.rc_control == "http://192.168.3.81:1234/custom/rc"


# ---------------------------------------------------------------------------
# Renderer：状态查询
# ---------------------------------------------------------------------------
def test_transport_state(upnp: SimpleNamespace, renderer: dlna.Renderer) -> None:
    assert renderer.transport_state() == "PLAYING"
    request = upnp.device.of("GetTransportInfo")[0]
    assert request["path"] == "/AVTransport/control"
    assert request["headers"]["SOAPACTION"] == (
        f'"{SERVICE_AVT_URN}#GetTransportInfo"'
    )
    assert request["headers"]["CONTENT-TYPE"] == 'text/xml; charset="utf-8"'
    assert "<InstanceID>0</InstanceID>" in request["body"]


def test_transport_state_unknown_when_field_missing(
    upnp: SimpleNamespace, renderer: dlna.Renderer
) -> None:
    upnp.device.overrides["GetTransportInfo"] = (
        200,
        soap_response("GetTransportInfo", "<CurrentSpeed>1</CurrentSpeed>"),
    )
    assert renderer.transport_state() == "UNKNOWN"


def test_position_info_parses_metadata_and_times(
    upnp: SimpleNamespace, renderer: dlna.Renderer
) -> None:
    info = renderer.position_info()
    assert info.state == "PLAYING"
    assert info.title == "晴天 & 你"
    assert info.artist == "周杰伦 <Jay>"
    assert info.duration == pytest.approx(201.0)
    assert info.position == pytest.approx(11.0)
    assert info.uri == upnp.device.uri
    assert info.duration_raw == "00:03:21"
    assert info.position_raw == "00:00:11"
    assert info.playing is True
    assert info.paused is False
    assert info.stopped is False
    assert info.to_dict()["artist"] == "周杰伦 <Jay>"
    assert [request["action"] for request in upnp.device.requests] == [
        "GetPositionInfo",
        "GetTransportInfo",
    ]


def test_position_info_handles_double_escaped_metadata(
    upnp: SimpleNamespace, renderer: dlna.Renderer
) -> None:
    upnp.device.double_escape_metadata = True
    info = renderer.position_info()
    assert info.title == "晴天 & 你"
    assert info.artist == "周杰伦 <Jay>"


def test_position_info_handles_non_time_values(
    upnp: SimpleNamespace, renderer: dlna.Renderer
) -> None:
    upnp.device.duration_raw = "NOT_IMPLEMENTED"
    upnp.device.position_raw = ""
    info = renderer.position_info()
    assert info.duration == 0.0
    assert info.position == 0.0
    assert info.duration_raw == "NOT_IMPLEMENTED"


def test_position_info_raises_on_soap_error(
    upnp: SimpleNamespace, renderer: dlna.Renderer
) -> None:
    upnp.device.overrides["GetPositionInfo"] = (500, upnp_error_response(701, "Transition"))
    with pytest.raises(dlna.DLNAError) as excinfo:
        renderer.position_info()
    assert excinfo.value.upnp_code == 701


# ---------------------------------------------------------------------------
# Renderer：错误映射
# ---------------------------------------------------------------------------
def test_http_500_upnp_error_maps_to_dlna_error(
    upnp: SimpleNamespace, renderer: dlna.Renderer
) -> None:
    upnp.device.overrides["GetTransportInfo"] = (500, upnp_error_response(501, "Action Failed"))
    with pytest.raises(dlna.DLNAError) as excinfo:
        renderer.transport_state()
    error = excinfo.value
    assert isinstance(error, HubError)
    assert error.upnp_code == 501
    assert error.status == 502
    assert error.code == "dlna_error"
    assert "501" in error.message


def test_http_500_without_error_code_has_no_upnp_code(
    upnp: SimpleNamespace, renderer: dlna.Renderer
) -> None:
    upnp.device.overrides["GetTransportInfo"] = (500, "<html>boom</html>")
    with pytest.raises(dlna.DLNAError) as excinfo:
        renderer.transport_state()
    error = excinfo.value
    assert error.upnp_code is None
    assert error.status == 502
    assert error.code == "dlna_error"


def test_http_500_is_not_retried(upnp: SimpleNamespace, renderer: dlna.Renderer) -> None:
    upnp.device.overrides["GetTransportInfo"] = (500, upnp_error_response(501, "Action Failed"))
    with pytest.raises(dlna.DLNAError):
        renderer.transport_state()
    assert len(upnp.device.of("GetTransportInfo")) == 1


def test_ping_true_when_device_responds(renderer: dlna.Renderer) -> None:
    assert renderer.ping() is True


def test_ping_false_when_device_unreachable() -> None:
    unreachable = dlna.Renderer(f"127.0.0.1:{_closed_port()}", timeout=0.2)
    assert unreachable.ping() is False


def test_ping_uses_short_timeout_and_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """离线设备上 ping 必须快速失败：短超时（≤2s）+ 不重试。"""
    calls: List[Optional[float]] = []

    def fake_urlopen(request: Any, timeout: Optional[float] = None) -> Any:
        calls.append(timeout)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    renderer = dlna.Renderer("127.0.0.1:9", timeout=30.0)
    assert renderer.ping() is False
    assert len(calls) == 1
    assert calls[0] is not None and calls[0] <= 2.0


def test_effective_timeout_prefers_instance_then_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dlna.config, "get", lambda: SimpleNamespace(request_timeout=7.5)
    )
    assert dlna.Renderer("127.0.0.1:9")._effective_timeout() == pytest.approx(7.5)
    assert dlna.Renderer("127.0.0.1:9", timeout=3.0)._effective_timeout() == pytest.approx(3.0)
    assert dlna.Renderer("127.0.0.1:9", timeout=0.01)._effective_timeout() >= 0.1


def test_network_error_retries_once_then_maps_to_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []

    def fake_urlopen(request: Any, timeout: Optional[float] = None) -> Any:
        calls.append("call")
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    unreachable = dlna.Renderer("127.0.0.1:9", timeout=0.1)
    with pytest.raises(dlna.DLNAError) as excinfo:
        unreachable.transport_state()
    error = excinfo.value
    assert len(calls) == 2
    assert error.status == 503
    assert error.code == "device_unavailable"
    assert error.upnp_code is None


def test_network_error_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: List[Any] = [
        urllib.error.URLError("flaky"),
        _FakeHTTPResponse(
            soap_response(
                "GetTransportInfo",
                "<CurrentTransportState>PLAYING</CurrentTransportState>",
            )
        ),
    ]

    def fake_urlopen(request: Any, timeout: Optional[float] = None) -> Any:
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    flaky = dlna.Renderer("127.0.0.1:9", timeout=0.1)
    assert flaky.transport_state() == "PLAYING"
    assert responses == []


# ---------------------------------------------------------------------------
# Renderer：播放控制
# ---------------------------------------------------------------------------
def test_play_url_sends_didl_metadata(upnp: SimpleNamespace, renderer: dlna.Renderer) -> None:
    upnp.device.state = "STOPPED"
    uri = "http://192.168.0.10:8010/media/1.mp3?token=a&sig=b"
    title = '晴天 & "你" <live>'
    artist = "周杰伦 & Co <Jay>"

    state = renderer.play_url(uri, title=title, artist=artist)
    assert state == "PLAYING"

    request = upnp.device.of("SetAVTransportURI")[0]
    assert request["headers"]["SOAPACTION"] == (
        f'"{SERVICE_AVT_URN}#SetAVTransportURI"'
    )
    fields = _soap_fields(request["body"])
    assert fields["CurrentURI"] == uri
    assert fields["InstanceID"] == "0"

    metadata = ET.fromstring(fields["CurrentURIMetaData"])
    tags: Dict[str, ET.Element] = {}
    for element in metadata.iter():
        tags.setdefault(element.tag.rsplit("}", 1)[-1], element)
    assert tags["title"].text == title
    assert tags["artist"].text == artist
    assert tags["res"].text == uri
    assert "audio/mpeg" in tags["res"].attrib["protocolInfo"]
    assert tags["class"].text == "object.item.audioItem.musicTrack"

    actions = upnp.device.actions()
    assert actions.index("SetAVTransportURI") < actions.index("Play")


def test_play_url_custom_mime(upnp: SimpleNamespace, renderer: dlna.Renderer) -> None:
    renderer.play_url("http://192.168.0.10:8010/media/1.flac", mime="audio/flac")
    request = upnp.device.of("SetAVTransportURI")[0]
    fields = _soap_fields(request["body"])
    metadata = ET.fromstring(fields["CurrentURIMetaData"])
    resources = [element for element in metadata.iter() if element.tag.endswith("res")]
    assert resources and "audio/flac" in resources[0].attrib["protocolInfo"]


def test_stop_pause_resume_roundtrip(upnp: SimpleNamespace, renderer: dlna.Renderer) -> None:
    assert renderer.stop() == "STOPPED"
    assert renderer.pause() == "PAUSED_PLAYBACK"
    assert renderer.resume() == "PLAYING"
    assert renderer.supports_pause is True
    assert upnp.device.actions() == [
        "Stop",
        "GetTransportInfo",
        "Pause",
        "GetTransportInfo",
        "Play",
        "GetTransportInfo",
    ]


def test_pause_rejection_soft_degrades(upnp: SimpleNamespace, renderer: dlna.Renderer) -> None:
    """设备可达但拒绝 Pause：返回当前状态（供上层降级），并记住不支持。"""
    upnp.device.overrides["Pause"] = (500, upnp_error_response(701, "Transition not available"))
    assert renderer.pause() == "PLAYING"
    assert renderer.supports_pause is False

    # 第二次 pause 不再发 Pause，只回读状态
    assert renderer.pause() == "PLAYING"
    assert len(upnp.device.of("Pause")) == 1
    assert len(upnp.device.of("GetTransportInfo")) == 2


def test_pause_raises_when_device_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: Optional[float] = None) -> Any:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    unreachable = dlna.Renderer("127.0.0.1:9", timeout=0.1)
    with pytest.raises(dlna.DLNAError) as excinfo:
        unreachable.pause()
    assert excinfo.value.status == 503
    assert excinfo.value.code == "device_unavailable"
    assert unreachable.supports_pause is False


def test_seek_success(upnp: SimpleNamespace, renderer: dlna.Renderer) -> None:
    assert renderer.seek(65) is True
    assert renderer.supports_seek is True
    request = upnp.device.of("Seek")[0]
    assert "<Unit>REL_TIME</Unit>" in request["body"]
    assert "<Target>00:01:05</Target>" in request["body"]
    assert upnp.device.position_raw == "00:01:05"


def test_seek_failure_returns_false_and_caches(
    upnp: SimpleNamespace, renderer: dlna.Renderer
) -> None:
    upnp.device.overrides["Seek"] = (500, upnp_error_response(710, "Seek mode not supported"))
    assert renderer.seek(10) is False
    assert renderer.supports_seek is False
    assert renderer.seek(20) is False
    assert len(upnp.device.of("Seek")) == 1


def test_seek_never_raises_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: Optional[float] = None) -> Any:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    renderer = dlna.Renderer("127.0.0.1:9", timeout=0.1)
    assert renderer.seek(30) is False
    assert renderer.supports_seek is False


# ---------------------------------------------------------------------------
# Renderer：音量
# ---------------------------------------------------------------------------
def test_set_volume_clamps(upnp: SimpleNamespace, renderer: dlna.Renderer) -> None:
    assert renderer.set_volume(150) == 100
    assert renderer.set_volume(-20) == 0
    assert renderer.set_volume(55) == 55
    assert upnp.device.volume == 55

    bodies = [request["body"] for request in upnp.device.of("SetVolume")]
    assert "<DesiredVolume>100</DesiredVolume>" in bodies[0]
    assert "<DesiredVolume>0</DesiredVolume>" in bodies[1]
    assert "<DesiredVolume>55</DesiredVolume>" in bodies[2]
    assert "<Channel>Master</Channel>" in bodies[0]
    assert "<InstanceID>0</InstanceID>" in bodies[0]
    request = upnp.device.of("SetVolume")[0]
    assert request["path"] == "/RenderingControl/control"
    assert request["headers"]["SOAPACTION"] == f'"{SERVICE_RC_URN}#SetVolume"'


def test_get_volume(upnp: SimpleNamespace, renderer: dlna.Renderer) -> None:
    upnp.device.volume = 42
    assert renderer.get_volume() == 42


def test_get_volume_none_when_field_missing(
    upnp: SimpleNamespace, renderer: dlna.Renderer
) -> None:
    upnp.device.overrides["GetVolume"] = (
        200,
        soap_response("GetVolume", "", SERVICE_RC_URN),
    )
    assert renderer.get_volume() is None


# ---------------------------------------------------------------------------
# 设备描述解析
# ---------------------------------------------------------------------------
def test_parse_device_description_absolute_control_urls() -> None:
    base = "http://192.168.3.81:49152"
    text = description_xml(
        avt_control=f"{base}/AVTransport/control",
        rc_control=f"{base}/RenderingControl/control",
    )
    device = dlna.parse_device_description(text, f"{base}/description.xml")
    assert device is not None
    assert device.ip == "192.168.3.81"
    assert device.name == "假小爱音箱"
    assert device.model == "FakeSpeaker"
    assert device.udn == RENDERER_UDN
    assert device.avt_control == f"{base}/AVTransport/control"
    assert device.rc_control == f"{base}/RenderingControl/control"
    assert device.to_dict() == {
        "ip": "192.168.3.81",
        "name": "假小爱音箱",
        "model": "FakeSpeaker",
        "udn": RENDERER_UDN,
        "avt_control": f"{base}/AVTransport/control",
        "rc_control": f"{base}/RenderingControl/control",
    }


def test_parse_device_description_relative_control_urls() -> None:
    location = "http://192.168.3.81:49152/description.xml"
    device = dlna.parse_device_description(description_xml(), location)
    assert device is not None
    assert device.ip == "192.168.3.81"
    assert device.avt_control == "http://192.168.3.81:49152/AVTransport/control"
    assert device.rc_control == "http://192.168.3.81:49152/RenderingControl/control"


def test_parse_device_description_relative_without_leading_slash() -> None:
    text = description_xml(
        avt_control="AVTransport/control",
        rc_control="RenderingControl/control",
    )
    device = dlna.parse_device_description(text, "http://10.0.0.5:8080/desc.xml")
    assert device is not None
    assert device.avt_control == "http://10.0.0.5:8080/AVTransport/control"
    assert device.rc_control == "http://10.0.0.5:8080/RenderingControl/control"


def test_parse_device_description_non_renderer_returns_none() -> None:
    text = description_xml(device_type="urn:schemas-upnp-org:device:Basic:1")
    assert dlna.parse_device_description(text, "http://10.0.0.5/description.xml") is None


def test_parse_device_description_invalid_xml_returns_none() -> None:
    assert dlna.parse_device_description("<not-xml", "http://10.0.0.5/description.xml") is None


def test_renderer_from_parsed_description(upnp: SimpleNamespace) -> None:
    text = upnp.descriptions["/description.xml"]
    device = dlna.parse_device_description(text, f"{upnp.base}/description.xml")
    assert device is not None
    renderer = dlna.Renderer(
        device.ip,
        avt_control=device.avt_control,
        rc_control=device.rc_control,
        timeout=2.0,
    )
    assert renderer.transport_state() == "PLAYING"
    assert upnp.device.actions() == ["GetTransportInfo"]


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00:03:21", 201.0),
        ("00:03:21.500", 201.5),
        ("01:00:00", 3600.0),
        ("00:00:00", 0.0),
        ("NOT_IMPLEMENTED", 0.0),
        ("", 0.0),
        (None, 0.0),
        (12.5, 12.5),
        (0, 0.0),
        ("123", 123.0),
        ("123.5", 123.5),
        ("bad:value", 0.0),
        ("aa:bb:cc", 0.0),
        (-5, 0.0),
    ],
)
def test_secs(raw: Any, expected: float) -> None:
    assert dlna.secs(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "00:00:00"),
        (59.9, "00:00:59"),
        (65, "00:01:05"),
        (3661, "01:01:01"),
        (-5, "00:00:00"),
        (360000, "100:00:00"),
        ("bad", "00:00:00"),
    ],
)
def test_fmt_time(seconds: Any, expected: str) -> None:
    assert dlna.fmt_time(seconds) == expected


def test_position_info_dataclass_defaults() -> None:
    info = dlna.PositionInfo()
    assert info.state == "UNKNOWN"
    assert info.playing is False
    assert info.paused is False
    assert info.stopped is False
    paused = dlna.PositionInfo(state="PAUSED_PLAYBACK")
    assert paused.paused is True
    assert dlna.PositionInfo(state="NO_MEDIA_PRESENT").stopped is True
    assert paused.to_dict()["state"] == "PAUSED_PLAYBACK"


# ---------------------------------------------------------------------------
# SSDP 发现
# ---------------------------------------------------------------------------
def test_discover_sends_msearch_and_dedupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locations = [
        "http://192.168.1.50:49152/description.xml",
        "http://192.168.1.50:9999/description.xml",
        "http://192.168.1.51:49152/description.xml",
        "http://192.168.1.50:49152/description.xml",
    ]
    fake_socket = FakeSSDPSocket([_ssdp_response(location) for location in locations])
    _patch_socket(monkeypatch, fake_socket)

    # 前两个 location 是同一台设备（UDN 相同），第三个是另一台
    descriptions = {
        locations[0]: description_xml(udn="uuid:renderer-a", friendly_name="客厅音箱"),
        locations[1]: description_xml(udn="uuid:renderer-a", friendly_name="客厅音箱"),
        locations[2]: description_xml(udn="uuid:renderer-b", friendly_name="卧室音箱"),
    }
    fetched: List[str] = []

    def fake_urlopen(url: Any, timeout: Optional[float] = None) -> Any:
        location = str(url)
        fetched.append(location)
        return _FakeHTTPResponse(descriptions[location])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    devices = dlna.discover(timeout=0.2)
    assert [device.udn for device in devices] == ["uuid:renderer-a", "uuid:renderer-b"]
    assert [device.name for device in devices] == ["客厅音箱", "卧室音箱"]
    assert all(device.ip == "192.168.1.50" for device in devices[:1])
    assert [device.ip for device in devices] == ["192.168.1.50", "192.168.1.51"]

    # M-SEARCH 报文与多播参数
    data, address = fake_socket.sent[0]
    text = data.decode("utf-8")
    assert "M-SEARCH * HTTP/1.1" in text
    assert "HOST: 239.255.255.250:1900" in text
    assert 'MAN: "ssdp:discover"' in text
    assert "MX: 2" in text
    assert f"ST: {dlna.SSDP_ST}" in text
    assert address == ("239.255.255.250", 1900)
    assert fake_socket.options == [(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)]
    assert fake_socket.closed is True
    assert sorted(fetched) == sorted(descriptions)


def test_discover_fetches_real_description_and_skips_bad(
    upnp: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只打桩 SSDP，description 走真实本地 HTTP（验证抓取 + 解析 + 失败跳过）。"""
    upnp.descriptions["/desc/other.xml"] = description_xml(
        udn="uuid:other-renderer", friendly_name="书房音箱"
    )
    fake_socket = FakeSSDPSocket(
        [
            _ssdp_response(f"{upnp.base}/description.xml"),
            _ssdp_response(f"{upnp.base}/desc/other.xml"),
            _ssdp_response(f"{upnp.base}/missing.xml"),
        ]
    )
    _patch_socket(monkeypatch, fake_socket)

    devices = dlna.discover(timeout=0.2)
    assert [device.udn for device in devices] == [RENDERER_UDN, "uuid:other-renderer"]
    assert [device.name for device in devices] == ["假小爱音箱", "书房音箱"]
    assert all(device.avt_control.startswith(upnp.base) for device in devices)


def test_discover_returns_empty_when_socket_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_socket = FakeSSDPSocket(fail=True)
    _patch_socket(monkeypatch, fake_socket)
    assert dlna.discover(timeout=0.1) == []
    assert fake_socket.closed is True


def test_discover_returns_empty_when_no_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_socket = FakeSSDPSocket()
    _patch_socket(monkeypatch, fake_socket)

    def fake_urlopen(url: Any, timeout: Optional[float] = None) -> Any:  # pragma: no cover
        raise AssertionError("没有 SSDP 响应时不应抓 description")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert dlna.discover(timeout=0.1) == []
    assert fake_socket.closed is True


def test_discover_skips_description_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_socket = FakeSSDPSocket([_ssdp_response("http://192.168.1.50/description.xml")])
    _patch_socket(monkeypatch, fake_socket)

    def fake_urlopen(url: Any, timeout: Optional[float] = None) -> Any:
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert dlna.discover(timeout=0.1) == []
    assert fake_socket.closed is True


def test_discover_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_socket(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("socket factory exploded")

    stub = SimpleNamespace(
        AF_INET=socket.AF_INET,
        SOCK_DGRAM=socket.SOCK_DGRAM,
        IPPROTO_IP=socket.IPPROTO_IP,
        IP_MULTICAST_TTL=socket.IP_MULTICAST_TTL,
        socket=broken_socket,
    )
    monkeypatch.setattr(dlna, "socket", stub)
    assert dlna.discover(timeout=0.1) == []


def test_discover_total_time_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """单设备 description 抓取失败也要静默跳过，总耗时不超过 timeout+2s。"""
    assert dlna.DESC_TIMEOUT <= 2.0
    fake_socket = FakeSSDPSocket([_ssdp_response("http://192.168.1.50/description.xml")])
    _patch_socket(monkeypatch, fake_socket)

    def timing_out_urlopen(url: Any, timeout: Optional[float] = None) -> Any:
        time.sleep(float(timeout or 0.0))
        raise urllib.error.URLError("description timeout")

    monkeypatch.setattr(urllib.request, "urlopen", timing_out_urlopen)
    started = time.monotonic()
    assert dlna.discover(timeout=0.2) == []
    elapsed = time.monotonic() - started
    assert elapsed <= 0.2 + dlna.DESC_TIMEOUT + 0.5
