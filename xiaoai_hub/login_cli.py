"""命令行扫码登录网易云（无头服务器友好）。

流程：调用 :func:`xiaoai_hub.netease.qr_start` 生成二维码，打印二维码文件路径，
然后循环 :func:`xiaoai_hub.netease.qr_poll` 打印登录状态；成功时 cookie 由 netease
模块原子写入 ``SECRETS_DIR/netease.cookie``（权限 0600）。

用法::

    python3 -m xiaoai_hub.login_cli
    python3 netease_login.py                       # 兼容壳，等价
    python3 -m xiaoai_hub.login_cli --qr /tmp/qr.png --timeout 300

把生成的二维码文件用 ``scp`` 拉到本地查看，或用支持图片预览的终端打开后手机扫码。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from typing import Any, Dict, List, Optional

from . import config, logging_setup, netease

#: 网易云扫码状态码 → 人类可读状态
_STATUS_TEXT = {
    800: "二维码已过期",
    801: "等待扫码",
    802: "已扫码，请在手机上确认",
    803: "扫码确认成功",
}

_QR_PATH_KEYS = ("png_path", "png", "qr", "path", "file")
_POLL_INTERVAL = 2.0
_DEFAULT_TIMEOUT = 300


def _out(message: str = "") -> None:
    print(message, flush=True)


def _err(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _qr_file(result: Dict[str, Any]) -> Optional[str]:
    for key in _QR_PATH_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _verify_account() -> None:
    """登录成功后校验账号信息（失败只提示，不影响退出码）。"""
    try:
        account = netease.account(force=True)
    except Exception as exc:  # 网络抖动不应把“已保存 cookie”判为失败
        _err(f"[提示] cookie 已保存，但校验账号失败：{exc}")
        return
    if getattr(account, "logged_in", False):
        nickname = getattr(account, "nickname", "") or "（未知昵称）"
        uid = getattr(account, "uid", "") or "-"
        vip = "VIP" if getattr(account, "vip", False) else "普通账号"
        _out(f"当前账号：{nickname}（uid={uid}，{vip}）")
    else:
        reason = getattr(account, "reason", "") or ""
        _err(f"[提示] cookie 已保存，但账号校验未通过：{reason}")


def _ask_help(parser: argparse.ArgumentParser) -> int:
    parser.print_help(sys.stderr)
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="xiaoai-music-hub-login",
        description="网易云扫码登录：生成二维码并把 cookie 保存到本地（无头服务器可用）。",
    )
    parser.add_argument(
        "--qr", metavar="PATH", default=None,
        help="把生成的二维码另存到指定路径（默认使用 netease 模块给出的临时文件路径）",
    )
    parser.add_argument(
        "--timeout", type=int, default=_DEFAULT_TIMEOUT,
        help=f"等待扫码的最长秒数，默认 {_DEFAULT_TIMEOUT}",
    )
    parser.add_argument(
        "--interval", type=float, default=_POLL_INTERVAL,
        help=f"轮询登录状态的间隔秒数，默认 {_POLL_INTERVAL:g}",
    )
    args = parser.parse_args(argv)

    settings = config.get()
    logging_setup.setup(settings.log_level, settings.log_file)

    _out("== xiaoai-music-hub / 网易云扫码登录 ==")
    _out(f"cookie 文件：{settings.paths.cookie_file}")
    _out("")

    try:
        result = netease.qr_start()
    except Exception as exc:
        _err(f"获取二维码失败：{exc}")
        return 1
    if not isinstance(result, dict):
        _err(f"获取二维码失败：返回类型异常 {type(result).__name__}")
        return 1
    if result.get("error"):
        _err(f"获取二维码失败：{result.get('error')}")
        return 1

    key = result.get("key")
    qr_file = _qr_file(result)
    if not key:
        _err("获取二维码失败：服务未返回 unikey")
        return 1
    if not qr_file:
        _err("获取二维码失败：服务未返回二维码文件路径")
        return 1

    _out(f"二维码文件：{qr_file}")
    if not os.path.isfile(qr_file):
        _err(f"[警告] 二维码文件不存在：{qr_file}")
    else:
        _out("请用网易云音乐 App 扫码，然后在手机上确认登录（约 5 分钟内有效）。")

    if args.qr:
        try:
            shutil.copyfile(qr_file, args.qr)
            _out(f"二维码已另存为：{args.qr}")
        except OSError as exc:
            _err(f"[警告] 另存二维码失败：{exc}")

    _out("")
    deadline = time.time() + max(10, int(args.timeout))
    interval = max(0.5, float(args.interval))
    last_status: Optional[int] = None
    while time.time() < deadline:
        try:
            code, saved = netease.qr_poll(key)
        except Exception as exc:  # 网络抖动不中断轮询
            _err(f"[警告] 轮询失败（稍后重试）：{exc!r}")
            time.sleep(interval)
            continue
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = -1
        if status != last_status:
            text = _STATUS_TEXT.get(status, f"未知状态 {status}")
            _out(f"[{time.strftime('%H:%M:%S')}] {text}")
            last_status = status
        if status == 803:
            if not saved:
                _err("登录流程完成，但没有拿到 MUSIC_U，请重新运行本命令。")
                return 1
            _verify_account()
            _out("")
            _out(f"登录成功，cookie 已保存到：{settings.paths.cookie_file}")
            return 0
        if status == 800:
            _err("二维码已过期，请重新运行本命令。")
            return 1
        time.sleep(interval)

    _err(f"等待扫码超时（{args.timeout}s），请重新运行本命令。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
