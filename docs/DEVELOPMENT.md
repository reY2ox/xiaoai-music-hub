# 开发指南（v3）

> 先读 [DESIGN.md](DESIGN.md)（设计决策）与 [API.md](API.md)（HTTP 契约），
> 再看 [ARCHITECTURE.md](ARCHITECTURE.md)（模块边界与状态机）。
> 维护 agent 的强制约定见根目录 [AGENTS.md](../AGENTS.md)。

---

## 1. 环境准备

要求：**Python 3.9+**（CI 覆盖 3.9 / 3.11 / 3.12）、git；本地曲库转码另需 `ffmpeg`（可选）。

```bash
git clone https://github.com/reY2ox/xiaoai-music-hub.git
cd xiaoai-music-hub

make venv        # python3 -m venv .venv && pip install -U pip
make dev         # pip install -e ".[dev]"（pytest / pytest-cov / ruff）
.venv/bin/python -m pytest -q      # 冒烟：测试应全绿
.venv/bin/python -m ruff check .   # 冒烟：lint 应通过
```

约定：

- 所有命令走仓库内 `.venv`（Makefile 里 `PY := .venv/bin/python`），不要污染系统 Python；
- `.venv/`、`data/`、`secrets/`、`music.db`、`cache/` 都不入 git；
- 本地跑服务时建议设置 `LAN_IP`（多网卡 / VPN 环境必设），避免媒体 URL 拼错：

```bash
LAN_IP=192.168.3.100 XIAOAI_IP=192.168.3.81 .venv/bin/python app.py
# 或：.venv/bin/python app.py --lan-ip 192.168.3.100 --speaker 192.168.3.81 --verbose
```

`app.py` 支持的参数：`--host --port --music-dir --data-dir --speaker --lan-ip
--log-level --verbose --version`（内部写回同名环境变量后统一走 `config.load()`）。

## 2. make 目标

| 目标 | 等价命令 | 说明 |
|---|---|---|
| `make venv` | `python3 -m venv .venv` + 升级 pip | 创建虚拟环境 |
| `make install` | `pip install -e .` | 安装运行依赖 |
| `make dev` | `pip install -e ".[dev]"` | 安装运行 + 开发依赖 |
| `make run` | `.venv/bin/python app.py` | 启动服务（:8010） |
| `make test` | `.venv/bin/python -m pytest` | 跑测试（`pyproject.toml` 已配 `testpaths=tests`、`-q`） |
| `make cov` | `pytest --cov=xiaoai_hub --cov-report=term-missing` | 覆盖率 |
| `make lint` | `ruff check .` | 静态检查 |
| `make fmt` | `ruff check --fix .` + `ruff format .` | 自动修复/格式化 |
| `make clean` | 删除 build/dist/缓存 | 清理产物（不动 `.venv` 与数据） |
| `make docker` | `docker build -f deploy/Dockerfile ...` | 构建镜像 |

每个目标都声明了自身依赖，可单独执行；`test/cov/lint/fmt` 会自动确保 `dev` 依赖已安装。

## 3. 如何新增一个 HTTP 接口（四处同步）

接口层的注册方式是「路由模块内定义 handler + `register(router, ctx)`」：

```
xiaoai_hub/api/router.py      Request/Response/Router（不要在这里加业务）
xiaoai_hub/api/routes/*.py    按资源拆分：system/player/playlists/netease/local/download
xiaoai_hub/api/server.py      build_router() 统一注册（新增文件要在这里挂上）
```

**新增接口必须同步修改四处（缺一不可，PR 会被要求补齐）：**

