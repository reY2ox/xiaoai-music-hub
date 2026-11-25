# AGENTS.md — 后续维护 agent / 贡献者约定

本文件是 **xiaoai-music-hub v3** 的强制约定。修改代码或文档前请先完整阅读。
若你的改动与本文冲突，先在 `docs/DESIGN.md` 记录设计变更并说明理由，再改代码。

---

## 0. 必读顺序

1. [`docs/DESIGN.md`](docs/DESIGN.md) —— 架构、v2 问题清单、核心状态机、模块契约、路线图（**设计权威**）；
2. [`docs/API.md`](docs/API.md) —— HTTP / MCP 契约（**接口权威**）；
3. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) —— 实现结构、数据表、并发模型、错误处理；
4. [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) —— 开发环境、新增接口/迁移、测试约定、风格、发布流程；
5. [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) —— 排障手册；
6. `xiaoai_hub/config.py` 的 `ENV_DOC` —— 环境变量权威列表（文档配置表以它为准，不要臆造）；
7. 相关源码与 `tests/`（测试即行为文档）。

## 1. 硬性禁止

- **禁止提交**：`secrets/`、任何 `*.cookie`、`music.db*`、`cache/`、`.venv/`、
  `data/`（含 `deploy/data/`）、日志、`.env*`、`node_modules/`、构建产物。`.gitignore` 已覆盖，不要用 `git add -f` 绕过。
- **禁止**在代码/文档/示例中写入真实 cookie、密码、内网 IP、个人路径；示例统一用 `192.168.3.x` 占位。
- **禁止**新增第三方运行依赖（当前仅 `segno` 为运行依赖，`mutagen` 仅可选导入）：
  需要时先改 `pyproject.toml` + `requirements*.txt`，说明理由并优先标准库实现。
- **禁止**把 MCP 日志/调试信息写到 stdout：stdout 是 JSON-RPC 协议流（日志一律 stderr）。
- **禁止**破坏 v2 兼容：旧 HTTP 路径（`/play`、`/pause`、`/playlist/*`、`/media/*` 等）语义保留或增强；
  旧 `music.db` 必须能自动迁移不丢数据；`xiaoai_music.py` / `mcp_server.py` / `netease_login.py` 兼容壳必须持续可用。
- **禁止**修改已发布的数据库迁移（见第 5 节）。
- **禁止**用 `innerHTML` 拼接网易云返回的文本（XSS）；前端渲染外部数据用 `textContent` / `createElement`。
- **禁止**在监听线程 / watchdog 中执行无超时的网络请求或长阻塞操作。

## 2. 测试门槛（Definition of Done）

任何改动合入前必须：

```bash
make lint     # ruff check .            → 必须通过
make test     # pytest -q               → 必须全绿（Python 3.9 / 3.11 / 3.12）
```

- 新增/修改**核心逻辑**（`player` / `downloader` / `library` / `netease` / `dlna` / `api`）必须带测试，
  外部依赖必须可注入（构造参数或 `AppContext`），测试**不得访问真实音箱/网易云网络**；
- 修 bug 时先写一个能复现的失败测试（或说明为何难以自动复现），再修；
- 覆盖率目标：核心模块 ≥ 80%，整体 ≥ 70%（`make cov` 查看）；
- 涉及前端时 `node --check xiaoai_hub/web/app.js` 应通过；
- 改接口时 `make test` 里应有对应状态码断言（成功 + 错误）。

## 3. 提交规范（Conventional Commits）

```
<type>(<scope>): <一句话说明>
```

- `type`：`feat` / `fix` / `refactor` / `docs` / `test` / `chore` / `perf` / `build` / `ci`；
- `scope` 可选，建议用模块名：`player` / `api` / `mcp` / `docs` / `deploy` 等；
- 标题用中文或英文均可，一行内说清「做了什么」；正文解释「为什么」与影响面；
- 一个提交只做一件事，不混入无关格式化；不提交注释掉的死代码和调试 print。

示例：`fix(player): 语音停止不再被误判为播完`、
`feat(api): 新增 GET /search v2 兼容接口`、`docs: 补充播放状态机说明`。

## 4. 接口文档同步（四处/五处联动）

新增或修改接口**必须同时改**（详见 `docs/DEVELOPMENT.md` 第 3 节）：

1. `xiaoai_hub/api/routes/*.py`（路由与 handler；新文件在 `api/server.py:build_router()` 注册）；
2. `docs/API.md`（方法/参数/响应/状态码，契约先行）；
3. 前端 `xiaoai_hub/web/`（`app.js` + `index.html`，遵守零构建与安全渲染）；
4. `tests/`（成功 + 错误路径）；
5. 若暴露给语音助手：`xiaoai_hub/mcp.py` 的 `TOOLS` / `_TOOL_HANDLERS`（与 `docs/API.md` §10 保持一一对应）。

