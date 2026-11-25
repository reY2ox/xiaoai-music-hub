"""MCP stdio server：把音乐中枢包装成语音助手（OpenClaw 等）可调用的工具。

协议
----
JSON-RPC 2.0 over stdio，**逐行 JSON**，MCP 协议版本 ``2024-11-05``。
stdout 只允许出现协议消息；所有日志/诊断一律写 stderr（MCP 客户端把 stdout 当协议流）。

支持的方法：``initialize`` / ``notifications/initialized`` / ``tools/list`` /
``tools/call`` / ``ping``；未知方法返回 JSON-RPC 错误码 ``-32601``。

环境变量
--------
- ``MUSIC_HUB_URL``：音乐中枢地址，默认 ``http://127.0.0.1:8010``；
- ``MUSIC_HUB_PASSWORD``：可选，Web 密码（HTTP Basic Auth，用户名任意）；
- ``MUSIC_HUB_TIMEOUT``：可选，普通请求超时秒数，默认 20（点歌固定 45）。

设计约束：只用标准库（urllib），Python >= 3.9；不执行任意命令，只与本机中枢通信。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import __version__

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "xiaoai-music-hub"
DEFAULT_BASE = "http://127.0.0.1:8010"

#: 兼容旧代码/测试直接引用 ``mcp.BASE``；实际请求时由 :func:`_base_url` 解析
BASE = (os.environ.get("MUSIC_HUB_URL") or DEFAULT_BASE).rstrip("/")

_QUALITY_LABELS = {
    "standard": "标准 192k",
    "high": "高品质 320k",
    "lossless": "无损 FLAC",
}
_MODE_LABELS = {
    "sequential": "顺序播放",
    "loop": "列表循环",
    "single": "单曲循环",
    "shuffle": "随机播放",
}
#: 服务端机器码 → 面向用户的友好文案（v2 的 bug：直接抛 ``HTTP Error 409``）
_FRIENDLY_CODES = {
    "no_url": "这首歌在网易云拿不到播放地址（可能是 VIP / 无版权 / 已下架），换一首试试。",
    "no_queue": "当前没有播放队列，先点一首歌或播放一个歌单。",
    "empty_playlist": "这个歌单是空的。",
    "not_found": "没有找到匹配的歌曲或歌单。",
    "device_unreachable": "音箱不可达：请检查音箱是否开机、IP 是否正确、是否和中枢在同一局域网。",
    "unauthorized": "鉴权失败：请检查 MUSIC_HUB_PASSWORD 是否与中枢的 UI_PASSWORD 一致。",
    "vip_required": "这首歌需要网易云 VIP，当前账号无权限播放。",
}


#: 服务端部分错误文案是英文（领域层返回），统一翻成面向用户的中文
_TEXT_FRIENDLY = {
    "no queue": "当前没有播放队列，先点一首歌或播放一个歌单。",
    "empty playlist": "这个歌单是空的。",
    "end of playlist": "已经是队列最后一首了。",
    "no url": "这首歌在网易云拿不到播放地址（可能是 VIP / 无版权 / 已下架），换一首试试。",
    "device unreachable": "音箱不可达：请检查音箱是否开机、IP 是否正确、是否和中枢在同一局域网。",
    "device unavailable": "音箱不可达：请检查音箱是否开机、IP 是否正确、是否和中枢在同一局域网。",
    "not found": "没有找到匹配的歌曲或歌单。",
}


class HubError(Exception):
    """调用音乐中枢失败（``message`` 已是面向用户的中文文案）。"""


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
def _base_url() -> str:
    return (os.environ.get("MUSIC_HUB_URL") or BASE or DEFAULT_BASE).rstrip("/")


def _password() -> Optional[str]:
    value = (os.environ.get("MUSIC_HUB_PASSWORD") or "").strip()
    return value or None


def _timeout(default: float = 20.0) -> float:
    try:
        value = float(os.environ.get("MUSIC_HUB_TIMEOUT", ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _log(message: str) -> None:
    """诊断日志只写 stderr，绝不污染 stdout 的 JSON-RPC 流。"""
    try:
        sys.stderr.write(f"[xiaoai-mcp] {message}\n")
        sys.stderr.flush()
    except Exception:  # pragma: no cover - stderr 不可写时静默
        pass


def _headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": f"{SERVER_NAME}/{__version__}",
    }
    password = _password()
    if password:
        token = base64.b64encode(f"xiaoai:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return headers


def _http_error_payload(exc: urllib.error.HTTPError) -> Tuple[str, str]:
    """从 HTTP 错误响应里提取 ``(code, error)``（服务端失败体固定为 JSON）。"""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # pragma: no cover - 读错误体失败
        raw = ""
    code = error = ""
    if raw:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            code = str(payload.get("code") or "")
            error = str(payload.get("error") or payload.get("message") or "")
        elif not error:
            error = raw.strip()[:200]
    return code, error


def _http_error(exc: urllib.error.HTTPError) -> HubError:
    """把 HTTPError 转成友好中文文案（409/404/503 不再裸奔给语音助手）。"""
    code, error = _http_error_payload(exc)
    if code and code in _FRIENDLY_CODES:
        return HubError(_FRIENDLY_CODES[code])
    text = _TEXT_FRIENDLY.get(error.strip().lower())
    if text:
        return HubError(text)
    status = int(getattr(exc, "code", 0) or 0)
    suffix = f"：{error}" if error else ""
    if status == 400:
        return HubError(error or "请求参数错误。")
    if status in (401, 403):
        return HubError(_FRIENDLY_CODES["unauthorized"])
    if status == 404:
        return HubError(error or "没有找到对应的歌曲、歌单或资源。")
    if status == 409:
        return HubError(error or "操作冲突：当前状态不允许该操作（例如没有播放队列）。")
    if status == 413:
        return HubError("请求内容过大。")
    if status == 429:
        return HubError("请求太频繁，稍等片刻再试。")
    if status == 502:
        return HubError(
            "网易云接口异常（上游服务报错），请稍后重试；若持续出现可能需要重新扫码登录。"
        )
    if status == 503:
        return HubError("音箱不可达：请检查音箱电源、IP 地址和局域网连通性。")
    if status >= 500:
        return HubError(f"音乐中枢内部错误（HTTP {status}）{suffix}")
    return HubError(f"音乐中枢返回 HTTP {status}{suffix}")


def _request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """发一个 HTTP 请求到音乐中枢，返回解析后的 JSON dict。

    失败统一抛 :class:`HubError`（含友好文案），调用方无需关心 urllib 细节。
    """
    url = _base_url() + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)

    data: Optional[bytes] = None
    headers = _headers()
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())

    try:
        with urllib.request.urlopen(request, timeout=timeout or _timeout()) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:  # 必须显式捕获（v2 的 bug：409 直接抛给语音助手）
        raise _http_error(exc) from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise HubError("音乐中枢响应超时，请确认音箱与网易云网络正常后重试。") from None
        raise HubError(
            f"连不上音乐中枢（{_base_url()}）：{reason}。"
            "请确认服务已启动，或检查 MUSIC_HUB_URL 环境变量。"
        ) from None
    except (socket.timeout, TimeoutError):
        raise HubError("音乐中枢响应超时，请稍后重试。") from None
    except OSError as exc:
        raise HubError(f"连接音乐中枢失败：{exc}。请确认服务已启动。") from None

    if not raw.strip():
        return {"ok": True}
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HubError(f"音乐中枢返回了无法解析的响应（HTTP {status}）。") from None
    if not isinstance(payload, dict):
        return {"ok": True, "data": payload}
    if payload.get("ok") is False:
        code = str(payload.get("code") or "")
        message = str(payload.get("error") or payload.get("message") or "")
        if code and code in _FRIENDLY_CODES:
            raise HubError(_FRIENDLY_CODES[code])
        raise HubError(message or "音乐中枢返回失败。")
    return payload


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clip_level(args: Dict[str, Any]) -> int:
    if "level" not in args or args.get("level") in (None, ""):
        raise HubError("请给出音量 level（0-100）。")
    level = _as_int(args.get("level"), -1)
    if level < 0:
        raise HubError("音量 level 必须是 0-100 的整数。")
    return max(0, min(100, level))


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def _play_music(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise HubError("没有给出歌名。请说歌名或「歌手 歌名」。")
    payload = _request("POST", "/play", body={"q": query}, timeout=max(_timeout(), 45.0))
    song = payload.get("song") or {}
    name = song.get("name") or query
    artist = song.get("artist") or ""
    source = payload.get("source") or ""
    text = f"正在播放：{name} - {artist}" if artist else f"正在播放：{name}"
    if source == "local":
        text += "（本地曲库）"
    return text + "。"


def _simple_command(path: str, done: str) -> str:
    _request("POST", path)
    return done


def _pause_music(args: Dict[str, Any]) -> str:
    payload = _request("POST", "/pause")
    if payload.get("fallback"):
        return "小爱不支持 DLNA 暂停，已停止播放并记住进度；说「继续播放」可从原位置恢复。"
    return "已暂停。"


def _resume_music(args: Dict[str, Any]) -> str:
    payload = _request("POST", "/resume")
    resumed = payload.get("resumed")
    if resumed == "replay":
        return "已重新推流并从原位置继续播放。"
    if resumed == "already":
        return "当前已经在播放了。"
    if resumed == "pause":
        return "已继续播放。"
    return "已继续播放。"


def _next_music(args: Dict[str, Any]) -> str:
    payload = _request("POST", "/next")
    return _playing_text(payload, "已切到下一首")


def _prev_music(args: Dict[str, Any]) -> str:
    payload = _request("POST", "/prev")
    return _playing_text(payload, "已切到上一首")


def _playing_text(payload: Dict[str, Any], prefix: str) -> str:
    song = payload.get("song") or {}
    name = song.get("name") or payload.get("title") or ""
    artist = song.get("artist") or payload.get("artist") or ""
    if name:
        return f"{prefix}：{name} - {artist}。" if artist else f"{prefix}：{name}。"
    return f"{prefix}。"


def _music_status(args: Dict[str, Any]) -> str:
    payload = _request("GET", "/now", timeout=10.0)
    state = str(payload.get("state") or "")
    title = str(payload.get("title") or "").strip()
    artist = str(payload.get("artist") or "").strip()
    position = str(payload.get("position") or "")
    duration = str(payload.get("duration") or "")
    interrupted = payload.get("interrupted")

    if interrupted:
        name = interrupted.get("title") or title or "当前曲目"
        return (
            f"音箱已被语音/其他控制器暂停：{name}。不会自动切歌；"
            "用户说「继续播放」时调用 resume_music 从原位置恢复。"
        )
    if payload.get("manual_stop"):
        return "已手动停止播放。"
    if not title or state in ("", "STOPPED", "NO_MEDIA_PRESENT", "UNKNOWN"):
        return "当前没有在播放。"

    text = f"正在播放：{title} - {artist}" if artist else f"正在播放：{title}"
    if position and duration:
        text += f"，进度 {position} / {duration}"
    queue = payload.get("queue") or {}
    total = _as_int(queue.get("total"), 0)
    index = _as_int(queue.get("index"), 0)
    if total > 0:
        text += f"（队列第 {index + 1}/{total} 首）"
    return text + "。"


def _search_music(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise HubError("没有给出搜索关键词。")
    limit = max(1, min(50, _as_int(args.get("limit"), 5) or 5))
    payload = _request("GET", "/search", params={"q": query, "limit": limit})
    songs = payload.get("songs") or payload.get("results") or payload.get("tracks") or []
    if not isinstance(songs, list):
        songs = []
    if not songs:
        return f"没有搜到「{query}」相关的歌。"
    lines = [f"搜到 {len(songs)} 首（{query}）："]
    for position, song in enumerate(songs[:limit], start=1):
        if not isinstance(song, dict):
            continue
        name = song.get("name") or song.get("title") or ""
        artist = song.get("artist") or ""
        album = song.get("album") or ""
        line = f"{position}. {name} - {artist}" if artist else f"{position}. {name}"
        if album:
            line += f"（{album}）"
        lines.append(line)
    return "\n".join(lines)


def _list_playlists(args: Dict[str, Any]) -> str:
    payload = _request("GET", "/playlists")
    playlists = payload.get("playlists") or []
    if not isinstance(playlists, list) or not playlists:
        return "还没有歌单。"
    lines = [f"共 {len(playlists)} 个歌单："]
    for playlist in playlists:
        if not isinstance(playlist, dict):
            continue
        name = playlist.get("name") or "未命名歌单"
        count = _as_int(playlist.get("count"), 0)
        lines.append(f"- [{playlist.get('id')}] {name}（{count} 首）")
    return "\n".join(lines)


def _resolve_playlist(args: Dict[str, Any]) -> Tuple[int, str]:
    raw_id = args.get("id")
    name = str(args.get("name") or "").strip()
    if raw_id not in (None, ""):
        playlist_id = _as_int(raw_id, -1)
        if playlist_id < 0:
            raise HubError("歌单 id 必须是整数。")
        return playlist_id, name
    if not name:
        raise HubError("请提供歌单名（name）或歌单 id。")
    payload = _request("GET", "/playlists")
    playlists = [p for p in (payload.get("playlists") or []) if isinstance(p, dict)]
    exact = [p for p in playlists if str(p.get("name") or "") == name]
    fuzzy = [p for p in playlists if name in str(p.get("name") or "")]
    matches = exact or fuzzy
    if not matches:
        available = "、".join(str(p.get("name")) for p in playlists[:10]) or "（无）"
        raise HubError(f"没有找到歌单「{name}」。现有歌单：{available}。")
    target = matches[0]
    return _as_int(target.get("id"), 0), str(target.get("name") or name)


def _play_playlist(args: Dict[str, Any]) -> str:
    playlist_id, name = _resolve_playlist(args)
    payload = _request("POST", "/playlist/play", body={"id": playlist_id, "i": 0})
    label = name or f"id={playlist_id}"
    text = _playing_text(payload, f"开始播放歌单「{label}」")
    return text


def _set_volume(args: Dict[str, Any]) -> str:
    level = _clip_level(args)
    payload = _request("POST", "/volume", body={"level": level})
    return f"音量已设为 {_as_int(payload.get('volume'), level)}。"


def _set_quality(args: Dict[str, Any]) -> str:
    quality = str(args.get("quality") or "").strip().lower()
    if quality not in _QUALITY_LABELS:
        raise HubError("音质只能是 standard / high / lossless。")
    _request("POST", "/quality", body={"set": quality})
    return f"音质已设为 {_QUALITY_LABELS[quality]}。"


def _set_mode(args: Dict[str, Any]) -> str:
    mode = str(args.get("mode") or "").strip().lower()
    if mode not in _MODE_LABELS:
        raise HubError("播放模式只能是 sequential / loop / single / shuffle。")
    _request("POST", "/mode", body={"set": mode})
    return f"播放模式已设为：{_MODE_LABELS[mode]}。"


_TOOL_HANDLERS = {
    "play_music": _play_music,
    "pause_music": _pause_music,
    "resume_music": _resume_music,
    "stop_music": lambda args: _simple_command("/stop", "已停止播放。"),
    "next_music": _next_music,
    "prev_music": _prev_music,
    "music_status": _music_status,
    "search_music": _search_music,
    "list_playlists": _list_playlists,
    "play_playlist": _play_playlist,
    "set_volume": _set_volume,
    "set_quality": _set_quality,
    "set_mode": _set_mode,
}


# ---------------------------------------------------------------------------
# 工具声明（MCP tools/list）
# ---------------------------------------------------------------------------
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "play_music",
        "description": (
            "点歌并让音箱播放。用户说想听某首歌/某个歌手时调用；"
            "传入歌名、歌手或「歌手 歌名」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "歌名或歌手，例如：周杰伦 晴天"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "pause_music",
        "description": "暂停当前播放。暂停后中枢不会自动切歌。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "resume_music",
        "description": "继续播放。也用于恢复被小爱语音/其他控制器暂停的曲目（从原位置续播）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "stop_music",
        "description": "停止播放，并结束当前队列的自动续播。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "next_music",
        "description": "切到下一首。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "prev_music",
        "description": "切到上一首。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "music_status",
        "description": "查询当前播放状态：歌名、歌手、进度，以及是否被语音暂停。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_music",
        "description": "只在网易云搜索歌曲并返回候选列表，不播放。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认 5，最大 50",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_playlists",
        "description": "列出歌单（本地歌单 + 已导入的网易云歌单）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "play_playlist",
        "description": "按歌单名（或歌单 id）播放整个歌单。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "歌单名，支持包含匹配"},
                "id": {"type": "integer", "description": "歌单 id（与 name 二选一，优先 id）"},
            },
        },
    },
    {
        "name": "set_volume",
        "description": "设置音箱音量，0-100。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "integer", "minimum": 0, "maximum": 100,
                    "description": "目标音量",
                }
            },
            "required": ["level"],
        },
    },
    {
        "name": "set_quality",
        "description": "设置播放音质：standard(192k) / high(320k) / lossless(无损 FLAC)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "quality": {"type": "string", "enum": ["standard", "high", "lossless"]}
            },
            "required": ["quality"],
        },
    },
    {
        "name": "set_mode",
        "description": (
            "设置播放模式：sequential(顺序) / loop(列表循环) / "
            "single(单曲循环) / shuffle(随机)。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["sequential", "loop", "single", "shuffle"]}
            },
            "required": ["mode"],
        },
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC 分发
# ---------------------------------------------------------------------------
def _write(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _reply_result(message_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _reply_error(message_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _tool_result(text: str, is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def _call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return _tool_result(f"未知工具：{name}", is_error=True)
    _log(f"tools/call {name} {json.dumps(arguments, ensure_ascii=False)}")
    try:
        return _tool_result(handler(arguments))
    except HubError as exc:
        _log(f"工具 {name} 失败：{exc}")
        return _tool_result(str(exc), is_error=True)
    except Exception as exc:  # pragma: no cover - 兜底，避免 MCP 进程被打断
        _log(f"工具 {name} 异常：{exc!r}")
        return _tool_result(f"执行失败：{exc}", is_error=True)


def handle_message(message: Any) -> Optional[Dict[str, Any]]:
    """处理一条 JSON-RPC 消息；通知类消息返回 ``None``（不写 stdout）。"""
    if not isinstance(message, dict):
        return _reply_error(None, -32600, "Invalid Request")
    method = message.get("method")
    message_id = message.get("id")
    # JSON-RPC 通知：没有 id 字段（显式 id:null 仍按请求处理并回复）
    is_notification = "id" not in message

    if method == "initialize":
        params = message.get("params") or {}
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        _log(f"initialize（客户端声明的协议版本：{requested}）")
        return _reply_result(
            message_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": (
                    "小爱音箱音乐中枢。用户想听歌时调用 play_music；"
                    "用户说继续播放（包括被小爱语音暂停后）调用 resume_music；"
                    "已暂停/停止的曲目不会自动切歌。"
                ),
            },
        )

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _reply_result(message_id, {})

    if method == "tools/list":
        return _reply_result(message_id, {"tools": TOOLS})

    if method == "tools/call":
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _reply_error(message_id, -32602, "Invalid params")
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _reply_error(message_id, -32602, "Invalid params: arguments 必须是对象")
        return _reply_result(message_id, _call_tool(name, arguments))

    if is_notification:
        _log(f"忽略未知通知：{method}")
        return None
    return _reply_error(message_id, -32601, f"Method not found: {method}")


def main() -> int:
    """从 stdin 逐行读取 JSON-RPC 消息，直到 EOF；返回进程退出码。"""
    _log(f"{SERVER_NAME} v{__version__} 启动，中枢地址 {_base_url()}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _write(_reply_error(None, -32700, "Parse error"))
            continue
        if isinstance(message, list):  # JSON-RPC 批量请求
            replies = []
            for item in message:
                reply = handle_message(item)
                if reply is not None:
                    replies.append(reply)
            if replies:
                _write(replies)  # type: ignore[arg-type]
            continue
        reply = handle_message(message)
        if reply is not None:
            _write(reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
