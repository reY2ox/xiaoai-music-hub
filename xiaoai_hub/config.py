"""静态配置：环境变量 → 不可变 :class:`Settings`。

本模块不依赖项目内任何其它模块，可以安全地被任何层导入。

配置来源与优先级
----------------
1. 代码默认值（本文件）
2. 环境变量（见 :data:`ENV_DOC`）
3. 运行时偏好（音质 / 播放模式 / 免打扰 / 当前设备 / Web 密码）在 :mod:`xiaoai_hub.prefs`，
   存于数据库 ``settings`` 表；其中 ``UI_PASSWORD`` 环境变量优先于数据库值。

测试用法::

    from xiaoai_hub import config
    config.reload(env={"MUSIC_DIR": "/tmp/music", "PORT": "9000"})
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

VERSION = "3.0.0"
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PACKAGE_DIR)

#: 音质档位 → 网易云 ``br`` 参数
QUALITY_LEVELS: Dict[str, Dict[str, Any]] = {
    "standard": {"br": 192000, "label": "标准 192k", "ext": "mp3"},
    "high": {"br": 320000, "label": "高品质 320k", "ext": "mp3"},
    "lossless": {"br": 999000, "label": "无损 FLAC", "ext": "flac"},
}
DEFAULT_QUALITY = "high"

PLAY_MODES: Tuple[str, ...] = ("sequential", "loop", "single", "shuffle")

AUDIO_EXT: Tuple[str, ...] = (
    ".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac", ".ape", ".wma",
)
#: 需要转成 mp3 才能被多数小爱音箱直接解码的格式
NEED_TRANSCODE: Tuple[str, ...] = (".flac", ".m4a", ".wav", ".ape", ".wma", ".aac")

#: 环境变量说明（同时用于 ``--help`` 与文档生成）
ENV_DOC: Dict[str, str] = {
    "XIAOAI_HOST": "HTTP 监听地址，默认 0.0.0.0",
    "PORT": "HTTP 端口，默认 8010",
    "XIAOAI_IP": "默认音箱 IP，默认 192.168.3.81",
    "LAN_IP": "媒体 URL 使用的本机 IP，默认自动探测",
    "DATA_DIR": "数据根目录（db/cache/secrets/music），默认仓库根目录",
    "DB_PATH": "SQLite 路径，默认 <DATA_DIR>/music.db",
    "MUSIC_DIR": "本地曲库目录，默认 <DATA_DIR>/music",
    "CACHE_DIR": "转码缓存目录，默认 <DATA_DIR>/cache",
    "SECRETS_DIR": "凭据目录，默认 <DATA_DIR>/secrets",
    "COOKIE_FILE": "网易云 cookie 文件，默认 <SECRETS_DIR>/netease.cookie",
    "WEB_DIR": "前端静态目录，默认包内 web/",
    "UI_PASSWORD": "Web 访问密码；为空表示不鉴权",
    "REQUEST_TIMEOUT": "DLNA / 状态轮询请求超时秒数，默认 10",
    "NETEASE_BASE": "网易云 API 根地址，默认 https://music.163.com（测试可指向假服务）",
    "NETEASE_TIMEOUT": "网易云请求超时秒数，默认 15",
    "NETEASE_ACCOUNT_TTL": "账号信息缓存秒数，默认 60",
    "DOWNLOAD_CONCURRENCY": "下载并发数，默认 2",
    "DOWNLOAD_MAX_RETRY": "下载瞬时错误重试次数，默认 2",
    "DOWNLOAD_TIMEOUT": "下载请求超时秒数，默认 120",
    "AUTO_ADVANCE_INTERVAL": "播放监测轮询间隔秒，默认 1.5",
    "AUTO_ADVANCE_FRESH": "判定自动续播要求最近仍在播放的秒数，默认 120",
    "END_TOLERANCE": "判定「播到末尾」的容差秒数，默认 8",
    "AUTO_ADVANCE_UNKNOWN_DURATION": (
        "设备不报时长时是否兜底续播，默认 0（关闭，避免把语音暂停误判为播完）"
    ),
    "QUIET_HOURS": "默认免打扰时段，默认 23:30-08:00",
    "CACHE_MAX_MB": "转码缓存上限 MB，默认 2048",
    "LOG_LEVEL": "日志级别，默认 INFO",
    "LOG_FILE": "日志文件路径，默认仅输出到控制台",
}

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


# ---------------------------------------------------------------------------
# 环境变量解析
# ---------------------------------------------------------------------------
def _env(env: Mapping[str, str], key: str, default: str = "") -> str:
    value = env.get(key)
    if value is None:
        return default
    return str(value)


def _env_opt(env: Mapping[str, str], key: str) -> Optional[str]:
    value = (env.get(key) or "").strip()
    return value or None


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(str(env.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float(str(env.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Paths:
    """所有落盘路径（绝对路径）。"""

    base_dir: str
    data_dir: str
    db_path: str
    cache_dir: str
    secrets_dir: str
    cookie_file: str
    web_dir: str
    music_dir: str


@dataclass(frozen=True)
class Settings:
    """进程级静态配置。"""

    paths: Paths
    version: str
    host: str
    port: int
    lan_ip: str
    lan_ip_source: str
    speaker_ip: str
    request_timeout: float
    netease_base: str
    netease_timeout: float
    netease_account_ttl: float
    download_concurrency: int
    download_max_retry: int
    download_timeout: float
    auto_advance_interval: float
    auto_advance_fresh: float
    end_tolerance: float
    auto_advance_unknown_duration: bool
    quiet_default: str
    quality_levels: Dict[str, Dict[str, Any]]
    default_quality: str
    cache_max_mb: int
    log_level: str
    log_file: Optional[str]
    ui_password: Optional[str]


# ---------------------------------------------------------------------------
# 本机地址探测
# ---------------------------------------------------------------------------
def _udp_source_ip(target: str, port: int = 80) -> Optional[str]:
    """通过 UDP connect（不发包）拿到访问 target 时使用的源地址。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, port))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    return ip