约定：查询用 GET、变更用 POST；错误抛 `xiaoai_hub.errors` 体系，由 `HubApp` 统一映射
（400/401/403/404/409/502/503/500，响应体 `{"ok": false, "error": "...", "code": "..."}`）。
环境变量新增/变更时同步 `config.ENV_DOC`、README 配置表、`deploy/xiaoai-music-hub.env.example`。

## 5. 数据库迁移策略

- 结构定义集中在 `xiaoai_hub/db.py`：`BASE_SCHEMA`（新库全量）+ `SCHEMA_VERSION` + `MIGRATIONS` + `LEGACY_COLUMNS`；
- 新增变更：`BASE_SCHEMA` 同步新结构 → `SCHEMA_VERSION += 1` → `MIGRATIONS` 追加 `(目标版本, SQL)`，
  SQL 必须幂等（`IF NOT EXISTS` / 先判断列是否存在）；
- **已发布版本的迁移禁止修改**（旧库不会重跑），需要修正就加新版本迁移；
- 迁移不得删除/重命名已有列、不得丢数据；历史 v1/v2 缺列继续走 `LEGACY_COLUMNS`；
- 同步更新 `docs/ARCHITECTURE.md` 第 4 节与 CHANGELOG；补 `tests/test_db.py`
  （旧库升级、幂等、索引存在）；
- 大表加索引用 `CREATE INDEX IF NOT EXISTS`；涉及数据回填时考虑分批、可重入。

## 6. 版本与 CHANGELOG 同步

- 版本号三处必须一致：`xiaoai_hub/__init__.py: __version__`、`xiaoai_hub/config.py: VERSION`、
  `pyproject.toml: [project].version`；
- `CHANGELOG.md` 遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)：
  `[Unreleased]` 记录开发中改动，发版时转成 `[x.y.z] - YYYY-MM-DD`，按
  `Added / Changed / Deprecated / Removed / Fixed / Security` 分类；
- 语义化版本：不兼容变更升 major（v3 → v4 时必须保留 v3 兼容壳/迁移路径并写进 CHANGELOG `Changed`）；
- 发版提交建议：`chore(release): vX.Y.Z`。

## 7. 发布推送

- 主仓：`origin` = gitea（`https://git.hawkyang.cn/reY2ox/xiaoai-music-hub.git`）；
- 公开镜像：`github`（`https://github.com/reY2ox/xiaoai-music-hub.git`），常用于 CI badge 与公开 issue；
  新克隆若没有该 remote：

  ```bash
  # SSH（已配置 GitHub SSH key 时）
  git remote add github git@github.com:reY2ox/xiaoai-music-hub.git
  # 或 HTTPS（依赖 credential helper，仓库当前使用这种方式）
  git remote add github https://github.com/reY2ox/xiaoai-music-hub.git
  ```

- 发布流程：`make lint && make test` 全绿 → 改版本号 + CHANGELOG →
  `git commit -m "chore(release): vX.Y.Z"` → `git tag -a vX.Y.Z -m "..."` →
  `git push origin main --tags` → `git push github main --tags`；
- 不要强推（`--force`）主仓/公开镜像；不要提交 tag 之外的发布产物。

## 8. 运行时与部署注意

- 环境变量优先级：代码默认值 < 环境变量 < 数据库运行时设置（音质/模式/免打扰/设备/密码）；
  `UI_PASSWORD` 环境变量优先于数据库。
- LAN IP：优先 `LAN_IP` 环境变量；多网卡 / VPN / Docker 场景**必须**显式设置，
  否则媒体 URL 可能指向音箱不可达的地址（启动日志会打印最终地址与探测来源）。
- systemd 单元：`deploy/xiaoai-music-hub.service`；环境文件 `/etc/xiaoai-music-hub.env`；
  日志走 journald（`journalctl -u xiaoai-music-hub -f`）。
- Docker：`deploy/Dockerfile`（python:3.12-slim + ffmpeg + 非 root 用户 + `/data` 卷 + `/health` 探活）、
  `deploy/docker-compose.yml`（挂载 `./data`、端口 8010）。
- `ffmpeg` 是可选但推荐依赖：无损/flac 转码、下载后写标签需要；缺失时启动只警告不阻断。
- 优雅退出：`SIGINT/SIGTERM` → 停 HTTP → 停 watchdog → 停下载 worker；新增后台线程必须在
  `shutdown()` 路径里可终止，避免 SQLite WAL 脏状态。

## 9. 遇到设计变更

1. **先改 `docs/DESIGN.md`**：在相应章节记录「为什么变、影响哪些模块/契约/数据」，再改代码；
2. 涉及模块契约（`docs/DESIGN.md` 第 5 节）时，同步修改所有实现方与测试；
3. 涉及 API 契约时同步 `docs/API.md`；涉及用户可见行为时同步 README / CHANGELOG；
4. 大改动拆成多个可独立验证的提交，并在 PR/汇报中列出验证命令与结果。

---

有问题时优先查 `docs/TROUBLESHOOTING.md`；仍无法定位再提 issue，附上版本、启动日志、
`/health` 输出与复现步骤（务必先脱敏）。
