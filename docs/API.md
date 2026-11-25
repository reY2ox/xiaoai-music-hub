# HTTP API 契约（v3）

- 所有响应均为 `application/json; charset=utf-8`，除非显式说明。
- 成功：`{"ok": true, ...}`；失败：`{"ok": false, "error": "人类可读消息", "code": "机器可读码"}`。
- 状态码：`200` 成功、`400` 参数错误、`401` 未登录/未授权、`403` 禁止、`404` 不存在、
  `409` 状态冲突（如无队列）、`502` 上游（网易云）异常、`503` 音箱不可达、`500` 内部错误。
- 鉴权：设置 `UI_PASSWORD` 后，除 `/`、静态资源、`/health`、`/version`、`/media/*` 外均需
  HTTP Basic Auth；`/media/*` 使用媒体令牌 `?t=`。
- 变更类请求（POST，以及保留的 GET 变更接口）会做同源校验：带 `Origin`/`Referer` 时必须与
  `Host` 一致，否则 `403`。curl/脚本无这两个头时放行。
- POST 请求体支持 `application/json` 与 `application/x-www-form-urlencoded`；参数也可放在 query。

---

## 1. 系统

### `GET /health`
无需鉴权。用于探活与部署检查。

```json
{"ok": true, "version": "3.0.0", "uptime": 1234.5,
 "device": {"ip": "192.168.3.81", "reachable": true},
 "db": {"ok": true},
 "downloads": {"pending": 0, "running": 0, "failed": 0},
 "netease": {"logged_in": true, "uid": 12345}}
```

### `GET /version`
无需鉴权。`{"ok": true, "version": "3.0.0", "api": 1}`

### `GET /state`
前端主轮询接口（建议 1.5s）。一次拿到播放器 + 设置 + 下载 + 账号缓存，**不产生网易云网络请求**。

```json
{"ok": true, "version": "3.0.0",
 "server": {"lan_ip": "192.168.0.10", "port": 8010, "music_dir": "/data/music"},
 "device": {"ip": "192.168.3.81", "name": "小爱音箱", "reachable": true},
 "player": {
   "state": "PLAYING", "title": "晴天", "artist": "周杰伦",
   "position": 12.3, "duration": 269.0,
   "position_text": "00:12", "duration_text": "04:29",
   "uri": "http://...", "active": true, "manual_stop": false,
   "interrupted": null,
   "queue": {"playlist_id": 1, "playlist_name": "我的歌单",
             "index": 2, "total": 10, "mode": "sequential"}
 },
 "settings": {
   "quality": "high", "quality_levels": {"standard": "标准 192k", "high": "高品质 320k", "lossless": "无损 FLAC"},
   "play_mode": "sequential", "play_modes": ["sequential","loop","single","shuffle"],
   "quiet_hours": "23:30-08:00", "quiet_active": false,
   "device": "192.168.3.81", "auth_required": false, "version": "3.0.0"
 },
 "download": {"pending": 0, "running": 1, "done": 20, "failed": 0},
 "account": {"logged_in": true, "uid": 12345, "nickname": "小明", "vip": true}}
```

`player.interrupted` 非空表示**音箱被外部（小爱语音/其他控制器）暂停或停止**：

```json
{"reason": "external_pause", "title": "晴天", "artist": "周杰伦",
 "position": 30.0, "at": 1730000000}
```

### `GET /settings`
`{"ok": true, "settings": {...同 /state.settings...}}`

### `POST /settings`
变更运行时偏好，字段均可选：`quality`、`play_mode`、`quiet_hours`（`""` 表示关闭免打扰）。

```json
{"ok": true, "settings": {...}}
```

---

## 2. 播放控制

> 保留的 GET 变更接口（兼容 v2）：`/pause`、`/resume`、`/stop`、`/next`、`/prev`、`/volume`、`/mode`、`/quality`、`/quiet`、`/play`、`/playlist/play` 等。新前端应使用 POST。

### `GET /now`
当前播放信息（来自音箱回报，不推测）。

```json
{"ok": true, "device": "192.168.3.81", "state": "PLAYING",
 "title": "晴天", "artist": "周杰伦",
 "duration": "04:29", "position": "00:12",
 "duration_seconds": 269.0, "position_seconds": 12.3,
 "url": "http://...", "interrupted": null,
 "active": true, "manual_stop": false,
 "queue": {"playlist_id": 1, "playlist_name": "我的歌单", "index": 2, "total": 10, "mode": "sequential"}}
```

