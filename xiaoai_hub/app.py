"""应用入口：解析参数 → 初始化基础设施 → 组装依赖 → 启动 HTTP → 优雅退出。"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import threading
from typing import List, Optional

from . import __version__, config, db, downloader, logging_setup
from .api import build_context, build_router, make_server, serve_forever

log = logging_setup.get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xiaoai-music-hub",
        description="把小爱 / 小米音箱当成 DLNA 播放器使用的音乐中枢",
    )
    parser.add_argument("--host", default=None, help="HTTP 监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=None, help="HTTP 端口（默认 8010）")
    parser.add_argument("--music-dir", default=None, help="本地曲库目录")
    parser.add_argument("--data-dir", default=None, help="数据目录（db/cache/secrets）")
    parser.add_argument("--speaker", default=None, help="默认音箱 IP（默认 192.168.3.81）")
    parser.add_argument("--lan-ip", default=None, help="媒体 URL 使用的本机 IP（默认自动探测）")
    parser.add_argument("--log-level", default=None, help="日志级别：DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--verbose", action="store_true", help="等价于 --log-level DEBUG")
    parser.add_argument(
        "--version", action="version", version=f"xiaoai-music-hub {__version__}"
    )
    return parser


def _apply_cli_env(args: argparse.Namespace) -> None:
    """把命令行参数写回环境变量，统一走 config 的加载逻辑。"""
    mapping = {
        "host": "XIAOAI_HOST",
        "port": "PORT",
        "music_dir": "MUSIC_DIR",
        "data_dir": "DATA_DIR",
        "speaker": "XIAOAI_IP",
        "lan_ip": "LAN_IP",
        "log_level": "LOG_LEVEL",
    }
    for attribute, env_key in mapping.items():
        value = getattr(args, attribute, None)
        if value is None or value == "":
            continue
        os.environ[env_key] = str(value)
    if args.verbose:
        os.environ["LOG_LEVEL"] = "DEBUG"


def _ensure_directories(settings: config.Settings) -> None:
    paths = settings.paths
    for directory in (paths.data_dir, paths.music_dir, paths.cache_dir, paths.secrets_dir):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            log.warning("无法创建目录 %s: %s", directory, exc)


def _startup_checks(settings: config.Settings) -> None:
    """启动自检：只警告，不阻断（缺依赖时给出明确修复建议）。"""
    if shutil.which("ffmpeg") is None:
        log.warning("未找到 ffmpeg：flac/m4a 等格式将无法转码推流，也无法写入下载标签。"
                    "Debian/Ubuntu: sudo apt install ffmpeg；macOS: brew install ffmpeg")
    try:
        import segno  # noqa: F401
    except ImportError:
        log.warning("未安装 segno：网页扫码登录不可用。pip install segno")
    if not os.path.isdir(settings.paths.web_dir):
        log.warning("前端目录不存在：%s", settings.paths.web_dir)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    _apply_cli_env(args)
    config.reset()
    settings = config.get()
    logging_setup.setup(settings.log_level, settings.log_file, force=True)
    log.info("xiaoai-music-hub v%s 启动中…", __version__)
    _ensure_directories(settings)
    _startup_checks(settings)

    db.configure(settings.paths.db_path)
    db.init_schema()
    downloader.init()
    downloader.recover()

    ctx = build_context(settings)
    ctx.player.start()
    router = build_router(ctx)
    server = make_server(ctx, router, settings.host, settings.port)
    host, port = server.server_address[0], server.server_address[1]

    log.info("监听地址        http://%s:%s/", host, port)
    log.info("音箱默认地址    %s", settings.speaker_ip)
    log.info("媒体 URL 地址   %s（来源：%s）", settings.lan_ip, settings.lan_ip_source)
    log.info("数据目录        %s", settings.paths.data_dir)
    log.info("曲库目录        %s", settings.paths.music_dir)
    log.info("鉴权            %s", "已开启（UI_PASSWORD）" if ctx.prefs.ui_password() else "未开启")
    if settings.host in ("0.0.0.0", "::") and ctx.prefs.ui_password():
        log.info("提示：已开启 Web 密码，局域网内通过 http://%s:%s/ 访问", settings.lan_ip, port)

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        log.info("收到信号 %s，准备退出…", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):  # 非主线程或平台不支持
            pass

    try:
        serve_forever(server, stop_event)
    except KeyboardInterrupt:
        log.info("收到 Ctrl-C，准备退出…")
    finally:
        try:
            ctx.player.shutdown()
        except Exception as exc:  # pragma: no cover - 退出路径尽力清理
            log.warning("停止播放线程失败: %r", exc)
        try:
            downloader.shutdown()
        except Exception as exc:  # pragma: no cover
            log.warning("停止下载线程失败: %r", exc)
        log.info("已退出")
    return 0