1. **路由模块**：在对应 `xiaoai_hub/api/routes/<resource>.py` 里写 handler 并 `router.add/get/post`。
   参考现有 handler 的写法：

   ```python
   def register(router: Any, ctx: Any) -> None:
       def my_endpoint(req: Request) -> Response:
           name = req.require("name")                 # 缺参 → 400
           limit = min(50, req.int_param("limit", 10))  # query / JSON / form 都能读
           value = ctx.some_service.do(name, limit)   # 通过 ctx 访问领域层，不 import 具体实现
           return Response.json({"ok": True, "value": value})

       router.post("/my/endpoint", my_endpoint)
       router.get("/my/endpoint", my_endpoint, mutation=True)  # 兼容旧 GET 变更接口时才加
   ```

   规则：
   - 查询用 GET，变更用 POST；变更类请求会由 `HubApp` 统一做同源校验；
   - 业务错误直接抛 `xiaoai_hub.errors` 下的异常（`BadRequest/NotFound/Conflict/DeviceUnavailable/...`），
     状态码与 `{"ok": false, ...}` 由 `HubApp.handle()` 统一映射，handler 里不要手写 try/except 包 HTTP 状态；
   - 需要免鉴权的接口（探活/静态资源/媒体流）用 `auth=False`；
   - 新增文件时在 `xiaoai_hub/api/server.py: build_router()` 中 import 并 `register(router, ctx)`。

2. **`docs/API.md`**：补接口契约（方法、路径、参数表、成功/失败响应示例、状态码）。
   契约是前端与 MCP 的唯一依据，先改文档再改代码也可以。

3. **前端 `xiaoai_hub/web/`**：`app.js` 增加调用（`fetch` + 错误 toast），必要时 `index.html` 加控件。
   渲染外部数据必须用 `textContent` / `createElement`，**禁止 `innerHTML` 拼接**（XSS 风险）。

4. **测试 `tests/test_api.py`**：覆盖成功路径 + 至少一个错误状态码（参数缺失 / 权限 / 状态冲突）。
   尽量用真实 `HubApp` + 假 `ctx`（见第 5 节），不要启动真实音箱/网易云。

> 如果该接口还要暴露给语音助手，需**第五处**：`xiaoai_hub/mcp.py` 的 `TOOLS` + `_TOOL_HANDLERS`
> 增加对应工具（工具描述要写给语音模型看，参数 schema 必填项准确）。

## 4. 如何新增数据库迁移

Schema 在 `xiaoai_hub/db.py`。迁移必须**向前兼容、不丢旧数据**，策略是「基线全量 + 版本化增量」：

1. 新增列/表时：
   - 修改 `BASE_SCHEMA`（保证全新安装直接建出完整 vN 结构）；
   - `SCHEMA_VERSION += 1`；
   - 在 `MIGRATIONS` 追加 `(目标版本, SQL)`，SQL 用 `ALTER TABLE ... ADD COLUMN ...` 或 `CREATE TABLE/INDEX`；
   - 旧版本（v1/v2）缺失的历史列继续用 `LEGACY_COLUMNS` 补齐，而不是塞进 `MIGRATIONS`。
2. 迁移 SQL 只允许幂等/可重复执行（`CREATE TABLE IF NOT EXISTS`、`ADD COLUMN` 前先判断）；
   迁移在 `init_schema()` 中按 `PRAGMA user_version` 顺序执行，每个版本一次。
3. **禁止修改已发布的旧迁移**（已升级的库不会重跑，改了等于没生效）；
   需要修正时新增一个更高版本的迁移。
4. 同步文档：`docs/ARCHITECTURE.md` 第 4 节表结构、CHANGELOG 的 `Changed`。
5. 测试：在 `tests/test_foundation.py`（DB/基础层用例）增加「旧库升级后数据完整 +
   重复调用 `init_schema()` 幂等 + 索引存在」。

```python
# db.py 示例
SCHEMA_VERSION = 4
MIGRATIONS = [
    (4, "ALTER TABLE download_jobs ADD COLUMN speed REAL DEFAULT 0;"),
]
```

## 5. 测试约定

框架：pytest（配置在 `pyproject.toml`）。测试目录：

```
tests/
├── conftest.py    # tmp_hub / fake_clock / fake_renderer / fake_netease / fake_library / player 夹具
├── fakes.py       # FakeClock、FakeRenderer、FakeNetease、FakeLibrary、FakeSong... 替身
└── test_*.py      # 按模块划分
```

### 5.1 核心夹具

- **`tmp_hub`**：为每个用例创建独立的 `data/`（含 db）与 `music/` 目录，
  自动 `config.reload(env=...)` + `db.configure()` + `db.init_schema()`，结束时重置单例。
  需要数据的测试直接用 `tmp_hub.db_path` / `tmp_hub.music_dir`。
