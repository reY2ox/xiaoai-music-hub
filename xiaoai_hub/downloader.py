"""下载队列：SQLite 持久化任务 + 并发工作线程 + 断点续传 + 错误分类。

对应设计文档 ``docs/DESIGN.md`` 第 4.4 节（下载队列）与第 5.6 节（模块契约）。

设计要点
--------
- 任务落在 SQLite ``download_jobs`` 表（表结构由 :mod:`xiaoai_hub.db` 统一创建），
  服务重启后 :func:`recover` 把 ``running`` 任务退回 ``pending``，保留 ``.part`` 续传；
- ``_running_workers`` 在**线程启动前**加锁递增、线程 ``finally`` 递减，
  因此连续 :func:`kick` 也不会突破配置的并发上限（修复 v2 的竞态缺陷）；
- 工作线程用 ``BEGIN IMMEDIATE`` 原子领取 pending 任务，队列领空即退出，由 :func:`kick` 补线程；
- 取消：内存集合 :data:`_CANCELED` + 数据库 ``cancel_requested`` 双标记，
  下载循环每 256KB 检查一次，命中则删除 ``.part`` 并退出；
- 错误分类：``no-url`` / 404 / 内容非法 / 文件过小等**永久失败**直接 ``failed``；
  ``URLError`` / 超时 / 5xx / 连接重置等**瞬时失败**按指数退避（2s、6s）重试；
- 内容校验：``Content-Type`` 不能是 ``text/*`` / JSON，文件魔数必须像音频
  （ID3 / MP3 帧同步 / fLaC / OggS / ftyp / RIFF）；
- 标签：优先可选依赖 ``mutagen``，其次 ``ffmpeg -c copy -metadata``，
  两者都不可用时直接把 ``.part`` 改名落盘（不阻断下载）。

测试注入
--------
:data:`_netease` / :data:`_library` 默认指向真实模块，测试可用 :func:`configure`
注入替身（传 ``None`` 恢复默认）::

    from xiaoai_hub import downloader
    downloader.configure(netease_api=FakeNetease(), library_api=FakeLibrary())
"""
from __future__ import annotations

import http.client
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from . import config, db, errors, logging_setup, prefs
from . import library as _library
from . import netease as _netease

log = logging_setup.get_logger(__name__)

#: 真实模块引用，供 :func:`configure` 恢复默认
_DEFAULT_NETEASE = _netease
_DEFAULT_LIBRARY = _library

#: 工作线程退出时 ``shutdown`` 最多等待的秒数
SHUTDOWN_JOIN_TIMEOUT = 2.0
#: 单个音频文件的最小合理字节数（小于该值视为错误页/空响应）
MIN_AUDIO_BYTES = 10240
#: 每次读取的块大小；同时是取消检查的粒度（每 256KB 检查一次）
CHUNK_SIZE = 256 * 1024
#: 进度落库节流：进度变化 ≥ 2% 或距上次写入 ≥ 2 秒
PROGRESS_STEP = 2
PROGRESS_INTERVAL = 2.0
#: 瞬时错误重试的退避秒数（第 1 次重试 2s、第 2 次 6s；测试可 monkeypatch 缩短）
RETRY_DELAYS: tuple = (2.0, 6.0)
#: 需要重试的 HTTP 状态码（4xx 一般视为永久失败，以下除外）
_TRANSIENT_HTTP_CODES = frozenset({408, 425, 429})
#: 永久失败的 HTTP 状态码
_PERMANENT_HTTP_CODES = frozenset({400, 401, 403, 404, 405, 410, 416, 451})
#: 默认下载请求头（真实 ``netease`` 模块若导出 UA/REFERER 则优先使用）
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_DEFAULT_REFERER = "https://music.163.com/"

#: 允许的音频扩展名白名单（防目录穿越式 ext）
_AUDIO_EXTS = frozenset({"mp3", "flac", "m4a", "aac", "ogg", "wav", "ape", "wma"})

# ---------- 运行时状态 ----------
_state_lock = threading.Lock()  #: 保护 :data:`_running_workers` / :data:`_threads`
_cancel_lock = threading.Lock()  #: 保护 :data:`_CANCELED`
_stop = threading.Event()  #: 置位后工作线程尽快退出
_running_workers = 0  #: 已启动且尚未退出的工作线程数（启动前 +1）
_threads: list = []  #: 存活工作线程（供 shutdown join）
_CANCELED: set = set()  #: 已被用户取消的任务 id（下载循环据此中断）
_ready_path: Optional[str] = None  #: 已初始化 Schema 的数据库路径

