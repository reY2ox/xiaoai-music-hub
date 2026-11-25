"""下载引擎：持久化队列 + 并发 + 重试 + 断点续传。

设计要点
--------
- 任务存 SQLite（`download_jobs`），服务重启后能恢复
- 工作线程按并发度领取 pending 任务
- 单曲下载中途失败可续传（.part 文件 + Range）
- 下载完成后写标签、落盘、重建索引
"""
import os
import re
import threading
import time
import urllib.request

from . import config, library, netease

_worker_lock = threading.Lock()
_workers_running = 0


# ---------- 表结构 ----------
def init_table():
    c = config.db()
    try:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS download_jobs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              song_id INTEGER, name TEXT, artist TEXT, album TEXT,
              quality TEXT, status TEXT, progress INTEGER DEFAULT 0,
              error TEXT, path TEXT, bytes_done INTEGER DEFAULT 0,
              bytes_total INTEGER DEFAULT 0, retries INTEGER DEFAULT 0,
              created_at INTEGER, updated_at INTEGER);
            """
        )
        c.commit()
    finally:
        c.close()


def _now():
    return int(time.time())


def add_job(song, quality=None):
    """加入下载队列，返回 job id；已存在同曲任务则复用"""
    init_table()
    c = config.db()
    try:
        exist = c.execute(
            "SELECT id,status FROM download_jobs WHERE song_id=? AND status IN ('pending','running')",
            (song["song_id"],),
        ).fetchone()
        if exist:
            return exist["id"]
        cur = c.execute(
            """INSERT INTO download_jobs(song_id,name,artist,album,quality,status,
               created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?)""",
            (song["song_id"], song.get("name", ""), song.get("artist", ""),
             song.get("album", ""), quality or config.get_setting("quality", config.DEFAULT_QUALITY),
             _now(), _now()),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def add_playlist(playlist_id):
    """把整个歌单里还没下载的曲目加入队列，返回新增数量"""
    c = config.db()
    try:
        tracks = c.execute(
            "SELECT song_id,name,artist,album,local_id FROM tracks WHERE playlist_id=?",
            (playlist_id,),
        ).fetchall()
    finally:
        c.close()
    n = 0
    for t in tracks:
        if t["local_id"] or not t["song_id"]:
            continue
        add_job({"song_id": t["song_id"], "name": t["name"],
                 "artist": t["artist"], "album": t["album"]})
        n += 1
    return n


def jobs(limit=100):
    init_table()
    c = config.db()
    try:
        rows = c.execute(
            "SELECT * FROM download_jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def stats():
    init_table()
    c = config.db()
    try:
        rows = c.execute(
            "SELECT status, COUNT(*) n FROM download_jobs GROUP BY status"
        ).fetchall()
        out = {r["status"]: r["n"] for r in rows}
    finally:
        c.close()
    out["running_workers"] = _workers_running
    return out


def _update(job_id, **kw):
    """更新任务状态；写库失败只记日志，绝不能中断正在进行的下载"""
    if not kw:
        return
    kw["updated_at"] = _now()
    sets = ",".join(f"{k}=?" for k in kw)
    for attempt in range(3):
        c = None
        try:
            c = config.db()
            c.execute(f"UPDATE download_jobs SET {sets} WHERE id=?", (*kw.values(), job_id))
            c.commit()
            return
        except Exception as e:
            if attempt == 2:
                try:
                    print(f"[downloader] update job {job_id} failed: {e!r}", flush=True)
                except Exception:
                    pass
            else:
                time.sleep(0.5)
        finally:
            if c:
                try:
                    c.close()
                except Exception:
                    pass


def retry_failed(job_id=None):
    c = config.db()
    try:
        if job_id:
            c.execute(
                "UPDATE download_jobs SET status='pending',error=NULL,retries=0 WHERE id=? AND status='failed'",
                (job_id,),
            )
        else:
            c.execute(
                "UPDATE download_jobs SET status='pending',error=NULL,retries=0 WHERE status='failed'"
            )
        c.commit()
    finally:
        c.close()


def cancel(job_id):
    _update(job_id, status="canceled", error="用户取消")


# ---------- 实际下载 ----------
def safe_name(x):
    x = re.sub(r'[\\/:*?"<>|]+', "_", (x or "").strip())
    return x[:120] or "unknown"


def _download_one(job):
    """下载单个任务，支持断点续传。返回 (status, path)"""
    quality = job.get("quality") or config.DEFAULT_QUALITY
    url, code, br, ext = netease.song_url(job["song_id"], quality)
    if not url:
        return f"no-url({code})", None

    folder = os.path.join(config.MUSIC_DIR, safe_name(job.get("artist") or "未知歌手"))
    os.makedirs(folder, exist_ok=True)
    base = safe_name(f'{job.get("artist")} - {job.get("name")}' if job.get("artist") else job.get("name"))
    final = os.path.join(folder, f"{base}.{ext}")
    part = final + ".part"

    if os.path.exists(final) and os.path.getsize(final) > 10240:
        return "skipped", final

    headers = {"User-Agent": netease.UA, "Referer": netease.REFERER}
    done = os.path.getsize(part) if os.path.exists(part) else 0
    if done:
        headers["Range"] = f"bytes={done}-"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=config.DOWNLOAD_TIMEOUT) as r:
        total = r.headers.get("Content-Length")
        total = int(total) + done if total else 0
        mode = "ab" if done and r.status == 206 else "wb"
        if mode == "wb":
            done = 0
        _update(job["id"], bytes_total=total, bytes_done=done)
        last_pct, last_write = -1, 0.0
        with open(part, mode) as f:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                pct = int(done * 100 / total) if total else 0
                # 降频写库：进度变化 2% 以上或超过 2 秒才写，避免和网页轮询抢锁
                if pct - last_pct >= 2 or (time.time() - last_write) >= 2:
                    _update(job["id"], bytes_done=done, progress=pct)
                    last_pct, last_write = pct, time.time()
        _update(job["id"], bytes_done=done, progress=100 if total else 0)

    if os.path.getsize(part) < 10240:
        os.remove(part)
        return "too-small", None

    # 写标签（无损直接改名，mp3 用 ffmpeg 写元数据）
    try:
        subprocess_ffmpeg_tag(part, final, job, ext)
        if os.path.exists(part):
            os.remove(part)
    except Exception:
        os.replace(part, final)
    return "ok", final


def subprocess_ffmpeg_tag(src, dst, job, ext):
    import subprocess
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-c", "copy",
         "-metadata", f'title={job.get("name","")}',
         "-metadata", f'artist={job.get("artist","")}',
         "-metadata", f'album={job.get("album","")}',
         dst],
        check=True, timeout=180,
    )


def _claim_next():
    """原子领取一个 pending 任务"""
    c = config.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT * FROM download_jobs WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            c.commit()
            return None
        c.execute(
            "UPDATE download_jobs SET status='running',updated_at=? WHERE id=?",
            (_now(), row["id"]),
        )
        c.commit()
        return dict(row)
    finally:
        c.close()


def _mark_playlist_downloaded(song_id, path):
    """把歌单里对应曲目标记为已下载（填 local_id）"""
    c = config.db()
    try:
        row = c.execute("SELECT id FROM local_tracks WHERE path=?", (path,)).fetchone()
        if not row:
            return
        c.execute(
            "UPDATE tracks SET local_id=? WHERE song_id=? AND COALESCE(local_id,0)=0",
            (row["id"], song_id))
        c.commit()
    finally:
        c.close()


def _index_file(path, job):
    """下载完成立刻入库，不必等手动扫描"""
    from . import library
    try:
        library.scan()
    except Exception:
        pass


def _worker_loop():
    global _workers_running
    with _worker_lock:
        _workers_running += 1
    try:
        while True:
            job = _claim_next()
            if not job:
                break
            try:
                status, path = _download_one(job)
                if status == "ok":
                    _update(job["id"], status="done", progress=100, path=path, error=None)
                elif status == "skipped":
                    _update(job["id"], status="done", progress=100, path=path, error="已存在")
                if status in ("ok", "skipped") and path:
                    _index_file(path, job)
                    try:
                        import re as _re
                        from . import library as _lib
                        _lib.scan()
                        _lib.index_downloaded(path, job["song_id"], job.get("name"), job.get("artist"))
                        _mark_playlist_downloaded(job["song_id"], path)
                    except Exception as e:
                        print(f"[downloader] index failed: {e!r}", flush=True)
                else:
                    raise RuntimeError(status)
            except Exception as e:
                retries = int(job.get("retries") or 0) + 1
                if retries <= config.DOWNLOAD_MAX_RETRY:
                    _update(job["id"], status="pending", retries=retries, error=repr(e)[:300])
                    time.sleep(3)
                else:
                    _update(job["id"], status="failed", retries=retries, error=repr(e)[:300])
    finally:
        with _worker_lock:
            _workers_running -= 1


def kick():
    """确保有工作线程在跑"""
    global _workers_running
    with _worker_lock:
        need = config.DOWNLOAD_CONCURRENCY - _workers_running
    for _ in range(max(0, need)):
        threading.Thread(target=_worker_loop, daemon=True).start()


def recover():
    """服务启动时调用：把 running 状态的任务退回 pending"""
    init_table()
    c = config.db()
    try:
        c.execute("UPDATE download_jobs SET status='pending' WHERE status='running'")
        c.commit()
    finally:
        c.close()
    kick()


def rescan_after_batch():
    """下载完成一批后重建索引（由调用方在合适时机触发）"""
    return library.scan()
