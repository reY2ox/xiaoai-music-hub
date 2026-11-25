# 架构说明（v3）

> 本文描述 **v3 的实现结构**，是理解代码的入口。设计决策与问题清单见
> [DESIGN.md](DESIGN.md)，HTTP 契约见 [API.md](API.md)。
> 若文档与代码冲突，以代码为准并提 PR 同步本文。

---

## 1. 分层与依赖方向

```
┌────────────────────────────────────────────────────────────────┐
│ 适配层（薄入口）                                                │
│   app.py  xiaoai_music.py  mcp_server.py  netease_login.py      │
│   = 转发到 xiaoai_hub.app:main / xiaoai_hub.mcp / login_cli     │
├────────────────────────────────────────────────────────────────┤
│ 接口层  xiaoai_hub/api/                                         │
│   router.py（Request/Response/Router）  server.py（HTTP/鉴权）  │
│   context.py（AppContext 依赖容器）     routes/*（按资源拆）     │
├────────────────────────────────────────────────────────────────┤
│ 领域层（业务规则）                                              │
│   player.py（播放状态机）  downloader.py（下载队列）             │
│   library.py（曲库/转码/流）  prefs.py（运行时偏好）             │
├────────────────────────────────────────────────────────────────┤
│ 能力层（外部系统）                                              │
│   netease.py（网易云 HTTP）  dlna.py（UPnP/SOAP 渲染器）          │
├────────────────────────────────────────────────────────────────┤
│ 基础层                                                          │
│   config.py  db.py  logging_setup.py  errors.py  security.py    │
└────────────────────────────────────────────────────────────────┘
```

依赖规则（超出即视为设计问题）：

1. **严格单向**：上层可以 import 下层，下层不得 import 上层。
2. `config.py` 不依赖任何项目内模块；`db.py` 只依赖 `config` / `logging_setup`。
3. 领域层只依赖基础层 + 能力层；`api/` 只通过 `AppContext` 访问领域能力（便于测试整体替换）。
4. 外部世界只有两个出口：`netease.py`（HTTP）与 `dlna.py`（SOAP），其余模块不直接发网络请求
   （`library.py` 调本机 `ffmpeg` 子进程除外）。
5. 入口文件不做业务，只做组装与信号处理。

`xiaoai_hub/__init__.py` 只放 `__version__`，导入包不产生副作用（修复 v2 的 import 期建表崩溃）。

## 2. 模块职责

| 模块 | 职责 | 关键入口 |
|---|---|---|
| `xiaoai_hub/app.py` | 组装依赖、初始化日志/DB、起 HTTP 与后台线程、优雅退出 | `main()` |
| `xiaoai_hub/config.py` | 环境变量 → 不可变 `Settings`；LAN IP 探测；音质/免打扰工具 | `load()` / `get()` / `detect_lan_ip()` |
| `xiaoai_hub/db.py` | SQLite 连接（WAL）、Schema、版本化迁移、键值设置 | `connect()` / `transaction()` / `init_schema()` |
| `xiaoai_hub/prefs.py` | 运行时偏好（音质/模式/免打扰/设备/密码），存 `settings` 表 | `public_settings()` |
| `xiaoai_hub/logging_setup.py` | 控制台 + 轮转文件日志（5MB×3） | `setup()` / `get_logger()` |
| `xiaoai_hub/errors.py` | 异常体系与 HTTP 状态映射 | `HubError` 子类、`to_http()` |
| `xiaoai_hub/security.py` | 媒体令牌、Basic Auth、同源校验 | `media_token()` / `basic_auth_ok()` |
| `xiaoai_hub/netease.py` | 网易云接口：会话/Cookie、账号缓存、搜索、直链降级、歌单分页、扫码登录 | 模块级函数 + `Song`/`SongUrl`/`Account`/`Playlist` 数据类 |
| `xiaoai_hub/dlna.py` | UPnP 渲染器：SOAP 调用、能力探测、SSDP 发现 | `Renderer` / `discover()` |
| `xiaoai_hub/library.py` | 曲库扫描（增量）、标签、转码缓存、Range/HEAD 流式输出 | `scan()` / `media_url()` / `stream_file()` |
| `xiaoai_hub/downloader.py` | 下载队列：持久化、并发控制、断点续传、取消、错误分类、入库 | `add_job()` / `kick()` / `shutdown()` |
| `xiaoai_hub/player.py` | **唯一播放状态持有者**：队列、模式、设备、暂停恢复、watchdog 状态机 | `Player` / `get_player()` |
| `xiaoai_hub/mcp.py` | stdio MCP server（JSON-RPC，13 个工具），只调用中枢 HTTP API | `main()` |
| `xiaoai_hub/login_cli.py` | 命令行扫码登录 | `main()` |
| `xiaoai_hub/api/router.py` | 极简路由：路径占位符、参数读取、统一响应 | `Router.add()` |
| `xiaoai_hub/api/server.py` | `ThreadingHTTPServer`、鉴权、同源校验、安全头、访问日志 | `HubApp` / `serve_forever()` |
| `xiaoai_hub/api/context.py` | `AppContext` 依赖容器（player/netease/library/downloader/prefs/db） | `build_context()` |
| `xiaoai_hub/api/routes/*.py` | 按资源拆分的 handler：`system/player/playlists/netease/local/download` | 各模块 `register(router, ctx)` |
| `xiaoai_hub/web/` | 前端三件套（零构建） | `index.html` / `app.js` / `style.css` |

