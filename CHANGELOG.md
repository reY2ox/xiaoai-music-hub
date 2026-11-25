# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [3.0.0] - 2026-11-25

v3 是对 v2 的全面重构：拆分 `xiaoai_hub/` 包、重写播放状态机、补齐工程化与测试、
新增 MCP 语音接入与部署形态。HTTP API 与数据库保持向后兼容（旧路径保留、旧库自动迁移）。

### Added

- **播放状态机**：区分「自然播完」与「外部暂停/停止」，新增 `interrupted` 中断态、
  网页中断横幅与「继续播放」恢复（`resumed: pause|replay|already`）。
- **新 HTTP 接口**：`GET /health`、`GET /version`、`GET /state`、`GET /queue`、`POST /seek`、
  `POST /volume`、`POST /settings`、`POST /playlist/reorder`、`GET /search`（v2 兼容）。
- **MCP 语音接入**：`xiaoai_hub/mcp.py`（stdio JSON-RPC，协议 `2024-11-05`）提供 13 个工具
  （点歌 / 暂停 / 继续 / 停止 / 切歌 / 状态 / 搜索 / 歌单 / 音量 / 音质 / 模式），
  `mcp_server.py` 保留为兼容入口；`MUSIC_HUB_URL` / `MUSIC_HUB_PASSWORD` 可配置。
- **命令行扫码登录**：`xiaoai_hub/login_cli.py`（`netease_login.py` 兼容壳），无头服务器可把二维码
  文件拉回本地扫码，cookie 原子写入且权限 0600。
- **工程化**：`pyproject.toml`（setuptools、console script、ruff、pytest 配置）、
  `requirements.txt` / `requirements-dev.txt`、`Makefile`、GitHub Actions CI（3.9/3.11/3.12）、
  systemd 单元、`deploy/Dockerfile` + `docker-compose.yml`（ffmpeg、非 root、`/data` 卷、healthcheck）。
- **文档**：重写 `README.md`，新增 `docs/ARCHITECTURE.md`、`docs/DEVELOPMENT.md`、
  `docs/TROUBLESHOOTING.md`、`AGENTS.md`、本 CHANGELOG。
- **下载完成态**：先完成入库（`scan_file` / `song_key` / `tracks.local_id` 回填）再标记 `done`，
  避免轮询方看到「已完成但尚未入库」的中间态。
- **下载**：内容校验（`Content-Type` + 文件魔数）、错误分类（永久性不重试 / 瞬时性指数退避）、
  可取消（内存标记 + DB 标记，256KB 粒度检查）、`.part` 断点续传、启动恢复、完成后单文件增量入库。
- **本地曲库**：增量扫描（`(path,size,mtime)`）、单文件 `scan_file()`、可选 prune、
  转码缓存按 `track_id+mtime` 失效与 LRU 清理、`Range`/`HEAD`/`416` 流式输出、正确 MIME。
- **网易云**：账号信息 TTL 缓存、音质自动降级（`lossless → high → standard`）、
  歌单超过 1000 首分页拉全、扫码登录与轮询 cookie 一致性、错误码到中文文案映射。
- **DLNA**：从 `description.xml` 解析 `AVTransport`/`RenderingControl` 控制 URL（失败回退 9999 端口）、
  能力探测缓存（`supports_pause`/`supports_seek`）、SSDP 发现去重、强类型 `PositionInfo`。
- **数据库**：`SCHEMA_VERSION=3`、`play_history` 表、`download_jobs.attempts/cancel_requested` 列、
  常用索引；v1/v2 旧库自动迁移不丢数据。
- **前端**：拆分为 `index.html` + `app.js` + `style.css`（零构建），`/state` 单请求轮询、
  进度条拖拽 Seek、外部中断横幅、歌单增删改/拖拽排序、下载队列、本地曲库、设备/音质/模式/免打扰设置、
  移动端适配。

### Changed

- 入口统一为 `python3 app.py` / `python3 -m xiaoai_hub` / console script `xiaoai-music-hub`；
  `xiaoai_music.py` 降级为打印弃用提示的兼容壳。