__all__ = [
    "CHUNK_SIZE",
    "MIN_AUDIO_BYTES",
    "RETRY_DELAYS",
    "add_job",
    "add_playlist",
    "cancel",
    "configure",
    "init",
    "jobs",
    "kick",
    "recover",
    "retry_failed",
    "safe_name",
    "shutdown",
    "stats",
    "worker_count",
]


# ---------------------------------------------------------------------------
# 内部异常
# ---------------------------------------------------------------------------
class _TransientError(Exception):
    """可重试的瞬时错误（网络抖动 / 超时 / 5xx / 连接重置等）。"""


# ---------------------------------------------------------------------------
# 初始化与依赖注入
# ---------------------------------------------------------------------------
def configure(netease_api: Any = None, library_api: Any = None) -> None:
    """注入网易云 / 曲库替身（测试用）；传 ``None`` 恢复真实模块。

    同时复位运行时控制状态（停止标志、取消集合），保证测试之间互不影响。
    """
    global _netease, _library
    _netease = netease_api if netease_api is not None else _DEFAULT_NETEASE
    _library = library_api if library_api is not None else _DEFAULT_LIBRARY
    _stop.clear()
    with _cancel_lock:
        _CANCELED.clear()


def init() -> None:
    """初始化数据库结构（幂等）；应用启动时调用一次即可。"""
    global _ready_path
    db.init_schema()
    _ready_path = db.db_path()


def _ensure_ready() -> None:
    """确保当前数据库路径的 Schema 已就绪（``db.configure`` 换库后自动重建）。"""
    if _ready_path != db.db_path():
        init()


def recover() -> None:
    """服务启动时调用：``running`` → ``pending``（保留 ``.part`` 续传）后补线程。"""
    init()
    _stop.clear()
    with _cancel_lock:
        _CANCELED.clear()  # 重启/恢复后取消标记不再适用
    db.execute(
        "UPDATE download_jobs SET status='pending', cancel_requested=0, updated_at=? "
        "WHERE status='running'",
        (_now(),),
    )
    log.info("下载队列已恢复：running 任务退回 pending")
    kick()


# ---------------------------------------------------------------------------
# 队列入队 / 查询
# ---------------------------------------------------------------------------
def add_job(song: Any, quality: Optional[str] = None) -> int:
    """加入下载队列并返回任务 id。

    去重规则：同一 ``song_id`` 已有 ``pending/running/done`` 任务时直接复用该行；
    若历史任务是 ``failed/canceled`` 则复位该行重新入队（不产生重复任务）。
    """
    _ensure_ready()
    data = _song_dict(song)
    song_id = _song_id(data)
    if song_id is None:
        raise errors.BadRequest("缺少有效的 song_id")
    name = _text(data.get("name"))
    artist = _text(data.get("artist"))
    album = _text(data.get("album"))
    chosen = quality or _text(data.get("quality")) or _default_quality()
    existing = db.query_one(
        "SELECT id,status FROM download_jobs WHERE song_id=? ORDER BY id DESC LIMIT 1",
        (song_id,),
    )
    now = _now()
    if existing is not None:
        job_id = int(existing["id"])
        if str(existing["status"] or "") in ("pending", "running", "done"):
            return job_id
        _forget_canceled(job_id)  # 用户重新点下载：清掉上次的取消标记
        db.execute(
            "UPDATE download_jobs SET status='pending', quality=?, progress=0, error=NULL, "
            "path=NULL, bytes_done=0, bytes_total=0, retries=0, attempts=0, "
            "cancel_requested=0, created_at=?, updated_at=? WHERE id=?",
            (chosen, now, now, job_id),
        )
        log.info("下载任务 %s 重新入队：%s - %s", job_id, artist, name)
        return job_id
    job_id = int(
        db.execute(
            "INSERT INTO download_jobs(song_id,name,artist,album,quality,status,progress,"
            "retries,attempts,cancel_requested,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'pending',0,0,0,0,?,?)",
            (song_id, name, artist, album, chosen, now, now),
        )
    )
    log.info("下载任务 %s 入队：%s - %s", job_id, artist, name)
    return job_id


