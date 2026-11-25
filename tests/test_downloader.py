"""下载队列（``xiaoai_hub.downloader``）单元测试。

隔离策略
--------
- ``tmp_hub`` 提供临时 ``MUSIC_DIR`` + 独立 SQLite，测试之间零共享状态；
- ``configure()`` 注入 ``FakeNetease`` / ``RecordingLibrary``，完全不走真实网易云与曲库；
- monkeypatch ``downloader._open_url`` 返回假响应对象，避免任何真实网络请求；
- monkeypatch 标签写入与重试间隔，保证测试确定、快速（总时长 < 40s）。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import types
import urllib.error
from types import SimpleNamespace

import pytest
from xiaoai_hub import config, db, downloader, errors

from tests.fakes import FakeLibrary, FakeNetease, FakeSong, FakeSongUrl

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeResponse:
    """最小的 ``urlopen`` 响应对象（``status`` / ``headers`` / ``read``）。

    参数 ``gate`` + ``entered`` + ``block_after`` 用来把下载卡在指定块之后，
    以便测试「取消运行中任务」与「并发上限」。
    """

    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        content_type: str = "audio/mpeg",
        content_length: int | None = None,
        chunk_size: int = 64 * 1024,
        gate: threading.Event | None = None,
        entered: threading.Event | None = None,
        block_after: int = 0,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self._chunks = [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)]
        self._chunks = self._chunks or [b""]
        self._index = 0
        self._gate = gate
        self._entered = entered
        self._block_after = block_after

    def read(self, size: int = -1) -> bytes:
        if self._gate is not None and self._index >= self._block_after:
            if self._entered is not None:
                self._entered.set()
            self._gate.wait(10)
        if self._index >= len(self._chunks):
            return b""
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    def close(self) -> None:
        """与真实响应保持同构。"""


class RecordingLibrary(FakeLibrary):
    """在 ``FakeLibrary`` 基础上记录入库调用，并模拟真实曲库的落库行为。"""

    def __init__(self) -> None:
        super().__init__()
        self.scanned: list = []
        self.indexed: list = []

    def scan_file(self, path: str) -> dict | None:
        self.scanned.append(str(path))
        try:
            stat = os.stat(path)
        except OSError:
            return None
        db.execute(
            "INSERT INTO local_tracks(path,title,artist,album,size,mtime,song_key,duration) "
            "VALUES(?,?,?,?,?,?,?,0) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime",
            (str(path), "", "", "", stat.st_size, int(stat.st_mtime), None),
        )
        row = db.query_one("SELECT id FROM local_tracks WHERE path=?", (str(path),))
        return {"id": row["id"]} if row is not None else None

    def index_downloaded(self, path: str, song_id: int) -> None:
        self.indexed.append((str(path), int(song_id)))
        db.execute(
            "UPDATE local_tracks SET song_key=? WHERE path=?",
            (int(song_id), str(path)),
        )


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------
@pytest.fixture()
def dl(tmp_hub, monkeypatch):
    """每个测试独立的下载器环境（假网易云 + 假曲库 + 假网络 + 零重试等待）。"""
    netease = FakeNetease()
    library = RecordingLibrary()
    real_mutagen_tag = downloader._tag_with_mutagen
    real_ffmpeg_tag = downloader._tag_with_ffmpeg
    monkeypatch.setattr(downloader, "RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(downloader, "_tag_with_mutagen", lambda *a, **k: False)
    monkeypatch.setattr(downloader, "_tag_with_ffmpeg", lambda *a, **k: False)
    downloader.configure(netease_api=netease, library_api=library)
    context = SimpleNamespace(
        hub=tmp_hub,
        netease=netease,
        library=library,
        real_mutagen_tag=real_mutagen_tag,
        real_ffmpeg_tag=real_ffmpeg_tag,
    )
    try:
        yield context
    finally:
        downloader.shutdown()
        drain_workers()
        downloader.configure()


def drain_workers(timeout: float = 3.0) -> None:
    """等待工作线程全部退出，避免残留线程影响下一个测试的数据库。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if downloader.worker_count() == 0:
            return
        time.sleep(0.01)


