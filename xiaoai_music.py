#!/usr/bin/env python3
"""兼容壳：旧入口 ``xiaoai_music.py``（v2 单体脚本）已弃用。

v3 的正式入口（任选其一）::

    python3 app.py
    python3 -m xiaoai_hub
    xiaoai-music-hub          # pip 安装后的 console script

本文件只打印弃用提示（stderr）后转发到 :func:`xiaoai_hub.app.main`，
保证旧脚本 / 旧 systemd 单元仍可启动。
"""
from __future__ import annotations

import sys

_DEPRECATION = (
    "[xiaoai-music-hub] 警告：xiaoai_music.py 已弃用，v3 起请改用 "
    "`python3 app.py`、`python3 -m xiaoai_hub` 或 `xiaoai-music-hub`。"
    "详见 README 的「快速开始」。"
)


def main() -> int:
    print(_DEPRECATION, file=sys.stderr, flush=True)
    from xiaoai_hub.app import main as hub_main

    result = hub_main()
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