- **`fake_clock`**：可 `advance(seconds)` 的单调时钟，让 watchdog 的宽限期/新鲜度判定可复现。
- **`fake_renderer`**：可脚本化的 DLNA 渲染器（`FakeRenderer`），提供
  `tick()/finish()/voice_pause()/voice_stop()/takeover()` 以及 `actions()` 断言收到哪些 SOAP 指令。
- **`fake_netease`**：假网易云（搜索/直链/账号/歌单），可配置 `fail_with`、`song_urls`。
- **`fake_library`**：假曲库，默认无本地文件（强制走在线直链），可手工塞 `song_map`。
- **`player`**：已注入上述替身的 `Player`（`player.player`），**不启动 watchdog 线程**，
  测试里手动调用 `poll_once()` 驱动状态机。

### 5.2 关键用例的写法

```python
def test_voice_pause_does_not_advance(player):
    p, renderer = player.player, player.renderer
    # 用假网易云/假曲库让播放器进入“歌单第 0 首正在播放”
    p.play_track({"id": 1, "name": "A", "song_id": 1, "duration": 180},
                 playlist_id=1, index=0)
    renderer.tick(30)
    p.poll_once()                        # 记录一次 PLAYING
    before = len(renderer.commands)      # 此刻已下发的 SOAP 指令数

    renderer.voice_pause()               # 用户对小爱说“暂停”
    p.poll_once()                        # PLAYING → PAUSED_PLAYBACK → 中断态

    assert len(renderer.commands) == before               # 不应重新推流/切歌
    assert p.snapshot()["player"]["interrupted"]["reason"] == "external_pause"
    assert p.snapshot()["player"]["queue"]["index"] == 0  # 队列指针没动
```

- 自然播完：`renderer.finish()` + 连续两次 `p.poll_once()`，断言收到新的 `play_url`；
- 瞬时抖动：停止一次后立刻 `renderer.voice_resume()`，再 `poll_once()`，断言无中断、无 advance；
- 指令静默期：刚 `play_track()` 后立刻 `voice_stop()`，断言 3s 内不判中断；
- 随机上一首：构造多曲队列，连续手动 next/prev，断言不乒乓且 `history` 符合预期。

### 5.3 规则

- **测试不联网**：假网易云要么注入 `FakeNetease`，要么把 `NETEASE_BASE` 指向本地假 HTTP 服务
  （`tmp_hub.env` 默认已指向 `http://127.0.0.1:9`）；不访问真实音箱；不依赖本机安装 ffmpeg
  （ffmpeg 相关用例用 `pytest.importorskip` 思路：`shutil.which("ffmpeg")` 为 None 时 `pytest.skip`）。
- 新增外部依赖必须可注入（构造参数或 `ctx`），否则无法测试；
- 每个用例自带临时目录，**不允许共享全局状态**；涉及单例（`config.get()`、`player.get_player()`、
  `db.configure()`）的用例结束要复位；
- 测试文件命名 `test_<module>.py`，函数命名 `test_<行为>_<期望>`；中文 docstring 说明场景；
- 时间相关禁止 `time.sleep`，用 `fake_clock` / 注入的 `sleep` 替身。

### 5.4 覆盖率

```bash
make cov     # 终端报告；HTML 报告：pytest --cov=xiaoai_hub --cov-report=html
```

目标：核心模块（`player/library/downloader/netease/dlna`）≥ 80%，整体 ≥ 70%。
新增/修改核心逻辑必须带测试，`make test` 全绿才能提交。

## 6. 代码风格

- **Python 3.9 兼容**：不用 `match`、运行时 `X | Y` 类型（注解可用
  `from __future__ import annotations` + `typing.Optional/List/Dict`）；f-string 可用。
- **ruff**：`line-length=100`、`target-version=py39`、`select=["E","F","W","I"]`、忽略 `E501`
  （见 `pyproject.toml`）。提交前 `make lint`；格式化用 `make fmt`（ruff format）。