def _usable(ip: str) -> bool:
    if not ip:
        return False
    if ip.startswith(("127.", "169.254.", "0.")):
        return False
    return ip.count(".") == 3


def _local_ipv4s() -> list:
    """尽力枚举本机 IPv4（跨平台，不依赖 ifconfig 解析）。"""
    found: list = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


def detect_lan_ip(
    speaker_ip: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[str, str]:
    """探测音箱/局域网可达的本机地址，返回 ``(ip, 来源说明)``。

    顺序：
    1. 显式环境变量 ``LAN_IP``；
    2. 到音箱 IP 的路由源地址（同网段时最准确）；
    3. 到公网地址的路由源地址；
    4. 本机枚举到的第一个可用 IPv4；
    5. 回退 ``127.0.0.1``。
    """
    env = os.environ if env is None else env
    explicit = (env.get("LAN_IP") or "").strip()
    if explicit:
        # 显式配置优先，即使是 127.0.0.1（本机联调 / 容器端口映射场景）
        return explicit, "env:LAN_IP"

    targets = []
    if speaker_ip:
        targets.append((speaker_ip, "speaker"))
    targets.append(("223.5.5.5", "public"))
    targets.append(("8.8.8.8", "public"))
    for target, source in targets:
        ip = _udp_source_ip(target)
        if ip and _usable(ip):
            return ip, source

    for ip in _local_ipv4s():
        if _usable(ip):
            return ip, "local"
    return "127.0.0.1", "fallback"


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
def _build_paths(env: Mapping[str, str], base_dir: str) -> Paths:
    data_dir = os.path.abspath(_env(env, "DATA_DIR", base_dir))
    return Paths(
        base_dir=base_dir,
        data_dir=data_dir,
        db_path=os.path.abspath(_env(env, "DB_PATH", os.path.join(data_dir, "music.db"))),
        cache_dir=os.path.abspath(_env(env, "CACHE_DIR", os.path.join(data_dir, "cache"))),
        secrets_dir=os.path.abspath(_env(env, "SECRETS_DIR", os.path.join(data_dir, "secrets"))),
        cookie_file=os.path.abspath(
            _env(env, "COOKIE_FILE", os.path.join(data_dir, "secrets", "netease.cookie"))
        ),
        web_dir=os.path.abspath(_env(env, "WEB_DIR", os.path.join(PACKAGE_DIR, "web"))),
        music_dir=os.path.abspath(_env(env, "MUSIC_DIR", os.path.join(data_dir, "music"))),
    )


def load(
    env: Optional[Mapping[str, str]] = None,
    base_dir: Optional[str] = None,
) -> Settings:
    """根据环境变量构造 :class:`Settings`（不修改全局单例）。"""
    env = os.environ if env is None else env
    base = os.path.abspath(base_dir or BASE_DIR)
    paths = _build_paths(env, base)
    speaker_ip = _env(env, "XIAOAI_IP", "192.168.3.81").strip() or "192.168.3.81"
    lan_ip, lan_source = detect_lan_ip(speaker_ip, env)
    return Settings(
        paths=paths,
        version=VERSION,
        host=_env(env, "XIAOAI_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_env_int(env, "PORT", 8010),
        lan_ip=lan_ip,
        lan_ip_source=lan_source,
        speaker_ip=speaker_ip,
        request_timeout=_env_float(env, "REQUEST_TIMEOUT", 10.0),
        netease_base=_env(env, "NETEASE_BASE", "https://music.163.com").rstrip("/"),
        netease_timeout=_env_float(env, "NETEASE_TIMEOUT", 15.0),
        netease_account_ttl=_env_float(env, "NETEASE_ACCOUNT_TTL", 60.0),
        download_concurrency=max(1, _env_int(env, "DOWNLOAD_CONCURRENCY", 2)),
        download_max_retry=max(0, _env_int(env, "DOWNLOAD_MAX_RETRY", 2)),
        download_timeout=_env_float(env, "DOWNLOAD_TIMEOUT", 120.0),
        auto_advance_interval=max(0.5, _env_float(env, "AUTO_ADVANCE_INTERVAL", 1.5)),
        auto_advance_fresh=max(10.0, _env_float(env, "AUTO_ADVANCE_FRESH", 120.0)),
        end_tolerance=max(1.0, _env_float(env, "END_TOLERANCE", 8.0)),
        auto_advance_unknown_duration=_env_bool(env, "AUTO_ADVANCE_UNKNOWN_DURATION", False),
        quiet_default=_env(env, "QUIET_HOURS", "23:30-08:00").strip(),
        quality_levels={k: dict(v) for k, v in QUALITY_LEVELS.items()},
        default_quality=DEFAULT_QUALITY,
        cache_max_mb=max(0, _env_int(env, "CACHE_MAX_MB", 2048)),
        log_level=_env(env, "LOG_LEVEL", "INFO").strip().upper() or "INFO",
        log_file=_env_opt(env, "LOG_FILE"),
        ui_password=_env_opt(env, "UI_PASSWORD"),
    )


_CURRENT: Optional[Settings] = None


def get() -> Settings:
    """进程内单例。"""
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = load()
    return _CURRENT


def reload(
    env: Optional[Mapping[str, str]] = None,
    base_dir: Optional[str] = None,
) -> Settings:
    """重新加载单例（测试与 CLI 使用）。"""
    global _CURRENT
    _CURRENT = load(env=env, base_dir=base_dir)
    return _CURRENT


def reset() -> None:
    """清空单例，下次 :func:`get` 重新读取环境变量。"""
    global _CURRENT
    _CURRENT = None


# ---------------------------------------------------------------------------
# 音质与免打扰工具
# ---------------------------------------------------------------------------
def quality_br(name: Optional[str] = None, settings: Optional[Settings] = None) -> int:
    """音质档位名 → 网易云 ``br`` 参数。"""
    settings = settings or get()
    key = name or settings.default_quality
    level = settings.quality_levels.get(key) or settings.quality_levels[settings.default_quality]
    return int(level["br"])


def quality_ext(name: Optional[str] = None, settings: Optional[Settings] = None) -> str:
    """音质档位名 → 文件扩展名。"""
    settings = settings or get()
    key = name or settings.default_quality
    level = settings.quality_levels.get(key) or settings.quality_levels[settings.default_quality]
    return str(level["ext"])


def quality_label(name: str, settings: Optional[Settings] = None) -> str:
    settings = settings or get()
    level = settings.quality_levels.get(name) or {}
    return str(level.get("label") or name)


def parse_quiet_hours(rng: Optional[str]) -> Optional[Tuple[int, int]]:
    """``"23:30-08:00"`` → ``(1410, 480)``；非法或空返回 ``None``。"""
    if not rng or "-" not in rng:
        return None
    try:
        start_text, end_text = rng.split("-", 1)
        start_h, start_m = (int(x) for x in start_text.strip().split(":"))
        end_h, end_m = (int(x) for x in end_text.strip().split(":"))
    except (TypeError, ValueError):
        return None
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23 and 0 <= start_m <= 59 and 0 <= end_m <= 59):
        return None
    start, end = start_h * 60 + start_m, end_h * 60 + end_m
    if start == end:
        return None
    return start, end
