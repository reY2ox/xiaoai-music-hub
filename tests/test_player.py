"""Player 状态机测试。

重点：**自然播完 vs 外部（小爱语音/其他控制器）暂停/停止** 的判定。
这是 v2 最严重的缺陷（用户语音暂停后会把暂停误判成播完并自动播下一首），
因此这里用可脚本化的 FakeRenderer + FakeClock 覆盖各种设备行为。
"""
from __future__ import annotations

import dataclasses

import pytest
from xiaoai_hub import config, db, errors

from tests.fakes import make_track


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def make_playlist(count: int = 3, name: str = "测试歌单") -> int:
    playlist_id = db.execute(
        "INSERT INTO playlists(name,created_at,source) VALUES(?,?,?)",
        (name, 0, "local"),
    )
    for index in range(count):
        db.execute(
            """INSERT INTO tracks(playlist_id,song_id,name,artist,album,pos)
               VALUES(?,?,?,?,?,?)""",
            (playlist_id, 1001 + index, f"歌曲{index + 1}", "测试歌手", "测试专辑", index + 1),
        )
    return playlist_id


def start(ctx, playlist_id: int, index: int = 0):
    result = ctx.player.play_index(playlist_id, index)
    assert result["ok"], result
    return result


def poll(ctx, step: float = 1.5):
    ctx.clock.advance(step)
    ctx.player.poll_once()


def poll_after_stop(ctx, step: float = 1.5):
    """STOPPED 需要连续两次轮询确认。"""
    poll(ctx, step)
    poll(ctx, step)


def observe(ctx, step: float = 1.5):
    """模拟 watchdog 的一次正常轮询（建立 previous 状态）。"""
    ctx.clock.advance(step)
    ctx.player.poll_once()


def play_url_count(ctx) -> int:
    return ctx.renderer.actions().count("play_url")


# ---------------------------------------------------------------------------
# 基础播放
# ---------------------------------------------------------------------------
def test_play_index_sets_state_and_plays(player):
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    assert ctx.player.playlist_id == playlist_id
    assert ctx.player.index == 0
    assert ctx.renderer.state == "PLAYING"
    assert play_url_count(ctx) == 1
    assert ctx.netease.calls.count("song_url:1001") == 1
    # 播放历史落库
    rows = db.query("SELECT * FROM play_history")
    assert len(rows) == 1 and rows[0]["name"] == "歌曲1"


def test_play_index_clamps_out_of_range(player):
    ctx = player
    playlist_id = make_playlist(2)
    start(ctx, playlist_id, 99)
    assert ctx.player.index == 1


def test_play_empty_playlist_returns_error(player):
    ctx = player
    playlist_id = make_playlist(0)
    result = ctx.player.play_index(playlist_id, 0)
    assert result == {"ok": False, "error": "empty playlist"}


def test_queue_reports_tracks_and_mode(player):
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 1)
    queue = ctx.player.queue()
    assert queue["playlist_id"] == playlist_id
    assert queue["index"] == 1
    assert len(queue["tracks"]) == 3
    assert queue["mode"] == "sequential"


# ---------------------------------------------------------------------------
# 自然播完 → 自动续播
# ---------------------------------------------------------------------------
def test_natural_end_while_playing_advances(player):
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    ctx.clock.advance(5)
    ctx.renderer.tick(178)  # duration 默认 180
    ctx.player.poll_once()

    assert ctx.player.index == 1
    assert play_url_count(ctx) == 2


def test_natural_end_via_stopped_state_advances(player):
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    observe(ctx, 5)
    ctx.renderer.tick(170)
    observe(ctx)  # 记录播放位置
    ctx.renderer.tick(9)
    ctx.renderer.finish()  # STOPPED + position=duration
    poll_after_stop(ctx)

    assert ctx.player.index == 1
    assert play_url_count(ctx) == 2


