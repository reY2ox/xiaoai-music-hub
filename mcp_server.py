#!/usr/bin/env python3
"""兼容壳：MCP stdio server。

v3 的实现已迁移到 :mod:`xiaoai_hub.mcp`；本文件仅为兼容旧的 MCP 客户端配置
（``args: [".../mcp_server.py"]``）而保留。新配置建议直接使用::

    python3 -m xiaoai_hub.mcp

环境变量：``MUSIC_HUB_URL``（默认 ``http://127.0.0.1:8010``）、
``MUSIC_HUB_PASSWORD``（可选，HTTP Basic Auth）。
"""
from __future__ import annotations

from xiaoai_hub.mcp import main

if __name__ == "__main__":
    raise SystemExit(main())
