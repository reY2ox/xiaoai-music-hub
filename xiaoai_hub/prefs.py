"""运行时偏好（存数据库 ``settings`` 表）。

与 :mod:`xiaoai_hub.config` 的分工：config 是进程启动时确定、基本不变的静态配置；
本模块是运行期可改的用户偏好（音质、播放模式、免打扰、当前设备、Web 密码）。

``UI_PASSWORD`` 环境变量优先级高于数据库值。
"""
from __future__ import annotations

import datetime as _dt
import threading
from typing import Any, Dict, Optional

from . import config, db

QUALITY_KEY = "quality"
MODE_KEY = "play_mode"
QUIET_KEY = "quiet_hours"
DEVICE_KEY = "active_device"
PASSWORD_KEY = "ui_password"

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 音质
# ---------------------------------------------------------------------------
def quality() -> str:
    levels = config.get().quality_levels
    value = db.get_setting(QUALITY_KEY) or config.get().default_quality
    return value if value in levels else config.get().default_quality


def set_quality(name: str) -> str:
    if name not in config.get().quality_levels:
        raise ValueError(f"未知音质档位: {name}")
    db.set_setting(QUALITY_KEY, name)
    return name


def quality_br(name: Optional[str] = None) -> int:
    return config.quality_br(name or quality())


def quality_ext(name: Optional[str] = None) -> str:
    return config.quality_ext(name or quality())


# ---------------------------------------------------------------------------
# 播放模式
# ---------------------------------------------------------------------------
def play_mode() -> str:
    value = db.get_setting(MODE_KEY) or "sequential"
    return value if value in config.PLAY_MODES else "sequential"


def set_play_mode(mode: str) -> str:
    if mode not in config.PLAY_MODES:
        raise ValueError(f"未知播放模式: {mode}")
    db.set_setting(MODE_KEY, mode)
    return mode


# ---------------------------------------------------------------------------
# 免打扰
# ---------------------------------------------------------------------------
def quiet_hours() -> str:
    """当前免打扰时段；空串表示关闭。"""
    stored = db.get_setting(QUIET_KEY)
    if stored is None:
        return config.get().quiet_default
    return stored


def set_quiet_hours(rng: str) -> str:
    rng = (rng or "").strip()
    if rng and config.parse_quiet_hours(rng) is None:
        raise ValueError("免打扰时段格式应为 HH:MM-HH:MM")
    db.set_setting(QUIET_KEY, rng)
    return rng


def disable_quiet_hours() -> str:
    db.set_setting(QUIET_KEY, "")
    return ""


def in_quiet_hours(now: Optional[_dt.datetime] = None) -> bool:
    """当前是否处于免打扰时段（支持跨零点）。"""
    parsed = config.parse_quiet_hours(quiet_hours())
    if not parsed:
        return False
    start, end = parsed
    now = now or _dt.datetime.now()
    current = now.hour * 60 + now.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


# ---------------------------------------------------------------------------
# 当前设备
# ---------------------------------------------------------------------------
def active_device() -> Optional[str]:
    value = (db.get_setting(DEVICE_KEY) or "").strip()
    return value or None


def set_active_device(ip: str) -> str:
    ip = (ip or "").strip()
    if not ip:
        raise ValueError("设备 IP 不能为空")
    db.set_setting(DEVICE_KEY, ip)
    return ip


# ---------------------------------------------------------------------------
# Web 密码
# ---------------------------------------------------------------------------
def ui_password() -> Optional[str]:
    """环境变量优先；其次数据库；都没有则不鉴权。"""
    env_value = config.get().ui_password
    if env_value:
        return env_value
    stored = (db.get_setting(PASSWORD_KEY) or "").strip()
    return stored or None


def set_ui_password(password: str) -> None:
    db.set_setting(PASSWORD_KEY, (password or "").strip())


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def public_settings() -> Dict[str, Any]:
    """返回给前端的设置快照。"""
    settings = config.get()
    return {
        "quality": quality(),
        "quality_levels": {
            key: value.get("label", key) for key, value in settings.quality_levels.items()
        },
        "play_mode": play_mode(),
        "play_modes": list(config.PLAY_MODES),
        "quiet_hours": quiet_hours(),
        "quiet_active": in_quiet_hours(),
        "device": active_device(),
        "auth_required": bool(ui_password()),
        "version": settings.version,
    }