- 每个模块文件头写中文 docstring：职责、边界、关键设计；复杂逻辑解释「为什么」而不是「做了什么」。
- 异常：领域层抛 `errors.HubError` 子类，消息面向用户；内部细节放 `details` 只进日志。
- 日志：统一 `logging_setup.get_logger(__name__)`；**禁止 print**（仅入口兼容壳/CLI 交互可用）；
  不吞异常，至少 `log.warning/debug` 记录。
- 线程：共享状态必须加锁（参考 `Player._lock`）；watchdog 等后台线程内不允许长耗时网络操作；
  线程数量在启动前计数（参考 `downloader.kick()` 的修复）。
- 外部数据（歌名/歌手/歌词/URL）进入前端前一律转义或用 `textContent`；媒体 URL 必须带 token。
- 不引入新的第三方运行依赖（`segno` 之外）；确实需要时先改 `pyproject.toml` /
  `requirements*.txt` 并说明理由，优先标准库实现。

## 7. 版本、CHANGELOG 与发布

1. **版本号三处同步**：`xiaoai_hub/__init__.py: __version__`、`xiaoai_hub/config.py: VERSION`、
   `pyproject.toml: project.version`（当前 `3.0.0`）。
2. **CHANGELOG**：遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
   每个版本按 `Added / Changed / Deprecated / Removed / Fixed / Security` 分类，未发布改动写进
   `[Unreleased]`；链接格式 `[x.y.z]: https://github.com/.../compare/vX.Y.Z...vX.Y.Z`。
3. **提交信息**：Conventional Commits —— `feat: ...` / `fix: ...` / `refactor: ...` /
   `docs: ...` / `test: ...` / `chore: ...` / `perf: ...`，一行标题 + 必要时正文。
4. **发布流程**：

   ```bash
   make lint && make test          # 1. 本地全绿
   # 2. 改版本号 + CHANGELOG（[Unreleased] → 新版本段落）
   git add -A && git commit -m "chore(release): v3.0.0"
   git tag -a v3.0.0 -m "xiaoai-music-hub v3.0.0"
   git push origin main --tags     # origin = gitea（主仓）
   git push github main --tags     # github = 公开镜像（若未添加：git remote add github ...）
   ```

5. 发布前检查：CI 通过（ruff + pytest 3.9/3.11/3.12）、`docker build` 可成功、
   `deploy/xiaoai-music-hub.service` 路径与实际部署一致、README/CHANGELOG 已更新。

## 8. 排障入口

- 先看 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)（症状 → 原因 → 解决）；
- 服务日志：默认控制台（stderr），`LOG_FILE` 可开轮转文件；systemd 用
  `journalctl -u xiaoai-music-hub -f`；调试加 `--verbose`；
- 可观测接口：`GET /health`（设备可达/DB/下载计数）、`GET /version`、`GET /state`；
- 复现问题时尽量给出：启动日志（含 LAN IP 来源）、`/health` 输出、音箱型号与固件、
  网易云账号状态（`GET /account`）、相关错误码。

## 8. 前端 UI 冒烟（可选，人工/agent 发布前检查）

前端零构建，但也容易因为「改了接口/改了 DOM」出现跨层回归。除 `tests/test_frontend.py` 的静态校验
（DOM id、接口路径、innerHTML/CDN 禁用、`node --check`）外，建议在有 Node 的环境做一次真实渲染冒烟：

```bash
# 1. 安装一次性依赖（不进 requirements，node_modules 已在 .gitignore）
npm install --no-save --no-package-lock jsdom

# 2. 启动一个测试实例（独立数据目录与端口，避免污染本机数据）
DATA_DIR=/tmp/hub-ui MUSIC_DIR=/tmp/hub-ui/music PORT=8012 \
  XIAOAI_IP=127.0.0.1 LAN_IP=127.0.0.1 python3 app.py &

# 3. 跑冒烟：验证首屏无 JS 报错、6 个标签页、搜索出结果、
#    「小爱语音暂停」中断横幅展示与「继续播放」按钮发出 POST /resume、音质切换
HUB_BASE=http://127.0.0.1:8012 node scripts/ui_smoke.mjs
```

退出码 0 表示通过；失败会打印 JSON 结果（含错误列表与请求记录）。
