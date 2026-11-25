"""SQLite 基础设施：连接、Schema、版本化迁移、键值设置。

设计要点
--------
- 每次操作使用独立连接（SQLite WAL + busy_timeout 足够应付本项目并发量），
  连接用 ``row_factory=sqlite3.Row``，``with`` 上下文保证关闭。
- Schema 采用「基线全量 + 幂等补列」策略：
  新库直接建 v3 全量表；旧库通过 :data:`LEGACY_COLUMNS` 逐列补齐，绝不丢数据。
- 未来的结构变更追加到 :data:`MIGRATIONS`（版本号 > :data:`SCHEMA_VERSION`）。
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from typing import Any, Iterable, Iterator, List, Optional, Sequence, Set

from . import config, logging_setup

log = logging_setup.get_logger(__name__)

SCHEMA_VERSION = 3

_db_path: Optional[str] = None
_init_lock = threading.Lock()

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
  ip TEXT PRIMARY KEY,
  name TEXT,
  model TEXT,
  udn TEXT,
  avt_control TEXT DEFAULT '',
  rc_control TEXT DEFAULT '',
  last_seen INTEGER,
  created_at INTEGER
);

CREATE TABLE IF NOT EXISTS playlists(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  created_at INTEGER,
  source TEXT DEFAULT 'local',
  remote_id INTEGER,
  cover TEXT,
  owner_uid INTEGER,
  position INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tracks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_id INTEGER NOT NULL,
  song_id INTEGER,
  name TEXT,
  artist TEXT,
  album TEXT,
  pos INTEGER,
  local_id INTEGER,
  duration REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS local_tracks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT UNIQUE,
  title TEXT,
  artist TEXT,
  album TEXT,
  size INTEGER,
  mtime INTEGER,
  song_key INTEGER,
  duration REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS download_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  song_id INTEGER,
  name TEXT,
  artist TEXT,
  album TEXT,
  quality TEXT,
  status TEXT,
  progress INTEGER DEFAULT 0,
  error TEXT,
  path TEXT,
  bytes_done INTEGER DEFAULT 0,
  bytes_total INTEGER DEFAULT 0,
  retries INTEGER DEFAULT 0,
  attempts INTEGER DEFAULT 0,
  cancel_requested INTEGER DEFAULT 0,
  created_at INTEGER,
  updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS play_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  song_id INTEGER,
  local_id INTEGER,
  name TEXT,
  artist TEXT,
  source TEXT,
  played_at INTEGER
);
"""

