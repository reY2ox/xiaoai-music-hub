"""应用上下文：依赖容器。

接口层只通过 ``AppContext`` 访问领域能力，测试时整体替换成假对象即可
（例如把 ``ctx.netease`` 换成 :class:`tests.fakes.FakeNetease`）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .. import config as config_mod
from .. import db as db_mod
from .. import dlna as dlna_mod
from .. import downloader as downloader_mod
from .. import library as library_mod
from .. import logging_setup
from .. import netease as netease_mod
from .. import player as player_mod
from .. import prefs as prefs_mod

log = logging_setup.get_logger(__name__)


def default_login_state() -> Dict[str, Any]:
    return {
        "status": "idle",  # idle / waiting / scanned / success / expired / error
        "key": None,
        "png": None,
        "started": 0,
        "message": "",
    }


def default_sync_state() -> Dict[str, Any]:
    return {
        "running": False,
        "done": 0,
        "total": 0,
        "current": "",
        "added": 0,
        "errors": [],
        "finished_at": None,
    }


@dataclass
class AppContext:
    """进程级依赖集合。"""

    settings: Any
    db: Any
    prefs: Any
    player: Any
    netease: Any
    library: Any
    downloader: Any
    dlna: Any
    version: str
    started_at: float = field(default_factory=time.time)
    login: Dict[str, Any] = field(default_factory=default_login_state)
    sync: Dict[str, Any] = field(default_factory=default_sync_state)
    extra: Dict[str, Any] = field(default_factory=dict)

    def uptime(self) -> float:
        return max(0.0, time.time() - self.started_at)


def build_context(settings: Optional[Any] = None) -> AppContext:
    """组装真实依赖。"""
    settings = settings or config_mod.get()
    return AppContext(
        settings=settings,
        db=db_mod,
        prefs=prefs_mod,
        player=player_mod.get_player(),
        netease=netease_mod,
        library=library_mod,
        downloader=downloader_mod,
        dlna=dlna_mod,
        version=config_mod.VERSION,
        started_at=time.time(),
        login=default_login_state(),
        sync=default_sync_state(),
    )