### `GET /queue`
```json
{"ok": true, "playlist_id": 1, "playlist_name": "我的歌单", "index": 2,
 "mode": "sequential", "tracks": [{"id": 1, "name": "晴天", "artist": "周杰伦", "local_id": 3, ...}]}
```

### `GET /search`
只搜索不播放（v2 兼容接口；前端搜索面板与 MCP `search_music` 使用）。

| 参数 | 说明 |
|---|---|
| `q` | 必填，关键词（歌名/歌手） |
| `limit` | 可选，默认 10，上限 50 |
| `offset` | 可选，默认 0 |

```json
{"ok": true, "songs": [{"song_id": 123, "name": "晴天", "artist": "周杰伦",
  "album": "叶惠美", "fee": 0, "duration": 270, "cover": "https://..."}]}
```

### `POST /play`
按关键词搜索并播放第一条（或第 `i` 条）。
| 参数 | 类型 | 说明 |
|---|---|---|
| `q` | string | 必填，歌名/歌手 |
| `i` | int | 可选，搜索结果下标，默认 0 |

```json
{"ok": true, "song": {"name": "晴天", "artist": "周杰伦"}, "device": "192.168.3.81", "source": "netease"}
```
失败：`404 {"error":"not found"}`；VIP/无直链：`409 {"error":"VIP 曲目需要有效 cookie","code":"no_url"}`。

### `POST /play_url`
把任意音频直链推给音箱（外部音源、临时播放）。

| 参数 | 说明 |
|---|---|
| `url` | 必填 |
| `title` / `artist` | 可选 |
| `mime` | 可选，默认 `audio/mpeg` |

### `POST /pause`、`POST /resume`、`POST /stop`、`POST /next`、`POST /prev`
返回 `{"ok": true, "state": "PLAYING", ...}`。
`resume` 会优先恢复被外部中断的曲目（重新推流 + Seek 回原位置），返回 `resumed: "pause"|"replay"`。

### `POST /seek`
| 参数 | 说明 |
|---|---|
| `pos` | 秒，浮点，必填 |

### `POST /volume`
| 参数 | 说明 |
|---|---|
| `level` | 0–100，必填 |

返回 `{"ok": true, "volume": 30}`。

---

## 3. 歌单

### `GET /playlists`
```json
{"ok": true, "playlists": [{"id": 1, "name": "我的歌单", "source": "local",
  "remote_id": null, "owner_uid": null, "count": 10, "position": 0}]}
```

### `POST /playlist/create` → `{"ok": true, "id": 1, "name": "新歌单"}`
### `POST /playlist/rename` → `{"ok": true}`
参数：`id`、`name`。
### `POST /playlist/delete` → `{"ok": true}` 参数：`id`。
### `GET /playlist?id=`
```json
{"ok": true, "tracks": [{"id": 1, "song_id": 123, "name": "晴天", "artist": "周杰伦",
  "album": "叶惠美", "pos": 1, "local_id": null, "duration": 269}]}
```
歌单属于其他网易云账号时 `403`。

### `POST /playlist/add`
参数：`id`、`song_id`、`name`、`artist`、`album`。重复返回 `{"ok": true, "added": false}`。

### `POST /playlist/add_local`
参数：`id`、`track`（本地曲目 id）。

### `POST /playlist/remove`
参数：`id`、`track`（tracks.id）。

### `POST /playlist/reorder`
参数：`id`、`order`（tracks.id 的数组，按新顺序）。

### `POST /playlist/play`
参数：`id`、`i`（可选，默认 0）、`whole`（可选，`1` 表示整张从随机位置开始）。

---

## 4. 网易云

### `GET /account`
```json
{"ok": true, "logged_in": true, "nickname": "小明", "uid": 12345, "avatar": "https://...", "vip": true, "reason": ""}
```
未登录 `{"ok": false, "logged_in": false, "reason": "..."}`。

### `POST /account/qr`
开始扫码登录，返回当前登录状态对象；随后前端轮询状态并展示 `/qr.png`。

### `GET /account/qr/status`
```json
{"status": "waiting|scanned|success|expired|error", "message": "等待扫码", "started": 1730000000}
```

### `GET /qr.png`
二维码 PNG（`image/png`）。

### `POST /account/logout` → `{"ok": true, "logged_in": false}`

### `GET /netease/playlists`
`{"ok": true, "uid": 12345, "playlists": [{"id": 123, "name": "我喜欢的音乐", "count": 100, "cover": "https://..."}]}`

