"""基础层测试：config / db / prefs / security / errors / logging。"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3

import pytest
from xiaoai_hub import config, db, errors, logging_setup, prefs, security


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def test_load_defaults(tmp_path):
    settings = config.load(env={}, base_dir=str(tmp_path))
    assert settings.port == 8010
    assert settings.host == "0.0.0.0"
    assert settings.speaker_ip == "192.168.3.81"
    assert settings.auto_advance_unknown_duration is False
    assert settings.default_quality == "high"
    assert settings.paths.music_dir == str(tmp_path / "music")
    assert settings.paths.db_path == str(tmp_path / "music.db")
    assert settings.paths.cookie_file == str(tmp_path / "secrets" / "netease.cookie")


def test_load_env_overrides(tmp_path):
    env = {
        "PORT": "9000",
        "XIAOAI_HOST": "127.0.0.1",
        "XIAOAI_IP": "10.0.0.5",
        "LAN_IP": "10.0.0.9",
        "MUSIC_DIR": str(tmp_path / "songs"),
        "DATA_DIR": str(tmp_path / "data"),
        "AUTO_ADVANCE_UNKNOWN_DURATION": "1",
        "DOWNLOAD_CONCURRENCY": "5",
        "END_TOLERANCE": "12.5",
        "UI_PASSWORD": "pw",
        "LOG_LEVEL": "debug",
    }
    settings = config.load(env=env, base_dir=str(tmp_path))
    assert settings.port == 9000
    assert settings.host == "127.0.0.1"
    assert settings.speaker_ip == "10.0.0.5"
    assert settings.lan_ip == "10.0.0.9"
    assert settings.lan_ip_source == "env:LAN_IP"
    assert settings.auto_advance_unknown_duration is True
    assert settings.download_concurrency == 5
    assert settings.end_tolerance == 12.5
    assert settings.ui_password == "pw"
    assert settings.log_level == "DEBUG"
    assert settings.paths.music_dir == str(tmp_path / "songs")
    assert settings.paths.db_path == str(tmp_path / "data" / "music.db")


def test_load_tolerates_invalid_env(tmp_path):
    env = {"PORT": "not-a-number", "DOWNLOAD_CONCURRENCY": "-3", "AUTO_ADVANCE_INTERVAL": "0"}
    settings = config.load(env=env, base_dir=str(tmp_path))
    assert settings.port == 8010
    assert settings.download_concurrency == 1
    assert settings.auto_advance_interval >= 0.5


def test_singleton_reload_and_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "8899")
    monkeypatch.setenv("BASE_DIR_IGNORED", "1")
    config.reset()
    assert config.get().port == 8899
    assert config.reload(env={"PORT": "8800"}, base_dir=str(tmp_path)).port == 8800
    assert config.get().port == 8800
    config.reset()


def test_detect_lan_ip_explicit_wins(tmp_path):
    ip, source = config.detect_lan_ip(speaker_ip="192.168.3.81",
                                      env={"LAN_IP": "192.168.1.50"})
    assert ip == "192.168.1.50" and source == "env:LAN_IP"


def test_parse_quiet_hours():
    assert config.parse_quiet_hours("23:30-08:00") == (1410, 480)
    assert config.parse_quiet_hours("08:00-09:00") == (480, 540)
    assert config.parse_quiet_hours("") is None
    assert config.parse_quiet_hours("bad") is None
    assert config.parse_quiet_hours("25:00-08:00") is None
    assert config.parse_quiet_hours("08:00-08:00") is None  # 时长 0 视为关闭


def test_quality_mapping():
    assert config.quality_br("lossless") == 999000
    assert config.quality_ext("lossless") == "flac"
    assert config.quality_br("unknown") == 320000
    assert config.quality_label("high") == "高品质 320k"


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------
def test_init_schema_idempotent(tmp_hub):
    db.init_schema()
    db.init_schema()
    columns = db.table_columns("tracks")
    assert {"playlist_id", "song_id", "name", "artist", "album", "pos",
            "local_id", "duration"} <= columns
    for table in ("devices", "playlists", "settings", "local_tracks",
                  "download_jobs", "play_history"):
        assert db.table_columns(table), table
    version = db.query_one("PRAGMA user_version")[0]
    assert version == db.SCHEMA_VERSION


def test_schema_seeds_defaults(tmp_hub):
    assert db.query_one("SELECT COUNT(*) AS n FROM devices")["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM playlists")["n"] == 1


def test_settings_roundtrip(tmp_hub):
    db.set_setting("k", "v")
    assert db.get_setting("k") == "v"
    assert db.get_setting("missing") is None
    assert db.get_setting("missing", "default") == "default"
    db.set_setting("k", "v2")
    assert db.get_setting("k") == "v2"
    db.delete_setting("k")
    assert db.get_setting("k") is None


def test_transaction_rolls_back(tmp_hub):
    before = db.query_one("SELECT COUNT(*) AS n FROM playlists")["n"]
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO playlists(name,created_at) VALUES('x',0)")
            raise RuntimeError("boom")
    assert db.query_one("SELECT COUNT(*) AS n FROM playlists")["n"] == before


def test_legacy_database_is_migrated(tmp_path):
    """模拟 v2 旧库：缺少新列/新表，init_schema 后应无损升级。"""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE devices(ip TEXT PRIMARY KEY, name TEXT, model TEXT, udn TEXT,
                             last_seen INTEGER, created_at INTEGER);
        CREATE TABLE playlists(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                               created_at INTEGER);
        CREATE TABLE tracks(id INTEGER PRIMARY KEY AUTOINCREMENT, playlist_id INTEGER NOT NULL,
                            song_id INTEGER, name TEXT, artist TEXT, album TEXT, pos INTEGER);
        CREATE TABLE settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE local_tracks(id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE,
                                  title TEXT, artist TEXT, album TEXT, size INTEGER, mtime INTEGER);
        INSERT INTO playlists(name, created_at) VALUES ('旧歌单', 1);
        INSERT INTO tracks(playlist_id, song_id, name, pos) VALUES (1, 555, '旧歌', 1);
        """
    )
    conn.commit()
    conn.close()

    db.configure(path)
    try:
        db.init_schema()
        assert {"local_id", "duration"} <= db.table_columns("tracks")
        assert {"source", "remote_id", "cover", "owner_uid", "position"} <= \
            db.table_columns("playlists")
        assert "song_key" in db.table_columns("local_tracks")
        assert db.table_columns("download_jobs")
        assert db.table_columns("play_history")
        # 数据仍在
        assert db.query_one("SELECT name FROM playlists WHERE id=1")["name"] == "旧歌单"
        assert db.query_one("SELECT name FROM tracks WHERE id=1")["name"] == "旧歌"
    finally:
        db.configure(None)


