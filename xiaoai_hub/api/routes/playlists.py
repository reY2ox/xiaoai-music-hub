"""歌单接口：列表、增删改、排序、播放。"""
from __future__ import annotations

import time
from typing import Any

from ... import errors
from ..router import Request, Response
from .common import (
    check_playlist_allowed,
    next_position,
    parse_order,
    playlist_row,
)


def register(router: Any, ctx: Any) -> None:
    player = ctx.player

    def list_playlists(req: Request) -> Response:
        try:
            uid = ctx.netease.current_uid()
        except errors.HubError:
            uid = None
        rows = [dict(row) for row in ctx.db.query(
            """SELECT p.id, p.name, p.source, p.remote_id, p.owner_uid, p.cover,
                      p.position, COUNT(t.id) AS count
               FROM playlists p LEFT JOIN tracks t ON t.playlist_id = p.id
               WHERE p.source = 'local' OR (p.source = 'netease' AND p.owner_uid IS ?)
               GROUP BY p.id ORDER BY p.position, p.id""",
            (uid,),
        )]
        return Response.json({"ok": True, "playlists": rows})

    def get_playlist(req: Request) -> Response:
        playlist_id = req.int_param("id", 0)
        if playlist_id <= 0:
            raise errors.BadRequest("缺少参数 id")
        check_playlist_allowed(ctx, playlist_id)
        return Response.json({"ok": True, "tracks": player.tracks(playlist_id)})

    def create_playlist(req: Request) -> Response:
        name = req.param("name").strip() or "新歌单"
        now = int(time.time())
        row = ctx.db.query_one("SELECT COALESCE(MAX(position),0) AS p FROM playlists")
        position = int(row["p"] if row else 0) + 1
        playlist_id = ctx.db.execute(
            "INSERT INTO playlists(name,created_at,source,position) VALUES(?,?,?,?)",
            (name, now, "local", position),
        )
        return Response.json({"ok": True, "id": playlist_id, "name": name})

    def rename_playlist(req: Request) -> Response:
        playlist_id = req.int_param("id", 0)
        name = req.require("name").strip()
        playlist_row(ctx, playlist_id)
        ctx.db.execute("UPDATE playlists SET name=? WHERE id=?", (name, playlist_id))
        return Response.json({"ok": True})

    def delete_playlist(req: Request) -> Response:
        playlist_id = req.int_param("id", 0)
        playlist_row(ctx, playlist_id)
        with ctx.db.transaction() as conn:
            conn.execute("DELETE FROM tracks WHERE playlist_id=?", (playlist_id,))
            conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        player.clear_queue(playlist_id)
        return Response.json({"ok": True})

    def add_song(req: Request) -> Response:
        playlist_id = req.int_param("id", 0)
        song_id = req.int_param("song_id", 0)
        if playlist_id <= 0 or song_id <= 0:
            raise errors.BadRequest("缺少参数 id / song_id")
        check_playlist_allowed(ctx, playlist_id)
        duplicate = ctx.db.query_one(
            """SELECT id FROM tracks
               WHERE playlist_id=? AND song_id=? AND COALESCE(local_id,0)=0""",
            (playlist_id, song_id),
        )
        if duplicate:
            return Response.json({"ok": True, "added": False, "message": "已在歌单中"})
        ctx.db.execute(
            """INSERT INTO tracks(playlist_id,song_id,name,artist,album,pos)
               VALUES(?,?,?,?,?,?)""",
            (playlist_id, song_id, req.param("name"), req.param("artist"),
             req.param("album"), next_position(ctx, playlist_id)),
        )
        return Response.json({"ok": True, "added": True})

    def add_local(req: Request) -> Response:
        playlist_id = req.int_param("id", 0)
        track_id = req.int_param("track", 0)
        if playlist_id <= 0 or track_id <= 0:
            raise errors.BadRequest("缺少参数 id / track")
        check_playlist_allowed(ctx, playlist_id)
        track = ctx.db.query_one("SELECT * FROM local_tracks WHERE id=?", (track_id,))
        if track is None:
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
            (playlist_id, 0, track["title"], track["artist"], track["album"],
             next_position(ctx, playlist_id), track_id),
        )
        return Response.json({"ok": True, "added": True})

    def remove_track(req: Request) -> Response:
        playlist_id = req.int_param("id", 0)
        track_id = req.int_param("track", 0)
        if playlist_id <= 0 or track_id <= 0:
            raise errors.BadRequest("缺少参数 id / track")
        ctx.db.execute(
            "DELETE FROM tracks WHERE id=? AND playlist_id=?", (track_id, playlist_id)
        )
        return Response.json({"ok": True})

    def reorder(req: Request) -> Response:
        playlist_id = req.int_param("id", 0)
        check_playlist_allowed(ctx, playlist_id)
        order = parse_order(req.data().get("order") if req.data() else req.param("order"))
        if not order:
            raise errors.BadRequest("缺少参数 order（曲目 id 数组）")
        existing = {row["id"] for row in ctx.db.query(
            "SELECT id FROM tracks WHERE playlist_id=?", (playlist_id,))}
        with ctx.db.transaction() as conn:
            for position, track_id in enumerate(order, start=1):
                if track_id in existing:
                    conn.execute(
                        "UPDATE tracks SET pos=? WHERE id=? AND playlist_id=?",
                        (position, track_id, playlist_id),
                    )
        return Response.json({"ok": True, "count": len(order)})

    def play_playlist(req: Request) -> Response:
        playlist_id = req.int_param("id", 0)
        if playlist_id <= 0:
            raise errors.BadRequest("缺少参数 id")
        check_playlist_allowed(ctx, playlist_id)
        index = req.int_param("i", 0)
        if req.flag("whole") and player.mode == "shuffle":
            total = len(player.tracks(playlist_id))
            if total > 1:
                import random

                index = random.randrange(total)
        result = player.play_index(playlist_id, index)
        if not result.get("ok"):
            return Response.json(result, 409)
        return Response.json(result)

    # ---------- 注册（GET 变更接口保留兼容，标记 mutation） ----------
    router.get("/playlists", list_playlists)
    router.get("/playlist", get_playlist)

    router.post("/playlist/create", create_playlist)
    router.get("/playlist/create", create_playlist, mutation=True)

    router.post("/playlist/rename", rename_playlist)
    router.get("/playlist/rename", rename_playlist, mutation=True)

    router.post("/playlist/delete", delete_playlist)
    router.get("/playlist/delete", delete_playlist, mutation=True)

    router.post("/playlist/add", add_song)
    router.get("/playlist/add", add_song, mutation=True)

    router.post("/playlist/add_local", add_local)
    router.get("/playlist/add_local", add_local, mutation=True)

    router.post("/playlist/remove", remove_track)
    router.get("/playlist/remove", remove_track, mutation=True)

    router.post("/playlist/reorder", reorder)
    router.get("/playlist/reorder", reorder, mutation=True)

    router.post("/playlist/play", play_playlist)
    router.get("/playlist/play", play_playlist, mutation=True)