## 3. 播放状态机（核心）

### 3.1 为什么需要状态机

DLNA 渲染器的 `AVTransport` 状态只有 `PLAYING / PAUSED_PLAYBACK / TRANSITIONING /
STOPPED / NO_MEDIA_PRESENT`，**不包含「是谁让它停的」**。小爱语音「暂停/停止」在多数设备上
直接表现为 `STOPPED`，与「一首歌自然播完」完全同态。v2 只要看到 `STOPPED` 就
`advance(1)`，于是用户语音暂停后中枢会「抢回」播放（表现为停不下来）。v3 的原则是：

> **自然播完必须有正证据；没有证据的停止一律视为外部控制，绝不自动切歌。**

实现位置：`player.Player.poll_once()`（watchdog 每 `AUTO_ADVANCE_INTERVAL` 秒调用一次）。
每次轮询记录一个 `Poll(at, state, position, duration, uri, title, artist)`，与上一次比较。

### 3.2 判定规则

**A. 自然播完（允许 `advance(1, natural=True)`）——必须有正证据：**

1. 已知时长且已到末尾附近：`duration > 0` 且 `max(prev.position, position) >= duration - END_TOLERANCE`
   （只有这条路会用到「上一次轮询位置」，因此停止候选的判定不会漏掉刚越过末尾的情况）；
2. 或设备播完后仍报 `PLAYING`：`duration > 0` 且 `position >= max(duration - 2, 1)`，
   且最近一次仍在播放的时间在 `AUTO_ADVANCE_FRESH`（默认 120s）新鲜期内；
3. 或显式开启的兜底（**默认关闭**）：`duration == 0` 时「上次位置 ≥3s、本次 ≤1s」，
   仅在 `AUTO_ADVANCE_UNKNOWN_DURATION=1` 时启用。

**B. 外部控制（不推进队列，进入中断态）：**

1. `PLAYING/TRANSITIONING → PAUSED_PLAYBACK`：外部暂停；
2. `PLAYING/TRANSITIONING → STOPPED` 且**不满足 A 的任一条件**：外部停止；
3. **宽限期**：第一次看到停止只记录 `_stop_candidate`，下一次轮询仍停止才落锤；
   期间恢复 `PLAYING` 视为瞬时抖动，忽略；
4. **指令静默期**：本机刚下发 `play_url` / `advance` 后的 `GRACE_AFTER_COMMAND`（3s）内
   忽略 `STOPPED`，避免 `SetAVTransportURI` 的瞬态被误判。

**C. 外部中断后的行为：**

- 记录 `_interrupted = {reason, title, artist, uri, position, at}`，`_active=False`；
- 不推进、不重推流、不改音量；
- `/state`、`/now` 暴露 `interrupted`，前端显示「音箱已被语音/其他控制器暂停 · 继续播放」；
- 用户继续（`POST /resume` / 横幅 / 语音）→ 用记录的 `uri + position` 重新推流并 Seek；
- 用户直接对小爱说「继续播放」→ 下一次轮询看到 `PLAYING` 且 URI 未变，自动清除中断态。

