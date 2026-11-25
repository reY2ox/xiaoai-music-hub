"""本地曲库：增量扫描、标签解析、转码缓存与媒体流输出。

模块契约见 ``docs/DESIGN.md`` 第 5.5 节，设计要点见第 4.3 / 4.4 节：

- 扫描按 ``(path, size, mtime)`` 增量更新，``prune=True`` 时清理已删除文件的记录；
- 下载完成只调用 :func:`scan_file` 落单条记录，避免全量扫描；
- 播放优先本地文件：:func:`local_for_song` 先按 ``song_key`` 再按下载命名约定兜底；
- 需要转码的格式输出到 ``<cache_dir>/<track_id>.mp3``，同一文件加进程内互斥锁；
- :func:`stream_file` 支持 ``Range``（206/416）与 ``HEAD``，客户端断开静默处理。

可选依赖 ``mutagen`` 用于读取标签；缺失时自动退化为文件名解析。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import config, db, errors, logging_setup

log = logging_setup.get_logger(__name__)

#: 流式输出分块大小（64KB，兼顾延迟与内存）
_CHUNK_SIZE = 64 * 1024

#: 各音频扩展名对应的 MIME（不依赖系统 mimetypes，保证跨平台一致）
_MIME_TYPES: Dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".ape": "audio/x-ape",
    ".wma": "audio/x-ms-wma",
}

#: 文件名开头的音轨号：``01.`` / ``01-`` / ``01_`` / ``01 名字`` 等
_TRACK_PREFIX = re.compile(r"^\s*(?:\d{1,3}\s*[.\-_、)]\s*|\d{2,3}\s+)")

#: 歌手/歌名分隔符，按从具体到宽泛的顺序尝试
_NAME_SEPARATORS: Tuple[str, ...] = (" - ", " – ", " — ", "—", "–", "-", "_")

#: 生成下载文件名时不允许的字符
_INVALID_COMPONENT = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

#: 下载命名约定兜底查找的扩展名顺序
_FALLBACK_EXTS: Tuple[str, ...] = (".mp3", ".flac", ".m4a")

_INSERT_TRACK = (
    "INSERT INTO local_tracks(path,title,artist,album,size,mtime,duration) VALUES(?,?,?,?,?,?,?)"
)
_UPDATE_TRACK = (
    "UPDATE local_tracks SET title=?,artist=?,album=?,size=?,mtime=?,duration=? WHERE id=?"
)

#: 同一 track_id 的转码互斥锁（进程内，惰性创建）
_TRANSCODE_LOCKS: Dict[int, threading.Lock] = {}
_TRANSCODE_GUARD = threading.Lock()


# ---------------------------------------------------------------------------
# 可选依赖与基础工具
# ---------------------------------------------------------------------------
def _load_mutagen() -> Any:
    """尽力导入可选依赖 ``mutagen``，缺失时返回 ``None``。"""
    try:
        import mutagen
    except ImportError:  # pragma: no cover - 取决于运行环境是否安装
        return None
    return mutagen


_MUTAGEN: Any = _load_mutagen()


def _as_int(value: Any) -> Optional[int]:
    """宽松转 int；失败返回 ``None``。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _field(track: Any, key: str, default: Any = None) -> Any:
    """同时兼容 ``dict`` 与 ``sqlite3.Row`` 的取值。"""
    try:
        return track[key]
    except (KeyError, IndexError, TypeError):
        return default


def _safe_component(text: Any) -> str:
    """把歌手/歌名清洗成可安全拼接进路径的片段。"""
    cleaned = _INVALID_COMPONENT.sub("_", str(text or "")).strip().strip(".")
    return cleaned


def _remove_quiet(path: str) -> None:
    """删除文件，忽略不存在/无权限等错误。"""
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 文件名与标签
# ---------------------------------------------------------------------------
def parse_name(path: Any) -> Tuple[str, str]:
    """从文件名猜测 ``(title, artist)``。

    支持的命名：

    - ``周杰伦 - 晴天.mp3`` → ``("晴天", "周杰伦")``
    - ``周杰伦-晴天.mp3`` / ``周杰伦_晴天.mp3`` → 同上
    - ``晴天.mp3``（无分隔符）→ ``("晴天", "")``
    - ``01. 晴天.mp3`` / ``01 - 周杰伦 - 晴天.mp3`` → 先去掉开头音轨号
    """
    base = os.path.splitext(os.path.basename(str(path)))[0]
    base = _TRACK_PREFIX.sub("", base).strip()
    if not base:
        return "", ""
    for separator in _NAME_SEPARATORS:
        if separator not in base:
            continue
        left, right = base.split(separator, 1)
        left, right = left.strip(), right.strip()
        if left and right:
            return right, left
        break
    return base, ""


