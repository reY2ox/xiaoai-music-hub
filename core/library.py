"""本地曲库：扫描、索引、转码、媒体流输出。"""
import os
import subprocess

from . import config


def parse_name(path):
    """从文件名猜 歌手 / 歌名：'周杰伦 - 晴天.mp3'"""
    base = os.path.splitext(os.path.basename(path))[0]
    for sep in (" - ", "-", "—"):
        if sep in base:
            a, b = base.split(sep, 1)
            return b.strip(), a.strip()
    return base, ""


def scan(root=None):
    """扫目录建索引，返回新增数量"""
    root = root or config.MUSIC_DIR
    os.makedirs(root, exist_ok=True)
    c = config.db()
    added = 0
    try:
        for dirpath, _, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith(config.AUDIO_EXT):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fp)
                except Exception:
                    continue
                title, artist = parse_name(fp)
                row = c.execute("SELECT id FROM local_tracks WHERE path=?", (fp,)).fetchone()
                if row:
                    c.execute("UPDATE local_tracks SET size=?, mtime=? WHERE id=?",
                              (st.st_size, int(st.st_mtime), row["id"]))
                else:
                    c.execute(
                        """INSERT INTO local_tracks(path,title,artist,album,size,mtime)
                           VALUES(?,?,?,?,?,?)""",
                        (fp, title, artist, os.path.basename(dirpath), st.st_size, int(st.st_mtime)),
                    )
                    added += 1
        c.commit()
    finally:
        c.close()
    return added


def list_tracks(query="", limit=200):
    c = config.db()
    try:
        if query:
            rows = c.execute(
                """SELECT * FROM local_tracks WHERE title LIKE ? OR artist LIKE ?
                   ORDER BY artist,title LIMIT ?""",
                (f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM local_tracks ORDER BY artist,title LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def local_for_song(song_id, name=None, artist=None):
    """查找某首网易云歌曲在本地曲库里的文件；没有则返回 None"""
    if not song_id:
        return None
    c = config.db()
    try:
        row = c.execute(
            "SELECT * FROM local_tracks WHERE song_key=?", (int(song_id),)
        ).fetchone()
        if row:
            return dict(row)
    finally:
        c.close()
    # 兜底：用 歌手 - 歌名 推断文件名
    if name:
        import re
        base = re.sub(r'[\\/:*?"<>|]+', "_", f"{artist} - {name}" if artist else name)[:120]
        for ext in (".flac", ".mp3", ".m4a"):
            for cand in (os.path.join(config.MUSIC_DIR, artist or "未知歌手", base + ext),
                         os.path.join(config.MUSIC_DIR, base + ext)):
                if os.path.exists(cand):
                    c = config.db()
                    try:
                        r = c.execute("SELECT * FROM local_tracks WHERE path=?", (cand,)).fetchone()
                        if r:
                            return dict(r)
                    finally:
                        c.close()
    return None


def index_downloaded(path, song_id, title=None, artist=None):
    """把下载完成的文件与网易云 song_id 绑定，便于日后直接播放本地文件"""
    c = config.db()
    try:
        c.execute("UPDATE local_tracks SET song_key=? WHERE path=?", (int(song_id), path))
        c.commit()
    finally:
        c.close()


def media_path(track):
    """给音箱播放的实际文件路径，必要时转码成 mp3"""
    src = track["path"]
    ext = os.path.splitext(src)[1].lower()
    if ext not in config.NEED_TRANSCODE:
        return src
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    out = os.path.join(config.CACHE_DIR, f'{track["id"]}.mp3')
    if os.path.exists(out) and os.path.getmtime(out) >= track["mtime"]:
        return out
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-codec:a", "libmp3lame", "-b:a", "320k", out],
        check=True, timeout=300,
    )
    return out


def media_url(track, token=""):
    q = f"?t={token}" if token else ""
    return f"http://{config.LAN_IP}:{config.PORT}/media/{track['id']}{q}"


def stream_file(handler, path, mime="audio/mpeg"):
    """支持 Range 的文件输出，DLNA 渲染器必需"""
    size = os.path.getsize(path)
    rng = handler.headers.get("Range")
    start, end = 0, size - 1
    if rng and rng.startswith("bytes="):
        parts = rng[6:].split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
    end = min(end, size - 1)
    length = max(0, end - start + 1)

    handler.send_response(206 if rng else 200)
    handler.send_header("Content-Type", mime)
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(length))
    if rng:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers()

    with open(path, "rb") as f:
        f.seek(start)
        left = length
        while left > 0:
            chunk = f.read(min(65536, left))
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except Exception:
                break
            left -= len(chunk)
