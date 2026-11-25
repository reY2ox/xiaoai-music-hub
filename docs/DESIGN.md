# xiaoai-music-hub 设计文档（v3）

> 状态：**已实施**（v3.0.0，2026-11-25）。本文中的 v2 行号仅作历史参考，对应文件已删除（见 git 历史）。
> 范围：架构分层、问题清单、核心状态机、模块契约、测试与工程化、兼容性与路线图。

---

## 1. 项目定位

把小米 / 小爱音箱当成一台普通 **DLNA 渲染器**（UPnP MediaRenderer）来用：

```
浏览器 / 语音助手(MCP) / curl
        │  HTTP (JSON)
        ▼
   xiaoai-music-hub  ── 网易云私有 API（搜索 / 直链 / 歌单 / 扫码登录）
        │  DLNA SOAP（AVTransport / RenderingControl）
        ▼
   小爱音箱（UPnP MediaRenderer）
```

设计原则（v3 新增，贯穿全项目）：

1. **单一事实来源**：播放状态只由 `player.Player` 持有；音箱回报优先于本地推测。
2. **尊重外部控制**：用户在音箱上（小爱语音、米家 App、遥控器、其他 DLNA 控制器）做的暂停/停止，
   中枢必须识别并服从，绝不"抢回"播放。
3. **本地优先**：已下载的曲目永远走本地文件，不受版权/直链过期影响。
4. **优雅降级**：设备能力差异（无 Pause、无时长、无 Seek、只支持 mp3）都要有兜底路径。
5. **可测试**：所有外部依赖（音箱、网易云）可注入替身；核心状态机 100% 单测覆盖。
6. **薄入口**：HTTP / MCP / CLI 都是薄适配层，业务逻辑在领域模块。

---

## 2. 现状评估（v2，提交 4e659f7）

> 本节记录重构前的 v2 状态，用于解释「为什么改」。文中 `core/*`、`web/index.html`、`xiaoai_music.py`
> 均为 v2 路径，已在 v3 中删除/替换，代码位置以 git 历史为准。

### 2.1 优点

- 模块已按职责拆到 `core/`（config/dlna/netease/library/downloader/player/api），方向正确。
- 本地优先播放、下载断点续传、静音时段、外部暂停降级恢复等业务细节考虑得比较细。
- 前端单文件、零构建、移动端适配可用。
- SQLite 持久化，重启可恢复。

### 2.2 问题清单（按严重度）

#### P0 — 阻断/数据错误

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| 1 | **全新安装无法启动**：`core/player.py` 在 import 时构造 `Player()`，`__init__` 读 `settings` 表；而建表在 `app.init_db()`（import 之后）。 | `core/player.py:261`、`app.py:71-72` | `sqlite3.OperationalError: no such table: settings`，公开仓库用户 clone 后直接跑不起来。 |
| 2 | **小爱语音暂停被误判为"播完"**：watchdog 只要看到 `PLAYING→STOPPED` 就 `advance(1)`。 | `core/player.py:195` | 用户对小爱说"暂停/停止"后，中枢自动播下一首，表现为"停不下来"。**（用户已反馈）** |
| 3 | **局域网地址探测错误**：`detect_lan_ip()` 固定连 `192.168.3.1:80`，本机存在 VPN/多网卡时返回 `utun` 地址（实测返回 `10.6.0.3`）。 | `core/config.py:22-35` | 媒体 URL 拼成音箱不可达的地址，本地曲库播放静默失败。 |
| 4 | **媒体令牌失效**：设置 `ui_password` 后，`/media/<id>` 校验 token，但 `player.play_track()` 调 `library.media_url()` 时不带 token。 | `core/api.py:495-507`、`core/player.py:76,88` | 一旦开启密码，音箱取流 403，本地播放全挂。 |
| 5 | **下载文件可能是 HTML**：只按 10KB 大小判断有效性，不校验 Content-Type / 魔数。 | `core/downloader.py:216-218` | 版权/风控返回的错误页可能被当成音频入库，播放时才失败。 |