def _read_tags(path: str) -> Dict[str, Any]:
    """优先用 ``mutagen`` 读取标签，返回 ``title/artist/album/duration``。

    任何失败（未安装 mutagen、格式不支持、文件损坏）都返回空字典，
    扫描流程继续使用文件名兜底。
    """
    if _MUTAGEN is None:
        return {}
    try:
        audio = _MUTAGEN.File(path, easy=True)
        if audio is None:
            return {}
        info = getattr(audio, "info", None)
        duration = float(getattr(info, "length", 0.0) or 0.0)
        tags = getattr(audio, "tags", None) or {}

        def first(key: str) -> str:
            try:
                values = tags.get(key) or []
            except (AttributeError, TypeError):
                return ""
            if isinstance(values, str):
                return values.strip()
            if not values:
                return ""
            return str(values[0]).strip()

        return {
            "title": first("title"),
            "artist": first("artist"),
            "album": first("album"),
            "duration": round(duration, 3) if duration > 0 else 0.0,
        }
    except Exception as exc:  # mutagen 对损坏文件会抛各类异常，这里统一降级
        log.debug("读取标签失败 %s: %s", path, exc)
        return {}


def _describe(path: str, st: Any) -> Dict[str, Any]:
    """汇总一个音频文件的入库字段：标签优先，文件名/目录名兜底。"""
    tags = _read_tags(path)
    fallback_title, fallback_artist = parse_name(path)
    album = tags.get("album") or os.path.basename(os.path.dirname(path))
    duration = tags.get("duration") or 0.0
    try:
        duration_value = round(float(duration), 3)
    except (TypeError, ValueError):
        duration_value = 0.0
    return {
        "path": path,
        "title": str(tags.get("title") or "").strip() or fallback_title,
        "artist": str(tags.get("artist") or "").strip() or fallback_artist,
        "album": str(album or "").strip(),
        "size": int(st.st_size),
        "mtime": int(st.st_mtime),
        "duration": max(0.0, duration_value),
    }


