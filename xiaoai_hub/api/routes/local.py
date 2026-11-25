"""本地曲库接口：扫描、列表、播放、加入歌单、媒体流。"""
from __future__ import annotations

from typing import Any

from ... import errors, logging_setup, security
from ..router import Request, Response

log = logging_setup.get_logger(__name__)


def register(router: Any, ctx: Any) -> None:
    player = ctx.player

    def scan(req: Request) -> Response:
        try:
            result = ctx.library.scan()
        except errors.HubError:
            raise
        return Response.json({"ok": True, **(result or {})})

    def tracks(req: Request) -> Response:
        result = ctx.library.list_tracks(
            req.param("q"),
            limit=max(1, min(req.int_param("limit", 200), 1000)),
            offset=max(0, req.int_param("offset", 0)),
        )
        return Response.json({"ok": True, "tracks": result})

    def play(req: Request) -> Response:
        track_id = req.int_param("id", 0)
        if track_id <= 0:
            raise errors.BadRequest("缺少参数 id")
        row = ctx.db.query_one("SELECT * FROM local_tracks WHERE id=?", (track_id,))
        if row is None:
            raise errors.NotFound("本地曲目不存在")
        result = player.play_track({"local_id": track_id})
        if not result.get("ok"):
            return Response.json(result, 409)
        return Response.json(result)

    def add(req: Request) -> Response:
        playlist_id = req.int_param("id", 0)
        track_id = req.int_param("track", 0)
        if playlist_id <= 0 or track_id <= 0:
            raise errors.BadRequest("缺少参数 id / track")
        from .common import check_playlist_allowed, next_position

        check_playlist_allowed(ctx, playlist_id)
        local = ctx.db.query_one("SELECT * FROM local_tracks WHERE id=?", (track_id,))
        if local is None:
            raise errors.NotFound("本地曲目不存在")
        duplicate = ctx.db.query_one(
            "SELECT id FROM tracks WHERE playlist_id=? AND local_id=?",
            (playlist_id, track_id),
        )
        if duplicate:
            return Response.json({"ok": True, "added": False, "message": "已在歌单中"})
        ctx.db.execute(
            """INSERT INTO tracks(playlist_id,song_id,name,artist,album,pos,local_id)
               VALUES(?,?,?,?,?,?,?)""",
            (playlist_id, 0, local["title"], local["artist"], local["album"],
             next_position(ctx, playlist_id), track_id),
        )
        return Response.json({"ok": True, "added": True})

    def media(req: Request) -> Response:
        raw_id = req.params.get("id", "")
        try:
            track_id = int(raw_id)
        except (TypeError, ValueError):
            raise errors.BadRequest("非法的曲目 id")
        password = ctx.prefs.ui_password()
        if not security.verify_media_token(track_id, req.param("t"), password):
            raise errors.Forbidden("媒体令牌无效")
        row = ctx.db.query_one("SELECT * FROM local_tracks WHERE id=?", (track_id,))
        if row is None:
            raise errors.NotFound("本地曲目不存在")
        track = dict(row)
        try:
            path = ctx.library.media_path(track)
        except errors.HubError:
            raise
        except Exception as exc:  # 转码失败等
            log.warning("媒体路径解析失败 track=%s: %r", track_id, exc)
            raise errors.UpstreamError(f"媒体文件不可用: {exc}")
        mime = ctx.library.guess_mime(path)
        return Response.streamed(
            lambda handler: ctx.library.stream_file(handler, path, mime)
        )

    router.post("/local/scan", scan)
    router.get("/local/scan", scan, mutation=True)
    router.get("/local/tracks", tracks)
    router.post("/local/play", play)
    router.get("/local/play", play, mutation=True)
    router.post("/local/add", add)
    router.get("/local/add", add, mutation=True)
    router.get("/media/{id}", media, auth=False)