**D. 本机主动控制：**

- `pause()`：先发 `Pause`，等待 ≤`PAUSE_VERIFY_TIMEOUT`（0.8s）复查；设备不支持 Pause
  （仍 `PLAYING`）则退化为 `Stop`，记录位置并标记 `_manual_stop=True`；
- `resume()`：优先 `PAUSED_PLAYBACK → Play`；否则重推流 + Seek（`resumed: pause|replay|already`）；
- `stop()`：`_manual_stop=True`、`_active=False`，队列不再自动续播。

**E. 队列自然结束：** 顺序模式到最后一首后 `_active=False`，`advance()` 返回 `end of playlist`，
`last_state` 不会卡在 `ADVANCING`（v2 的 bug）。

### 3.3 状态转换图

```
                  play_track / play_index / play_url
        ┌──────────────────────────────────────────────┐
        ▼                                              │
   ┌─────────┐   poll: 到末尾(A)     ┌──────────────┐    │
   │ PLAYING │ ────────────────────► │ ADVANCING    │────┘
   └────┬────┘                        │ (内部瞬态)   │
        │                             └──────┬───────┘
        │ poll: STOPPED/PAUSED             │ 队列尾(sequential)
        │ 且无正证据(B)                     ▼
        ▼                            ┌──────────┐
   ┌──────────────┐  resume(replay)  │ STOPPED  │
   │ INTERRUPTED  │ ◄──────────────► │(不续播)  │
   │(外部暂停/停止)│                  └──────────┘
   └──────┬───────┘
          │ 用户对小爱说「继续播放」→ poll 看到 PLAYING 且 URI 未变
          ▼
     清除中断态，恢复监测
```

补充：

- 本机 `pause()` 后设备可能处于 `PAUSED_PLAYBACK`（支持 Pause）或 `STOPPED`（降级），
  两者都通过 `_manual_stop=True` 阻止 watchdog 推进；
- 单曲播放（搜索点歌 / 本地单曲）`playlist_id=None, index=None, active=False`，
  不挂队列、不自动续播，`/next` 返回 `no queue`；
- 免打扰时段内 `poll_once()` 直接返回（不自动续播）。

### 3.4 队列与播放模式

| 模式 | 自然播完 | 手动下一首 | 手动上一首 | 队列尾 |
|---|---|---|---|---|
| `sequential` | index+1 | index+1 | index-1 | 停止（`_active=False`） |
| `loop` | index+1，越界回 0 | 同左 | 越界到 n-1 | 回到 0 |
| `single` | 重播当前 | index+1 | index-1 | 按顺序 |
| `shuffle` | 随机非当前 | 随机非当前 | 历史栈 `pop()` 回退 | 持续随机 |

随机模式维护 `history: list[int]`（上限 50），推进时压栈、上一首时出栈，
不再使用「append 当前 index」的写法，避免在相邻两首之间乒乓。

## 4. 数据存储

SQLite 单文件（默认 `<DATA_DIR>/music.db`），WAL 模式，`busy_timeout=30s`，
`SCHEMA_VERSION = 3`。每次操作使用独立连接（`db.connect()`），写操作用 `db.transaction()`。

### 4.1 表结构

| 表 | 主键 | 关键列 | 说明 |
|---|---|---|---|
| `devices` | `ip` | `name, model, udn, avt_control, rc_control, last_seen` | 发现的 DLNA 渲染器与 SOAP 控制 URL |
| `playlists` | `id` | `name, source, remote_id, cover, owner_uid, position` | `source=local/netease`；`remote_id` 对应网易云歌单；`owner_uid` 做账号隔离 |
| `tracks` | `id` | `playlist_id, song_id, name, artist, album, pos, local_id, duration` | 歌单曲目；`local_id` 指向已下载的 `local_tracks.id` |
| `settings` | `k` | `v` | 键值设置：`quality/play_mode/quiet_hours/active_device/ui_password` |
| `local_tracks` | `id` | `path(UNIQUE), title, artist, album, size, mtime, song_key, duration` | 本地曲库；`song_key` 绑定网易云 `song_id` |
| `download_jobs` | `id` | `song_id, quality, status, progress, error, path, bytes_done, bytes_total, retries, attempts, cancel_requested, created_at, updated_at` | 持久化下载队列；`status ∈ pending/running/done/failed/canceled` |
| `play_history` | `id` | `song_id, local_id, name, artist, source, played_at` | 播放历史（随机模式/统计） |

