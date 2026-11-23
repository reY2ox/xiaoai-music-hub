"""DLNA / UPnP 控制层。

只跟音箱说话：播放、暂停、停止、音量、状态、Seek。
不认识网易云，也不认识本地曲库，方便单独测试。
"""
import re
import xml.sax.saxutils as sx

from . import config

UA_HEADER = 'text/xml; charset="utf-8"'
SERVICE_AVT = "AVTransport:1"
SERVICE_RC = "RenderingControl:1"


def _ctrl_url(ip, service):
    return f"http://{ip}:9999/{service.split(':')[0]}/control"


def soap(ip, service, action, body="", timeout=8):
    """发一个 SOAP 动作，返回 (status, text)"""
    import urllib.request
    env = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}">{body}</u:{action}>'
        "</s:Body></s:Envelope>"
    )
    req = urllib.request.Request(_ctrl_url(ip, service), data=env.encode(), method="POST")
    req.add_header("Content-Type", UA_HEADER)
    req.add_header("SOAPACTION", f'"urn:schemas-upnp-org:service:{service}#{action}"')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def av(ip, action, extra=""):
    return soap(ip, SERVICE_AVT, action, f"<InstanceID>0</InstanceID>{extra}")


def transport(ip):
    """当前传输状态：PLAYING / PAUSED_PLAYBACK / STOPPED …"""
    _, txt = av(ip, "GetTransportInfo")
    m = re.search(r"<CurrentTransportState>([^<]*)<", txt)
    return m.group(1) if m else "UNKNOWN"


def _unesc(v):
    import html
    for _ in range(2):
        v = html.unescape(v)
    return v


def now_playing(ip):
    """当前曲目信息（全部来自音箱回报，不做推测）"""
    _, txt = av(ip, "GetPositionInfo")

    def grab(tag):
        m = re.search(rf"<{tag}>([^<]*)</{tag}>", txt)
        return _unesc(m.group(1)) if m else ""

    meta = grab("TrackMetaData")
    title = re.search(r"<dc:title>([^<]*)</dc:title>", meta)
    artist = re.search(r"<upnp:artist>([^<]*)</upnp:artist>", meta)
    return {
        "device": ip,
        "state": transport(ip),
        "title": _unesc(title.group(1)) if title else "",
        "artist": _unesc(artist.group(1)) if artist else "",
        "duration": grab("TrackDuration"),
        "position": grab("RelTime"),
        "url": grab("TrackURI"),
    }


def secs(ts):
    """'00:03:21' -> 201"""
    try:
        h, m, s = [int(x) for x in ts.split(":")]
        return h * 3600 + m * 60 + s
    except Exception:
        return 0


def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def play_url(ip, media, title="", artist="", mime="audio/mpeg"):
    """把一条媒体地址交给音箱播放"""
    meta = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        f'<item id="1" parentID="0" restricted="1"><dc:title>{sx.escape(title)}</dc:title>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<upnp:artist>{sx.escape(artist)}</upnp:artist>'
        f'<res protocolInfo="http-get:*:{mime}:*">{sx.escape(media)}</res></item></DIDL-Lite>'
    )
    av(ip, "SetAVTransportURI",
       f"<CurrentURI>{sx.escape(media)}</CurrentURI>"
       f"<CurrentURIMetaData>{sx.escape(meta)}</CurrentURIMetaData>")
    av(ip, "Play", "<Speed>1</Speed>")
    return transport(ip)


def stop(ip):
    av(ip, "Stop")
    return transport(ip)


def pause(ip):
    av(ip, "Pause")
    return transport(ip)


def resume(ip):
    av(ip, "Play", "<Speed>1</Speed>")
    return transport(ip)


def seek(ip, seconds):
    return av(ip, "Seek",
              f"<Unit>REL_TIME</Unit><Target>{fmt_time(seconds)}</Target>")


def set_volume(ip, level):
    level = max(0, min(100, int(level)))
    soap(ip, SERVICE_RC, "SetVolume",
         f"<InstanceID>0</InstanceID><Channel>Master</Channel>"
         f"<DesiredVolume>{level}</DesiredVolume>")
    return level


def get_volume(ip):
    _, txt = soap(ip, SERVICE_RC, "GetVolume",
                  "<InstanceID>0</InstanceID><Channel>Master</Channel>")
    m = re.search(r"<CurrentVolume>(\d+)<", txt)
    return int(m.group(1)) if m else None


# ---------- 设备发现 ----------
def ssdp_scan(timeout=5):
    """扫描局域网内的 MediaRenderer"""
    import socket
    import time
    import urllib.request

    msg = (
        'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
        'MAN: "ssdp:discover"\r\nMX: 3\r\nST: ssdp:all\r\n\r\n'
    ).encode()
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sk.settimeout(1.0)
    sk.sendto(msg, ("239.255.255.250", 1900))

    found, end = {}, time.time() + timeout
    while time.time() < end:
        try:
            data, addr = sk.recvfrom(65535)
        except Exception:
            continue
        txt = data.decode("utf-8", "ignore")
        loc = re.search(r"(?im)^location:\s*(\S+)", txt)
        if not loc or addr[0] in found:
            continue
        try:
            desc = urllib.request.urlopen(loc.group(1), timeout=3).read().decode("utf-8", "ignore")
        except Exception:
            continue
        if "MediaRenderer" not in desc:
            continue
        fn = re.search(r"<friendlyName>([^<]*)</friendlyName>", desc)
        mn = re.search(r"<modelName>([^<]*)</modelName>", desc)
        udn = re.search(r"<UDN>([^<]*)</UDN>", desc)
        found[addr[0]] = {
            "ip": addr[0],
            "name": _unesc(fn.group(1)).strip() if fn else addr[0],
            "model": mn.group(1) if mn else "",
            "udn": udn.group(1) if udn else "",
        }
    sk.close()
    return list(found.values())