- 目录结构从 `core/` 迁移到 `xiaoai_hub/`（基础层 / 能力层 / 领域层 / 接口层 / 适配层），
  入口只做组装，业务逻辑全部在包内且可注入测试。
- 配置统一为「代码默认值 < 环境变量 < 数据库运行时设置」，环境变量权威列表收敛到
  `config.ENV_DOC`；新增 `LAN_IP`、`DATA_DIR`、`DB_PATH`、`CACHE_DIR`、`SECRETS_DIR`、
  `COOKIE_FILE`、`UI_PASSWORD`、`NETEASE_*`、`DOWNLOAD_*`、`AUTO_ADVANCE_*`、`END_TOLERANCE`、
  `QUIET_HOURS`、`CACHE_MAX_MB`、`LOG_LEVEL`、`LOG_FILE` 等。
- API 层改为 `router/routes` 结构：查询用 GET、变更用 POST；旧 GET 变更接口保留并标记兼容；
  统一错误体 `{"ok": false, "error": "...", "code": "..."}` 与状态码（400/401/403/404/409/502/503/500）。
- MCP 工具从 6 个扩展到 13 个，并与 `docs/API.md` §10 一一对应。
- 日志从 `print` 改为标准 `logging`（控制台 + 5MB×3 轮转文件），访问日志与关键事件 INFO。
- README / 设计文档 / API 文档 / 部署文档全面重写，明确「尊重小爱语音暂停」行为与排障手册。

### Fixed

修复 v2 问题清单（`docs/DESIGN.md` 第 2 节）中的主要问题：

- **P0**
  - 全新安装启动崩溃：`Player` 改为懒加载单例，`init_schema()` 先于任何领域对象创建（`no such table: settings`）。
  - **小爱语音暂停被误判为播完**：watchdog 不再「见到 `STOPPED` 就下一首」，改为多信号 + 宽限期判定；
    外部暂停/停止进入中断态、不抢播，用户继续时从原位置恢复。
  - 局域网地址探测错误：优先 `LAN_IP`，其次对**音箱 IP** 做 UDP 路由探测，排除 127/169.254/utun/CGNAT，
    启动日志打印最终地址与来源。
  - 媒体令牌失效：`player.play_track()` 生成的媒体 URL 显式携带 `security.media_token()`，
    设置 `ui_password` 后本地播放不再 403。
  - 下载文件可能是 HTML：校验 `Content-Type` 与文件魔数（ID3/`\xff\xfb`/fLaC/OggS/ftyp/RIFF），不匹配即失败。
- **P1**
  - `/play` 后置 `pl.active=False` 的队列语义混乱：单曲播放（`playlist_id=None`）与歌单队列分离，
    队列播放才会自动续播。
  - 前端 `dlPl()` 重复定义、引用不存在节点导致的 JS 报错：前端重写为三文件并修复。
  - 前端调用未实现的 `/local/download*` 死接口：统一走 `/download/*` 接口与下载队列 UI。
  - 无 Seek：新增 `POST /seek` 与可拖拽进度条。
  - `HEAD` 501：`HubRequestHandler` 实现 `do_HEAD`，媒体流支持 HEAD。
  - `playlist_allowed()` 每次请求实时打网易云：账号信息加 TTL 缓存（默认 60s，登录/登出失效）。
  - 播放模式/暂停恢复竞态（`_lock` 建而不用）：`Player._lock`（RLock）保护全部状态变更。
  - 随机模式「上一首」两首乒乓：改为历史栈 `push/pop`。
  - 队列结束卡在 `ADVANCING`：自然结束 `active=False` 并回到 `STOPPED`，之后仍可重新播放。
  - 下载取消无效：内存取消集合 + DB `cancel_requested` 双标记，下载循环定期检查并清理 `.part`。
  - 下载并发超限/线程泄漏：`_running_workers` 改为**线程启动前**加锁 +1、`finally` -1。
  - 每下载完一首全目录扫描（O(n²)）：改为 `scan_file()` 单文件增量入库。
  - VIP/无版权错误无意义重试：错误分类，永久性错误直接失败。
  - MCP 对 HTTP 409 直接抛异常：显式捕获 `HTTPError`，409/404/503 → 友好中文提示。
  - 歌单超过 1000 首被截断：分页拉全。
  - 退出登录写空 cookie：删除 cookie 文件，`cookie()` 回退 `os=pc`。
  - 缺少 `/health`、`/version`、`/state`：新增并可观测（设备可达性、DB、下载计数、账号）。
  - 音箱不可达返回 500 + `URLError` repr：统一映射 `503 {"code":"device_unavailable"}`。