def _iter_audio(root: str) -> Iterator[Tuple[str, Any]]:
    """遍历 ``root`` 下的音频文件，产出 ``(绝对路径, stat)``。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.lower().endswith(config.AUDIO_EXT):
                continue
            path = os.path.normpath(os.path.join(dirpath, name))
            try:
                st = os.stat(path)
            except OSError as exc:
                log.debug("跳过无法访问的文件 %s: %s", path, exc)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            yield path, st


def _under_root(path: str, root: str) -> bool:
    """判断 ``path`` 是否位于 ``root`` 目录内。"""
    return path == root or path.startswith(root + os.sep)


# ---------------------------------------------------------------------------
# 扫描与索引
# ---------------------------------------------------------------------------
def scan(root: Optional[str] = None, *, incremental: bool = True, prune: bool = False) -> dict:
    """扫描曲库目录并增量更新 ``local_tracks``。

    参数
    ----
    root: 曲库根目录，缺省为 ``config.get().paths.music_dir``。
    incremental: ``True`` 时仅处理 ``(size, mtime)`` 变化的文件；``False`` 强制刷新全部。
    prune: ``True`` 时删除 ``root`` 下已不存在的文件记录。

    返回 ``{"added", "updated", "removed", "total", "elapsed"}``，
    其中 ``total`` 是 ``root`` 范围内最终索引条数。
    """
    settings = config.get()
    target_root = os.path.normpath(os.path.abspath(str(root or settings.paths.music_dir)))
    os.makedirs(target_root, exist_ok=True)
    started = time.monotonic()

    entries = [_describe(path, st) for path, st in _iter_audio(target_root)]
    seen = {entry["path"] for entry in entries}
    added = updated = removed = 0

    with db.transaction() as conn:
        existing = {
            str(row["path"]): (int(row["id"]), int(row["size"] or 0), int(row["mtime"] or 0))
            for row in conn.execute("SELECT id,path,size,mtime FROM local_tracks")
        }
        for entry in entries:
            current = existing.get(entry["path"])
            if current is None:
                conn.execute(
                    _INSERT_TRACK,
                    (
                        entry["path"],
                        entry["title"],
                        entry["artist"],
                        entry["album"],
                        entry["size"],
                        entry["mtime"],
                        entry["duration"],
                    ),
                )
                added += 1
                continue
            row_id, size, mtime = current
            if incremental and size == entry["size"] and mtime == entry["mtime"]:
                continue
            conn.execute(
                _UPDATE_TRACK,
                (
                    entry["title"],
                    entry["artist"],
                    entry["album"],
                    entry["size"],
                    entry["mtime"],
                    entry["duration"],
                    row_id,
                ),
            )
            updated += 1

        if prune:
            for path, (row_id, _size, _mtime) in existing.items():
                if path in seen or not _under_root(path, target_root):
                    continue
                conn.execute("DELETE FROM local_tracks WHERE id=?", (row_id,))
                removed += 1

        total = sum(
            1
            for row in conn.execute("SELECT path FROM local_tracks")
            if _under_root(str(row["path"]), target_root)
        )

    elapsed = round(time.monotonic() - started, 3)
    if added or updated or removed:
        log.info(
            "曲库扫描完成：新增 %d，更新 %d，删除 %d，共 %d，用时 %.3fs",
            added,
            updated,
            removed,
            total,
            elapsed,
        )
    return {
        "added": added,
        "updated": updated,
        "removed": removed,
        "total": total,
        "elapsed": elapsed,
    }


def scan_file(path: Any) -> Optional[dict]:
    """单文件入库/更新，返回该行 ``dict``（非音频或不可读返回 ``None``）。

    供下载完成后即时入库使用，不做全量扫描。
    """
    target = os.path.normpath(os.path.abspath(str(path)))
    if not target.lower().endswith(config.AUDIO_EXT):
        log.debug("不是音频文件，跳过索引：%s", target)
        return None
    try:
        st = os.stat(target)
    except OSError as exc:
        log.warning("无法读取音频文件 %s: %s", target, exc)
        return None
    if not stat.S_ISREG(st.st_mode):
        return None

    entry = _describe(target, st)
    params = (
        entry["title"],
        entry["artist"],
        entry["album"],
        entry["size"],
        entry["mtime"],
        entry["duration"],
    )
    with db.transaction() as conn:
        row = conn.execute("SELECT id FROM local_tracks WHERE path=?", (target,)).fetchone()
        if row is None:
            cursor = conn.execute(_INSERT_TRACK, (target,) + params)
            row_id = int(cursor.lastrowid or 0)
        else:
            row_id = int(row["id"])
            conn.execute(_UPDATE_TRACK, params + (row_id,))
        record = conn.execute("SELECT * FROM local_tracks WHERE id=?", (row_id,)).fetchone()
        return dict(record) if record is not None else None


def list_tracks(query: str = "", limit: int = 200, offset: int = 0) -> List[dict]:
    """按 ``title/artist/album`` 模糊搜索曲库，按 ``artist, title`` 排序。"""
    limit_value = _as_int(limit)
    offset_value = _as_int(offset)
    limit_value = 200 if limit_value is None else max(0, limit_value)
    offset_value = 0 if offset_value is None else max(0, offset_value)
    text = str(query or "").strip()
    if text:
        pattern = f"%{text}%"
        rows = db.query(
            "SELECT * FROM local_tracks "
            "WHERE title LIKE ? OR artist LIKE ? OR album LIKE ? "
            "ORDER BY artist, title LIMIT ? OFFSET ?",
            (pattern, pattern, pattern, limit_value, offset_value),
        )
    else:
        rows = db.query(
            "SELECT * FROM local_tracks ORDER BY artist, title LIMIT ? OFFSET ?",
            (limit_value, offset_value),
        )
    return [dict(row) for row in rows]


def local_for_song(
    song_id: Optional[int],
    name: Optional[str] = None,
    artist: Optional[str] = None,
) -> Optional[dict]:
    """查找某首歌在本地曲库里对应的文件行。

    顺序：先按 ``local_tracks.song_key``；未命中再按下载命名约定兜底
    （``MUSIC_DIR/<artist>/<artist> - <name>.<ext>`` 与 ``MUSIC_DIR/<artist> - <name>.<ext>``）。
    记录存在但文件已被删除时视为未命中。
    """
    key = _as_int(song_id)
    if key is not None:
        row = db.query_one(
            "SELECT * FROM local_tracks WHERE song_key=? ORDER BY id LIMIT 1", (key,)
        )
        if row is not None and os.path.isfile(str(row["path"] or "")):
            return dict(row)

    title_part = _safe_component(name) if name else ""
    if not title_part:
        return None
    artist_part = _safe_component(artist) if artist else ""
    music_dir = config.get().paths.music_dir

    stems: List[str] = []
    if artist_part:
        stems.append(os.path.join(music_dir, artist_part, f"{artist_part} - {title_part}"))
        stems.append(os.path.join(music_dir, f"{artist_part} - {title_part}"))
    else:
        stems.append(os.path.join(music_dir, title_part))

    for stem in stems:
        for ext in _FALLBACK_EXTS:
            candidate = os.path.normpath(stem + ext)
            if not os.path.isfile(candidate):
                continue
            row = db.query_one("SELECT * FROM local_tracks WHERE path=?", (candidate,))
            if row is not None:
                return dict(row)
    return None


def index_downloaded(
    path: Any,
    song_id: int,
    title: Optional[str] = None,
    artist: Optional[str] = None,
) -> None:
    """把下载完成的文件与网易云 ``song_id`` 绑定（更新/补齐 ``song_key``）。

    文件尚未入库时自动补一条最小记录；``title``/``artist`` 为空则保留原值。
    """
    target = os.path.normpath(os.path.abspath(str(path)))
    key = _as_int(song_id)
    if key is None:
        raise errors.BadRequest(f"无效的歌曲 ID：{song_id!r}")
    title_value = str(title).strip() if title else None
    artist_value = str(artist).strip() if artist else None

    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE local_tracks SET song_key=?, "
            "title=COALESCE(?, title), artist=COALESCE(?, artist) WHERE path=?",
            (key, title_value, artist_value, target),
        )
        if cursor.rowcount:
            return

        try:
            st = os.stat(target)
        except OSError:
            log.warning("索引下载文件失败，文件不存在：%s", target)
            return
        if not stat.S_ISREG(st.st_mode):
            return
        entry = _describe(target, st)
        conn.execute(
            "INSERT INTO local_tracks"
            "(path,title,artist,album,size,mtime,song_key,duration) VALUES(?,?,?,?,?,?,?,?)",
            (
                target,
                title_value or entry["title"],
                artist_value or entry["artist"],
                entry["album"],
                entry["size"],
                entry["mtime"],
                key,
                entry["duration"],
            ),
        )


# ---------------------------------------------------------------------------
# 转码与媒体地址
# ---------------------------------------------------------------------------
def _transcode_lock(track_id: int) -> threading.Lock:
    """取 ``track_id`` 对应的进程内互斥锁（同一文件避免并发转码）。"""
    with _TRANSCODE_GUARD:
        lock = _TRANSCODE_LOCKS.get(track_id)
        if lock is None:
            lock = threading.Lock()
            _TRANSCODE_LOCKS[track_id] = lock
        return lock


def _source_mtime(src: str, fallback: Any) -> float:
    """源文件 mtime（秒）；文件不存在时退回数据库里的 mtime。"""
    try:
        return float(os.path.getmtime(src))
    except OSError:
        try:
            return float(fallback or 0)
        except (TypeError, ValueError):
            return 0.0


def _cache_fresh(out: str, source_mtime: float) -> bool:
    """转码缓存是否有效：存在且 mtime 不早于源文件。"""
    try:
        return os.path.getmtime(out) >= source_mtime
    except OSError:
        return False


def _transcode(src: str, out: str) -> str:
    """调用 ffmpeg 把 ``src`` 转成 320k mp3，临时文件 + ``os.replace`` 原子落盘。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise errors.UpstreamError(
            "缺少 ffmpeg，请先安装：macOS 可执行 `brew install ffmpeg`，"
            "Debian/Ubuntu 可执行 `apt install ffmpeg`。"
        )
    tmp = f"{out}.{os.getpid()}.{threading.get_ident()}.tmp.mp3"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        src,
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "320k",
        tmp,
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        _remove_quiet(tmp)
        raise errors.UpstreamError("音频转码超时，请检查文件是否损坏") from exc
    except OSError as exc:
        _remove_quiet(tmp)
        raise errors.UpstreamError(f"无法启动 ffmpeg：{exc}") from exc

    if completed.returncode != 0:
        _remove_quiet(tmp)
        detail = (completed.stderr or "").strip().splitlines()
        message = detail[-1] if detail else "未知错误"
        raise errors.UpstreamError(f"音频转码失败：{message}")
    if not os.path.exists(tmp):
        raise errors.UpstreamError("音频转码失败：未生成输出文件")
    try:
        os.replace(tmp, out)
    except OSError as exc:
        _remove_quiet(tmp)
        raise errors.UpstreamError(f"写入转码缓存失败：{exc}") from exc

    log.info("已转码缓存：%s -> %s", os.path.basename(src), out)
    return out