def test_natural_end_within_tolerance_advances(player):
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    observe(ctx, 5)
    ctx.renderer.tick(170)
    observe(ctx)
    ctx.renderer.position = 174.0  # 距末尾 6 秒，在 END_TOLERANCE=8 内
    ctx.renderer.state = "STOPPED"
    poll_after_stop(ctx)

    assert ctx.player.index == 1


def test_sequential_end_of_playlist_stops(player):
    ctx = player
    playlist_id = make_playlist(2)
    start(ctx, playlist_id, 1)

    ctx.clock.advance(5)
    ctx.renderer.tick(178)
    ctx.player.poll_once()

    assert ctx.player.index == 1  # 没得下一首
    assert ctx.player.snapshot()["player"]["active"] is False
    assert play_url_count(ctx) == 1


def test_loop_mode_wraps_to_first(player):
    ctx = player
    playlist_id = make_playlist(3)
    ctx.player.set_mode("loop")
    start(ctx, playlist_id, 2)

    ctx.clock.advance(5)
    ctx.renderer.tick(178)
    ctx.player.poll_once()

    assert ctx.player.index == 0
    assert play_url_count(ctx) == 2


def test_single_mode_repeats_same_track(player):
    ctx = player
    playlist_id = make_playlist(3)
    ctx.player.set_mode("single")
    start(ctx, playlist_id, 0)

    ctx.clock.advance(5)
    ctx.renderer.tick(179)
    ctx.player.poll_once()

    assert ctx.player.index == 0
    assert play_url_count(ctx) == 2


# ---------------------------------------------------------------------------
# 外部控制（用户 bug 的回归测试）
# ---------------------------------------------------------------------------
def test_voice_pause_does_not_advance(player):
    """小爱语音暂停（PAUSED_PLAYBACK）→ 不能自动播下一首。"""
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    observe(ctx, 5)
    ctx.renderer.tick(30)
    ctx.renderer.voice_pause()
    ctx.player.poll_once()

    assert ctx.player.index == 0
    assert play_url_count(ctx) == 1
    snapshot = ctx.player.snapshot()["player"]
    assert snapshot["interrupted"]["reason"] == "external_pause"
    assert snapshot["interrupted"]["position"] == pytest.approx(30.0)
    assert snapshot["active"] is False


def test_voice_stop_midtrack_does_not_advance(player):
    """小爱语音停止（STOPPED）且位置在曲中 → 不能自动播下一首。"""
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    observe(ctx, 5)
    ctx.renderer.tick(30)
    ctx.renderer.voice_stop(position=30)
    ctx.player.poll_once()  # 第一次：记录候选，不动作
    assert ctx.player.snapshot()["player"]["interrupted"] is None

    poll(ctx)
    snapshot = ctx.player.snapshot()["player"]
    assert snapshot["interrupted"]["reason"] == "external_stop"
    assert snapshot["interrupted"]["position"] == pytest.approx(30.0)
    assert ctx.player.index == 0
    assert play_url_count(ctx) == 1


def test_voice_stop_with_position_reset_uses_last_known_position(player):
    """小爱停止时音箱把 RelTime 归零，也要能记住真实位置。"""
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    observe(ctx, 5)
    ctx.renderer.tick(42)
    observe(ctx)  # 让 watchdog 记住真实位置
    ctx.renderer.voice_stop(position=0)
    poll_after_stop(ctx)

    interrupted = ctx.player.snapshot()["player"]["interrupted"]
    assert interrupted is not None
    assert interrupted["position"] == pytest.approx(42.0)
    assert ctx.player.index == 0


def test_voice_stop_near_end_is_natural_end(player):
    """位置确实在末尾附近时，停止可以判定为播完。"""
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    observe(ctx, 5)
    ctx.renderer.tick(176)
    observe(ctx)
    ctx.renderer.voice_stop(position=0)
    poll_after_stop(ctx)

    assert ctx.player.index == 1