### `POST /netease/import`
参数：`id`（远端歌单 id）、`name`（可选）。返回 `{"ok": true, "playlist_id": 3, "name": "...", "added": 10, "total": 100}`。

### `POST /netease/sync`
参数：`id`（本地歌单 id），增量同步。

### `POST /netease/sync_all`
启动后台全量同步。`GET /netease/sync_all/status` 查询进度：
```json
{"running": true, "done": 2, "total": 10, "current": "歌单名", "added": 12, "errors": [], "finished_at": null}
```

---

## 5. 本地曲库

### `POST /local/scan`
`{"ok": true, "added": 0, "updated": 0, "removed": 0, "total": 100, "elapsed": 0.12}`

### `GET /local/tracks?q=&limit=&offset=`
```json
{"ok": true, "tracks": [{"id": 1, "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
  "path": "/data/music/...", "size": 1234567, "mtime": 1730000000, "song_key": 123, "duration": 269}]}
```

### `POST /local/play`
参数：`id`（local_tracks.id）。

### `POST /local/add`
参数：`id`（歌单）、`track`（local_tracks.id）。

### `GET /media/<id>?t=<token>`
音频流，支持 `Range`（206/416）与 `HEAD`。设置密码时 `t` 必须匹配。

---

## 6. 下载

### `GET /download/jobs?limit=100&status=`
```json
{"ok": true, "jobs": [{"id": 1, "song_id": 123, "name": "晴天", "artist": "周杰伦",
  "status": "running", "progress": 42, "error": null, "path": null,
  "bytes_done": 100, "bytes_total": 200, "retries": 0, "attempts": 1,
  "created_at": 1730000000, "updated_at": 1730000001}],
 "stats": {"pending": 0, "running": 1, "done": 20, "failed": 0, "canceled": 0, "running_workers": 1}}
```

### `POST /download/playlist` 参数：`id`（歌单 id）→ `{"ok": true, "queued": 8}`
### `POST /download/song` 参数：`song_id`、`name`、`artist`、`album`、`quality`（可选）→ `{"ok": true, "job": 12}`
### `POST /download/retry` 参数：`id`（可选，缺省重试全部失败）→ `{"ok": true, "retried": 2}`
### `POST /download/cancel` 参数：`id` → `{"ok": true}`

---

## 7. 设备

### `GET /devices`
```json
{"ok": true, "active": "192.168.3.81",
 "devices": [{"ip": "192.168.3.81", "name": "小爱音箱", "model": "S12", "udn": "",
              "last_seen": 1730000000, "active": true}]}
```

### `POST /devices/scan`
SSDP 扫描（约 5s），返回 `{"ok": true, "found": [...]}` 并写入设备表。

### `POST /devices/select`
参数：`ip` → `{"ok": true, "active": "..."}`。

---

## 8. 免打扰（兼容接口）

- `GET /quiet` → `{"ok": true, "quiet_hours": "23:30-08:00", "active": false}`
- `POST /quiet`：`set=22:00-07:00` 或 `off=1`
- `GET /quality` / `POST /quality`：`set=standard|high|lossless`
- `GET /mode` / `POST /mode`：`set=sequential|loop|single|shuffle`

---

## 9. 静态资源

| 路径 | 内容 |
|---|---|
| `/` | `xiaoai_hub/web/index.html` |
| `/app.js` | 前端脚本 |
| `/style.css` | 前端样式 |
| `/favicon.svg` | 图标 |

支持 `ETag` / `If-None-Match`（304）。

---

## 10. MCP（stdio）

`mcp_server.py` 暴露工具：

| 工具 | 参数 | 说明 |
|---|---|---|
| `play_music` | `query` | 搜索并播放 |
| `pause_music` / `resume_music` / `stop_music` | — | 控制 |
| `next_music` / `prev_music` | — | 切歌 |
| `music_status` | — | 当前播放 |
| `search_music` | `query`, `limit` | 只搜索不播放 |
| `list_playlists` | — | 歌单列表 |
| `play_playlist` | `name` 或 `id` | 播放歌单 |
| `set_volume` | `level` | 音量 |
| `set_quality` | `quality` | 音质 |
| `set_mode` | `mode` | 播放模式 |

环境变量：`MUSIC_HUB_URL`（默认 `http://127.0.0.1:8010`）、`MUSIC_HUB_PASSWORD`（可选）。
