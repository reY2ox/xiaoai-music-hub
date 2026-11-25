#!/usr/bin/env python3
"""小爱音乐中枢 v3 — 入口。

结构：
  core/config.py      常量与设置
  core/dlna.py        UPnP 控制
  core/netease.py     网易云接口
  core/library.py     本地曲库
  core/downloader.py  下载队列
  core/player.py      播放调度
  core/api.py         HTTP 路由
  web/index.html      前端

薄入口：只负责建表、起线程、启动 HTTP 服务。
"""
import os
import threading
from http.server import ThreadingHTTPServer

from core import config, downloader, player as player_mod
from core.api import Handler


def init_db():
    c = config.db()
    try:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices(
              ip TEXT PRIMARY KEY, name TEXT, model TEXT, udn TEXT,
              last_seen INTEGER, created_at INTEGER);
            CREATE TABLE IF NOT EXISTS playlists(
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
              created_at INTEGER);
            CREATE TABLE IF NOT EXISTS tracks(
              id INTEGER PRIMARY KEY AUTOINCREMENT, playlist_id INTEGER NOT NULL,
              song_id INTEGER, name TEXT, artist TEXT, album TEXT, pos INTEGER);
            CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS local_tracks(
              id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE, title TEXT,
              artist TEXT, album TEXT, size INTEGER, mtime INTEGER);
            """
        )
        # 渐进式迁移
        for ddl in (
            "ALTER TABLE tracks ADD COLUMN local_id INTEGER",
            "ALTER TABLE playlists ADD COLUMN source TEXT DEFAULT 'local'",
            "ALTER TABLE playlists ADD COLUMN remote_id INTEGER",
            "ALTER TABLE playlists ADD COLUMN cover TEXT",
            "ALTER TABLE playlists ADD COLUMN owner_uid INTEGER",
            "ALTER TABLE local_tracks ADD COLUMN song_key INTEGER",
        ):
            try:
                c.execute(ddl)
            except Exception:
                pass
        import time
        if not c.execute("SELECT 1 FROM devices").fetchone():
            c.execute(
                "INSERT INTO devices(ip,name,model,udn,last_seen,created_at) VALUES(?,?,?,?,?,?)",
                (config.DEFAULT_SPEAKER_IP, "小爱音箱", "S12", "", int(time.time()), int(time.time())),
            )
        if not c.execute("SELECT 1 FROM playlists").fetchone():
            c.execute("INSERT INTO playlists(name,created_at) VALUES(?,?)",
                      ("我的歌单", int(time.time())))
        c.commit()
    finally:
        c.close()


def main():
    init_db()
    downloader.recover()                      # 恢复中断的下载任务
    threading.Thread(target=player_mod.player.run_auto_advance, daemon=True).start()
    print(f"[music-hub] listening on :{config.PORT}, speaker={config.DEFAULT_SPEAKER_IP}, "
          f"lan={config.LAN_IP}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
