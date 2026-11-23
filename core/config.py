"""配置与常量。

所有可调参数集中在这里，改行为先改这里，不要散到各处。
"""
import json
import os
import sqlite3

# ---------- 路径 ----------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "music.db")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
SECRETS_DIR = os.path.join(BASE_DIR, "secrets")
COOKIE_FILE = os.path.join(SECRETS_DIR, "netease.cookie")
WEB_DIR = os.path.join(BASE_DIR, "web")

# ---------- 网络 ----------
DEFAULT_SPEAKER_IP = os.environ.get("XIAOAI_IP", "192.168.3.81")
PORT = int(os.environ.get("PORT", "8010"))


def detect_lan_ip():
    """本机在局域网中的地址，用于拼媒体 URL"""
    import socket
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("192.168.3.1", 80))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except Exception:
        return "127.0.0.1"


LAN_IP = detect_lan_ip()

# ---------- 媒体 ----------
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/root/music")
AUDIO_EXT = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac", ".ape", ".wma")
NEED_TRANSCODE = (".flac", ".m4a", ".wav", ".ape", ".wma", ".aac")

# 音质档位：网易云 br 参数
QUALITY_LEVELS = {
    "standard": {"br": 192000, "label": "标准 192k", "ext": "mp3"},
    "high": {"br": 320000, "label": "高品质 320k", "ext": "mp3"},
    "lossless": {"br": 999000, "label": "无损 FLAC", "ext": "flac"},
}
DEFAULT_QUALITY = "high"

# 下载
DOWNLOAD_CONCURRENCY = 2
DOWNLOAD_MAX_RETRY = 2
DOWNLOAD_TIMEOUT = 120

# ---------- 播放行为 ----------
AUTO_ADVANCE_FRESH_SECONDS = 120   # 自动续播要求最近 N 秒确实在播
AUTO_ADVANCE_INTERVAL = 2          # 轮询间隔
QUIET_DEFAULT = "23:30-08:00"      # 夜间免打扰

PLAY_MODES = ("sequential", "loop", "single", "shuffle")


# ---------- 数据库 ----------
def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def get_setting(key, default=None):
    c = db()
    try:
        row = c.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    finally:
        c.close()
    return row["v"] if row else default


def set_setting(key, value):
    c = db()
    try:
        c.execute(
            "INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value)),
        )
        c.commit()
    finally:
        c.close()


def quality_br(name=None):
    """把档位名字换成网易云 br 参数"""
    name = name or get_setting("quality", DEFAULT_QUALITY) or DEFAULT_QUALITY
    return QUALITY_LEVELS.get(name, QUALITY_LEVELS[DEFAULT_QUALITY])["br"]


def quality_ext(name=None):
    name = name or get_setting("quality", DEFAULT_QUALITY) or DEFAULT_QUALITY
    return QUALITY_LEVELS.get(name, QUALITY_LEVELS[DEFAULT_QUALITY])["ext"]