# ---------------------------------------------------------------------------
# prefs
# ---------------------------------------------------------------------------
def test_quality_pref(tmp_hub):
    assert prefs.quality() == "high"
    assert prefs.quality_br() == 320000
    prefs.set_quality("lossless")
    assert prefs.quality() == "lossless"
    assert prefs.quality_ext() == "flac"
    with pytest.raises(ValueError):
        prefs.set_quality("bad")


def test_play_mode_pref(tmp_hub):
    assert prefs.play_mode() == "sequential"
    prefs.set_play_mode("shuffle")
    assert prefs.play_mode() == "shuffle"
    with pytest.raises(ValueError):
        prefs.set_play_mode("bad")


def test_quiet_hours_pref(tmp_hub):
    prefs.set_quiet_hours("22:00-07:00")
    assert prefs.quiet_hours() == "22:00-07:00"
    assert prefs.in_quiet_hours(dt.datetime(2026, 1, 1, 23, 0)) is True
    assert prefs.in_quiet_hours(dt.datetime(2026, 1, 1, 6, 59)) is True
    assert prefs.in_quiet_hours(dt.datetime(2026, 1, 1, 8, 0)) is False
    assert prefs.in_quiet_hours(dt.datetime(2026, 1, 1, 12, 0)) is False
    prefs.disable_quiet_hours()
    assert prefs.quiet_hours() == ""
    assert prefs.in_quiet_hours(dt.datetime(2026, 1, 1, 23, 0)) is False
    with pytest.raises(ValueError):
        prefs.set_quiet_hours("bad")