def add_playlist(playlist_id: Any) -> int:
    """把歌单中尚未下载的曲目加入队列，返回本次新入队数量。

    跳过：``tracks.local_id`` 已绑定、本地曲库已存在同一 ``song_id``、
    已有 ``pending/running/done`` 任务的行；歌单内重复曲目只入队一次。
    """
    _ensure_ready()
    try:
        pid = int(playlist_id)
    except (TypeError, ValueError):
        raise errors.BadRequest("歌单 id 无效") from None
    rows = db.query(
        "SELECT song_id,name,artist,album FROM tracks "
        "WHERE playlist_id=? AND song_id IS NOT NULL AND COALESCE(local_id,0)=0 "
        "ORDER BY COALESCE(pos,0), id",
        (pid,),
    )
    queued = 0
    seen: set = set()
    for row in rows:
        data = dict(row)
        song_id = _song_id(data)
        if song_id is None or song_id in seen:
            continue
        seen.add(song_id)
        if _song_in_library(song_id):
            continue
        existing = db.query_one(
            "SELECT id,status FROM download_jobs WHERE song_id=? ORDER BY id DESC LIMIT 1",
            (song_id,),
        )
        if existing is not None and str(existing["status"] or "") in (
            "pending",
            "running",
            "done",
        ):
            continue
        add_job(data)
        queued += 1
    if queued:
        log.info("歌单 %s：新增 %s 个下载任务", pid, queued)
    return queued


def jobs(limit: int = 100, status: Optional[str] = None) -> list:
    """最近的任务列表（按 id 倒序），可按状态过滤。"""
    _ensure_ready()
    try:
        count = max(1, min(1000, int(limit)))
    except (TypeError, ValueError):
        count = 100
    if status:
        rows = db.query(
            "SELECT * FROM download_jobs WHERE status=? ORDER BY id DESC LIMIT ?",
            (str(status), count),
        )
    else:
        rows = db.query("SELECT * FROM download_jobs ORDER BY id DESC LIMIT ?", (count,))
    return [dict(row) for row in rows]


def stats() -> dict:
    """各状态任务数 + 当前工作线程数（与 ``docs/API.md`` 的 download 结构一致）。"""
    _ensure_ready()
    result = {"pending": 0, "running": 0, "done": 0, "failed": 0, "canceled": 0}
    for row in db.query("SELECT status, COUNT(*) AS n FROM download_jobs GROUP BY status"):
        key = str(row["status"] or "")
        if key in result:
            result[key] = int(row["n"] or 0)
    result["running_workers"] = worker_count()
    return result


def retry_failed(job_id: Optional[Any] = None) -> int:
    """把失败任务重置为 pending，返回重置数量（不自动启动线程，调用方需 kick）。"""
    _ensure_ready()
    now = _now()
    if job_id:
        try:
            target = int(job_id)
        except (TypeError, ValueError):
            return 0
        with db.transaction() as conn:
            cur = conn.execute(
                "UPDATE download_jobs SET status='pending', retries=0, cancel_requested=0, "
                "error=NULL, updated_at=? WHERE id=? AND status='failed'",
                (now, target),
            )
            return int(cur.rowcount or 0)
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE download_jobs SET status='pending', retries=0, cancel_requested=0, "
            "error=NULL, updated_at=? WHERE status='failed'",
            (now,),
        )
        return int(cur.rowcount or 0)


def cancel(job_id: Any) -> bool:
    """请求取消任务：``pending`` 直接置 canceled，``running`` 打标记由工作线程清理。

    返回 ``True`` 表示请求已被接受（任务已完成/不存在返回 ``False``）。
    """
    _ensure_ready()
    try:
        target = int(job_id)
    except (TypeError, ValueError):
        return False
    with _cancel_lock:
        _CANCELED.add(target)
    conn = None
    try:
        conn = db.connect()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM download_jobs WHERE id=?", (target,)).fetchone()
        if row is None:
            _forget_canceled(target)
            return False
        status = str(row["status"] or "")
        now = _now()
        if status == "pending":
            conn.execute(
                "UPDATE download_jobs SET status='canceled', cancel_requested=1, error=?, "
                "updated_at=? WHERE id=?",
                ("用户取消", now, target),
            )
            conn.commit()
            log.info("下载任务 %s 已取消（排队中）", target)
            return True
        if status == "running":
            conn.execute(
                "UPDATE download_jobs SET cancel_requested=1, updated_at=? WHERE id=?",
                (now, target),
            )
            conn.commit()
            log.info("下载任务 %s 取消请求已登记（运行中）", target)
            return True
        conn.commit()
        if status != "canceled":
            _forget_canceled(target)
        return status == "canceled"
    except Exception as exc:  # 取消失败不应影响调用方
        log.warning("取消下载任务 %s 失败：%s", target, exc)
        _forget_canceled(target)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - 关闭失败无补救价值
                pass