#: 索引必须在旧库补列之后再创建（旧库可能还没有 song_key / remote_id 等列）
BASE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tracks_playlist ON tracks(playlist_id, pos);
CREATE INDEX IF NOT EXISTS idx_tracks_song ON tracks(song_id);
CREATE INDEX IF NOT EXISTS idx_local_song_key ON local_tracks(song_key);
CREATE INDEX IF NOT EXISTS idx_local_artist ON local_tracks(artist);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON download_jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_history_played ON play_history(played_at DESC);
CREATE INDEX IF NOT EXISTS idx_playlists_remote ON playlists(remote_id);
"""

#: 旧库（v1/v2）补齐的列：(表, 列, 类型定义)
LEGACY_COLUMNS: Sequence[tuple] = (
    ("tracks", "local_id", "INTEGER"),
    ("tracks", "duration", "REAL DEFAULT 0"),
    ("playlists", "source", "TEXT DEFAULT 'local'"),
    ("playlists", "remote_id", "INTEGER"),
    ("playlists", "cover", "TEXT"),
    ("playlists", "owner_uid", "INTEGER"),
    ("playlists", "position", "INTEGER DEFAULT 0"),
    ("local_tracks", "song_key", "INTEGER"),
    ("local_tracks", "duration", "REAL DEFAULT 0"),
    ("devices", "avt_control", "TEXT DEFAULT ''"),
    ("devices", "rc_control", "TEXT DEFAULT ''"),
    ("download_jobs", "attempts", "INTEGER DEFAULT 0"),
    ("download_jobs", "cancel_requested", "INTEGER DEFAULT 0"),
)

#: 未来迁移：``(目标版本, SQL)``，版本号从 SCHEMA_VERSION + 1 开始
MIGRATIONS: List[tuple] = []


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def configure(db_path: Optional[str] = None) -> None:
    """覆盖数据库路径（测试用）；传 ``None`` 恢复默认。"""
    global _db_path
    _db_path = db_path


def db_path() -> str:
    return _db_path or config.get().paths.db_path


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    """新建连接（调用方负责关闭）。"""
    target = path or db_path()
    directory = os.path.dirname(os.path.abspath(target))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextlib.contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------
def query(sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
    with connection() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    with connection() as conn:
        return conn.execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """执行写语句，返回 ``lastrowid``（非 INSERT 时无意义）。"""
    with transaction() as conn:
        cur = conn.execute(sql, tuple(params))
        return int(cur.lastrowid or 0)


def execute_many(sql: str, rows: Iterable[Sequence[Any]]) -> int:
    with transaction() as conn:
        cur = conn.executemany(sql, list(rows))
        return int(cur.rowcount or 0)


def executescript(sql: str) -> None:
    with transaction() as conn:
        conn.executescript(sql)


def table_columns(table: str) -> Set[str]:
    try:
        rows = query(f"PRAGMA table_info({table})")
    except sqlite3.Error:
        return set()
    return {row["name"] for row in rows}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    exists = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not exists:
        return False
    if column in exists:
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        log.info("数据库迁移：%s 增加列 %s", table, column)
        return True
    except sqlite3.OperationalError as exc:  # 并发下可能已被其它进程补上
        log.debug("补列跳过 %s.%s: %s", table, column, exc)
        return False


def init_schema() -> None:
    """建表 / 补列 / 迁移 / 种子数据（幂等，进程内串行）。"""
    with _init_lock:
        with transaction() as conn:
            conn.executescript(BASE_SCHEMA)
            for table, column, ddl in LEGACY_COLUMNS:
                _ensure_column(conn, table, column, ddl)
            conn.executescript(BASE_INDEXES)

            version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
            for target, sql in MIGRATIONS:
                if version < target:
                    log.info("数据库迁移：升级到 v%s", target)
                    conn.executescript(sql)
                    conn.execute(f"PRAGMA user_version = {int(target)}")
                    version = int(target)
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

            _seed(conn)


def _seed(conn: sqlite3.Connection) -> None:
    import time

    now = int(time.time())
    settings = config.get()
    if not conn.execute("SELECT 1 FROM devices LIMIT 1").fetchone():
        conn.execute(
            "INSERT INTO devices(ip,name,model,udn,last_seen,created_at) VALUES(?,?,?,?,?,?)",
            (settings.speaker_ip, "小爱音箱", "S12", "", now, now),
        )
    if not conn.execute("SELECT 1 FROM playlists LIMIT 1").fetchone():
        conn.execute(
            "INSERT INTO playlists(name,created_at,source,position) VALUES(?,?,?,?)",
            ("我的歌单", now, "local", 0),
        )


# ---------------------------------------------------------------------------
# 键值设置
# ---------------------------------------------------------------------------
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        row = query_one("SELECT v FROM settings WHERE k=?", (key,))
    except sqlite3.Error:
        return default
    return row["v"] if row else default


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, "" if value is None else str(value)),
    )


def delete_setting(key: str) -> None:
    execute("DELETE FROM settings WHERE k=?", (key,))


def get_int_setting(key: str, default: int) -> int:
    raw = get_setting(key)
    try:
        return int(raw) if raw is not None and raw != "" else default
    except (TypeError, ValueError):
        return default


def get_bool_setting(key: str, default: bool = False) -> bool:
    raw = get_setting(key)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def all_settings() -> dict:
    return {row["k"]: row["v"] for row in query("SELECT k,v FROM settings")}