def set_concurrency(hub: SimpleNamespace, concurrency: int, **env_overrides: object) -> None:
    """重新加载配置以调整并发/重试次数（不改数据库路径）。"""
    env = dict(hub.env)
    env["DOWNLOAD_CONCURRENCY"] = str(concurrency)
    env.update({key: str(value) for key, value in env_overrides.items()})
    config.reload(env=env, base_dir=str(hub.tmp_path))


def audio_body(size: int = 12000, magic: bytes = b"ID3") -> bytes:
    """构造一个能通过魔数校验的假音频文件内容。"""
    return magic + b"\x00" * max(0, size - len(magic))


def install_http(monkeypatch, factory) -> list:
    """让 ``downloader._open_url`` 返回工厂制造的假响应；返回请求记录。"""
    calls: list = []

    def fake_open(url, headers=None):
        calls.append({"url": url, "headers": dict(headers or {})})
        return factory(url, headers or {})

    monkeypatch.setattr(downloader, "_open_url", fake_open)
    return calls


def http_error(code: int) -> urllib.error.HTTPError:
    """构造一个带状态码的 ``HTTPError``。"""
    return urllib.error.HTTPError("http://fake.local/audio.mp3", code, f"HTTP {code}", {}, None)


def raise_http(code: int):
    """返回一个总是抛指定 HTTP 错误的响应工厂。"""

    def factory(url, headers):
        raise http_error(code)

    return factory


class FakeAudio:
    """假 mutagen 音频对象，记录写入的标签。"""

    def __init__(self) -> None:
        self.tags: dict = {}
        self.saved = False

    def __setitem__(self, key: str, value) -> None:
        self.tags[key] = value

    def save(self) -> None:
        self.saved = True


def install_fake_mutagen(monkeypatch) -> FakeAudio:
    """把 ``mutagen`` 注入 ``sys.modules``，返回被写入标签的假音频对象。"""
    module = types.ModuleType("mutagen")
    audio = FakeAudio()
    module.File = lambda path, easy=False: audio  # noqa: E731 - 简单替身
    monkeypatch.setitem(sys.modules, "mutagen", module)
    return audio


def new_playlist(name: str = "测试歌单") -> int:
    return int(
        db.execute(
            "INSERT INTO playlists(name,created_at,source,position) VALUES(?,?, 'local', 0)",
            (name, int(time.time())),
        )
    )


def add_track(playlist_id: int, song_id, name: str = "晴天", artist: str = "周杰伦",
              local_id=None, pos: int = 0) -> int:
    return int(
        db.execute(
            "INSERT INTO tracks(playlist_id,song_id,name,artist,album,pos,local_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (playlist_id, song_id, name, artist, "", pos, local_id),
        )
    )


def insert_job(song_id: int, status: str = "pending", **overrides) -> int:
    """直接插入一条任务行（用于恢复/统计等不起线程的场景）。"""
    fields = {
        "song_id": song_id,
        "name": f"歌曲{song_id}",
        "artist": "歌手",
        "album": "",
        "quality": "high",
        "status": status,
        "progress": 0,
        "retries": 0,
        "attempts": 0,
        "cancel_requested": 0,
        "created_at": 0,
        "updated_at": 0,
    }
    fields.update(overrides)
    columns = ",".join(fields)
    placeholders = ",".join("?" for _ in fields)
    return int(
        db.execute(
            f"INSERT INTO download_jobs({columns}) VALUES({placeholders})",
            tuple(fields.values()),
        )
    )