# ---------------------------------------------------------------------------
# 工作线程调度
# ---------------------------------------------------------------------------
def worker_count() -> int:
    """当前存活的工作线程数。"""
    with _state_lock:
        return _running_workers


def kick() -> None:
    """确保工作线程数补足到配置的并发上限（幂等，可连续调用）。

    修复点：``_running_workers`` 在**线程启动前**加锁递增，线程 ``finally`` 递减，
    所以并发在任意时刻都不会超过 ``download_concurrency``。
    """
    global _running_workers
    _ensure_ready()
    with _state_lock:
        if _stop.is_set():
            return
        concurrency = max(1, int(config.get().download_concurrency))
        while _running_workers < concurrency:
            _running_workers += 1
            worker = threading.Thread(
                target=_worker_loop,
                name=f"xiaoai-download-{_running_workers}",
                daemon=True,
            )
            _threads.append(worker)
            worker.start()


def shutdown() -> None:
    """停止工作线程并最多等待 2 秒；之后可用 :func:`recover` 重新启动队列。"""
    _stop.set()
    deadline = time.monotonic() + SHUTDOWN_JOIN_TIMEOUT
    with _state_lock:
        pending_join = list(_threads)
    for worker in pending_join:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if worker.is_alive():
            worker.join(remaining)
    with _state_lock:
        _threads[:] = [worker for worker in _threads if worker.is_alive()]
        remaining_workers = _running_workers
    log.info("下载队列已停止（剩余工作线程 %s 个）", remaining_workers)


def _worker_loop() -> None:
    """工作线程主体：循环领取任务，直到队列为空或收到停止信号。"""
    global _running_workers
    try:
        while not _stop.is_set():
            job = _claim_next()
            if job is None:
                break
            _run_job(job)
    except Exception:  # pragma: no cover - 防御：单线程异常不应拖垮队列
        log.exception("下载工作线程异常退出")
    finally:
        with _state_lock:
            _running_workers -= 1
            try:
                _threads.remove(threading.current_thread())
            except ValueError:  # pragma: no cover - 已被 shutdown 清理
                pass
        _compensate()


def _compensate() -> None:
    """线程退出瞬间若刚好有新 pending 任务，补踢一次，避免队列饿死。"""
    if _stop.is_set():
        return
    try:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM download_jobs WHERE status='pending' "
            "AND COALESCE(cancel_requested,0)=0"
        )
        if row is not None and int(row["n"] or 0) > 0:
            kick()
    except Exception:  # pragma: no cover - 补偿失败下次 kick 还能补
        log.debug("下载队列退出补偿检查失败", exc_info=True)


