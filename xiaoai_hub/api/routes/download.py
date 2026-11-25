"""下载队列接口。"""
from __future__ import annotations

from typing import Any

from ... import errors
from ..router import Request, Response
from .common import check_playlist_allowed


def register(router: Any, ctx: Any) -> None:
    downloader = ctx.downloader

    def jobs(req: Request) -> Response:
        status = req.param("status") or None
        limit = max(1, min(req.int_param("limit", 100), 1000))
        return Response.json({
            "ok": True,
            "jobs": downloader.jobs(limit, status),
            "stats": downloader.stats(),
        })

    def stats(req: Request) -> Response:
        return Response.json({"ok": True, "stats": downloader.stats()})

    def download_playlist(req: Request) -> Response:
        playlist_id = req.int_param("id", 0)
        if playlist_id <= 0:
            raise errors.BadRequest("缺少参数 id")
        check_playlist_allowed(ctx, playlist_id)
        queued = downloader.add_playlist(playlist_id)
        downloader.kick()
        return Response.json({"ok": True, "queued": queued})

    def download_song(req: Request) -> Response:
        song_id = req.int_param("song_id", 0)
        if song_id <= 0:
            raise errors.BadRequest("缺少参数 song_id")
        job_id = downloader.add_job({
            "song_id": song_id,
            "name": req.param("name"),
            "artist": req.param("artist"),
            "album": req.param("album"),
        }, req.param("quality") or None)
        downloader.kick()
        return Response.json({"ok": True, "job": job_id})

    def retry(req: Request) -> Response:
        job_id = req.int_param("id", 0) or None
        retried = downloader.retry_failed(job_id)
        downloader.kick()
        return Response.json({"ok": True, "retried": retried})

    def cancel(req: Request) -> Response:
        job_id = req.int_param("id", 0)
        if job_id <= 0:
            raise errors.BadRequest("缺少参数 id")
        return Response.json({"ok": bool(downloader.cancel(job_id))})

    router.get("/download/jobs", jobs)
    router.get("/download/stats", stats)

    router.post("/download/playlist", download_playlist)
    router.get("/download/playlist", download_playlist, mutation=True)

    router.post("/download/song", download_song)
    router.get("/download/song", download_song, mutation=True)

    router.post("/download/retry", retry)
    router.get("/download/retry", retry, mutation=True)

    router.post("/download/cancel", cancel)
    router.get("/download/cancel", cancel, mutation=True)