#### P1 — 功能缺陷

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| 6 | `/play` 后置 `pl.active = False`，单曲播放与队列语义混在一起 | `core/api.py:232` | 行为不明确；搜索播放后自动续播永久关闭。 |
| 7 | 前端 `dlPl()` 定义两次，后一个覆盖前一个，且引用不存在的 `#dlplinfo` | `web/index.html:309,374-381` | "下载整张"点击后 JS 抛 `TypeError`，功能失效。 |
| 8 | 前端调用 `/local/download`、`/local/download/status`，后端从未实现 | `web/index.html:311,316` | 死接口。 |
| 9 | 无 Seek 能力（后端无接口、前端无进度条拖拽） | — | 长音频/有声书无法跳转。 |
| 10 | `HEAD` 不支持（`BaseHTTPRequestHandler` 未实现 `do_HEAD`） | `core/api.py` | 部分 DLNA 渲染器/浏览器预检失败；实测 501。 |
| 11 | `playlist_allowed()` 每次请求都调 `netease.current_uid()` → 实时请求网易云 | `core/api.py:28-38,283` | 每次打开歌单列表多 300ms 网络请求，网易云抖动直接拖慢 UI。 |
| 12 | 播放模式/暂停恢复存在竞态：`self._lock` 创建但从未使用；自动续播线程与 HTTP 线程并发改 `index` | `core/player.py:26` | 偶发连跳两首、历史错乱。 |
| 13 | 随机模式"上一首"用 `history[-1]` 且 `play_index` 又 append 当前 index | `core/player.py:109-111,137-139` | 随机模式上一首会在两首之间来回跳。 |
| 14 | 队列结束（`end of playlist`）后 `last_state="ADVANCING"` 永久卡住 | `core/player.py:203-207` | 之后同一首歌播完不再自动续播。 |
| 15 | `cancel()` 只改数据库状态，下载线程继续跑并覆盖为 done | `core/downloader.py:162-163` | 取消无效。 |
| 16 | `kick()` 的 worker 计数在子线程启动后才 +1，连续两次 kick 会超并发 | `core/downloader.py:327-333` | 线程泄漏、并发超限。 |
| 17 | 每下载完一首就 `library.scan()` 全目录扫描 | `core/downloader.py:283,308` | O(n²)，曲库大时 CPU 飙升。 |
| 18 | 网易云 VIP/无版权错误重试 2 次（永久性错误当瞬时错误） | `core/downloader.py:315-321` | 无意义重试、日志噪音。 |
| 19 | MCP `play_music` 对 `/play` 的 409 直接抛 `HTTPError`，友好文案不可达 | `mcp_server.py:66-82` | 语音助手报"执行失败: HTTP Error 409"。 |
| 20 | 网易云歌单超过 1000 首会被截断（无分页） | `core/netease.py:185-196` | 大歌单丢歌。 |
| 21 | 退出登录写入空 cookie 文件，`cookie()` 返回空字符串而不是 `os=pc` | `core/netease.py:21-32`、`api.py:253-256` | 匿名接口可能被风控。 |
| 22 | 无 `GET /health` / `GET /version` / `GET /state`，前端只能拼多个接口 | — | 可观测性差，轮询次数多。 |
| 23 | 音箱不可达时 `/now` 返回 500 + 原始 `URLError` repr | `core/api.py:449-450` | 前端轮询报错刷屏，无法区分"没在播"与"设备离线"。 |

#### P2 — 工程质量

| # | 问题 | 影响 |
|---|---|---|
| 24 | 双入口：README 让跑 `xiaoai_music.py`（1515 行 v2 遗留单体），实际入口是 `app.py`；两者配置路径不同（`/root/...` 硬编码） | 用户按 README 操作得到的是旧版本，且旧版路径不可配置。 |
| 25 | `netease_login.py` 硬编码 `/root/.openclaw/workspace/secrets/netease.cookie`，与 `config.COOKIE_FILE` 不一致 | 扫码登录写入位置错误。 |
| 26 | 无 `requirements.txt` / `pyproject.toml` / 测试 / CI / lint；`segno` 缺失时只有运行到扫码才报错 | 不可复现、不可协作。 |
| 27 | 日志全靠 `print`，且 `log_message` 被完全吞掉 | 线上排障没有线索。 |
| 28 | 配置散落：环境变量 + 代码常量 + 数据库 settings，优先级未定义 | 部署易错。 |
| 29 | API 是 500 行 if/elif，GET 全部有副作用（删除歌单用 GET） | 难维护、有 CSRF 风险。 |
| 30 | 前端 `innerHTML` 直接拼网易云返回的曲名/歌手 | 元数据含 `<` 时页面错乱（潜在 XSS）。 |
| 31 | 无优雅退出：Ctrl-C 后下载线程/自动续播线程被硬杀，SQLite 可能留 WAL | 偶发脏状态。 |
| 32 | 数据库无索引（`tracks.playlist_id`、`local_tracks.song_key`、`download_jobs.status`） | 数据量大后列表/播放变慢。 |

---

## 3. 目标架构（v3）

### 3.1 分层

