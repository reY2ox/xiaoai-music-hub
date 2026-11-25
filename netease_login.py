#!/usr/bin/env python3
"""兼容壳：命令行扫码登录网易云。

v3 的实现已迁移到 :mod:`xiaoai_hub.login_cli`；本文件保留给旧脚本调用::

    python3 netease_login.py

等价于 ``python3 -m xiaoai_hub.login_cli``。
"""
from __future__ import annotations

from xiaoai_hub.login_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
