"""网易云账号、扫码登录、歌单导入/同步接口。"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from ... import errors, logging_setup
from ..router import Request, Response
from .common import account_dict, playlist_dict, song_dict

log = logging_setup.get_logger(__name__)

QR_TIMEOUT = 300  # 扫码轮询最长秒数


def register(router: Any, ctx: Any) -> None:
    # ------------------------------------------------------------------
    # 账号
    # ------------------------------------------------------------------
    def account(req: Request) -> Response:
        try:
            result = ctx.netease.account()
        except errors.HubError as exc:
            return Response.json(
                {"ok": False, "logged_in": False, "reason": exc.message}, 200
            )
        return Response.json(account_dict(result))

    def start_qr(req: Request) -> Response:
        try:
            result = ctx.netease.qr_start()
        except errors.HubError as exc:
            ctx.login.update({"status": "error", "message": exc.message})
            return Response.json({"ok": False, "error": exc.message, "code": exc.code}, 502)
        if not isinstance(result, dict):
            result = {}
        if result.get("error"):
            ctx.login.update({"status": "error", "message": str(result["error"])})
            return Response.json({"ok": False, "error": result["error"], "code": "qr_failed"}, 502)
        key = result.get("key")
        png = result.get("png_path") or result.get("png")
        ctx.login.update({
            "status": "waiting",
            "key": key,
            "png": png,
            "started": int(time.time()),
            "message": "等待扫码",
        })
        threading.Thread(
            target=_qr_poll_worker, args=(ctx, key), name="qr-poll", daemon=True
        ).start()
        return Response.json({"ok": True, **dict(ctx.login)})

    def qr_status(req: Request) -> Response:
        return Response.json(dict(ctx.login))

    def qr_png(req: Request) -> Response:
        path = ctx.login.get("png")
        if not path or not os.path.isfile(path):
            raise errors.NotFound("二维码尚未生成")
        with open(path, "rb") as handle:
            body = handle.read()
        return Response.raw(body, "image/png", headers={"Cache-Control": "no-store"})

    def logout(req: Request) -> Response:
        try:
            ctx.netease.logout()
        except errors.HubError as exc:
            log.warning("退出登录失败: %s", exc)
        ctx.login.update({"status": "idle", "key": None, "png": None,
                          "started": 0, "message": "已退出"})
        return Response.json({"ok": True, "logged_in": False})

    router.get("/account", account)
    router.post("/account/qr", start_qr)
    router.get("/account/qr", start_qr, mutation=True)
    router.get("/account/qr/status", qr_status)
    router.get("/qr.png", qr_png)
    router.post("/account/logout", logout)
    router.get("/account/logout", logout, mutation=True)

    # ------------------------------------------------------------------
    # 网易云歌单
    # ------------------------------------------------------------------
    def netease_playlists(req: Request) -> Response:
        playlists = ctx.netease.playlists()
        uid = ctx.netease.current_uid()
        return Response.json({
            "ok": True,
            "uid": uid,
            "playlists": [playlist_dict(item) for item in playlists],
        })

    def import_playlist(req: Request) -> Response:
        remote_id = req.int_param("id", 0)
        if remote_id <= 0:
            raise errors.BadRequest("缺少参数 id")
        result = _import_playlist(
            ctx, remote_id, req.param("name") or None, req.param("cover") or ""
        )
        return Response.json(result)

    def sync_playlist(req: Request) -> Response:
        playlist_id = req.int_param("id", 0)
        row = ctx.db.query_one(
            "SELECT remote_id FROM playlists WHERE id=?", (playlist_id,)
        )
        if row is None:
            raise errors.NotFound("歌单不存在")
        if not row["remote_id"]:
            raise errors.BadRequest("该歌单不是从网易云导入的")
        return Response.json(_import_playlist(ctx, int(row["remote_id"])))

    def sync_all(req: Request) -> Response:
        if not ctx.sync.get("running"):
            threading.Thread(
                target=_sync_all_worker, args=(ctx,), name="sync-all", daemon=True
            ).start()
        return Response.json({"ok": True, "started": True})

    def sync_all_status(req: Request) -> Response:
        return Response.json(dict(ctx.sync))

    router.get("/netease/playlists", netease_playlists)
    router.post("/netease/import", import_playlist)
    router.get("/netease/import", import_playlist, mutation=True)
    router.post("/netease/sync", sync_playlist)
    router.get("/netease/sync", sync_playlist, mutation=True)
    router.post("/netease/sync_all", sync_all)
    router.get("/netease/sync_all", sync_all, mutation=True)
    router.get("/netease/sync_all/status", sync_all_status)


# ---------------------------------------------------------------------------
# 内部：导入与同步
# ---------------------------------------------------------------------------
def _import_playlist(
    ctx: Any, remote_id: int, name: Any = None, cover: str = ""
) -> dict:
    """把网易云歌单增量导入本地库（同名远端歌单复用本地歌单）。"""
    songs = ctx.netease.playlist_tracks(int(remote_id))
    if name is None:
        try:
            for item in ctx.netease.playlists():
                data = playlist_dict(item)
                if int(data["id"] or 0) == int(remote_id):
                    name, cover = data["name"], data["cover"]
                    break
        except errors.HubError as exc:
            log.warning("读取远端歌单信息失败: %s", exc)
    owner = ctx.netease.current_uid()
    added = 0
    with ctx.db.transaction() as conn:
        row = conn.execute(
            "SELECT id, owner_uid FROM playlists WHERE source='netease' AND remote_id=?",
            (int(remote_id),),
        ).fetchone()
        if row is not None:
            playlist_id = int(row["id"])
            if owner and row["owner_uid"] != owner:
                conn.execute(
                    "UPDATE playlists SET owner_uid=? WHERE id=?", (owner, playlist_id)
                )
        else:
            next_pos = conn.execute(
                "SELECT COALESCE(MAX(position),0) FROM playlists"
            ).fetchone()[0]
            cursor = conn.execute(
                """INSERT INTO playlists(name,created_at,source,remote_id,cover,
                                         owner_uid,position)
                   VALUES(?,?,?,?,?,?,?)""",
                (name or "网易云歌单", int(time.time()), "netease", int(remote_id),
                 cover, owner, int(next_pos or 0) + 1),
            )
            playlist_id = int(cursor.lastrowid)
        have = {
            row["song_id"]
            for row in conn.execute(
                "SELECT song_id FROM tracks WHERE playlist_id=?", (playlist_id,)
            ).fetchall()
            if row["song_id"]
        }
        position = int(conn.execute(
            "SELECT COALESCE(MAX(pos),0) FROM tracks WHERE playlist_id=?", (playlist_id,)
        ).fetchone()[0] or 0)
        for song in songs:
            data = song_dict(song)
            song_id = int(data["song_id"] or 0)
            if song_id <= 0 or song_id in have:
                continue
            position += 1
            conn.execute(
                """INSERT INTO tracks(playlist_id,song_id,name,artist,album,pos,duration)
                   VALUES(?,?,?,?,?,?,?)""",
                (playlist_id, song_id, data["name"], data["artist"], data["album"],
                 position, data["duration"]),
            )
            have.add(song_id)
            added += 1
    return {
        "ok": True,
        "playlist_id": playlist_id,
        "name": name,
        "cover": cover,
        "added": added,
        "total": len(songs),
    }


def _sync_all_worker(ctx: Any) -> None:
    ctx.sync.update({"running": True, "done": 0, "total": 0, "current": "",
                     "added": 0, "errors": [], "finished_at": None})
    log.info("开始全量同步网易云歌单")
    try:
        playlists = ctx.netease.playlists()
        ctx.sync["total"] = len(playlists)
        for index, item in enumerate(playlists):
            data = playlist_dict(item)
            ctx.sync["current"] = data["name"]
            try:
                result = _import_playlist(ctx, int(data["id"] or 0), data["name"], data["cover"])
                ctx.sync["added"] = int(ctx.sync.get("added") or 0) + int(result.get("added") or 0)
            except Exception as exc:  # 单个歌单失败不终止整体
                ctx.sync.setdefault("errors", []).append(f"{data['name']}: {exc!r}")
                log.warning("同步歌单失败 %s: %r", data["name"], exc)
            ctx.sync["done"] = index + 1
    except Exception as exc:
        ctx.sync.setdefault("errors", []).append(repr(exc))
        log.warning("全量同步失败: %r", exc)
    finally:
        ctx.sync.update({
            "running": False,
            "current": "",
            "finished_at": int(time.time()),
        })
        log.info("全量同步结束：新增 %s 首", ctx.sync.get("added"))


def _qr_poll_worker(ctx: Any, key: Any) -> None:
    deadline = time.time() + QR_TIMEOUT
    while time.time() < deadline:
        try:
            code, saved = ctx.netease.qr_poll(key)
        except errors.HubError as exc:
            ctx.login["message"] = f"轮询异常：{exc.message}"
            time.sleep(2)
            continue
        except Exception as exc:  # 网络抖动不中断轮询
            ctx.login["message"] = f"轮询异常：{exc!r}"
            time.sleep(2)
            continue
        if code == 802:
            ctx.login.update({"status": "scanned", "message": "已扫码，请在手机上确认"})
        elif code == 803:
            if saved:
                ctx.login.update({"status": "success", "message": "登录成功"})
                log.info("网易云扫码登录成功")
            else:
                ctx.login.update({"status": "error", "message": "未拿到 MUSIC_U"})
            return
        elif code == 800:
            ctx.login.update({"status": "expired", "message": "二维码已过期"})
            return
        time.sleep(2)
    ctx.login.update({"status": "expired", "message": "超时未扫码"})