```
┌──────────────────────────────────────────────────────────────┐
│ 适配层  app.py / xiaoai_music.py / mcp_server.py / login_cli │
├──────────────────────────────────────────────────────────────┤
│ 接口层  xiaoai_hub/api/{router,server,routes/*}              │
├──────────────────────────────────────────────────────────────┤
│ 领域层  player.py（播放状态机） downloader.py（下载队列）      │
│         library.py（曲库）    prefs.py（运行时偏好）           │
├──────────────────────────────────────────────────────────────┤
│ 能力层  netease.py（音源）    dlna.py（渲染器控制）            │
├──────────────────────────────────────────────────────────────┤
│ 基础层  config.py  db.py  logging_setup.py  errors.py         │
└──────────────────────────────────────────────────────────────┘
```

依赖方向严格单向：上层可依赖下层，下层**不得**依赖上层。
`config.py` 不依赖任何项目模块；`db.py` 只依赖 `config`；领域层只依赖基础层 + 能力层。

### 3.2 目录

```
xiaoai_hub/
├── __init__.py          版本号
├── __main__.py          python -m xiaoai_hub
├── app.py               组装依赖、启动 HTTP、优雅退出
├── config.py            静态配置（环境变量 → Settings 不可变对象）
├── db.py                SQLite 连接、Schema、版本化迁移、键值设置
├── prefs.py             运行时偏好（音质/模式/免打扰/设备/密码）
├── logging_setup.py     日志初始化
├── errors.py            异常体系 → HTTP 状态映射
├── netease.py           网易云接口（会话/缓存/重试/降级）
├── dlna.py              UPnP 渲染器（SOAP/发现/能力探测）
├── library.py           曲库扫描/标签/转码/流式输出
├── downloader.py        下载队列（持久化/并发/续传/取消/错误分类）
├── player.py            播放调度 + watchdog 状态机
├── mcp.py               MCP stdio server
├── login_cli.py         命令行扫码登录
├── api/
│   ├── router.py        Request/Response/Router/中间件
│   ├── server.py        ThreadingHTTPServer + Handler
│   ├── context.py       AppContext（依赖容器）
│   └── routes/{system,player,playlists,netease,local,download}.py
└── web/{index.html,app.js,style.css}
```

根目录保留：`app.py`（兼容入口，转发到 `xiaoai_hub.app:main`）、`xiaoai_music.py`（旧名兼容壳，打印弃用提示）、
`mcp_server.py`（兼容壳）、`netease_login.py`（兼容壳）。

---

## 4. 核心设计

### 4.1 播放状态机与"外部控制识别"（修复问题 #2）

#### 4.1.1 为什么必须区分"播完"和"被外部暂停"

音箱的 AVTransport 状态只有 `PLAYING / PAUSED_PLAYBACK / TRANSITIONING / STOPPED / NO_MEDIA_PRESENT`，
**没有**"是谁让它停的"。小爱语音"暂停/停止"在多数小米音箱上直接表现为 `STOPPED`，
与"一首歌自然播完"的状态完全相同。v2 用"看到 STOPPED 就下一首"处理，于是把用户的暂停当成了播完。

#### 4.1.2 判定算法（多信号 + 宽限期）

每次轮询（默认 1.5s）记录一个 `Poll(state, position, duration, uri, at)`，并与上一次比较：

**A. 自然播完（可以 advance）——必须有"正证据"：**

1. `duration > 0` 且 `max(prev.position, position) >= duration - END_TOLERANCE`
   （`END_TOLERANCE` 默认 8s，覆盖轮询间隔；即"确实播到末尾附近"）；
2. 或设备在末尾仍报 `PLAYING`：`state == PLAYING and duration > 0 and position >= duration - 2`；
3. 或**显式开启**的兜底（默认关闭）：`duration == 0 and prev.position >= 3 and position <= 1`
   （旧版行为，仅在设备不返回时长且用户明确开启 `AUTO_ADVANCE_UNKNOWN_DURATION=1` 时启用）。

**B. 外部控制（禁止 advance，进入"被中断"态）：**

1. `prev.state ∈ {PLAYING, TRANSITIONING}` 且 `state == PAUSED_PLAYBACK` → 外部暂停；
2. `prev.state ∈ {PLAYING, TRANSITIONING}` 且 `state == STOPPED`，且**不满足 A 的任何条件**
   （时长已知但位置离末尾还远；或时长未知）→ 外部停止/暂停；
3. 宽限期：第一次看到 STOPPED 时不立即判定，先记录候选，**下一次轮询仍为 STOPPED** 才落锤；
   若恢复 PLAYING 则视为瞬时抖动，忽略。
