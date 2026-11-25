"""入口层测试。

覆盖：全新数据目录能启动（回归 v2「import 期访问数据库」启动崩溃）、CLI 正常、SIGTERM 优雅退出。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_cli_version():
    result = subprocess.run(
        [PYTHON, "app.py", "--version"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0
    assert "xiaoai-music-hub" in (result.stdout + result.stderr)


def test_cli_help_lists_entry_options():
    result = subprocess.run(
        [PYTHON, "app.py", "--help"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0
    for option in ("--port", "--music-dir", "--data-dir", "--speaker", "--lan-ip"):
        assert option in result.stdout


def test_module_entry_help():
    result = subprocess.run(
        [PYTHON, "-m", "xiaoai_hub", "--help"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0
    assert "--port" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="依赖 SIGTERM 优雅退出")
def test_fresh_install_starts_serves_and_shuts_down(tmp_path):
    """全新数据目录（无 music.db）必须能启动 —— v2 在这里直接崩溃。"""
    port = free_port()
    data_dir = tmp_path / "data"
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    env = dict(os.environ)
    env.update({
        "DATA_DIR": str(data_dir),
        "MUSIC_DIR": str(music_dir),
        "PORT": str(port),
        "XIAOAI_IP": "127.0.0.1",
        "LAN_IP": "127.0.0.1",
        "LOG_LEVEL": "INFO",
    })
    process = subprocess.Popen(
        [PYTHON, "app.py"], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    payload = None
    try:
        deadline = time.time() + 25
        while time.time() < deadline:
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1
                ) as response:
                    payload = json.loads(response.read().decode())
                break
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.2)
        output = ""
        if payload is None:
            if process.poll() is None:
                process.terminate()
            output = process.communicate(timeout=20)[0]
            pytest.fail(f"服务未能启动：\n{output}")
        assert payload["ok"] is True
        assert payload["db"]["ok"] is True
        assert (data_dir / "music.db").exists()
        assert (data_dir / "cache").exists()
        assert (data_dir / "secrets").exists()

        # 静态首页与状态接口可用
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=5) as response:
            state = json.loads(response.read().decode())
        assert state["ok"] is True and "player" in state
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            output = process.communicate(timeout=20)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            output = process.communicate()[0]
    assert process.returncode == 0, output
    assert "已退出" in output
