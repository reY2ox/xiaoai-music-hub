"""系统与设置接口：静态资源、健康检查、/state、设置、设备。"""
from __future__ import annotations

import os
import time
from typing import Any

from ... import errors, logging_setup
from ..router import Request, Response

log = logging_setup.get_logger(__name__)

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}


def _static(ctx: Any, req: Request, filename: str, *, cache: bool = True) -> Response:
    path = os.path.join(ctx.settings.paths.web_dir, filename)
    if not os.path.isfile(path):
        raise errors.NotFound(f"静态资源不存在: {filename}")
    stat = os.stat(path)
    etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache" if cache else "no-store"}
    if req.header("if-none-match") == etag:
        return Response(status=304, headers=headers)
    with open(path, "rb") as handle:
        body = handle.read()
    ext = os.path.splitext(filename)[1].lower()
    return Response.raw(body, _MIME.get(ext, "application/octet-stream"), headers=headers)


def register(router: Any, ctx: Any) -> None:
    # ---------- 静态资源（无需鉴权） ----------
    router.get("/", lambda req: _static(ctx, req, "index.html", cache=False), auth=False)
    router.get("/app.js", lambda req: _static(ctx, req, "app.js"), auth=False)
    router.get("/style.css", lambda req: _static(ctx, req, "style.css"), auth=False)
    router.get("/favicon.svg", lambda req: _static(ctx, req, "favicon.svg"), auth=False)
    router.get("/favicon.ico", lambda req: _static(ctx, req, "favicon.svg"), auth=False)

    # ---------- 健康与版本（无需鉴权） ----------
    def health(req: Request) -> Response:
        db_ok = True
        try:
            ctx.db.query_one("SELECT 1")
        except Exception:  # pragma: no cover - 数据库损坏时
            db_ok = False
        stats = _safe_stats(ctx)
        account = _safe_account_cached(ctx)
        return Response.json({
            "ok": True,
            "version": ctx.version,
            "uptime": round(ctx.uptime(), 1),
            "device": {"ip": ctx.player.active_ip(), "reachable": bool(ctx.player.ping())},
            "db": {"ok": db_ok},
            "downloads": {key: int(stats.get(key, 0) or 0)
                          for key in ("pending", "running", "failed")},
            "netease": {
                "logged_in": bool(getattr(account, "logged_in", False)) if account else False,
                "uid": getattr(account, "uid", None) if account else None,
            },
        })

    router.get("/health", health, auth=False)
    router.get("/version", lambda req: Response.json(
        {"ok": True, "version": ctx.version, "api": 1}), auth=False)

    # ---------- 全量状态 ----------
    def state(req: Request) -> Response:
        snapshot = ctx.player.snapshot()
        settings = ctx.prefs.public_settings()
        stats = _safe_stats(ctx)
        account = _safe_account_cached(ctx)
        snapshot.update({
            "version": ctx.version,
            "server": {
                "lan_ip": ctx.settings.lan_ip,
                "port": ctx.settings.port,
                "music_dir": ctx.settings.paths.music_dir,
            },
            "settings": settings,
            "download": {key: int(stats.get(key, 0) or 0)
                         for key in ("pending", "running", "done", "failed")},
            "account": _account_payload(account),
            "quiet": bool(settings.get("quiet_active")),
        })
        return Response.json(snapshot)

    router.get("/state", state)

    # ---------- 设置 ----------
    def get_settings(req: Request) -> Response:
        return Response.json({"ok": True, "settings": ctx.prefs.public_settings()})

    def update_settings(req: Request) -> Response:
        if req.has("quality"):
            ctx.prefs.set_quality(req.param("quality"))
        if req.has("play_mode"):
            ctx.player.set_mode(req.param("play_mode"))
        if req.has("quiet_hours"):
            ctx.prefs.set_quiet_hours(req.param("quiet_hours"))
        return Response.json({"ok": True, "settings": ctx.prefs.public_settings()})

    router.get("/settings", get_settings)
    router.post("/settings", update_settings)

    # ---------- 免打扰 ----------
    def quiet(req: Request) -> Response:
        if req.method == "POST" or req.is_mutation:
            if req.flag("off"):
                ctx.prefs.disable_quiet_hours()
            elif req.has("set"):
                ctx.prefs.set_quiet_hours(req.param("set"))
            elif req.has("quiet_hours"):
                ctx.prefs.set_quiet_hours(req.param("quiet_hours"))
        return Response.json({
            "ok": True,
            "quiet_hours": ctx.prefs.quiet_hours(),
            "active": ctx.prefs.in_quiet_hours(),
        })

    router.get("/quiet", quiet)
    router.post("/quiet", quiet)

    # ---------- 音质 ----------
    def quality(req: Request) -> Response:
        if req.is_mutation and req.has("set"):
            ctx.prefs.set_quality(req.param("set"))
        current = ctx.prefs.quality()
        return Response.json({
            "ok": True,
            "quality": current,
            "levels": {key: value.get("label", key)
                       for key, value in ctx.settings.quality_levels.items()},
        })

    router.get("/quality", quality)
    router.post("/quality", quality)

    # ---------- 播放模式 ----------
    def mode(req: Request) -> Response:
        if req.is_mutation and req.has("set"):
            ctx.player.set_mode(req.param("set"))
        return Response.json({"ok": True, "mode": ctx.player.mode})

    router.get("/mode", mode)
    router.post("/mode", mode)

    # ---------- 设备 ----------
    def devices(req: Request) -> Response:
        rows = [dict(row) for row in ctx.db.query(
            "SELECT * FROM devices ORDER BY last_seen DESC")]
        active = ctx.player.active_ip()
        for row in rows:
            row["active"] = row["ip"] == active
        return Response.json({"ok": True, "devices": rows, "active": active})

    def scan_devices(req: Request) -> Response:
        try:
            found = ctx.dlna.discover(timeout=5.0)
        except Exception as exc:  # 发现失败不应 500
            log.warning("SSDP 扫描失败: %r", exc)
            found = []
        now = int(time.time())
        devices_payload = []
        for device in found:
            data = device.to_dict() if hasattr(device, "to_dict") else dict(device)
            devices_payload.append(data)
            ctx.db.execute(
                """INSERT INTO devices(ip,name,model,udn,avt_control,rc_control,
                                       last_seen,created_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(ip) DO UPDATE SET name=excluded.name, model=excluded.model,
                     udn=excluded.udn, avt_control=excluded.avt_control,
                     rc_control=excluded.rc_control, last_seen=excluded.last_seen""",
                (data.get("ip"), data.get("name", ""), data.get("model", ""),
                 data.get("udn", ""), data.get("avt_control", ""),
                 data.get("rc_control", ""), now, now),
            )
        ctx.player.reset_renderers()
        return Response.json({"ok": True, "found": devices_payload})

    def select_device(req: Request) -> Response:
        ip = req.require("ip")
        ctx.player.set_device(ip)
        return Response.json({"ok": True, "active": ip})

    router.get("/devices", devices)
    router.post("/devices/scan", scan_devices)
    router.get("/devices/scan", scan_devices, mutation=True)
    router.post("/devices/select", select_device)
    router.get("/devices/select", select_device, mutation=True)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _safe_stats(ctx: Any) -> dict:
    try:
        return ctx.downloader.stats() or {}
    except Exception:  # pragma: no cover - 下载模块异常不应拖垮 /health
        return {}


def _safe_account_cached(ctx: Any) -> Any:
    getter = getattr(ctx.netease, "account_cached", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:  # pragma: no cover
        return None


def _account_payload(account: Any) -> dict:
    from .common import account_dict

    if account is None:
        return {"ok": False, "logged_in": False}
    return account_dict(account)