def test_transient_stop_then_playing_is_ignored(player):
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    observe(ctx, 5)
    ctx.renderer.tick(30)
    ctx.renderer.voice_stop(position=30)
    ctx.player.poll_once()  # 候选
    ctx.renderer.voice_resume()
    poll(ctx)

    snapshot = ctx.player.snapshot()["player"]
    assert snapshot["interrupted"] is None
    assert ctx.player.index == 0
    assert play_url_count(ctx) == 1


def test_unknown_duration_stop_is_external_by_default(player):
    """设备不报时长时，STOPPED 默认按外部停止处理（保守策略）。"""
    ctx = player
    playlist_id = make_playlist(3)
    ctx.renderer.duration = 0.0
    start(ctx, playlist_id, 0)

    observe(ctx, 5)
    ctx.renderer.position = 30.0
    observe(ctx)  # 记录 last_playing_pos
    ctx.renderer.voice_stop(position=30)
    poll_after_stop(ctx)

    assert ctx.player.index == 0
    assert ctx.player.snapshot()["player"]["interrupted"]["reason"] == "external_stop"


def test_unknown_duration_advances_when_opted_in(player, monkeypatch):
    ctx = player
    playlist_id = make_playlist(3)
    settings = dataclasses.replace(config.get(), auto_advance_unknown_duration=True)
    monkeypatch.setattr(config, "_CURRENT", settings)
    ctx.renderer.duration = 0.0
    start(ctx, playlist_id, 0)

    observe(ctx, 5)
    ctx.renderer.position = 30.0
    observe(ctx)
    ctx.renderer.voice_stop(position=0)
    poll_after_stop(ctx)

    assert ctx.player.index == 1


def test_external_resume_by_voice_clears_interrupted(player):
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    observe(ctx, 5)
    ctx.renderer.tick(30)
    ctx.renderer.voice_pause()
    ctx.player.poll_once()
    assert ctx.player.snapshot()["player"]["interrupted"] is not None

    ctx.renderer.voice_resume()
    poll(ctx)

    snapshot = ctx.player.snapshot()["player"]
    assert snapshot["interrupted"] is None
    assert snapshot["active"] is True


def test_external_takeover_stops_queue(player):
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    observe(ctx, 5)
    ctx.renderer.tick(30)
    ctx.renderer.voice_stop(position=30)
    poll_after_stop(ctx)
    assert ctx.player.snapshot()["player"]["interrupted"] is not None

    ctx.renderer.takeover()  # 音箱被切到自有内容
    poll(ctx)
    snapshot = ctx.player.snapshot()["player"]
    assert ctx.player.index == 0
    assert snapshot["state"] == "NO_MEDIA_PRESENT"


def test_grace_period_ignores_stopped_right_after_command(player):
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    ctx.renderer.finish()  # 指令后立刻 STOPPED（设备切换抖动）
    ctx.player.poll_once()

    assert ctx.player.index == 0
    assert ctx.player.snapshot()["player"]["interrupted"] is None


# ---------------------------------------------------------------------------
# 本机暂停 / 恢复 / 停止
# ---------------------------------------------------------------------------
def test_pause_and_resume_with_device_pause_support(player):
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    result = ctx.player.pause()
    assert result["ok"] is True
    assert result["fallback"] is False
    assert ctx.renderer.state == "PAUSED_PLAYBACK"

    resumed = ctx.player.resume()
    assert resumed["ok"] is True
    assert resumed["resumed"] == "pause"
    assert ctx.renderer.state == "PLAYING"


def test_pause_fallback_replays_at_position(player):
    ctx = player
    ctx.renderer.supports_pause = False
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    observe(ctx, 5)
    ctx.renderer.tick(30)
    result = ctx.player.pause()
    assert result["ok"] is True
    assert result["fallback"] is True
    assert "stop" in ctx.renderer.actions()

    resumed = ctx.player.resume()
    assert resumed["ok"] is True
    assert resumed["resumed"] == "replay"
    assert resumed["position"] == pytest.approx(30.0)
    seek = ctx.renderer.last("seek")
    assert seek is not None and seek["seconds"] == pytest.approx(30.0)