- **P2**
  - 双入口混乱：`xiaoai_music.py` 兼容壳 + `app.py` 薄入口，路径不再硬编码 `/root/...`。
  - `netease_login.py` cookie 路径与配置不一致：统一走 `SECRETS_DIR/COOKIE_FILE`，命令行登录可用。
  - 缺 `requirements.txt` / `pyproject.toml` / 测试 / CI / lint：全部补齐。
  - 日志全靠 `print`、`log_message` 被吞：标准 logging + 访问日志。
  - 配置散落、优先级未定义：`config.Settings` + `ENV_DOC` 统一。
  - 500 行 if/elif、GET 有副作用：router/routes 拆分，变更改 POST 并做同源校验。
  - 前端 `innerHTML` 拼接外部数据（XSS 风险）：改用 `textContent`/`createElement` 安全渲染。
  - 无优雅退出：处理 SIGINT/SIGTERM，依次停 HTTP、watchdog、下载 worker。
  - 数据库无索引：补齐 `tracks/local_tracks/download_jobs/play_history/playlists` 索引。

### Deprecated

- `xiaoai_music.py`：v2 单体入口，保留为打印弃用提示的兼容壳，后续大版本将移除。
- 变更类 GET 接口（`GET /play`、`GET /pause`、`GET /playlist/delete` 等）：仅为 v2 兼容保留，
  新代码请使用对应 POST 接口。

### Removed

- 移除 v2 `core/` 单体实现（能力并入 `xiaoai_hub/`）；`xiaoai_music.py` 仅保留兼容壳。
- 移除前端对不存在接口（`/local/download*`）的调用与重复的 `dlPl()` 定义。

### Security

- 可选 HTTP Basic Auth（`UI_PASSWORD` / 数据库设置），密码比较使用 `hmac.compare_digest`。
- `/media/*` 使用由密码 + 曲目 id 派生的短令牌，DLNA 渲染器无需携带 Basic Auth。
- 变更类请求做同源校验（`Origin/Referer` 与 `Host` 不一致即 403），降低 CSRF 风险。
- 安全响应头：`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`X-Frame-Options: DENY`。
- 前端对外部数据统一转义，修复 v2 的潜在 XSS。
- cookie 原子写入（临时文件 + `os.replace`）且权限 0600；`secrets/`、`*.cookie`、`music.db*`、
  `.venv/` 等加入 `.gitignore`，仓库不提交任何凭据。
- Docker 镜像以非 root 用户运行，仅暴露 `/data` 卷。

## [2.0.0] - 2026-11-23

### Added

- 模块化拆分 `core/`（config/dlna/netease/library/downloader/player/api）。
- 音质档位控制（standard/high/lossless）与下载队列（持久化、并发、重试、断点续传）。
- 本地优先播放、静音时段、外部暂停降级恢复。

### Fixed

- 本地优先播放；忽略重复下载任务（2026-11-25）。

## [1.0.0] - 2026-11-20

### Added

- 初始版本：DLNA 发现与播放控制、网易云搜索/直链/歌单/扫码登录、单文件 Web UI（`xiaoai_music.py`）。

---

[Unreleased]: https://github.com/reY2ox/xiaoai-music-hub/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/reY2ox/xiaoai-music-hub/releases/tag/v3.0.0
[2.0.0]: https://github.com/reY2ox/xiaoai-music-hub/releases/tag/v2.0.0
[1.0.0]: https://github.com/reY2ox/xiaoai-music-hub/releases/tag/v1.0.0