def media_path(track: Any) -> str:
    """返回给音箱播放的实际文件路径；需要转码时返回缓存里的 mp3。

    ``track`` 可为 ``dict`` 或 ``sqlite3.Row``，需包含 ``id``/``path``（可选 ``mtime``）。
    """
    src = str(_field(track, "path") or "")
    if not src:
        raise errors.NotFound("曲目缺少文件路径")
    extension = os.path.splitext(src)[1].lower()
    if extension not in config.NEED_TRANSCODE:
        return src

    track_id = _as_int(_field(track, "id"))
    if track_id is None:
        raise errors.BadRequest("曲目缺少 id，无法定位转码缓存")
    cache_dir = config.get().paths.cache_dir
    os.makedirs(cache_dir, exist_ok=True)
    out = os.path.join(cache_dir, f"{track_id}.mp3")
    source_mtime = _source_mtime(src, _field(track, "mtime"))
    if _cache_fresh(out, source_mtime):
        return out

    with _transcode_lock(track_id):
        if _cache_fresh(out, source_mtime):
            return out
        return _transcode(src, out)


def media_url(track: Any, token: str = "") -> str:
    """生成 DLNA 渲染器可访问的媒体 URL（``/media/<id>``，可带媒体令牌）。"""
    track_id = _as_int(_field(track, "id"))
    if track_id is None:
        raise errors.BadRequest("曲目缺少 id，无法生成媒体链接")
    settings = config.get()
    url = f"http://{settings.lan_ip}:{settings.port}/media/{track_id}"
    if token:
        url = f"{url}?t={token}"
    return url