def test_active_device_pref(tmp_hub):
    assert prefs.active_device() is None
    prefs.set_active_device("192.168.3.81")
    assert prefs.active_device() == "192.168.3.81"
    with pytest.raises(ValueError):
        prefs.set_active_device("")


def test_ui_password_env_overrides_db(tmp_hub, monkeypatch):
    prefs.set_ui_password("db-pass")
    assert prefs.ui_password() == "db-pass"
    monkeypatch.setenv("UI_PASSWORD", "env-pass")
    config.reset()
    try:
        assert prefs.ui_password() == "env-pass"
    finally:
        monkeypatch.delenv("UI_PASSWORD", raising=False)
        config.reset()
        prefs.set_ui_password("")


def test_public_settings_shape(tmp_hub):
    payload = prefs.public_settings()
    for key in ("quality", "quality_levels", "play_mode", "play_modes",
                "quiet_hours", "quiet_active", "device", "auth_required", "version"):
        assert key in payload


# ---------------------------------------------------------------------------
# security
# ---------------------------------------------------------------------------
def test_media_token():
    assert security.media_token(1, None) == ""
    token = security.media_token(7, "secret")
    assert token and len(token) == 16
    assert security.verify_media_token(7, token, "secret") is True
    assert security.verify_media_token(7, "bad", "secret") is False
    assert security.verify_media_token(7, None, None) is True


def test_basic_auth():
    import base64

    assert security.basic_auth_ok(None, None) is True
    assert security.basic_auth_ok(None, "pw") is False

    header = "Basic " + base64.b64encode(b"user:pw").decode()
    assert security.basic_auth_ok(header, "pw") is True
    assert security.basic_auth_ok(header, "other") is False
    assert security.basic_auth_ok("Basic !!!not-base64!!!", "pw") is False
    assert security.basic_auth_ok("Bearer token", "pw") is False


def test_same_origin():
    assert security.same_origin({}, "127.0.0.1:8010") is True
    assert security.same_origin({"Origin": "http://127.0.0.1:8010"},
                                "127.0.0.1:8010") is True
    assert security.same_origin({"Origin": "http://evil.com"},
                                "127.0.0.1:8010") is False
    assert security.same_origin({"Referer": "http://evil.com/x"},
                                "127.0.0.1:8010") is False
    assert security.same_origin({"Origin": "null"}, "127.0.0.1:8010") is True


# ---------------------------------------------------------------------------
# errors / logging
# ---------------------------------------------------------------------------
def test_error_mapping():
    error = errors.NotFound("不存在")
    assert error.status == 404 and error.code == "not_found"
    payload = error.to_dict()
    assert payload["ok"] is False and payload["error"] == "不存在"

    assert errors.http_status(errors.Conflict("x")) == 409
    assert errors.http_status(ValueError("x")) == 500

    generic = errors.to_http(ValueError("boom"))
    assert generic["code"] == "internal"
    assert "details" in generic


def test_logging_setup_idempotent(tmp_path):
    log_file = tmp_path / "logs" / "hub.log"
    first = logging_setup.setup("DEBUG", str(log_file), force=True)
    second = logging_setup.setup("DEBUG", str(log_file), force=False)
    assert first is second
    logging_setup.get_logger("test").debug("hello")
    assert os.path.exists(log_file)


def test_detect_lan_ip_explicit_loopback_is_honored():
    """显式 LAN_IP 必须生效（本机联调/reverse proxy 场景常用 127.0.0.1）。"""
    ip, source = config.detect_lan_ip(speaker_ip="192.168.3.81",
                                      env={"LAN_IP": "127.0.0.1"})
    assert ip == "127.0.0.1" and source == "env:LAN_IP"