索引：`tracks(playlist_id, pos)`、`tracks(song_id)`、`local_tracks(song_key)`、
`local_tracks(artist)`、`download_jobs(status, id)`、`play_history(played_at DESC)`、
`playlists(remote_id)`。

### 4.2 迁移策略

- 新库：`BASE_SCHEMA` 全量建表 + 索引；
- 旧库（v1/v2）：`LEGACY_COLUMNS` 逐列 `ALTER TABLE ... ADD COLUMN` 幂等补齐，**不丢数据**；
- 未来结构变更：在 `db.MIGRATIONS` 追加 `(目标版本, SQL)`，通过 `PRAGMA user_version` 顺序执行；
  **禁止修改已发布的旧迁移**；
- 种子数据：无设备时写入默认音箱 IP，无歌单时创建「我的歌单」；全部包裹在 `_init_lock` 内，
  并发启动多个进程也安全。

## 5. 并发模型

| 执行体 | 数量 | 生命周期 | 线程名 | 作用 |
|---|---|---|---|---|
| HTTP 请求线程 | 每请求一个 | 请求级 | `ThreadingHTTPServer` 内部 | 分发、鉴权、调用领域层；媒体流式输出在本线程完成 |
| 播放 watchdog | 1 | 进程级 | `player-watchdog` | 每 `AUTO_ADVANCE_INTERVAL` 秒 `poll_once()`，状态机唯一驱动 |
| 下载 worker | ≤ `DOWNLOAD_CONCURRENCY`（默认 2） | 任务级 | `xiaoai-download-N` | 领取 `pending` 任务、下载、校验、入库 |
| 扫码轮询 | ≤1 | 一次登录 | `qr-poll` | 网页扫码时后台轮询登录状态 |
| 全量歌单同步 | ≤1 | 一次同步 | `sync-all` | `POST /netease/sync_all` 的后台任务 |
| `ffmpeg` 子进程 | 每转码一个 | 调用级 | — | 同步阻塞调用线程；同曲目加锁串行 |

同步原语：

| 原语 | 位置 | 保护对象 |
|---|---|---|
| `Player._lock`（RLock） | `player.py` | 播放状态、队列指针、中断/暂停信息（HTTP 线程与 watchdog 共享） |
| `downloader._state_lock` / `_cancel_lock` | `downloader.py` | `_running_workers` / 线程表 / 取消标记集合 |
| `library._TRANSCODE_GUARD` + 每曲目 `Lock` | `library.py` | 同一文件的转码互斥，避免重复 ffmpeg |
| `db._init_lock` | `db.py` | Schema 初始化串行化 |
| SQLite WAL + `busy_timeout` | `db.py` | 多线程/多进程写并发（每次操作独立连接） |
| `ctx.login` / `ctx.sync` dict | `api/context.py` | 单写者（后台线程）更新、多读者读快照 |

关键约束：

1. **播放状态只由 `Player` 修改**，HTTP handler 不直接改 DB 里的播放指针；
2. watchdog 内不执行长网络操作（`position_info` 有超时），异常全部 `log.exception` 兜底，
   守护线程不允许崩溃退出；
3. 下载 worker 数在**线程启动前**加锁 `+1`、`finally` 中 `-1`（修复 v2 并发超限）；
4. 退出顺序：停止接收请求 → 停 watchdog → `downloader.shutdown()` → 等待 worker 收尾，
   保证 SQLite 正常关闭并清理临时文件。

## 6. 错误处理

### 6.1 领域异常 → HTTP

`xiaoai_hub/errors.py` 定义统一异常，消息面向用户可直接展示：

