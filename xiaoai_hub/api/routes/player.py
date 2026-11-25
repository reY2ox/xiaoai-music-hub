"""播放控制接口：/now、/queue、/play、暂停恢复、Seek、音量。"""
from __future__ import annotations

from typing import Any

from ... import errors
from ..router import Request, Response
from .common import song_dict


def _player_result(result: Any) -> Response:
    """把 Player 返回的 dict 映射为响应；失败时按语义给出状态码。"""
    if isinstance(result, Response):
        return result
    if result.get("ok"):
        return Response.json(result)
    error = str(result.get("error") or "操作失败")
    status = 409
    if "device" in error or "不可达" in error or "超时" in error:
        status = 503
    return Response.json({**result, "error": error, "code": result.get("code") or "failed"}, status)


def register(router: Any, ctx: Any) -> None:
    player = ctx.player

    router.get("/now", lambda req: Response.json(player.now()))
    router.get("/queue", lambda req: Response.json(player.queue()))

    def search(req: Request) -> Response:
        """只搜索不播放（v2 兼容 + 前端搜索面板 + MCP search_music 使用）。"""
        query = req.param("q").strip()
        if not query:
            raise errors.BadRequest("缺少参数 q")
        limit = max(1, min(req.int_param("limit", 10), 50))
        offset = max(0, req.int_param("offset", 0))
        songs = ctx.netease.search(query, limit=limit, offset=offset)
        return Response.json({"ok": True, "songs": [song_dict(item) for item in songs]})

    router.get("/search", search)

    def play(req: Request) -> Response:
        query = req.param("q").strip()
        if not query:
            raise errors.BadRequest("缺少参数 q")
        songs = ctx.netease.search(query, limit=10)
        if not songs:
            raise errors.NotFound("没有找到这首歌")
        index = req.int_param("i", 0)
        index = max(0, min(index, len(songs) - 1))
        track = song_dict(songs[index])
        result = player.play_track(track)
        if not result.get("ok"):
            status = 503 if result.get("code") == "device_unavailable" else 409
            return Response.json(
                {**result, "song": track, "error": result.get("error") or "播放失败",
                 "code": result.get("code") or "no_url"},
                status,
            )
        return Response.json(result)

    router.post("/play", play)
    router.get("/play", play, mutation=True)

    def play_url(req: Request) -> Response:
        url = req.require("url")
        result = player.play_url(
            url,
            req.param("title"),
            req.param("artist"),
            req.param("mime") or "audio/mpeg",
        )
        return _player_result(result)

    router.post("/play_url", play_url)

    for action in ("pause", "resume", "stop", "next", "prev"):

        def handler(req: Request, _action: str = action) -> Response:
            if _action == "next":
                return _player_result(player.advance(1))
            if _action == "prev":
                return _player_result(player.advance(-1))
            return _player_result(getattr(player, _action)())

        router.post(f"/{action}", handler)
        router.get(f"/{action}", handler, mutation=True)

    def seek(req: Request) -> Response:
        if not req.has("pos"):
            raise errors.BadRequest("缺少参数 pos")
        return _player_result(player.seek(req.float_param("pos", 0.0)))

    router.post("/seek", seek)

    def volume(req: Request) -> Response:
        if req.has("level"):
            return _player_result(player.set_volume(req.int_param("level", 30)))
        return Response.json({"ok": True, "volume": player.get_volume()})

    router.post("/volume", volume)
    router.get("/volume", volume, mutation=True)
