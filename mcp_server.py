#!/usr/bin/env python3
"""小爱音乐中枢 MCP server（stdio）

暴露点歌能力给 OpenClaw 的语音 agent：
  - play_music(query)  搜索并播放
  - stop_music()
  - music_status()
  - set_volume(level)

只与本机 8010 的音乐中枢通信，不执行任意命令。
"""
import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8010"
PROTOCOL = "2024-11-05"

TOOLS = [
    {
        "name": "play_music",
        "description": "点歌并让音箱播放。传入歌名、歌手，或“歌手 歌名”。用于用户说想听某首歌时。",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "歌名或歌手，例如：周杰伦 晴天"}},
            "required": ["query"],
        },
    },
    {
        "name": "stop_music",
        "description": "停止当前播放。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pause_music",
        "description": "暂停当前播放。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "music_status",
        "description": "查询当前播放状态：歌名、歌手、进度。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_quality",
        "description": "设置当前播放音质：standard(192k) / high(320k) / lossless(无损FLAC)。",
        "inputSchema": {
            "type": "object",
            "properties": {"quality": {"type": "string", "enum": ["standard", "high", "lossless"]}},
            "required": ["quality"],
        },
    },
    {
        "name": "set_volume",
        "description": "设置音箱音量，0-100。",
        "inputSchema": {
            "type": "object",
            "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["level"],
        },
    },
]


def call(path, timeout=20):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode())


def run_tool(name, args):
    if name == "play_music":
        q = (args.get("query") or "").strip()
        if not q:
            return "没有给歌名。"
        d = call("/play?q=" + urllib.parse.quote(q), timeout=40)
        if d.get("ok"):
            s = d.get("song", {})
            return f"正在播放：{s.get('name')} - {s.get('artist')}"
        if d.get("error") == "no url":
            return f"这首歌在网易云拿不到播放地址（{d.get('song', {}).get('name', q)}），换一首试试。"
        return f"播放失败：{d.get('error') or d}"
    if name == "stop_music":
        call("/stop", timeout=10)
        return "已停止播放。"
    if name == "pause_music":
        call("/pause", timeout=10)
        return "已暂停。"
    if name == "music_status":
        d = call("/now", timeout=10)
        if d.get("state") in (None, "", "STOPPED"):
            return "当前没有在播放。"
        return f"正在播放 {d.get('title')} - {d.get('artist')}，进度 {d.get('position')}。"
    if name == "set_quality":
        q = args.get("quality", "high")
        d = call(f"/quality?set={q}", timeout=10)
        return f"音质已设为 {d.get('quality')}。"
    if name == "set_volume":
        lv = int(args.get("level", 30))
        call(f"/volume?level={lv}", timeout=10)
        return f"音量已设为 {lv}。"
    return f"未知工具：{name}"


def reply(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return reply({
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "music-hub", "version": "1.0.0"},
            },
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return reply({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            text = run_tool(name, args)
            return reply({
                "jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            })
        except Exception as e:
            return reply({
                "jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": f"执行失败：{e}"}], "isError": True},
            })
    if method == "ping":
        return reply({"jsonrpc": "2.0", "id": mid, "result": {}})
    if mid is not None:
        return reply({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32601, "message": f"unknown method {method}"}})
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        handle(msg)


if __name__ == "__main__":
    main()