| 异常 | HTTP | `code` | 典型场景 |
|---|---|---|---|
| `BadRequest` | 400 | `bad_request` | 缺参数、参数非法 |
| `Unauthorized` / `LoginRequired` | 401 | `unauthorized` / `login_required` | 未设置密码却要求鉴权 / 网易云未登录 |
| `Forbidden` | 403 | `forbidden` | 跨站请求、访问他人歌单 |
| `NotFound` | 404 | `not_found` | 歌单/曲目不存在 |
| `Conflict` | 409 | `conflict` | 无播放队列、VIP 曲目无直链 |
| `UpstreamError` | 502 | `upstream_error` | 网易云异常 |
| `DeviceUnavailable` | 503 | `device_unavailable` | 音箱不可达 |
| 其它 `HubError` | 500 | `internal` | 业务内部错误 |
| 非 `HubError` 异常 | 500 | `internal` | 记录堆栈，响应不泄露细节 |

`HubApp.handle()` 统一捕获并映射；`BrokenPipeError` / `ConnectionResetError`（客户端取流中断）
静默处理，不刷错误日志。响应体固定：
`{"ok": false, "error": "...", "code": "..."}`。

### 6.2 外部错误分类

- **网易云**：`NeteaseError(code, message)`；`-110/-460/301` 映射为「需要登录 / 需要 VIP / 版权受限」；
  永久性错误不重试，瞬时网络错误按退避重试；
- **下载**：永久性（无直链/版权/VIP/404/文件过小/内容非音频）直接 `failed`；
  瞬时性（超时/5xx/连接重置）指数退避重试；
- **DLNA**：SOAP HTTP 500 时解析 UPnPError 返回 `DLNAError(code, desc)`；能力探测失败后缓存结果，
  避免每次试错；
- **MCP**：`urllib.error.HTTPError` 必须捕获，把 409/404/503 等转成友好中文文案
  （v2 直接把 `HTTP Error 409` 抛给语音助手）；
- **UI**：统一 toast 提示，网络失败自动重试一次。

## 7. 测试策略

目标：外部依赖零真实访问，核心逻辑确定性可复现。全部替身定义在
[`tests/fakes.py`](../tests/fakes.py)。

| 层 | 方式 | 关键用例 |
|---|---|---|
| `config` | 纯单测（注入 env） | 环境变量优先级、LAN IP 选择、免打扰跨零点 |
| `db` | 临时文件 | 迁移幂等、旧库升级、索引存在、并发写 |
| `netease` | 本地假 HTTP 服务（`NETEASE_BASE` 指向它） | 搜索解析、直链降级、账号缓存 TTL、歌单分页、QR 流程、登出 |
| `dlna` | 本地假 UPnP 渲染器（HTTP + SOAP 响应） | 状态解析、Seek、音量、UPnPError、发现 |
| `library` | 临时目录 + 生成的假音频文件 | 增量扫描、转码缓存（无 ffmpeg 时 skip）、Range/HEAD/416 |
| `downloader` | 假网易云 + 临时目录 | 断点续传、取消、错误分类、并发上限、恢复 |
| `player` | **注入 `FakeRenderer` + `FakeClock`** | **自然播完续播**、**语音暂停不续播**、**语音停止不续播**、瞬时抖动、暂停恢复、随机上一首、队列尾 |
| `api` | 真实 server（随机端口）+ 假渲染器/假网易云 | 全接口状态码、鉴权、媒体令牌、同源校验、静态资源、错误映射 |
| `mcp` | 子进程 stdio | initialize / tools/list / tools/call 全流程、409 友好文案 |

夹具：

- `tmp_hub`：每个用例独立数据目录 + SQLite，自动 `config.reload()` / `db.configure()` / `init_schema()`；
- `fake_clock` / `fake_renderer` / `fake_netease` / `fake_library` / `player`：可直接驱动状态机；
- 测试结束自动 `config.reset()` / `db.configure(None)`，用例之间零共享状态。

覆盖率目标：核心模块（`player/library/downloader/netease/dlna`）≥ 80%，整体 ≥ 70%。
CI（`.github/workflows/ci.yml`）在 Python 3.9 / 3.11 / 3.12 上跑 `ruff check .` 与 `pytest -q`。