4. 本机刚发起过 `play_url`/`advance` 后的 `GRACE_AFTER_COMMAND`（3s）内忽略 STOPPED。

**C. 外部中断后的行为：**

- 记录 `interrupted = {reason: external_pause|external_stop, title, artist, uri, position, at}`；
- 不推进队列、不重新拉流、不改变音量；
- `/state` 与 `/now` 暴露 `interrupted`，前端显示横幅「音箱已被语音/其他控制器暂停 · 继续播放」；
- 用户点"继续" → 用记录的 `uri + position` 重新推流并 Seek（与现有暂停降级同一套逻辑）；
- 若用户直接对小爱说"继续播放"（状态回到 `PLAYING` 且 URI 未变）→ 自动清除中断态，恢复监测。

**D. 本机主动暂停/停止：**

- `pause()`：先发 `Pause`，800ms 后复查；若未进入 `PAUSED_PLAYBACK`，退化为 `Stop` 并记录位置，
  标记 `manual_stop=True`，watchdog 不再推进；
- `resume()`：优先 `PAUSED_PLAYBACK → Play`；否则重新 `SetAVTransportURI` + `Play` + `Seek(pos)`；
- `stop()`：`manual_stop=True`、`active=False`（队列不再自动续播）。

**E. 队列自然结束：** 顺序模式到最后一首后 `active=False`，`last_state` 回到 `STOPPED`（不再卡在 ADVANCING）。

#### 4.1.3 状态字段

```python
@dataclass
class Poll:
    at: float; state: str; position: float; duration: float
    uri: str; title: str; artist: str

class Player:
    _last: Poll | None                 # 上一次轮询
    _interrupted: dict | None          # 外部中断信息
    _paused_info: dict | None          # 本机暂停的可恢复信息
    _stop_candidate: Poll | None       # STOPPED 宽限期候选
    _command_grace_until: float        # 本机指令后的静默期
    _last_playing_at: float
    _manual_stop: bool
    _active: bool
    _lock: threading.RLock
```

### 4.2 队列与播放模式语义

| 模式 | 自然播完 | 下一首 | 上一首 | 队列尾 |
|---|---|---|---|---|
| sequential | index+1 | index+1 | index-1 | 停止（active=False） |
| loop | index+1，越界回 0 | 同 | 越界到 n-1 | 回到 0 |
| single | 重播当前 | index+1（手动） | index-1 | 按顺序 |
| shuffle | 随机非当前 | 随机非当前 | 历史栈回退 | 持续随机 |

- 随机模式维护 `_history: list[int]`：推进时 `push(current)`，上一首时 `pop()` 并播放；
  不再用"append 当前 index"的写法，避免乒乓。
- 单曲播放（搜索点歌/本地单曲）语义：`playlist_id=None, index=None, active=False`
  ——不挂队列，不自动续播；`/next` 返回 `no queue`。
- 队列查询统一 `ORDER BY pos, id`。

### 4.3 媒体输出（修复 #3 #4 #10 #17）

1. **本地优先**：`play_track` 先查 `local_tracks.song_key = song_id`，命中直接推本地文件；
   未命中再查 `歌手 - 歌名` 约定路径；最后才联网取直链。
2. **媒体 URL**：`media_url(track, token)` 显式带 token；token 由 `ui_password + track_id` 派生，
   `player` 与 `api` 共用 `security.media_token()`（新增 `xiaoai_hub/security.py`）。
3. **LAN IP**：优先 `LAN_IP` 环境变量；否则对**音箱 IP** 做 UDP 路由探测取源地址；
   再否则遍历本机 IPv4，排除 127/169.254/utun/CGNAT(100.64/10)，优先 192.168/10/172.16 私网地址。
   启动日志打印最终选中的地址与探测依据。
4. **流式输出**：支持 `Range`（206/416）、`HEAD`、`Accept-Ranges`、`Content-Length`、
   `Cache-Control: no-store`、按扩展名给出正确 MIME；客户端断开（BrokenPipe）静默处理。
5. **转码**：`ffmpeg` 不可用时明确报错并提示安装；缓存文件按 `track_id + mtime` 失效；
   同一文件转码加进程内互斥锁；临时文件 + `os.replace` 原子落盘；`CACHE_MAX_MB` 超限时按 LRU 清理。
6. **增量扫描**：`scan_file()` 单文件入库；`scan()` 按 `(path, size, mtime)` 增量更新，
   可选 `prune=True` 清理已删除文件；下载完成后只 `scan_file()`，不再全量扫描。

### 4.4 下载队列（修复 #5 #15 #16 #18）

状态机：`pending → running → done | failed | canceled`；失败可 `retry` 回 `pending`。

