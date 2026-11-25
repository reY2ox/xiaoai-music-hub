"""路由共享工具：把领域对象归一化成 API 契约里的 JSON 形状。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ... import errors


def to_dict(obj: Any) -> Dict[str, Any]:
    """领域对象 / 模块替身 → dict。"""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    converter = getattr(obj, "to_dict", None)
    if callable(converter):
        try:
            data = converter()
            if isinstance(data, dict):
                return dict(data)
        except Exception:  # pragma: no cover - 替身实现异常时兜底
            pass
    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict):
        return {key: value for key, value in data.items() if not key.startswith("_")}
    return {}


def account_dict(account: Any) -> Dict[str, Any]:
    data = to_dict(account)
    logged_in = bool(data.get("logged_in", data.get("ok", False)))
    return {
        "ok": logged_in,
        "logged_in": logged_in,
        "uid": data.get("uid"),
        "nickname": data.get("nickname") or "",
        "avatar": data.get("avatar") or "",
        "vip": bool(data.get("vip")),
        "reason": data.get("reason") or "",
    }


def song_dict(song: Any) -> Dict[str, Any]:
    data = to_dict(song)
    song_id = data.get("song_id", data.get("id"))
    return {
        "song_id": song_id,
        "name": data.get("name") or "",
        "artist": data.get("artist") or "",
        "album": data.get("album") or "",
        "fee": data.get("fee"),
        "duration": data.get("duration") or 0,
        "cover": data.get("cover") or "",
    }


def playlist_dict(playlist: Any) -> Dict[str, Any]:
    data = to_dict(playlist)
    remote_id = data.get("id", data.get("remote_id"))
    return {
        "id": remote_id,
        "name": data.get("name") or "",
        "count": data.get("count") or 0,
        "cover": data.get("cover") or "",
    }


def playlist_row(ctx: Any, playlist_id: int) -> Dict[str, Any]:
    row = ctx.db.query_one("SELECT * FROM playlists WHERE id=?", (int(playlist_id),))
    if row is None:
        raise errors.NotFound("歌单不存在")
    return dict(row)


def playlist_allowed(ctx: Any, playlist_id: int) -> bool:
    """网易云歌单只能被当前登录账号访问；本地歌单始终可用。"""
    row = ctx.db.query_one(
        "SELECT source, owner_uid FROM playlists WHERE id=?", (int(playlist_id),)
    )
    if row is None:
        return False
    if (row["source"] or "local") != "netease":
        return True
    try:
        uid = ctx.netease.current_uid()
    except errors.HubError:
        return False
    return row["owner_uid"] is not None and uid is not None and int(row["owner_uid"]) == int(uid)


def check_playlist_allowed(ctx: Any, playlist_id: int) -> Dict[str, Any]:
    row = playlist_row(ctx, playlist_id)
    if not playlist_allowed(ctx, playlist_id):
        raise errors.Forbidden("该歌单属于其他账号或账号已退出")
    return row


def next_position(ctx: Any, playlist_id: int) -> int:
    row = ctx.db.query_one(
        "SELECT COALESCE(MAX(pos),0) AS p FROM tracks WHERE playlist_id=?",
        (int(playlist_id),),
    )
    return int(row["p"] if row else 0) + 1


def parse_order(raw: Any) -> Optional[list]:
    """``order`` 参数：JSON 数组或逗号分隔字符串。"""
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        text = str(raw or "").strip()
        if not text:
            return None
        values = [part for part in text.replace(" ", "").split(",") if part]
    order = []
    for value in values:
        try:
            order.append(int(value))
        except (TypeError, ValueError):
            continue
    return order or None