def guess_mime(path: Any) -> str:
    """按扩展名猜测 MIME，未知类型返回 ``application/octet-stream``。"""
    extension = os.path.splitext(str(path))[1].lower()
    return _MIME_TYPES.get(extension, "application/octet-stream")


# ---------------------------------------------------------------------------
# 媒体流输出
# ---------------------------------------------------------------------------
def _header(handler: Any, name: str) -> Optional[str]:
    """读取请求头（``email.message.Message`` 与普通 ``dict`` 都兼容）。"""
    headers = getattr(handler, "headers", None)
    if not headers:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(name)
    if value is None:
        for candidate in (name.lower(), name.upper()):
            value = getter(candidate)
            if value is not None:
                break
    if value is None:
        return None
    return str(value)


def _parse_range(value: str, size: int) -> Optional[Tuple[int, int]]:
    """解析单区间 ``Range``，返回 ``(start, end)``；非法/越界返回 ``None``。

    支持 ``bytes=a-b``、``bytes=a-``、``bytes=-n``；多区间与其它单位不支持（按 416 处理）。
    """
    text = (value or "").strip()
    if not text.startswith("bytes="):
        return None
    spec = text[len("bytes=") :].strip()
    if not spec or "," in spec or "-" not in spec:
        return None
    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    if not first and not last:
        return None
    try:
        if not first:
            suffix = int(last)
            if suffix <= 0 or size <= 0:
                return None
            suffix = min(suffix, size)
            return size - suffix, size - 1
        start = int(first)
        if start < 0 or start >= size:
            return None
        if not last:
            return start, size - 1
        end = int(last)
        if end < start:
            return None
        return start, min(end, size - 1)
    except ValueError:
        return None


