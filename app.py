#!/usr/bin/env python3
"""兼容入口：``python3 app.py``。

真正的实现位于 ``xiaoai_hub/`` 包；这里只保证旧的启动方式继续可用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xiaoai_hub.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