def job_row(job_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM download_jobs WHERE id=?", (job_id,))
    return dict(row) if row is not None else None


def wait_job(job_id: int, statuses: set, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = job_row(job_id)
        if last is not None and last["status"] in statuses:
            return last
        time.sleep(0.01)
    raise AssertionError(f"任务 {job_id} 未在 {timeout}s 内进入 {statuses}，当前：{last}")


def wait_until(predicate, message: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"{message} 未在 {timeout}s 内满足")


def expected_path(hub: SimpleNamespace, artist: str, name: str, ext: str = "mp3") -> str:
    return os.path.join(str(hub.music_dir), artist, f"{artist} - {name}.{ext}")


def download_song(song_id: int, artist: str = "周杰伦", name: str = "晴天", **extra) -> int:
    """入队 + 启动线程，返回任务 id。"""
    song = {"song_id": song_id, "name": name, "artist": artist, "album": ""}
    song.update(extra)
    job_id = downloader.add_job(song)
    downloader.kick()
    return job_id


# ---------------------------------------------------------------------------
# 入队与查询
# ---------------------------------------------------------------------------
def test_add_job_dedupes_same_song(dl):
    first = downloader.add_job({"song_id": 1001, "name": "晴天", "artist": "周杰伦"})
    second = downloader.add_job({"song_id": 1001, "name": "晴天", "artist": "周杰伦"})
    other = downloader.add_job({"song_id": 1002, "name": "七里香", "artist": "周杰伦"})
    assert first == second != other
    assert len(downloader.jobs()) == 2

    # 已完成的任务再次入队仍复用原行，不重复下载
    db.execute("UPDATE download_jobs SET status='done' WHERE id=?", (first,))
    assert downloader.add_job({"song_id": 1001}) == first
    assert len(downloader.jobs()) == 2

    # 失败任务重新入队：复用同一行并复位计数
    db.execute("UPDATE download_jobs SET status='failed', retries=2 WHERE id=?", (other,))
    assert downloader.add_job({"song_id": 1002}) == other
    row = job_row(other)
    assert row["status"] == "pending"
    assert row["retries"] == 0
    assert row["error"] is None


def test_add_job_rejects_invalid_song(dl):
    with pytest.raises(errors.BadRequest):
        downloader.add_job({"name": "没有 song_id"})
    assert downloader.jobs() == []


def test_add_playlist_skips_downloaded_and_duplicates(dl):
    playlist_id = new_playlist()
    add_track(playlist_id, 1001, "晴天", pos=0)
    add_track(playlist_id, 1001, "晴天（歌单内重复）", pos=1)
    add_track(playlist_id, 1002, "七里香", local_id=77, pos=2)  # 已下载
    add_track(playlist_id, None, "无 song_id", pos=3)
    add_track(playlist_id, 1003, "已在本地", pos=4)
    # 本地曲库已存在 1003 的绑定：即使 tracks.local_id 没回填也应跳过
    db.execute(
        "INSERT INTO local_tracks(path,title,artist,album,size,mtime,song_key,duration) "
        "VALUES(?,?,?,?,?,?,?,0)",
        ("/tmp/xiaoai/1003.mp3", "已在本地", "", "", 1, 1, 1003),
    )

    assert downloader.add_playlist(playlist_id) == 1
    jobs = downloader.jobs()
    assert [job["song_id"] for job in jobs] == [1001]
    # 重复调用不会重复入队
    assert downloader.add_playlist(playlist_id) == 0
    # 不存在的歌单返回 0
    assert downloader.add_playlist(999999) == 0


def test_stats_jobs_and_retry_failed(dl):
    first = downloader.add_job({"song_id": 1001, "name": "晴天", "artist": "周杰伦"})
    second = downloader.add_job({"song_id": 1002, "name": "七里香", "artist": "周杰伦"})
    third = downloader.add_job({"song_id": 1003, "name": "夜曲", "artist": "周杰伦"})
    db.execute("UPDATE download_jobs SET status='failed' WHERE id IN (?,?)", (first, second))
    db.execute("UPDATE download_jobs SET status='done' WHERE id=?", (third,))

    assert downloader.stats() == {
        "pending": 0,
        "running": 0,
        "done": 1,
        "failed": 2,
        "canceled": 0,
        "running_workers": 0,
    }
    assert downloader.retry_failed() == 2
    assert downloader.stats()["pending"] == 2
    assert len(downloader.jobs(status="pending")) == 2
    assert len(downloader.jobs(limit=2)) == 2

    db.execute("UPDATE download_jobs SET status='failed' WHERE id=?", (first,))
    assert downloader.retry_failed(first) == 1
    assert downloader.retry_failed(99999) == 0


# ---------------------------------------------------------------------------
# 下载成功 / 跳过
# ---------------------------------------------------------------------------
def test_download_success_persists_file_and_indexes(dl, monkeypatch):
    body = audio_body(20000)
    calls = install_http(
        monkeypatch, lambda url, headers: FakeResponse(body, content_length=len(body))
    )
    playlist_id = new_playlist()
    add_track(playlist_id, 1001)
    job_id = download_song(1001)

    job = wait_job(job_id, {"done"})
    path = expected_path(dl.hub, "周杰伦", "晴天")
    assert os.path.isfile(path)
    with open(path, "rb") as handle:
        assert handle.read(3) == b"ID3"
    assert os.path.getsize(path) == len(body)
    assert job["progress"] == 100
    assert job["path"] == path
    assert job["error"] is None
    assert job["attempts"] == 1
    assert job["bytes_done"] == len(body)

    # 请求带上了直链与下载请求头
    assert calls == [{"url": "http://fake.local/audio.mp3", "headers": calls[0]["headers"]}]
    assert calls[0]["headers"]["User-Agent"]
    assert "Referer" in calls[0]["headers"]

    # 入库与 song_key 绑定
    assert dl.library.scanned == [path]
    assert dl.library.indexed == [(path, 1001)]
    # tracks.local_id 回填
    track = db.query_one("SELECT local_id FROM tracks WHERE song_id=1001")
    local = db.query_one("SELECT id FROM local_tracks WHERE path=?", (path,))
    assert track["local_id"] == local["id"]


def test_existing_file_is_skipped(dl, monkeypatch):
    path = expected_path(dl.hub, "周杰伦", "晴天")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(audio_body(15000))
    calls = install_http(
        monkeypatch, lambda url, headers: FakeResponse(audio_body(15000), content_length=15000)
    )

    job_id = download_song(1001)
    job = wait_job(job_id, {"done"})
    assert calls == []  # 已存在文件不应发起网络请求
    assert job["path"] == path
    assert job["progress"] == 100
    assert job["error"] == "已存在"
    assert job["attempts"] == 1


# ---------------------------------------------------------------------------
# 错误分类：永久失败 / 瞬时重试
# ---------------------------------------------------------------------------
def test_no_url_is_permanent_without_retry(dl, monkeypatch):
    dl.netease.song_urls[1001] = FakeSongUrl(None, code=404, ok=False)
    calls = install_http(monkeypatch, lambda url, headers: FakeResponse(audio_body()))

    job_id = download_song(1001)
    job = wait_job(job_id, {"failed"})
    assert "no-url(404)" in job["error"]
    assert job["attempts"] == 1
    assert job["retries"] == 0
    assert calls == []
    # 稍等确认不会被重试
    time.sleep(0.15)
    assert job_row(job_id)["attempts"] == 1
    assert job_row(job_id)["status"] == "failed"


def test_transient_error_retries_then_succeeds(dl, monkeypatch):
    set_concurrency(dl.hub, 1)
    body = audio_body(12000)
    attempts = {"n": 0}

    def factory(url, headers):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.URLError("临时连接失败")
        return FakeResponse(body, content_length=len(body))

    calls = install_http(monkeypatch, factory)
    job_id = download_song(1001)
    job = wait_job(job_id, {"done"})
    assert job["attempts"] == 2
    assert job["retries"] == 1
    assert len(calls) == 2
    assert os.path.isfile(expected_path(dl.hub, "周杰伦", "晴天"))


def test_transient_error_exhausts_retries(dl, monkeypatch):
    set_concurrency(dl.hub, 1, DOWNLOAD_MAX_RETRY=1)
    calls = install_http(
        monkeypatch,
        lambda url, headers: (_ for _ in ()).throw(urllib.error.URLError("始终失败")),
    )
    job_id = download_song(1001)
    job = wait_job(job_id, {"failed"})
    assert job["attempts"] == 2
    assert job["retries"] == 2
    assert "URLError" in job["error"]
    assert len(calls) == 2


@pytest.mark.parametrize("content_type", ["text/html", "application/json"])
def test_invalid_content_type_is_permanent(dl, monkeypatch, content_type):
    body = b"<html>not audio</html>" + b"x" * 20000
    install_http(
        monkeypatch,
        lambda url, headers: FakeResponse(
            body, content_type=content_type, content_length=len(body)
        ),
    )
    job_id = download_song(1001)
    job = wait_job(job_id, {"failed"})
    assert "内容非法" in job["error"]
    assert job["attempts"] == 1
    assert not os.path.exists(expected_path(dl.hub, "周杰伦", "晴天"))
    assert not os.path.exists(expected_path(dl.hub, "周杰伦", "晴天") + ".part")


def test_invalid_magic_is_permanent(dl, monkeypatch):
    body = b"\x00" * 20000
    install_http(
        monkeypatch,
        lambda url, headers: FakeResponse(
            body, content_type="application/octet-stream", content_length=len(body)
        ),
    )
    job_id = download_song(1001)
    job = wait_job(job_id, {"failed"})
    assert "并非音频" in job["error"]
    assert job["attempts"] == 1
    assert not os.path.exists(expected_path(dl.hub, "周杰伦", "晴天") + ".part")


def test_too_small_audio_is_permanent(dl, monkeypatch):
    body = audio_body(1000)  # < 10KB
    install_http(
        monkeypatch, lambda url, headers: FakeResponse(body, content_length=len(body))
    )
    job_id = download_song(1001)
    job = wait_job(job_id, {"failed"})
    assert "文件过小" in job["error"]
    assert job["attempts"] == 1


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------
def test_cancel_pending_job(dl):
    job_id = downloader.add_job({"song_id": 1001, "name": "晴天", "artist": "周杰伦"})
    assert downloader.cancel(job_id) is True
    job = job_row(job_id)
    assert job["status"] == "canceled"
    assert job["cancel_requested"] == 1
    # 幂等 & 非法 id
    assert downloader.cancel(job_id) is True
    assert downloader.cancel(99999) is False

    done_id = downloader.add_job({"song_id": 1002, "name": "七里香", "artist": "周杰伦"})
    db.execute("UPDATE download_jobs SET status='done' WHERE id=?", (done_id,))
    assert downloader.cancel(done_id) is False


def test_cancelled_job_can_be_requeued(dl, monkeypatch):
    job_id = downloader.add_job({"song_id": 1001, "name": "晴天", "artist": "周杰伦"})
    assert downloader.cancel(job_id) is True
    assert job_id in downloader._CANCELED  # 内存取消标记（规范要求）

    body = audio_body(12000)
    install_http(
        monkeypatch, lambda url, headers: FakeResponse(body, content_length=len(body))
    )
    # 用户重新点下载：复用同一任务行，且旧的取消标记必须被清掉
    assert downloader.add_job({"song_id": 1001, "name": "晴天", "artist": "周杰伦"}) == job_id
    assert job_id not in downloader._CANCELED
    downloader.kick()
    wait_job(job_id, {"done"})
    assert os.path.isfile(expected_path(dl.hub, "周杰伦", "晴天"))


def test_cancel_running_job_removes_part(dl, monkeypatch):
    set_concurrency(dl.hub, 1)
    gate = threading.Event()
    entered = threading.Event()
    body = audio_body(300000)

    def factory(url, headers):
        return FakeResponse(
            body,
            content_length=len(body),
            chunk_size=100000,
            gate=gate,
            entered=entered,
            block_after=1,
        )

    install_http(monkeypatch, factory)
    job_id = download_song(1001)
    path = expected_path(dl.hub, "周杰伦", "晴天")

    assert entered.wait(5), "下载线程未进入读取循环"
    wait_until(lambda: job_row(job_id)["status"] == "running", "任务进入 running")
    assert downloader.cancel(job_id) is True
    assert job_row(job_id)["cancel_requested"] == 1

    gate.set()
    job = wait_job(job_id, {"canceled"})
    assert job["error"] == "用户取消"
    assert not os.path.exists(path + ".part")
    assert not os.path.exists(path)


# ---------------------------------------------------------------------------
# 断点续传
# ---------------------------------------------------------------------------
def test_resume_from_part_with_range_header(dl, monkeypatch):
    body = audio_body(40000)
    path = expected_path(dl.hub, "周杰伦", "晴天")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    head_len = 15000
    with open(path + ".part", "wb") as handle:
        handle.write(body[:head_len])

    calls = install_http(
        monkeypatch,
        lambda url, headers: FakeResponse(
            body[head_len:], status=206, content_length=len(body) - head_len
        ),
    )
    job_id = download_song(1001)
    job = wait_job(job_id, {"done"})

    assert calls[0]["headers"].get("Range") == f"bytes={head_len}-"
    with open(path, "rb") as handle:
        assert handle.read() == body
    assert not os.path.exists(path + ".part")
    assert job["bytes_done"] == len(body)


# ---------------------------------------------------------------------------
# 并发 / 恢复 / 停止
# ---------------------------------------------------------------------------
def test_worker_count_never_exceeds_concurrency(dl, monkeypatch):
    set_concurrency(dl.hub, 2)
    gate = threading.Event()
    entered = threading.Event()
    body = audio_body(12000)
    active = {"now": 0, "max": 0}
    active_lock = threading.Lock()

    def factory(url, headers):
        with active_lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        entered.set()
        gate.wait(10)
        with active_lock:
            active["now"] -= 1
        return FakeResponse(body, content_length=len(body))

    install_http(monkeypatch, factory)
    job_ids = [
        downloader.add_job({"song_id": 2000 + index, "name": f"歌曲{index}", "artist": "歌手"})
        for index in range(5)
    ]
    observed_workers = 0
    # 连续 kick 多次也不能突破并发上限
    for _ in range(5):
        downloader.kick()
        observed_workers = max(observed_workers, downloader.worker_count())
        time.sleep(0.02)
    assert entered.wait(5), "没有下载线程开始工作"
    for _ in range(10):
        observed_workers = max(observed_workers, downloader.worker_count())
        time.sleep(0.01)
    assert observed_workers <= 2
    assert active["max"] <= 2

    gate.set()
    wait_until(
        lambda: all(job_row(job_id)["status"] == "done" for job_id in job_ids),
        "5 个任务全部完成",
        timeout=10.0,
    )
    assert active["max"] <= 2
    wait_until(lambda: downloader.worker_count() == 0, "工作线程全部退出")


def test_recover_requeues_running_jobs(dl, monkeypatch):
    kicked: list = []
    monkeypatch.setattr(downloader, "kick", lambda: kicked.append(True))
    running_id = insert_job(1001, status="running", cancel_requested=1)
    pending_id = insert_job(1002, status="pending")

    downloader.recover()
    assert job_row(running_id)["status"] == "pending"
    assert job_row(running_id)["cancel_requested"] == 0
    assert job_row(pending_id)["status"] == "pending"
    assert kicked


def test_shutdown_stops_idle_workers(dl):
    downloader.kick()
    wait_until(lambda: downloader.worker_count() == 0, "空队列工作线程自行退出")
    downloader.shutdown()
    assert downloader.worker_count() == 0


# ---------------------------------------------------------------------------
# 标签兜底与工具函数
# ---------------------------------------------------------------------------
def test_tag_and_move_falls_back_to_rename(dl):
    final = os.path.join(str(dl.hub.music_dir), "周杰伦", "周杰伦 - 晴天.mp3")
    os.makedirs(os.path.dirname(final), exist_ok=True)
    part = final + ".part"
    with open(part, "wb") as handle:
        handle.write(audio_body(12000))

    # dl 夹具已把 mutagen / ffmpeg 都 patch 成不可用 → 应直接改名落盘
    downloader._tag_and_move(part, final, {"name": "晴天", "artist": "周杰伦", "album": ""}, "mp3")
    assert not os.path.exists(part)
    assert os.path.isfile(final)
    with open(final, "rb") as handle:
        assert handle.read(3) == b"ID3"


def test_safe_name_is_filesystem_safe():
    for raw in ("../../etc/passwd", 'a/b\\c:*?"<>|', "  ..  ", "", None):
        name = downloader.safe_name(raw)
        assert name
        assert name not in (".", "..")
        assert not set(name) & set('/\\:*?"<>|')


def test_looks_like_audio_magic():
    assert downloader._looks_like_audio(b"ID3\x03\x00\x00")
    assert downloader._looks_like_audio(b"\xff\xfb\x90\x64")
    assert downloader._looks_like_audio(b"\xff\xf3\x90\x64")
    assert downloader._looks_like_audio(b"\xff\xf2\x90\x64")
    assert downloader._looks_like_audio(b"fLaC\x00\x00")
    assert downloader._looks_like_audio(b"OggS\x00\x00")
    assert downloader._looks_like_audio(b"\x00\x00\x00\x00ftyp")
    assert downloader._looks_like_audio(b"RIFF\x00\x00\x00\x00")
    assert not downloader._looks_like_audio(b"<html>")
    assert not downloader._looks_like_audio(b"")


# ---------------------------------------------------------------------------
# 可选标签写入：mutagen 优先 / ffmpeg 兜底 / 都失败直接改名
# ---------------------------------------------------------------------------
def test_tag_with_mutagen_writes_metadata(dl, monkeypatch):
    audio = install_fake_mutagen(monkeypatch)
    monkeypatch.setattr(downloader, "_tag_with_mutagen", dl.real_mutagen_tag)
    final = os.path.join(str(dl.hub.music_dir), "周杰伦", "周杰伦 - 晴天.mp3")
    os.makedirs(os.path.dirname(final), exist_ok=True)
    part = final + ".part"
    with open(part, "wb") as handle:
        handle.write(audio_body(12000))

    downloader._tag_and_move(
        part, final, {"name": "晴天", "artist": "周杰伦", "album": "叶惠美"}, "mp3"
    )
    assert audio.saved is True
    assert audio.tags["title"] == ["晴天"]
    assert audio.tags["artist"] == ["周杰伦"]
    assert audio.tags["album"] == ["叶惠美"]
    assert not os.path.exists(part)
    assert os.path.isfile(final)


def test_tag_with_ffmpeg_writes_temp_then_replaces(dl, monkeypatch):
    monkeypatch.setattr(downloader, "_tag_with_ffmpeg", dl.real_ffmpeg_tag)
    commands: list = []

    def fake_run(command, **kwargs):
        commands.append(command)
        with open(command[-1], "wb") as handle:
            handle.write(b"tagged-by-ffmpeg")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(downloader.subprocess, "run", fake_run)
    final = os.path.join(str(dl.hub.music_dir), "周杰伦", "周杰伦 - 晴天.mp3")
    os.makedirs(os.path.dirname(final), exist_ok=True)
    part = final + ".part"
    with open(part, "wb") as handle:
        handle.write(audio_body(12000))

    downloader._tag_and_move(
        part, final, {"name": "晴天", "artist": "周杰伦", "album": "叶惠美"}, "mp3"
    )
    assert commands and command_has_metadata(commands[0])
    with open(final, "rb") as handle:
        assert handle.read() == b"tagged-by-ffmpeg"
    assert not os.path.exists(part)


def command_has_metadata(command: list) -> bool:
    """断言 ffmpeg 命令带上了 title/artist/album 元数据参数。"""
    return "-metadata" in command and any("title=晴天" == item for item in command)


def test_tag_with_ffmpeg_failure_falls_back_to_rename(dl, monkeypatch):
    monkeypatch.setattr(downloader, "_tag_with_ffmpeg", dl.real_ffmpeg_tag)

    def fake_run(command, **kwargs):
        with open(command[-1], "wb") as handle:
            handle.write(b"partial-output")
        return SimpleNamespace(returncode=1, stderr=b"boom")

    monkeypatch.setattr(downloader.subprocess, "run", fake_run)
    body = audio_body(12000)
    final = os.path.join(str(dl.hub.music_dir), "周杰伦", "周杰伦 - 晴天.mp3")
    os.makedirs(os.path.dirname(final), exist_ok=True)
    part = final + ".part"
    with open(part, "wb") as handle:
        handle.write(body)

    downloader._tag_and_move(part, final, {"name": "晴天", "artist": "周杰伦"}, "mp3")
    with open(final, "rb") as handle:
        assert handle.read() == body
    assert not os.path.exists(final + ".tagtmp.mp3")


# ---------------------------------------------------------------------------
# HTTP 错误分类 / 断点失效 / 声明大小异常
# ---------------------------------------------------------------------------
def test_http_404_is_permanent(dl, monkeypatch):
    install_http(monkeypatch, raise_http(404))
    job = wait_job(download_song(1001), {"failed"})
    assert "http-404" in job["error"]
    assert job["attempts"] == 1
    assert job["retries"] == 0


def test_http_500_retries_then_succeeds(dl, monkeypatch):
    set_concurrency(dl.hub, 1)
    body = audio_body(12000)
    count = {"n": 0}

    def factory(url, headers):
        count["n"] += 1
        if count["n"] == 1:
            raise http_error(500)
        return FakeResponse(body, content_length=len(body))

    install_http(monkeypatch, factory)
    job = wait_job(download_song(1001), {"done"})
    assert job["attempts"] == 2
    assert job["retries"] == 1


def test_resume_416_restarts_from_scratch(dl, monkeypatch):
    body = audio_body(40000)
    path = expected_path(dl.hub, "周杰伦", "晴天")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "wb") as handle:
        handle.write(body[:15000])

    calls: list = []
    count = {"n": 0}

    def factory(url, headers):
        calls.append(dict(headers))
        count["n"] += 1
        if count["n"] == 1:
            raise http_error(416)
        return FakeResponse(body, content_length=len(body))

    install_http(monkeypatch, factory)
    job = wait_job(download_song(1001), {"done"})
    assert calls[0].get("Range") == "bytes=15000-"
    assert "Range" not in calls[1]
    with open(path, "rb") as handle:
        assert handle.read() == body
    assert job["attempts"] == 2


def test_declared_content_length_too_small_fails(dl, monkeypatch):
    install_http(monkeypatch, lambda url, headers: FakeResponse(b"", content_length=100))
    job = wait_job(download_song(1001), {"failed"})
    assert "文件过小" in job["error"]
    assert job["attempts"] == 1


def test_db_cancel_flag_short_circuits_download(dl):
    job_id = downloader.add_job({"song_id": 1001, "name": "晴天", "artist": "周杰伦"})
    db.execute(
        "UPDATE download_jobs SET status='running', cancel_requested=1 WHERE id=?", (job_id,)
    )
    status, path, error = downloader._download_one(job_row(job_id))
    assert (status, path, error) == ("canceled", None, "用户取消")
    assert dl.netease.calls == []


# ---------------------------------------------------------------------------
# 字段兼容与工具函数边界
# ---------------------------------------------------------------------------
def test_song_url_mapping_and_flac_extension(dl, monkeypatch):
    # netease.song_url 理论上返回 SongUrl，但下载器同时兼容 dict 形态
    dl.netease.song_url = lambda song_id, quality=None: {
        "url": "http://fake.local/a.flac",
        "ok": True,
        "ext": "FLAC",
        "code": 200,
    }
    body = audio_body(12000, magic=b"fLaC")
    install_http(
        monkeypatch, lambda url, headers: FakeResponse(body, content_length=len(body))
    )
    job = wait_job(download_song(1001), {"done"})
    assert job["path"].endswith(".flac")


def test_add_playlist_invalid_id_raises(dl):
    with pytest.raises(errors.BadRequest):
        downloader.add_playlist("abc")


def test_jobs_clamps_limit_and_retry_failed_invalid_id(dl):
    downloader.add_job({"song_id": 1001, "name": "晴天", "artist": "周杰伦"})
    assert len(downloader.jobs(limit="oops")) == 1
    assert len(downloader.jobs(limit=0)) == 1
    assert downloader.retry_failed("abc") == 0


def test_add_job_accepts_song_object(dl):
    job_id = downloader.add_job(FakeSong(1001, "晴天", "周杰伦", "叶惠美"))
    row = job_row(job_id)
    assert row["song_id"] == 1001
    assert row["name"] == "晴天"
    assert row["artist"] == "周杰伦"


def test_normalize_ext_and_base_name_edges():
    assert downloader._normalize_ext("FLAC") == "flac"
    assert downloader._normalize_ext("exe") == "mp3"
    assert downloader._normalize_ext("../x") == "mp3"
    assert downloader._job_base_name({"name": "晴天"}, 1001) == "晴天"
    assert "song-1001" in downloader._job_base_name({}, 1001)


def test_song_dict_and_header_helpers():
    class BareSong:
        song_id = 1001
        name = "晴天"
        artist = "周杰伦"
        album = "叶惠美"

    assert downloader._song_dict(BareSong())["song_id"] == 1001
    response = SimpleNamespace(headers={})
    assert downloader._content_type(response) == ""
    assert downloader._content_length(response) == 0
    assert downloader._content_length(SimpleNamespace(headers={"Content-Length": "abc"})) == 0
    assert downloader._content_type(
        SimpleNamespace(headers={"Content-Type": "Audio/MPEG; charset=utf-8"})
    ) == "audio/mpeg"
    assert downloader._invalid_content_type("application/problem+json") is True
    assert downloader._read_head("/nonexistent/path/audio.mp3") == b""