def test_stop_disables_auto_advance(player):
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    ctx.player.stop()
    ctx.clock.advance(5)
    ctx.renderer.tick(180)
    ctx.player.poll_once()

    assert ctx.player.index == 0
    assert play_url_count(ctx) == 1


# ---------------------------------------------------------------------------
# 队列语义
# ---------------------------------------------------------------------------
def test_manual_next_and_prev(player):
    ctx = player
    playlist_id = make_playlist(3)
    start(ctx, playlist_id, 0)

    assert ctx.player.advance(1)["ok"] is True
    assert ctx.player.index == 1
    assert ctx.player.advance(-1)["ok"] is True
    assert ctx.player.index == 0


def test_shuffle_prev_does_not_pingpong(player):
    ctx = player
    playlist_id = make_playlist(4)
    ctx.player.set_mode("shuffle")
    start(ctx, playlist_id, 0)

    first = ctx.player.advance(1)
    assert first["ok"] is True
    assert ctx.player.index != 0

    back = ctx.player.advance(-1)
    assert back["ok"] is True
    assert ctx.player.index == 0

    again = ctx.player.advance(-1)
    assert again["ok"] is False  # 没有更早的历史


def test_single_track_play_has_no_queue(player):
    ctx = player
    result = ctx.player.play_track(make_track(song_id=1001))
    assert result["ok"] is True
    assert ctx.player.playlist_id is None
    assert ctx.player.queue()["tracks"] == []

    ctx.clock.advance(5)
    ctx.renderer.tick(180)
    ctx.player.poll_once()
    assert play_url_count(ctx) == 1


# ---------------------------------------------------------------------------
# 本地优先 / 音量 / Seek / 设备异常
# ---------------------------------------------------------------------------
def test_local_file_is_preferred_over_network(player):
    ctx = player
    ctx.library.song_map[1001] = {
        "id": 7, "path": "/tmp/local-song.mp3", "title": "本地晴天",
        "artist": "测试歌手", "song_key": 1001,
    }
    playlist_id = make_playlist(2)
    start(ctx, playlist_id, 0)

    assert "song_url:1001" not in ctx.netease.calls
    last = ctx.renderer.last("play_url")
    assert last is not None and "/media/7" in last["uri"]
    assert "local_for_song:1001" in ctx.library.calls


def test_volume_and_seek(player):
    ctx = player
    assert ctx.player.set_volume(55) == {"ok": True, "volume": 55}
    assert ctx.player.get_volume() == 55
    assert ctx.player.seek(42)["ok"] is True
    assert ctx.renderer.position == pytest.approx(42.0)


def test_unreachable_device_degrades(player):
    ctx = player
    ctx.renderer.fail_with = errors.DeviceUnavailable("设备离线")
    snapshot = ctx.player.snapshot()
    assert snapshot["device"]["reachable"] is False
    assert snapshot["player"]["state"] == "UNKNOWN"
    ctx.player.poll_once()  # 不应抛异常
    assert ctx.player.now()["state"] == "UNKNOWN"


def test_device_selection_is_persisted(player):
    ctx = player
    ctx.player.set_device("192.168.3.99")
    assert ctx.player.active_ip() == "192.168.3.99"
    from xiaoai_hub import prefs

    assert prefs.active_device() == "192.168.3.99"
    ctx.player.set_device("") if False else None
    with pytest.raises(errors.BadRequest):
        ctx.player.set_device("")


def test_clear_queue_after_playlist_delete(player):
    ctx = player
    playlist_id = make_playlist(2)
    start(ctx, playlist_id, 0)
    ctx.player.clear_queue(playlist_id)
    assert ctx.player.playlist_id is None
    assert ctx.player.index is None
