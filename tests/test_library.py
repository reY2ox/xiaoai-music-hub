"""本地曲库模块单测：扫描、索引、查找、转码缓存与媒体流输出。

全部用例使用 ``tmp_hub`` 夹具（独立临时目录 + 已初始化数据库），
不需要网络；转码相关用例在缺少 ffmpeg 时自动跳过。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from xiaoai_hub import config, db, errors, library

#: 假音频内容（>1KB，扫描只看扩展名与 stat，内容不影响结果）
AUDIO_BYTES = bytes(range(256)) * 12

#: 测试用固定 mtime，避免依赖真实时间
FIXED_MTIME = 1_600_000_000

FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="需要 ffmpeg")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _write(path, data: bytes = AUDIO_BYTES) -> Path:
    """写一个假的音频文件并返回其路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _set_mtime(path, timestamp: int) -> None:
    os.utime(str(path), (timestamp, timestamp))


def _count_rows() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM local_tracks")
    return int(row["n"]) if row is not None else 0


def _make_flac(path, seconds: int = 1) -> Path:
    """用 ffmpeg 生成一段正弦波 flac，用于验证转码链路。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-ac",
            "1",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return target


class FakeHandler:
    """最小 HTTP handler 替身：捕获状态码、响应头与正文。"""

    def __init__(self, command: str = "GET", range_header=None) -> None:
        self.command = command
        self.headers = {} if range_header is None else {"Range": range_header}
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []
        self.ended = False

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, keyword: str, value: str) -> None:
        self.sent_headers.append((keyword, value))

    def end_headers(self) -> None:
        self.ended = True

    def header(self, name: str):
        lowered = name.lower()
        for keyword, value in reversed(self.sent_headers):
            if keyword.lower() == lowered:
                return value
        return None

    @property
    def body(self) -> bytes:
        return self.wfile.getvalue()


class _FailingWriter:
    """写入即抛错的 wfile 替身，模拟客户端断开。"""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def write(self, _data: bytes) -> None:
        raise self.exc


# ---------------------------------------------------------------------------
# parse_name / _read_tags
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "filename,expected",
    [
        ("周杰伦 - 晴天.mp3", ("晴天", "周杰伦")),
        ("周杰伦-晴天.mp3", ("晴天", "周杰伦")),
        ("周杰伦—晴天.mp3", ("晴天", "周杰伦")),
        ("周杰伦 – 晴天.mp3", ("晴天", "周杰伦")),
        ("周杰伦_晴天.flac", ("晴天", "周杰伦")),
        ("晴天.mp3", ("晴天", "")),
        ("01. 晴天.mp3", ("晴天", "")),
        ("01 - 周杰伦 - 晴天.mp3", ("晴天", "周杰伦")),
        ("02_林俊杰_江南.m4a", ("江南", "林俊杰")),
        ("7 rings.mp3", ("7 rings", "")),
        ("/music/周杰伦 - 晴天.mp3", ("晴天", "周杰伦")),
    ],
)
def test_parse_name_variants(filename, expected):
    assert library.parse_name(filename) == expected


def test_read_tags_returns_empty_for_fake_audio(tmp_hub):
    path = _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    assert library._read_tags(str(path)) == {}


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------
def test_scan_adds_then_skips_unchanged(tmp_hub):
    _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    _write(tmp_hub.music_dir / "林俊杰 - 江南.flac")
    _write(tmp_hub.music_dir / "周杰伦" / "周杰伦 - 七里香.mp3")

    first = library.scan()
    assert (first["added"], first["updated"], first["removed"]) == (3, 0, 0)
    assert first["total"] == 3
    assert isinstance(first["elapsed"], float) and first["elapsed"] >= 0
    assert _count_rows() == 3

    second = library.scan()
    assert (second["added"], second["updated"], second["removed"]) == (0, 0, 0)
    assert second["total"] == 3


def test_scan_updates_changed_file(tmp_hub):
    path = _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    library.scan()
    _write(path, AUDIO_BYTES + b"changed")
    _set_mtime(path, FIXED_MTIME)

    result = library.scan()
    assert (result["added"], result["updated"]) == (0, 1)
    row = db.query_one("SELECT * FROM local_tracks WHERE path=?", (str(path),))
    assert row is not None
    assert row["size"] == len(AUDIO_BYTES) + len(b"changed")
    assert row["mtime"] == FIXED_MTIME


def test_scan_incremental_false_refreshes_everything(tmp_hub):
    _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    _write(tmp_hub.music_dir / "林俊杰 - 江南.mp3")
    library.scan()

    forced = library.scan(incremental=False)
    assert (forced["added"], forced["updated"], forced["total"]) == (0, 2, 2)


def test_scan_prune_removes_missing_files(tmp_hub):
    keep = _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    gone = _write(tmp_hub.music_dir / "林俊杰 - 江南.mp3")
    library.scan()

    os.remove(gone)
    result = library.scan(prune=True)
    assert (result["removed"], result["total"]) == (1, 1)
    assert db.query_one("SELECT 1 FROM local_tracks WHERE path=?", (str(gone),)) is None
    assert db.query_one("SELECT 1 FROM local_tracks WHERE path=?", (str(keep),)) is not None


def test_scan_without_prune_keeps_stale_rows(tmp_hub):
    path = _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    library.scan()
    os.remove(path)

    result = library.scan(prune=False)
    assert (result["removed"], result["total"]) == (0, 1)
    assert _count_rows() == 1


def test_scan_prefers_tags_over_filename(tmp_hub, monkeypatch):
    _write(tmp_hub.music_dir / "未知 - 未知.mp3")
    tags = {"title": "晴天", "artist": "周杰伦", "album": "叶惠美", "duration": 269.5}
    monkeypatch.setattr(library, "_read_tags", lambda path: dict(tags))

    result = library.scan()
    assert result["added"] == 1
    row = library.list_tracks()[0]
    assert (row["title"], row["artist"], row["album"]) == ("晴天", "周杰伦", "叶惠美")
    assert row["duration"] == 269.5


def test_scan_album_falls_back_to_parent_dir(tmp_hub):
    _write(tmp_hub.music_dir / "叶惠美" / "周杰伦 - 晴天.mp3")
    library.scan()

    row = library.list_tracks()[0]
    assert row["title"] == "晴天"
    assert row["artist"] == "周杰伦"
    assert row["album"] == "叶惠美"


def test_scan_ignores_non_audio_files(tmp_hub):
    _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    (tmp_hub.music_dir / "readme.txt").write_text("not audio", encoding="utf-8")

    result = library.scan()
    assert result["added"] == 1
    assert result["total"] == 1


def test_scan_creates_missing_root(tmp_hub):
    root = tmp_hub.music_dir / "还不存在的目录"
    assert not root.exists()

    result = library.scan(root=str(root))
    assert root.is_dir()
    assert result["added"] == 0 and result["total"] == 0


def test_scan_custom_root_scope(tmp_hub):
    sub = tmp_hub.music_dir / "专辑A"
    _write(sub / "周杰伦 - 晴天.mp3")
    _write(tmp_hub.music_dir / "林俊杰 - 江南.mp3")

    scoped = library.scan(root=str(sub))
    assert (scoped["added"], scoped["total"]) == (1, 1)

    full = library.scan()
    assert (full["added"], full["total"]) == (1, 2)


def test_scan_prune_only_touches_given_root(tmp_hub):
    sub = tmp_hub.music_dir / "专辑A"
    scoped_file = _write(sub / "周杰伦 - 晴天.mp3")
    other_file = _write(tmp_hub.music_dir / "林俊杰 - 江南.mp3")
    library.scan()

    os.remove(scoped_file)
    result = library.scan(root=str(sub), prune=True)
    assert (result["removed"], result["total"]) == (1, 0)
    assert db.query_one("SELECT 1 FROM local_tracks WHERE path=?", (str(other_file),)) is not None


# ---------------------------------------------------------------------------
# scan_file
# ---------------------------------------------------------------------------
def test_scan_file_upsert(tmp_hub):
    path = _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    created = library.scan_file(str(path))
    assert created is not None
    assert created["title"] == "晴天" and created["artist"] == "周杰伦"
    assert created["size"] == len(AUDIO_BYTES)

    again = library.scan_file(str(path))
    assert again is not None and again["id"] == created["id"]
    assert _count_rows() == 1

    _write(path, AUDIO_BYTES + b"plus")
    _set_mtime(path, FIXED_MTIME + 5)
    updated = library.scan_file(str(path))
    assert updated is not None and updated["id"] == created["id"]
    assert updated["size"] == len(AUDIO_BYTES) + len(b"plus")
    assert updated["mtime"] == FIXED_MTIME + 5


def test_scan_file_rejects_non_audio_and_missing(tmp_hub):
    missing = tmp_hub.music_dir / "不存在.mp3"
    assert library.scan_file(str(missing)) is None

    text = tmp_hub.music_dir / "note.txt"
    text.write_text("x", encoding="utf-8")
    assert library.scan_file(str(text)) is None


# ---------------------------------------------------------------------------
# list_tracks
# ---------------------------------------------------------------------------
def test_list_tracks_search_and_paging(tmp_hub):
    _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    _write(tmp_hub.music_dir / "周杰伦 - 七里香.mp3")
    _write(tmp_hub.music_dir / "林俊杰 - 江南.mp3")
    library.scan()

    all_rows = library.list_tracks()
    assert [(row["artist"], row["title"]) for row in all_rows] == [
        ("周杰伦", "七里香"),
        ("周杰伦", "晴天"),
        ("林俊杰", "江南"),
    ]
    assert len(library.list_tracks("周杰伦")) == 2
    assert [row["title"] for row in library.list_tracks("晴天")] == ["晴天"]
    assert [row["title"] for row in library.list_tracks("江南")] == ["江南"]
    assert library.list_tracks("不存在的歌") == []

    page = library.list_tracks(limit=1, offset=1)
    assert len(page) == 1 and page[0]["title"] == "晴天"
    assert library.list_tracks(limit=0) == []


# ---------------------------------------------------------------------------
# local_for_song / index_downloaded
# ---------------------------------------------------------------------------
def test_local_for_song_by_song_key(tmp_hub):
    path = _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    library.scan()
    library.index_downloaded(str(path), 1001)

    hit = library.local_for_song(1001)
    assert hit is not None and hit["path"] == str(path) and hit["song_key"] == 1001
    assert library.local_for_song("1001") is not None


def test_local_for_song_path_fallback(tmp_hub):
    nested = _write(tmp_hub.music_dir / "林俊杰" / "林俊杰 - 江南.flac")
    flat = _write(tmp_hub.music_dir / "周杰伦 - 晴天.m4a")
    library.scan()

    nested_hit = library.local_for_song(2002, "江南", "林俊杰")
    assert nested_hit is not None and nested_hit["path"] == str(nested)
    flat_hit = library.local_for_song(3003, "晴天", "周杰伦")
    assert flat_hit is not None and flat_hit["path"] == str(flat)
    assert library.local_for_song(4004, "不存在", "无人") is None
    assert library.local_for_song(None) is None


def test_local_for_song_requires_indexed_row(tmp_hub):
    library.scan()
    _write(tmp_hub.music_dir / "未入库 - 歌.mp3")
    assert library.local_for_song(5005, "歌", "未入库") is None


def test_local_for_song_skips_deleted_file(tmp_hub):
    path = _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    library.scan()
    library.index_downloaded(str(path), 6006)
    os.remove(path)

    assert library.local_for_song(6006) is None


def test_index_downloaded_backfills_missing_row(tmp_hub):
    path = _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    library.index_downloaded(str(path), 7007, title="晴天", artist="周杰伦")

    row = db.query_one("SELECT * FROM local_tracks WHERE path=?", (str(path),))
    assert row is not None
    assert row["song_key"] == 7007
    assert (row["title"], row["artist"]) == ("晴天", "周杰伦")


def test_index_downloaded_preserves_fields_on_rescan(tmp_hub):
    path = _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    library.scan()
    library.index_downloaded(str(path), 8008, title="晴天", artist="周杰伦")
    library.scan_file(str(path))

    row = db.query_one("SELECT * FROM local_tracks WHERE path=?", (str(path),))
    assert row is not None and row["song_key"] == 8008
    assert (row["title"], row["artist"]) == ("晴天", "周杰伦")

    library.index_downloaded(str(path), 8009)
    row = db.query_one("SELECT * FROM local_tracks WHERE path=?", (str(path),))
    assert row is not None and row["song_key"] == 8009 and row["title"] == "晴天"


def test_index_downloaded_rejects_invalid_song_id(tmp_hub):
    path = _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    with pytest.raises(errors.BadRequest):
        library.index_downloaded(str(path), "not-a-number")


# ---------------------------------------------------------------------------
# media_path / media_url / guess_mime
# ---------------------------------------------------------------------------
def test_media_path_returns_mp3_unchanged(tmp_hub):
    path = _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3")
    track = {"id": 1, "path": str(path), "mtime": int(os.path.getmtime(path))}
    assert library.media_path(track) == str(path)

    library.scan()
    row = db.query_one("SELECT * FROM local_tracks WHERE path=?", (str(path),))
    assert library.media_path(row) == str(path)


def test_media_path_requires_id_for_transcode(tmp_hub):
    track = {"path": str(tmp_hub.music_dir / "无 id.flac")}
    with pytest.raises(errors.BadRequest):
        library.media_path(track)


def test_media_url(tmp_hub):
    assert library.media_url({"id": 7}) == "http://192.168.0.10:8010/media/7"
    assert library.media_url({"id": 7}, "abc123") == "http://192.168.0.10:8010/media/7?t=abc123"


@pytest.mark.parametrize(
    "filename,mime",
    [
        ("a.mp3", "audio/mpeg"),
        ("a.FLAC", "audio/flac"),
        ("a.m4a", "audio/mp4"),
        ("a.wav", "audio/wav"),
        ("a.ogg", "audio/ogg"),
        ("a.ape", "audio/x-ape"),
        ("a.wma", "audio/x-ms-wma"),
        ("a.txt", "application/octet-stream"),
    ],
)
def test_guess_mime(filename, mime):
    assert library.guess_mime(filename) == mime


# ---------------------------------------------------------------------------
# stream_file
# ---------------------------------------------------------------------------
def test_stream_file_full_response(tmp_hub):
    path = _write(tmp_hub.music_dir / "stream.mp3", AUDIO_BYTES)
    handler = FakeHandler()

    library.stream_file(handler, str(path))

    assert handler.status == 200 and handler.ended
    assert handler.header("Content-Type") == "audio/mpeg"
    assert handler.header("Accept-Ranges") == "bytes"
    assert handler.header("Content-Length") == str(len(AUDIO_BYTES))
    assert handler.header("Cache-Control") == "no-store"
    assert handler.header("Content-Range") is None
    assert handler.body == AUDIO_BYTES


def test_stream_file_head_only(tmp_hub):
    path = _write(tmp_hub.music_dir / "stream.flac", AUDIO_BYTES)
    handler = FakeHandler(command="HEAD")

    library.stream_file(handler, str(path))

    assert handler.status == 200
    assert handler.header("Content-Type") == "audio/flac"
    assert handler.header("Content-Length") == str(len(AUDIO_BYTES))
    assert handler.body == b""


def test_stream_file_mime_override(tmp_hub):
    path = _write(tmp_hub.music_dir / "stream.bin", AUDIO_BYTES)
    handler = FakeHandler()

    library.stream_file(handler, str(path), mime="audio/mpeg")

    assert handler.header("Content-Type") == "audio/mpeg"


def test_stream_file_empty_file(tmp_hub):
    path = _write(tmp_hub.music_dir / "empty.mp3", b"")
    handler = FakeHandler()
    library.stream_file(handler, str(path))
    assert handler.status == 200
    assert handler.header("Content-Length") == "0"
    assert handler.body == b""


@pytest.mark.parametrize(
    "range_header,start,end",
    [
        ("bytes=0-99", 0, 99),
        ("bytes=100-", 100, len(AUDIO_BYTES) - 1),
        ("bytes=-100", len(AUDIO_BYTES) - 100, len(AUDIO_BYTES) - 1),
        ("bytes=0-999999", 0, len(AUDIO_BYTES) - 1),
        ("bytes=2000-2500", 2000, 2500),
    ],
)
def test_stream_file_range_requests(tmp_hub, range_header, start, end):
    path = _write(tmp_hub.music_dir / "stream.mp3", AUDIO_BYTES)
    handler = FakeHandler(range_header=range_header)

    library.stream_file(handler, str(path))

    assert handler.status == 206
    assert handler.header("Content-Range") == f"bytes {start}-{end}/{len(AUDIO_BYTES)}"
    assert handler.header("Content-Length") == str(end - start + 1)
    assert handler.header("Accept-Ranges") == "bytes"
    assert handler.body == AUDIO_BYTES[start : end + 1]


def test_stream_file_head_with_range(tmp_hub):
    path = _write(tmp_hub.music_dir / "stream.mp3", AUDIO_BYTES)
    handler = FakeHandler(command="HEAD", range_header="bytes=0-9")

    library.stream_file(handler, str(path))

    assert handler.status == 206
    assert handler.header("Content-Range") == f"bytes 0-9/{len(AUDIO_BYTES)}"
    assert handler.header("Content-Length") == "10"
    assert handler.body == b""


@pytest.mark.parametrize(
    "range_header",
    [
        "bytes=99999-",
        "bytes=abc",
        "bytes=0-1,2-3",
        "bytes=100-50",
        "bytes=-0",
        "bytes=-",
        "items=0-1",
    ],
)
def test_stream_file_unsatisfiable_range(tmp_hub, range_header):
    path = _write(tmp_hub.music_dir / "stream.mp3", AUDIO_BYTES)
    handler = FakeHandler(range_header=range_header)

    library.stream_file(handler, str(path))

    assert handler.status == 416
    assert handler.header("Content-Range") == f"bytes */{len(AUDIO_BYTES)}"
    assert handler.header("Content-Length") == "0"
    assert handler.body == b""


@pytest.mark.parametrize("exc", [BrokenPipeError, ConnectionResetError])
def test_stream_file_client_disconnect_is_silent(tmp_hub, exc):
    path = _write(tmp_hub.music_dir / "stream.mp3", AUDIO_BYTES)
    handler = FakeHandler()
    handler.wfile = _FailingWriter(exc())

    library.stream_file(handler, str(path))

    assert handler.status == 200


# ---------------------------------------------------------------------------
# 转码缓存
# ---------------------------------------------------------------------------
def test_transcode_lock_is_per_track():
    first = library._transcode_lock(1)
    assert library._transcode_lock(1) is first
    assert library._transcode_lock(2) is not first


@requires_ffmpeg
def test_media_path_transcodes_and_reuses_cache(tmp_hub, monkeypatch):
    source = _make_flac(tmp_hub.music_dir / "周杰伦 - 晴天.flac")
    row = library.scan_file(str(source))
    assert row is not None
    cache_file = os.path.join(config.get().paths.cache_dir, f"{row['id']}.mp3")

    first = library.media_path(row)
    assert first == cache_file
    assert os.path.getsize(first) > 0

    # 缓存比源文件旧时应重新转码
    _set_mtime(first, 0)
    assert library.media_path(row) == first
    cached_mtime = os.path.getmtime(first)
    assert cached_mtime > 0

    # 缓存有效时不应再调用 ffmpeg
    def _no_transcode(*args, **kwargs):
        raise AssertionError("缓存有效时不应重复转码")

    monkeypatch.setattr(library.subprocess, "run", _no_transcode)
    assert library.media_path(row) == first
    assert os.path.getmtime(first) == cached_mtime


@requires_ffmpeg
def test_media_path_raises_when_ffmpeg_missing(tmp_hub, monkeypatch):
    source = _make_flac(tmp_hub.music_dir / "无 ffmpeg.flac")
    row = library.scan_file(str(source))
    assert row is not None
    cache_file = os.path.join(config.get().paths.cache_dir, f"{row['id']}.mp3")
    if os.path.exists(cache_file):
        os.remove(cache_file)

    monkeypatch.setattr(library.shutil, "which", lambda name: None)
    with pytest.raises(errors.UpstreamError) as excinfo:
        library.media_path(row)
    assert "缺少 ffmpeg" in str(excinfo.value)


# ---------------------------------------------------------------------------
# cache_cleanup / stats
# ---------------------------------------------------------------------------
def test_cache_cleanup_lru(tmp_hub):
    cache_dir = Path(config.get().paths.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    old = _write(cache_dir / "old.mp3", b"x" * 500)
    mid = _write(cache_dir / "mid.mp3", b"y" * 700)
    new = _write(cache_dir / "new.mp3", b"z" * 800)
    _set_mtime(old, 1000)
    _set_mtime(mid, 2000)
    _set_mtime(new, 3000)

    # 总大小 2000 字节，上限约 1887 字节：只删最旧的一个
    freed = library.cache_cleanup(1887 / (1024 * 1024))
    assert freed == 500
    assert not old.exists() and mid.exists() and new.exists()

    assert library.cache_cleanup(1887 / (1024 * 1024)) == 0

    # 上限为 0 时清空缓存
    assert library.cache_cleanup(0) == 1500
    assert not mid.exists() and not new.exists()


def test_cache_cleanup_missing_dir_and_default_limit(tmp_hub):
    cache_dir = Path(config.get().paths.cache_dir)
    assert not cache_dir.exists()
    assert library.cache_cleanup() == 0


def test_stats(tmp_hub):
    _write(tmp_hub.music_dir / "周杰伦 - 晴天.mp3", b"a" * 100)
    _write(tmp_hub.music_dir / "林俊杰 - 江南.mp3", b"b" * 200)
    library.scan()

    cache_dir = Path(config.get().paths.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "1.mp3").write_bytes(b"c" * 50)

    result = library.stats()
    assert result == {"tracks": 2, "bytes": 300, "cache_bytes": 50}

    assert library.stats()["tracks"] == 2