def _claim_next() -> Optional[dict]:
    """用 ``BEGIN IMMEDIATE`` 原子领取最早的一个 pending 任务；没有则返回 ``None``。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM download_jobs WHERE status='pending' "
            "AND COALESCE(cancel_requested,0)=0 ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            "UPDATE download_jobs SET status='running', "
            "attempts=COALESCE(attempts,0)+1, updated_at=? WHERE id=?",
            (_now(), int(row["id"])),
        )
        conn.commit()
        job = dict(row)
        job["attempts"] = int(job.get("attempts") or 0) + 1
        return job
    except BaseException:
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - 回滚失败无补救价值
            pass
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 单个任务执行
# ---------------------------------------------------------------------------
def _run_job(job: dict) -> None:
    """执行一个已领取的任务，并把结果写回数据库。"""
    job_id = int(job["id"])
    try:
        status, path, error = _download_one(job)
    except _TransientError as exc:
        _handle_transient(job, exc)
        return
    except Exception as exc:  # 未知异常也按瞬时错误处理，避免任务卡在 running
        log.exception("下载任务 %s 执行异常", job_id)
        _handle_transient(job, exc)
        return
    if status in ("ok", "skipped"):
        _mark_done(job, path, error)
        _index_result(job, path)
        return
    _forget_canceled(job_id)
    if status == "canceled":
        _update(job_id, status="canceled", error=error or "用户取消")
        log.info("下载任务 %s 已取消", job_id)
        return
    _update(job_id, status="failed", error=error or "下载失败")
    log.warning("下载任务 %s 失败：%s", job_id, error)


def _handle_transient(job: dict, exc: BaseException) -> None:
    """处理瞬时错误：可重试时回 pending 并指数退避，超过上限置 failed。"""
    job_id = int(job["id"])
    if _is_canceled(job_id):
        _forget_canceled(job_id)
        _update(job_id, status="canceled", error="用户取消")
        return
    if _stop.is_set():
        _update(job_id, status="pending", error=None)
        return
    retries = int(job.get("retries") or 0) + 1
    max_retry = int(config.get().download_max_retry)
    message = _short_error(exc)
    if retries <= max_retry:
        _update(job_id, status="pending", retries=retries, error=message)
        delay = _retry_delay(retries)
        log.info(
            "下载任务 %s 瞬时失败，%ss 后重试（%s/%s）：%s",
            job_id,
            delay,
            retries,
            max_retry,
            message,
        )
        if delay > 0:
            _stop.wait(delay)
        return
    _update(job_id, status="failed", retries=retries, error=message)
    log.warning("下载任务 %s 重试耗尽，失败：%s", job_id, message)


def _retry_delay(retries: int) -> float:
    """第 ``retries`` 次重试前的等待秒数（默认 2s、6s，之后沿用最后一档）。"""
    if not RETRY_DELAYS:
        return 0.0
    index = min(max(int(retries) - 1, 0), len(RETRY_DELAYS) - 1)
    return float(RETRY_DELAYS[index])


def _mark_done(job: dict, path: Optional[str], note: Optional[str] = None) -> None:
    """把任务标记为 done（含文件大小回填）。"""
    job_id = int(job["id"])
    fields: dict = {"status": "done", "progress": 100, "path": path, "error": note}
    if path and os.path.exists(path):
        size = os.path.getsize(path)
        fields["bytes_done"] = size
        fields["bytes_total"] = size
    _update(job_id, **fields)
    log.info("下载完成：%s - %s → %s", job.get("artist") or "", job.get("name") or "", path)


def _index_result(job: dict, path: Optional[str]) -> None:
    """下载完成后入库：单文件扫描 + 绑定 song_key + 回填 ``tracks.local_id``。"""
    if not path:
        return
    song_id = _song_id(job)
    library_api = _library
    scan_file = getattr(library_api, "scan_file", None)
    if callable(scan_file):
        try:
            scan_file(path)
        except Exception as exc:  # 入库失败不影响文件已下载的事实
            log.warning("下载完成后扫描文件失败 %s：%s", path, exc)
    index_downloaded = getattr(library_api, "index_downloaded", None)
    if callable(index_downloaded) and song_id is not None:
        try:
            index_downloaded(path, song_id)
        except Exception as exc:
            log.warning("绑定下载文件与 song_id 失败 %s：%s", path, exc)
    if song_id is not None:
        _bind_local_track(song_id, path)


def _bind_local_track(song_id: int, path: str) -> None:
    """回填 ``tracks.local_id``：文件已入 ``local_tracks`` 但歌单行未绑定。"""
    try:
        row = db.query_one("SELECT id FROM local_tracks WHERE path=?", (path,))
        if row is None:
            return
        db.execute(
            "UPDATE tracks SET local_id=? WHERE song_id=? AND COALESCE(local_id,0)=0",
            (int(row["id"]), int(song_id)),
        )
    except Exception as exc:  # pragma: no cover - 绑定失败只记日志
        log.warning("回填 tracks.local_id 失败 song_id=%s：%s", song_id, exc)


# ---------------------------------------------------------------------------
# 下载实现
# ---------------------------------------------------------------------------
def _download_one(job: dict) -> tuple:
    """下载单个任务，返回 ``(status, path, error)``。

    ``status`` 为 ``ok`` / ``skipped`` / ``canceled`` / ``failed``；
    瞬时错误不返回，而是抛 :class:`_TransientError` 交给工作线程重试。
    """
    job_id = int(job["id"])
    song_id = _song_id(job)
    if _is_canceled(job_id) or _cancel_requested(job_id):
        return "canceled", None, "用户取消"
    quality = job.get("quality") or _default_quality()
    try:
        song_url = _netease.song_url(song_id, quality)
    except Exception as exc:
        raise _TransientError(f"获取直链失败：{_short_error(exc)}") from exc
    url = _field(song_url, "url")
    ext = _normalize_ext(_field(song_url, "ext", "mp3"))
    code = _field(song_url, "code", 0)
    if not _field(song_url, "ok", True) or not url:
        return "failed", None, f"no-url({code})"

    settings = config.get()
    folder = os.path.join(settings.paths.music_dir, safe_name(job.get("artist") or "未知歌手"))
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        return "failed", None, f"创建目录失败：{_short_error(exc)}"
    final = os.path.join(folder, f"{_job_base_name(job, song_id)}.{ext}")
    part = final + ".part"
    if os.path.exists(final) and os.path.getsize(final) > MIN_AUDIO_BYTES:
        return "skipped", final, "已存在"
    if os.path.exists(final):
        _remove(final)  # 过小的残留文件视为无效，重新下载

    done = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": _user_agent(), "Referer": _referer()}
    if done:
        headers["Range"] = f"bytes={done}-"
    try:
        response = _open_url(str(url), headers)
    except urllib.error.HTTPError as exc:
        code_http = int(getattr(exc, "code", 0) or 0)
        if code_http == 416 and done:
            _remove(part)  # 断点失效：清掉 .part 后从头重来
            raise _TransientError("断点失效(http-416)，重新下载") from exc
        if code_http in _PERMANENT_HTTP_CODES or (
            400 <= code_http < 500 and code_http not in _TRANSIENT_HTTP_CODES
        ):
            return "failed", None, f"http-{code_http}"
        raise _TransientError(f"http-{code_http}") from exc
    except (
        urllib.error.URLError,
        TimeoutError,
        ConnectionError,
        OSError,
        http.client.HTTPException,
    ) as exc:
        raise _TransientError(f"网络错误：{_short_error(exc)}") from exc

    canceled = False
    try:
        content_type = _content_type(response)
        if _invalid_content_type(content_type):
            _remove(part)
            return "failed", None, f"内容非法(content-type={content_type or '空'})"
        total = _content_length(response)
        status_code = int(_field(response, "status", 200) or 200)
        mode = "ab" if done and status_code == 206 else "wb"
        if mode == "wb":
            done = 0
        total = done + total if total else 0
        if total and total < MIN_AUDIO_BYTES:
            _remove(part)
            return "failed", None, "文件过小"
        _update(job_id, bytes_done=done, bytes_total=total)
        last_pct = -1
        last_write = time.monotonic()
        with open(part, mode) as handle:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                if _is_canceled(job_id):
                    canceled = True
                    break
                handle.write(chunk)
                done += len(chunk)
                pct = min(100, int(done * 100 / total)) if total else 0
                if pct - last_pct >= PROGRESS_STEP or time.monotonic() - last_write >= (
                    PROGRESS_INTERVAL
                ):
                    _update(job_id, bytes_done=done, progress=pct)
                    last_pct = pct
                    last_write = time.monotonic()
    finally:
        _close_quietly(response)

    if canceled:
        _remove(part)
        return "canceled", None, "用户取消"
    size = os.path.getsize(part) if os.path.exists(part) else 0
    if size < MIN_AUDIO_BYTES:
        _remove(part)
        return "failed", None, "文件过小"
    if not _looks_like_audio(_read_head(part)):
        _remove(part)
        return "failed", None, "内容非法(并非音频)"
    if _is_canceled(job_id):
        _remove(part)
        return "canceled", None, "用户取消"
    try:
        _tag_and_move(part, final, job, ext)
    except OSError as exc:
        _remove(part)
        return "failed", None, f"写入文件失败：{_short_error(exc)}"
    _update(job_id, bytes_done=size, bytes_total=size, progress=100)
    return "ok", final, None


def _open_url(url: str, headers: dict) -> Any:
    """发起下载请求并返回响应对象（测试可 monkeypatch 本函数注入假响应）。"""
    request = urllib.request.Request(url, headers=headers)
    timeout = float(config.get().download_timeout)
    return urllib.request.urlopen(request, timeout=timeout)


# ---------------------------------------------------------------------------
# 标签写入
# ---------------------------------------------------------------------------
def _tag_and_move(part: str, final: str, job: dict, ext: str) -> None:
    """写标签并落盘：优先 mutagen，其次 ffmpeg，都不可用时直接改名。"""
    if _tag_with_mutagen(part, final, job, ext):
        _remove(part)
        return
    if _tag_with_ffmpeg(part, final, job, ext):
        _remove(part)
        return
    log.debug("mutagen / ffmpeg 均不可用，下载文件按原样落盘：%s", final)
    os.replace(part, final)


def _tag_with_mutagen(part: str, final: str, job: dict, ext: str) -> bool:
    """用可选依赖 ``mutagen`` 写 title/artist/album；不可用或失败返回 ``False``。"""
    try:
        import mutagen
    except Exception:
        log.debug("未安装 mutagen，跳过写标签")
        return False
    temp = f"{final}.tagtmp.{ext}"
    replaced = False
    try:
        shutil.copyfile(part, temp)
        audio = mutagen.File(temp, easy=True)
        if audio is None:
            return False
        for key, value in _tags(job).items():
            if not value:
                continue
            try:
                audio[key] = [value]
            except Exception:
                audio[key] = value
        audio.save()
        os.replace(temp, final)
        replaced = True
        return True
    except Exception as exc:
        log.warning("mutagen 写标签失败：%s", exc)
        return False
    finally:
        if not replaced:
            _remove(temp)


def _tag_with_ffmpeg(part: str, final: str, job: dict, ext: str) -> bool:
    """用 ``ffmpeg -c copy -metadata`` 写临时文件再 replace；不可用或失败返回 ``False``。"""
    temp = f"{final}.tagtmp.{ext}"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", part, "-c", "copy"]
    for key, value in _tags(job).items():
        if value:
            command.extend(["-metadata", f"{key}={value}"])
    command.append(temp)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffmpeg 写标签失败：%s", exc)
        return False
    if result.returncode != 0:
        log.warning("ffmpeg 写标签失败（exit=%s）", result.returncode)
        _remove(temp)
        return False
    try:
        os.replace(temp, final)
        return True
    except OSError as exc:
        log.warning("ffmpeg 写标签结果替换失败：%s", exc)
        _remove(temp)
        return False


def _tags(job: dict) -> dict:
    """任务 → 标签键值。"""
    return {
        "title": _text(job.get("name")),
        "artist": _text(job.get("artist")),
        "album": _text(job.get("album")),
    }


# ---------------------------------------------------------------------------
# 内容校验
# ---------------------------------------------------------------------------
def _looks_like_audio(head: bytes) -> bool:
    """按文件魔数判断是否像音频：ID3 / MP3 帧同步 / fLaC / OggS / ftyp / RIFF。"""
    if not head:
        return False
    if head.startswith(b"ID3"):
        return True
    if len(head) >= 2 and head[0] == 0xFF and head[1] in (0xFB, 0xF3, 0xF2):
        return True
    if head.startswith((b"fLaC", b"OggS", b"RIFF")):
        return True
    return len(head) >= 8 and head[4:8] == b"ftyp"


def _read_head(path: str, size: int = 16) -> bytes:
    """读取文件头部若干字节（读失败返回空 bytes）。"""
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def _content_type(response: Any) -> str:
    """响应 ``Content-Type`` 主类型（小写、去掉 charset 参数）。"""
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    raw = headers.get("Content-Type") or ""
    return str(raw).split(";")[0].strip().lower()


def _invalid_content_type(content_type: str) -> bool:
    """``text/*`` 与 JSON 错误页直接判永久失败；其余交给魔数校验。"""
    if not content_type:
        return False
    return content_type.startswith("text/") or content_type in (
        "application/json",
    ) or content_type.endswith("+json")


def _content_length(response: Any) -> int:
    """响应 ``Content-Length``（缺失或非法返回 0）。"""
    headers = getattr(response, "headers", None)
    if headers is None:
        return 0
    raw = headers.get("Content-Length")
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 数据库辅助
# ---------------------------------------------------------------------------
def _update(job_id: int, **fields: Any) -> None:
    """更新任务字段；写库失败只记日志，绝不中断正在进行的下载。"""
    if not fields:
        return
    fields["updated_at"] = _now()
    columns = ", ".join(f"{key}=?" for key in fields)
    try:
        db.execute(
            f"UPDATE download_jobs SET {columns} WHERE id=?",
            (*fields.values(), int(job_id)),
        )
    except Exception as exc:  # 进度写库失败不应让下载失败
        log.warning("更新下载任务 %s 失败：%s", job_id, exc)


def _cancel_requested(job_id: int) -> bool:
    """数据库层面的取消标记（覆盖跨进程 / 重启恢复场景）。"""
    try:
        row = db.query_one("SELECT cancel_requested FROM download_jobs WHERE id=?", (job_id,))
    except Exception:  # pragma: no cover - 查询失败按未取消处理
        return False
    return row is not None and int(row["cancel_requested"] or 0) == 1


def _is_canceled(job_id: int) -> bool:
    """内存取消集合（下载循环高频调用，避免每次查库）。"""
    with _cancel_lock:
        return job_id in _CANCELED


def _forget_canceled(job_id: int) -> None:
    """任务结束后清掉内存取消标记，避免集合无限增长。"""
    with _cancel_lock:
        _CANCELED.discard(job_id)


def _song_in_library(song_id: int) -> bool:
    """本地曲库是否已有该 ``song_id`` 的绑定（``local_tracks.song_key``）。"""
    try:
        row = db.query_one("SELECT 1 AS x FROM local_tracks WHERE song_key=? LIMIT 1", (song_id,))
    except Exception:  # pragma: no cover - 旧库没有该列时忽略
        return False
    return row is not None


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def safe_name(value: Any) -> str:
    """把任意文本转成安全文件名片段（去掉路径分隔符/控制字符并限长）。"""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value or "")).strip()
    text = text.strip(". ")
    if not text:
        return "unknown"
    if len(text) > 120:
        text = text[:120]
    while text and len(text.encode("utf-8")) > 200:
        text = text[:-1]
    return text.rstrip(". ") or "unknown"


def _job_base_name(job: dict, song_id: Optional[int]) -> str:
    """下载文件名主体：``artist - name``（无歌手时退化为歌名 / song-<id>）。"""
    artist = _text(job.get("artist"))
    name = _text(job.get("name"))
    if artist and name:
        return safe_name(f"{artist} - {name}")
    if name:
        return safe_name(name)
    return safe_name(f"song-{song_id if song_id is not None else 0}")


def _normalize_ext(raw: Any) -> str:
    """规范化扩展名：只保留字母数字并小写；不认识时退回 mp3。"""
    text = re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).lower()
    return text if text in _AUDIO_EXTS else "mp3"


def _song_dict(song: Any) -> dict:
    """把入参统一成 dict（兼容 ``netease.Song`` 等带 ``to_dict`` 的对象）。"""
    if isinstance(song, dict):
        return song
    to_dict = getattr(song, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            if isinstance(data, dict):
                return data
        except Exception:  # pragma: no cover - 异常对象退回属性提取
            pass
    data = {}
    for key in ("song_id", "name", "artist", "album", "quality"):
        if hasattr(song, key):
            data[key] = getattr(song, key)
    return data


def _song_id(song: Any) -> Optional[int]:
    """提取 ``song_id``（允许 ``id`` 作为别名）；非法返回 ``None``。"""
    data = song if isinstance(song, dict) else _song_dict(song)
    raw = data.get("song_id")
    if raw is None:
        raw = data.get("id")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """同时兼容 dataclass 属性与 dict 的字段读取。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _default_quality() -> str:
    """默认音质：优先用户运行时偏好，失败退回静态配置。"""
    try:
        return prefs.quality()
    except Exception:  # pragma: no cover - prefs 不可用时退回默认档
        return config.get().default_quality


def _user_agent() -> str:
    return str(getattr(_netease, "UA", "") or _DEFAULT_UA)


def _referer() -> str:
    return str(getattr(_netease, "REFERER", "") or _DEFAULT_REFERER)


def _now() -> int:
    return int(time.time())


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _short_error(exc: BaseException, limit: int = 300) -> str:
    """异常 → 可入库的短描述（含类型名，便于排查）。"""
    text = f"{exc.__class__.__name__}: {exc}".strip()
    return text[:limit] if text else exc.__class__.__name__


def _remove(path: str) -> None:
    """静默删除文件。"""
    try:
        os.remove(path)
    except OSError:
        pass


def _close_quietly(response: Any) -> None:
    """静默关闭响应对象。"""
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # pragma: no cover - 关闭失败无补救价值
            pass