- **并发控制**：`_running_workers` 在**启动线程前**加锁 +1（修复 #16）；线程退出 finally -1。
- **取消**：`cancel(job_id)` 写 `cancel_requested=1`（内存集合 + DB 标记）；
  下载循环每 256KB 检查一次，命中则删除 `.part`、置 `canceled` 并退出。
- **错误分类**：
  - 永久性：`no-url` / 版权 / VIP 无权限 / 404 / 文件过小 / 内容非音频 → 直接 `failed`，不重试；
  - 瞬时性：网络超时 / 5xx / 连接重置 → 指数退避重试（默认 2 次，间隔 2s/6s）。
- **内容校验**：检查 `Content-Type`（audio/* 或 application/octet-stream）与文件魔数
  （`ID3`/`\xff\xfb`/`fLaC`/`OggS`/`ftyp`/`RIFF`）；不匹配则删除并判失败。
- **标签**：优先用 `mutagen`（可选依赖，纯 Python，安全）；不可用时退回 `ffmpeg -c copy -metadata`；
  两者都不可用则直接落盘（文件名即元数据）。
- **恢复**：启动时 `running → pending`，保留 `.part` 续传。
- **入库**：完成后 `library.scan_file()` + 绑定 `song_key` + 回填 `tracks.local_id`。

### 4.5 网易云接入（修复 #11 #19 #20 #21）

- **会话**：模块级会话状态（UA/Referer/Cookie 统一封装），`account()` 带 TTL 缓存（默认 60s），
  登录/登出时失效；`current_uid()` 不再每次请求打网络。
- **错误**：`NeteaseError(code, message)`；`code=-110/-460/301` 映射为"需要登录/需要 VIP/版权受限"。
- **音质降级**：`song_url_auto()` 按 `lossless → high → standard` 依次尝试，返回首个可用直链，
  并在结果里标注实际档位。
- **歌单**：`playlist_tracks()` 超过 1000 首时用 `/api/playlist/track/all` 分页拉全。
- **登录**：`qr_start()` 生成二维码（segno），`qr_poll()` 轮询；Cookie 原子写入（临时文件 + replace），
  登出删除文件而非写空串；扫码会话 cookie 与轮询 cookie 保持一致。
- **搜索**：支持 `limit/offset`，返回 `song_id/name/artist/album/fee/duration`。

### 4.6 DLNA 层（修复 #10 相关能力探测）

- `Renderer(ip)`：从 `description.xml` 解析 `AVTransport`/`RenderingControl` 的 `controlURL`，
  失败时回退到 `http://ip:9999/{AVTransport,RenderingControl}/control`。
- 每次 SOAP 调用带超时 + 1 次重试；HTTP 500 时解析 UPnPError 返回 `DLNAError(code, desc)`。
- `position_info()` 合并 `GetPositionInfo` + `GetTransportInfo`，返回强类型 `PositionInfo`；
  `"NOT_IMPLEMENTED"` 等非时间串按 0 处理。
- `discover()`：SSDP M-SEARCH（ST=MediaRenderer:1，MX=2），按 UDN 去重，并发抓取 description，
  返回 `Device(ip, name, model, udn, avt_control, rc_control)`；5s 超时。
- 能力缓存：`supports_pause/supports_seek` 首次失败后记住，避免每次都试错。

### 4.7 API 层（修复 #7 #8 #9 #22 #23 #29）

- 路由：`Router.add(method, pattern, handler)`，`pattern` 支持 `/playlist/{id}` 占位符；
  统一返回 `Response`；异常 → `errors.to_http()` 映射（`400/403/404/409/500`），
  响应体固定 `{"ok": false, "error": "...", "code": "..."}`。
- 方法：**查询用 GET，变更用 POST**；旧 GET 变更接口保留兼容（标记 deprecated），
  并在变更请求上做同源校验（`Origin/Referer` 与 Host 不一致则 403）降低 CSRF 风险。
- 鉴权：可选 Basic Auth（`UI_PASSWORD` 环境变量或数据库 `ui_password`）；`/media/*` 走媒体令牌，
  其余接口走 Basic；`/health`、`/version`、`/` 免鉴权（便于探活与首屏）。
- 安全响应头：`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、
  `X-Frame-Options: DENY`、`Cache-Control: no-store`（API）。
- 新接口：`GET /health`、`GET /version`、`GET /state`、`GET /seek?pos=`、`POST /volume`、
  `POST /playlist/reorder`、`GET /queue`（含完整队列）、`GET /logs?lines=`（可选，仅本地）。
- 错误语义：音箱离线 → `503 {"error":"device unreachable"}`；无队列 → `409`；参数错误 → `400`。
- 静态资源：`/`、`/app.js`、`/style.css`、`/favicon.svg`，带 ETag + 304。
- 日志：访问日志（method path status ms）走 logging，`--verbose` 时打印请求体。

### 4.8 配置与运行时设置

优先级（后者覆盖前者）：**代码默认值 < 环境变量 < 数据库 settings（运行时）**。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `XIAOAI_HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8010` | 端口 |
| `XIAOAI_IP` | `192.168.3.81` | 默认音箱 IP |
| `LAN_IP` | 自动探测 | 媒体 URL 使用的本机地址 |
| `MUSIC_DIR` | `<data_dir>/music` | 曲库目录 |
| `DATA_DIR` | 仓库根 | db/cache/secrets 根目录 |
| `DB_PATH` | `<data_dir>/music.db` | SQLite 路径 |
| `UI_PASSWORD` | 空（不鉴权） | Web 密码 |
| `NETEASE_BASE` | `https://music.163.com` | 测试可指向假服务 |
| `DOWNLOAD_CONCURRENCY` | `2` | 下载并发 |
| `AUTO_ADVANCE_INTERVAL` | `1.5` | watchdog 轮询秒 |
| `AUTO_ADVANCE_UNKNOWN_DURATION` | `0` | 时长未知时是否兜底续播 |
| `LOG_LEVEL` / `LOG_FILE` | `INFO` / 空 | 日志 |

运行时设置（DB `settings` 表）：`quality`、`play_mode`、`quiet_hours`、`active_device`、`ui_password`。

### 4.9 日志与可观测性

- `logging_setup.setup(level, file)`：控制台 + 可选轮转文件（5MB×3），格式
  `时间 级别 [模块] 消息`。
- 关键事件 INFO：启动参数、设备选择、开始播放、外部中断识别、自动续播、下载任务状态、登录状态。
- 异常 WARNING/ERROR 带堆栈；HTTP 访问日志 INFO。
- `GET /health`：`{ok, version, uptime, device:{ip,reachable}, db:{ok}, download:{pending,running,failed}}`。

### 4.10 前端

- 拆分为 `index.html` + `app.js` + `style.css`，零构建、原生 ES2020，无第三方依赖。
- 渲染统一走 `textContent` / `el()` 工具，杜绝 `innerHTML` 拼接外部数据（修复 #30）。
- 状态：优先 `GET /state` 单请求（1.5s 轮询，页面隐藏时暂停轮询）；
  进度条可拖拽 → `POST /seek`；音量滑块回读设备音量；音质/模式/免打扰设置持久化。
- 外部中断横幅：`interrupted` 非空时显示「音箱已被语音暂停，点此继续」。
- 歌单：新建/重命名/删除/拖拽排序、单曲移除、整张下载进度（轮询 `/download/jobs`）。
- 本地曲库：搜索、扫描、播放、加入歌单、删除缓存。
- 错误提示：统一 toast，网络失败自动重试一次。
- 移动端：断点 600px，控件可点区域 ≥ 40px；支持深色/浅色跟随系统（可选）。

---

## 5. 模块契约（冻结，供并行开发）

### 5.1 `config.py`

```python
@dataclass(frozen=True) class Paths: base_dir, data_dir, db_path, cache_dir, secrets_dir,
                                     cookie_file, web_dir, music_dir: str
@dataclass(frozen=True) class Settings: paths, host, port, lan_ip, speaker_ip,
    request_timeout, netease_base, netease_timeout, netease_account_ttl,
    download_concurrency, download_max_retry, download_timeout,
    auto_advance_interval, auto_advance_fresh, end_tolerance,
    auto_advance_unknown_duration, quiet_default, quality_levels,
    default_quality, log_level, log_file, ui_password: ...
QUALITY_LEVELS: dict; PLAY_MODES: tuple; AUDIO_EXT: tuple; NEED_TRANSCODE: tuple
def load(env=None, base_dir=None) -> Settings
def get() -> Settings            # 进程内单例
def reload(env=None, base_dir=None) -> Settings   # 测试用
def detect_lan_ip(speaker_ip=None, env=None) -> str
def parse_quiet_hours(rng: str) -> tuple[int, int] | None
```

### 5.2 `db.py`

```python
SCHEMA_VERSION = 3
def configure(db_path: str | None = None) -> None
def connect() -> sqlite3.Connection
@contextmanager def transaction()
def query(sql, params=()) -> list[sqlite3.Row]
def query_one(sql, params=()) -> sqlite3.Row | None
def execute(sql, params=()) -> int
def executescript(sql) -> None
def init_schema() -> None
def get_setting(key, default=None) -> str | None
def set_setting(key, value) -> None
def delete_setting(key) -> None
def table_columns(table) -> set[str]
```

Schema 变更：新增 `schema_version`、`download_jobs.cancel_requested`、`download_jobs.attempts`、
`play_history`；索引 `tracks(playlist_id,pos)`、`local_tracks(song_key)`、`download_jobs(status,id)`。

### 5.3 `dlna.py`

```python
@dataclass Device: ip, name="", model="", udn="", avt_control="", rc_control=""
@dataclass PositionInfo: state, title, artist, duration, position, uri, duration_raw, position_raw
    @property playing/paused/stopped; def to_dict()
class Renderer:
    def __init__(self, ip, *, avt_control="", rc_control="", timeout=None)
    def transport_state() -> str
    def position_info() -> PositionInfo
    def play_url(uri, title="", artist="", mime="audio/mpeg") -> str
    def stop()/pause()/resume() -> str
    def seek(seconds) -> bool
    def set_volume(level) -> int; def get_volume() -> int | None
    def ping() -> bool
def discover(timeout=5.0) -> list[Device]
def secs(ts) -> float; def fmt_time(sec) -> str
```

### 5.4 `netease.py`

```python
@dataclass Song: song_id, name, artist, album, fee=None, duration=0, cover=""; to_dict()
@dataclass SongUrl: url, code, br, ext, ok, quality; to_dict()
@dataclass Account: logged_in, uid, nickname, avatar, vip, reason; to_dict()
@dataclass Playlist: remote_id, name, count, cover; to_dict()
class NeteaseError(Exception): code, message
def account(force=False) -> Account; def current_uid(force=False) -> int | None
def search(query, limit=10, offset=0) -> list[Song]
def song_url(song_id, quality=None) -> SongUrl; def song_url_auto(song_id) -> SongUrl
def playlists() -> list[Playlist]; def playlist_tracks(remote_id) -> list[Song]
def qr_start() -> dict; def qr_poll(key, headers=None) -> tuple[int, bool]
def save_cookie(text) / cookie() / logout() / configure(base=None, cookie_file=None) / reset_cache()
```

### 5.5 `library.py`

```python
def scan(root=None, *, incremental=True, prune=False) -> dict  # {added,updated,removed,total,elapsed}
def scan_file(path) -> dict | None
def list_tracks(query="", limit=200, offset=0) -> list[dict]
def local_for_song(song_id, name=None, artist=None) -> dict | None
def index_downloaded(path, song_id) -> None
def media_path(track) -> str
def media_url(track, token="") -> str
def stream_file(handler, path, mime=None) -> None   # 支持 Range/HEAD/416
def guess_mime(path) -> str
def cache_cleanup(max_mb) -> int
def stats() -> dict
```

### 5.6 `downloader.py`

```python
def init() -> None; def recover() -> None
def add_job(song: dict, quality=None) -> int
def add_playlist(playlist_id) -> int
def jobs(limit=100, status=None) -> list[dict]
def stats() -> dict
def retry_failed(job_id=None) -> int
def cancel(job_id) -> bool
def kick() -> None; def shutdown() -> None
```

### 5.7 `player.py`

```python
class Player:
    def __init__(self, *, renderer_factory=None, netease_api=None, library_api=None)
    def snapshot() -> dict; def now() -> dict; def queue() -> dict
    def active_ip() -> str; def set_device(ip) -> None
    def tracks(pid=None) -> list[dict]; def set_mode(mode) -> str
    def play_track(track, ip=None, *, playlist_id=None, index=None) -> dict
    def play_index(pid, idx, ip=None) -> dict
    def play_url(url, title="", artist="", mime="audio/mpeg", ip=None) -> dict
    def advance(delta=1, *, natural=False) -> dict
    def pause()/resume()/stop()/seek(seconds) -> dict
    def set_volume(level) -> dict; def get_volume() -> int | None
    def in_quiet_hours() -> bool
    def start() -> None; def shutdown() -> None; def poll_once() -> None
def get_player() -> Player   # 懒加载单例
```

### 5.8 `api`

```python
class AppContext: settings, player, netease, library, downloader, login_state, sync_state, started_at
class Request: method, path, params(dict), query(dict), headers, body, json
class Response: status, body, headers, content_type
class Router: add(method, pattern, fn); dispatch(req) -> Response
def build_router(ctx) -> Router
def make_server(ctx, host, port) -> ThreadingHTTPServer
```

---

## 6. 测试策略

| 层 | 方式 | 关键用例 |
|---|---|---|
| config | 纯单测 | 环境变量优先级、LAN IP 选择、免打扰时段跨零点 |
| db | 临时文件 | 迁移幂等、旧库升级、索引存在、并发写 |
| netease | 本地假 HTTP 服务（`NETEASE_BASE` 指向它） | 搜索解析、直链降级、账号缓存 TTL、歌单分页、QR 流程、登出 |
| dlna | 本地假 UPnP 渲染器（HTTP + SOAP 响应） | 状态解析、Range 无关、Seek、音量、错误码、发现 |
| library | 临时目录 + 生成的假音频文件 | 增量扫描、转码缓存、Range/HEAD/416 流 |
| downloader | 假网易云 + 临时目录 | 断点续传、取消、错误分类、并发上限、恢复 |
| player | **注入 FakeRenderer**（可脚本化状态序列） | **自然播完续播**、**语音暂停不续播**、**语音停止不续播**、瞬时抖动、暂停恢复、随机上一首、队列尾 |
| api | 启动真实 server（随机端口）+ 假渲染器/假网易云 | 全接口状态码、鉴权、媒体令牌、CSRF 同源校验、静态资源、错误映射 |
| mcp | 子进程 stdio | initialize/tools/list/tools/call 全流程、409 友好文案 |

覆盖率目标：核心模块（player/library/downloader/netease/dlna）≥ 80%，整体 ≥ 70%。
CI：ruff check + ruff format --check + pytest（Python 3.9 / 3.11 / 3.12）。

---

## 7. 工程化

- `pyproject.toml`：项目元数据、依赖（`segno`，可选 `mutagen`）、`ruff`、`pytest` 配置、console script。
- `requirements.txt` / `requirements-dev.txt`。
- `Makefile`：`make venv/install/run/test/lint/fmt/cov/clean/docker`。
- `.github/workflows/ci.yml`：lint + test matrix。
- `deploy/xiaoai-music-hub.service`：systemd 单元（EnvironmentFile、Restart、日志）。
- `deploy/Dockerfile` + `docker-compose.yml`：python:3.12-slim + ffmpeg，`/data` 卷，healthcheck。
- `AGENTS.md`：给后续维护 agent 的约定（见第 9 节）。
- `CHANGELOG.md`：Keep a Changelog 格式。

---

## 8. 兼容性

| 维度 | 承诺 |
|---|---|
| HTTP API | 旧路径全部保留（语义不变或增强）；变更类新增 POST 版本 |
| 数据库 | 自动迁移，不丢数据；旧 `music.db` 直接可用 |
| 入口 | `python3 app.py`、`python3 -m xiaoai_hub`、`xiaoai-music-hub` 均可；`xiaoai_music.py` 保留为弃用壳 |
| 配置 | 旧环境变量（`XIAOAI_IP`/`PORT`/`MUSIC_DIR`）继续生效 |
| 前端 | 单页无构建，浏览器直接访问 |

---

## 9. 后续维护约定（写入 AGENTS.md）

1. 改动前先读 `docs/DESIGN.md` 与 `docs/API.md`；新增接口必须同步文档。
2. 所有外部依赖必须可注入，新增逻辑必须有测试；`make test` 全绿才能提交。
3. 提交信息遵循 Conventional Commits（feat/fix/refactor/docs/test/chore/perf）。
4. 数据库变更走 `db.MIGRATIONS` 版本化迁移，禁止直接改旧迁移。
5. 版本号在 `xiaoai_hub/__init__.py` 与 `pyproject.toml` 同步，更新 CHANGELOG。
6. 发布：提交 → 推 `origin`（gitea）→ 推 `github` 公开镜像 → 打 tag。
7. 禁止提交 `secrets/`、`music.db`、`cache/`、`.venv/`。

---

## 10. 路线图

| 里程碑 | 内容 |
|---|---|
| M1 | 基础设施（config/db/logging/errors/security）+ 测试夹具；修复启动崩溃 |
| M2 | player + watchdog 重构；修复语音暂停/停止被误判；单元测试覆盖 |
| M3 | netease / dlna / library / downloader 重构 + 测试 |
| M4 | API 重构（router/routes/鉴权/安全头/新接口）+ 集成测试 |
| M5 | 前端重写（拆分、seek、中断横幅、歌单管理、XSS 修复） |
| M6 | 工程化（pyproject/CI/deploy/MCP）+ 文档（README/ARCHITECTURE/API/AGENTS/CHANGELOG） |
| M7 | 全量回归、冒烟、代码审查、提交推送（gitea + github） |