def _send_headers(handler: Any, status: int, headers: List[Tuple[str, str]]) -> None:
    """发送状态行与响应头。"""
    handler.send_response(status)
    for name, value in headers:
        handler.send_header(name, value)
    handler.end_headers()


def stream_file(handler: Any, path: Any, mime: Optional[str] = None) -> None:
    """向 HTTP handler 输出音频流。

    - 支持 ``HEAD``（只发响应头）；
    - 支持单区间 ``Range``（200/206），非法或越界返回 416 + ``Content-Range: bytes */size``；
    - 64KB 分块写出；客户端断开（``BrokenPipe`` / ``ConnectionReset``）静默返回。
    """
    file_path = str(path)
    file_size = os.path.getsize(file_path)
    content_type = mime or guess_mime(file_path)
    range_header = _header(handler, "Range")

    if range_header is None:
        status, start, end = 200, 0, max(0, file_size - 1)
    else:
        bounds = _parse_range(range_header, file_size)
        if bounds is None:
            _send_headers(
                handler,
                416,
                [
                    ("Content-Range", f"bytes */{file_size}"),
                    ("Accept-Ranges", "bytes"),
                    ("Content-Length", "0"),
                    ("Cache-Control", "no-store"),
                ],
            )
            return
        start, end = bounds
        status = 206

    length = max(0, end - start + 1) if file_size else 0
    headers: List[Tuple[str, str]] = [
        ("Content-Type", content_type),
        ("Accept-Ranges", "bytes"),
        ("Content-Length", str(length)),
        ("Cache-Control", "no-store"),
    ]
    if status == 206:
        headers.append(("Content-Range", f"bytes {start}-{end}/{file_size}"))
    _send_headers(handler, status, headers)

    if str(getattr(handler, "command", "GET") or "GET").upper() == "HEAD":
        return
    if length <= 0:
        return

    try:
        with open(file_path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        return


# ---------------------------------------------------------------------------
# 缓存与统计
# ---------------------------------------------------------------------------
def cache_cleanup(max_mb: Optional[float] = None) -> int:
    """按 mtime LRU 清理转码缓存，返回释放的字节数。

    ``max_mb`` 缺省取 ``config.get().cache_max_mb``；``0`` 表示清空整个缓存目录。
    """
    settings = config.get()
    if max_mb is None:
        limit_mb = float(settings.cache_max_mb)
    else:
        try:
            limit_mb = float(max_mb)
        except (TypeError, ValueError):
            limit_mb = float(settings.cache_max_mb)
    limit_bytes = max(0.0, limit_mb) * 1024 * 1024
    cache_dir = settings.paths.cache_dir

    entries: List[Tuple[float, str, int]] = []
    for dirpath, _dirnames, filenames in os.walk(cache_dir):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            entries.append((st.st_mtime, path, int(st.st_size)))

    total = sum(size for _mtime, _path, size in entries)
    if total <= limit_bytes:
        return 0

    freed = 0
    for _mtime, path, size in sorted(entries):
        if total - freed <= limit_bytes:
            break
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("删除缓存文件失败 %s: %s", path, exc)
            continue
        freed += size
    if freed:
        log.info("已清理转码缓存 %.1f MB", freed / (1024 * 1024))
    return freed


def stats() -> dict:
    """曲库统计：``{"tracks": 条数, "bytes": 曲库总大小, "cache_bytes": 缓存大小}``。"""
    row = db.query_one(
        "SELECT COUNT(*) AS tracks, COALESCE(SUM(size), 0) AS bytes FROM local_tracks"
    )
    tracks = int(row["tracks"] or 0) if row is not None else 0
    library_bytes = int(row["bytes"] or 0) if row is not None else 0

    cache_bytes = 0
    cache_dir = config.get().paths.cache_dir
    for dirpath, _dirnames, filenames in os.walk(cache_dir):
        for name in filenames:
            try:
                cache_bytes += int(os.path.getsize(os.path.join(dirpath, name)))
            except OSError:
                continue
    return {"tracks": tracks, "bytes": library_bytes, "cache_bytes": cache_bytes}
