"""pytest 共享夹具。

隔离策略：每个测试用独立临时目录 + 独立 SQLite，并重置 ``config``/``db`` 单例，
保证测试之间零共享状态。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.fakes import FakeClock, FakeLibrary, FakeNetease, FakeRenderer  # noqa: E402


@pytest.fixture()
def tmp_hub(tmp_path):
    """临时数据目录 + 已初始化数据库的配置环境。"""
    from xiaoai_hub import config, db

    data_dir = tmp_path / "data"
    music_dir = tmp_path / "music"
    data_dir.mkdir(parents=True, exist_ok=True)
    music_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "DATA_DIR": str(data_dir),
        "MUSIC_DIR": str(music_dir),
        "DB_PATH": str(data_dir / "music.db"),
        "CACHE_DIR": str(data_dir / "cache"),
        "SECRETS_DIR": str(data_dir / "secrets"),
        "COOKIE_FILE": str(data_dir / "secrets" / "netease.cookie"),
        "LAN_IP": "192.168.0.10",
        "NETEASE_BASE": "http://127.0.0.1:9",
        "LOG_LEVEL": "WARNING",
        "UI_PASSWORD": "",
        "XIAOAI_IP": "192.168.3.81",
    }
    config.reload(env=env, base_dir=str(tmp_path))
    db.configure(env["DB_PATH"])
    db.init_schema()
    try:
        yield SimpleNamespace(
            tmp_path=tmp_path,
            data_dir=data_dir,
            music_dir=music_dir,
            env=env,
            settings=config.get(),
            db_path=env["DB_PATH"],
        )
    finally:
        db.configure(None)
        config.reset()


@pytest.fixture()
def fake_clock():
    return FakeClock()


@pytest.fixture()
def fake_renderer(fake_clock):
    return FakeRenderer(clock=fake_clock)


@pytest.fixture()
def fake_netease():
    return FakeNetease()


@pytest.fixture()
def fake_library():
    return FakeLibrary()


@pytest.fixture()
def player(tmp_hub, fake_clock, fake_renderer, fake_netease, fake_library):
    """注入替身与假时钟的 Player（不启动 watchdog 线程）。"""
    from xiaoai_hub.player import Player

    instance = Player(
        renderer_factory=lambda ip, **kwargs: fake_renderer,
        netease_api=fake_netease,
        library_api=fake_library,
        clock=fake_clock,
        sleep=lambda seconds: fake_clock.advance(seconds),
    )
    return SimpleNamespace(
        player=instance,
        renderer=fake_renderer,
        netease=fake_netease,
        library=fake_library,
        clock=fake_clock,
        hub=tmp_hub,
    )
