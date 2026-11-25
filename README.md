# xiaoai-music-hub

[![CI](https://github.com/reY2ox/xiaoai-music-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/reY2ox/xiaoai-music-hub/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

把小米 / 小爱音箱当成一台普通 **DLNA 播放器（UPnP MediaRenderer）** 来用的音乐中枢：
用你自己的网易云账号听歌，不需要小爱音乐会员、不改音箱固件、不依赖米家 App。

```
浏览器 Web UI ─┐
语音助手(MCP) ─┼─ HTTP(JSON) ─►  xiaoai-music-hub  ─┬─ 网易云私有 API（搜索 / 直链 / 歌单 / 扫码登录）
curl / 脚本   ─┘                        │            │
                                        │            └─ 本地曲库（扫描 / 转码 / Range 流）
                                        ▼
                              DLNA SOAP（AVTransport / RenderingControl）
                                        ▼
                              小爱音箱（UPnP MediaRenderer）
```

## 特性

- **点歌**：搜网易云，取直链推给音箱播放；本地已有该曲时自动走本地文件（本地优先）。
- **尊重小爱语音暂停（v3 核心修复）**：你说「暂停 / 停止」后中枢**不会自动切下一首**；
  它通过「多信号 + 宽限期」区分「自然播完」与「外部暂停」，网页显示「已被语音暂停 · 继续播放」横幅，
  你说「继续播放」时从原位置续播。
- **扫码登录**：网页扫码；无头服务器可用命令行扫码登录（`netease_login.py`）。
- **歌单**：本地歌单增删改、网易云歌单导入 / 增量同步 / 全量同步，支持拖拽排序。
- **下载**：单曲或整张歌单入队下载，任务持久化、断点续传、并发上限、取消、错误分类重试，
  完成后写标签并自动入库。已下载曲目永远本地优先，不受直链过期 / 版权波动影响。
- **本地曲库**：扫描目录、增量更新；flac/m4a/wav 等自动转 mp3（需要 ffmpeg）；
  支持 HTTP `Range` / `HEAD` 流式输出。
- **播放模式**：顺序 / 列表循环 / 单曲循环 / 随机（随机模式维护历史栈，「上一首」不乒乓）。
- **多设备**：SSDP 自动发现局域网 MediaRenderer，可切换当前音箱。
- **优雅降级**：设备无 Pause 时用「记位置 + 重推流 + Seek」模拟；无时长设备有显式兜底开关。
- **免打扰**：可配置时段（默认 `23:30-08:00`），期间不自动续播。
- **语音接入**：内置 stdio MCP server（13 个工具），可被 OpenClaw 等 agent 直接调用点歌 / 控制。
- **可观测**：`GET /health`、`GET /version`、`GET /state`、结构化日志（控制台 / 可选轮转文件）。
- **工程化**：pyproject + ruff + pytest + GitHub Actions + systemd + Docker。

## 快速开始

要求：Python **3.9+**（推荐 3.11/3.12）、局域网内的小爱音箱；本地曲库转码另需 `ffmpeg`。

### 方式一：本地 venv（推荐）

```bash
git clone https://github.com/reY2ox/xiaoai-music-hub.git
cd xiaoai-music-hub

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                   # 或开发用：pip install -e ".[dev]"

export XIAOAI_IP=192.168.3.81      # 改成你的音箱 IP
python3 app.py                     # 监听 0.0.0.0:8010
```

也可以用 `make dev`（安装依赖）、`make run`（启动）、`make test`（测试），见「开发与测试」。

浏览器打开 `http://<本机局域网 IP>:8010/`，点右上角账号按钮（显示「未登录」）扫码登录网易云，然后搜歌播放。

> 首次启动会在 `DATA_DIR`（默认仓库根目录）下创建 `music.db`、`music/`、`cache/`、`secrets/`。
> 想让音箱能取到媒体流，请保证 `LAN_IP` 是音箱能访问到的本机地址；多网卡 / VPN 环境请显式设置：

```bash
export LAN_IP=192.168.3.100        # 本机在局域网中的 IP，音箱可访问
```

### 方式二：Docker

```bash
git clone https://github.com/reY2ox/xiaoai-music-hub.git
cd xiaoai-music-hub

docker build -f deploy/Dockerfile -t xiaoai-music-hub:latest .

# 镜像以 UID 10001 的非 root 用户运行，先让数据目录可写
mkdir -p data && sudo chown -R 10001:10001 data

docker run -d --name xiaoai-music-hub --restart unless-stopped \
  -p 8010:8010 \
  -v "$PWD/data:/data" \
  -e XIAOAI_IP=192.168.3.81 \
  -e LAN_IP=192.168.3.100 \
  xiaoai-music-hub:latest
```

或使用 compose（数据持久化在 `deploy/data/`，路径相对 compose 文件解析；
同样需要先 `mkdir -p deploy/data && sudo chown -R 10001:10001 deploy/data`）：

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f
```

> 容器 bridge 网络下自动探测到的是容器 IP，音箱访问不到，**必须**把 `LAN_IP`
> 显式设为宿主机的局域网 IP；镜像自带 `ffmpeg`，并通过 `/health` 做健康检查。

### 方式三：systemd（Linux 服务器常驻）

```bash
sudo mkdir -p /opt/xiaoai-music-hub
sudo rsync -a --exclude .venv --exclude .git ./ /opt/xiaoai-music-hub/
cd /opt/xiaoai-music-hub
sudo python3 -m venv .venv && sudo .venv/bin/pip install -e .

sudo useradd --system --home /opt/xiaoai-music-hub --shell /usr/sbin/nologin xiaoai || true
sudo chown -R xiaoai:xiaoai /opt/xiaoai-music-hub
sudo mkdir -p /opt/xiaoai-music-hub/data

sudo cp deploy/xiaoai-music-hub.env.example /etc/xiaoai-music-hub.env
sudo vi /etc/xiaoai-music-hub.env        # 至少改 XIAOAI_IP / LAN_IP / DATA_DIR
sudo cp deploy/xiaoai-music-hub.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xiaoai-music-hub
sudo journalctl -u xiaoai-music-hub -f
```

## 配置

配置分两类：**静态配置**（监听地址、端口、路径、超时、下载并发、`UI_PASSWORD` 等）只由环境变量决定；
**用户偏好**（音质 / 播放模式 / 免打扰 / 当前设备）存数据库，可在网页或 `POST /settings` 修改。
其中 `UI_PASSWORD` 环境变量优先于数据库值。
环境变量的权威列表见 [`xiaoai_hub/config.py`](xiaoai_hub/config.py) 的 `ENV_DOC`；systemd 可参考
[`deploy/xiaoai-music-hub.env.example`](deploy/xiaoai-music-hub.env.example)。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `XIAOAI_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `PORT` | `8010` | HTTP 端口 |
| `XIAOAI_IP` | `192.168.3.81` | 默认音箱 IP |
| `LAN_IP` | 自动探测 | 媒体 URL 使用的本机 IP，默认自动探测 |
| `DATA_DIR` | 仓库根目录 | 数据根目录（db/cache/secrets/music） |
| `DB_PATH` | `<DATA_DIR>/music.db` | SQLite 路径 |
| `MUSIC_DIR` | `<DATA_DIR>/music` | 本地曲库目录 |
| `CACHE_DIR` | `<DATA_DIR>/cache` | 转码缓存目录 |
| `SECRETS_DIR` | `<DATA_DIR>/secrets` | 凭据目录 |
| `COOKIE_FILE` | `<SECRETS_DIR>/netease.cookie` | 网易云 cookie 文件 |
| `WEB_DIR` | 包内 `web/` | 前端静态目录 |
| `UI_PASSWORD` | 空（不鉴权） | Web 访问密码（HTTP Basic Auth） |
| `REQUEST_TIMEOUT` | `10` | DLNA / 状态轮询请求超时秒数 |
| `NETEASE_BASE` | `https://music.163.com` | 网易云 API 根地址（测试可指向假服务） |
| `NETEASE_TIMEOUT` | `15` | 网易云请求超时秒数 |
| `NETEASE_ACCOUNT_TTL` | `60` | 账号信息缓存秒数 |
| `DOWNLOAD_CONCURRENCY` | `2` | 下载并发数 |
| `DOWNLOAD_MAX_RETRY` | `2` | 下载瞬时错误重试次数 |
| `DOWNLOAD_TIMEOUT` | `120` | 下载请求超时秒数 |
| `AUTO_ADVANCE_INTERVAL` | `1.5` | 播放监测轮询间隔秒 |
| `AUTO_ADVANCE_FRESH` | `120` | 判定自动续播要求最近仍在播放的秒数 |
| `END_TOLERANCE` | `8` | 判定「播到末尾」的容差秒数 |
| `AUTO_ADVANCE_UNKNOWN_DURATION` | `0` | 设备不报时长时是否兜底续播（默认关闭，避免把语音暂停误判为播完） |
| `QUIET_HOURS` | `23:30-08:00` | 默认免打扰时段 |
| `CACHE_MAX_MB` | `2048` | 转码缓存上限 MB |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `LOG_FILE` | 空（仅控制台） | 日志文件路径（5MB × 3 轮转） |

运行时设置（网页 / `POST /settings` 修改，存 `settings` 表）：`quality`、`play_mode`、
`quiet_hours`、`active_device`、`ui_password`。

## 使用说明

### 扫码登录网易云

- **有浏览器**：打开 `http://<IP>:8010/` → 点右上角账号按钮（显示「未登录」）→ 用网易云音乐 App 扫码 → 自动保存 cookie。
- **无头服务器**：

  ```bash
  python3 netease_login.py            # 等价于 python3 -m xiaoai_hub.login_cli
  ```

  终端会打印二维码文件路径（用 `scp` 拉到本地或直接在终端查看）与登录状态，
  成功后 cookie 原子写入 `DATA_DIR/secrets/netease.cookie`（权限 0600）。

  ```bash
  python3 netease_login.py --qr /tmp/qr.png --timeout 300
  ```

### 点歌

网页搜索框输入歌名 / 歌手（如「晴天」或「周杰伦 晴天」），点播放；也可直接播放本地曲库曲目。
本地已下载的曲目优先走本地文件，未命中再取网易云直链（按音质档位自动降级）。
`POST /play` 单曲播放**不挂队列、不自动续播**；从歌单进入的播放才会自动切歌。

### 歌单与同步

- 新建 / 重命名 / 删除本地歌单，拖拽排序，移除单曲；
- 从网易云导入歌单（`POST /netease/import`），或对已导入歌单做增量同步（`POST /netease/sync`）；
- 全量同步所有网易云歌单（`POST /netease/sync_all` + `GET /netease/sync_all/status`）；
- 歌单属于其它网易云账号时不会展示，也不允许播放（403）。

### 下载

- 网页上对单曲点「下载」、对歌单点「整张下载」；对应接口 `POST /download/song`、`POST /download/playlist`。
- 队列持久化在 SQLite：重启自动恢复，未完成任务保留 `.part` 断点续传；
- 并发默认 2（`DOWNLOAD_CONCURRENCY`），瞬时错误指数退避重试（`DOWNLOAD_MAX_RETRY`），
  版权 / VIP / 无直链等永久性错误直接失败不重试；
- 完成后按音质写入 ID3 标签（优先 mutagen，缺失时用 ffmpeg）并自动入库。

### 本地曲库

- 把音频放进 `MUSIC_DIR`（默认 `<DATA_DIR>/music`），网页点「扫描」或 `POST /local/scan`；
- 支持 mp3/flac/m4a/wav/ogg/aac/ape/wma；小爱不能直解的格式自动用 ffmpeg 转 mp3（缓存于 `CACHE_DIR`）；
- 媒体流支持 `Range`（206/416）与 `HEAD`，播放中可拖进度条（Seek）；
- 下载完成只做单文件增量入库，不会全目录重扫。

### 播放模式

| 模式 | 值 | 行为 |
|---|---|---|
| 顺序 | `sequential` | 播完下一首；队列尾停止 |
| 列表循环 | `loop` | 播完下一首，尾接首 |
| 单曲循环 | `single` | 自然播完重播当前曲目 |
| 随机 | `shuffle` | 随机非当前曲目，「上一首」按历史栈回退 |

### 语音 / MCP

`xiaoai_hub/mcp.py`（兼容入口 `mcp_server.py`）是一个 stdio MCP server，工具见下表；
它只调用本机音乐中枢的 HTTP API，不执行任意命令。

| 工具 | 参数 | 说明 |
|---|---|---|
| `play_music` | `query` | 搜索并播放 |
| `pause_music` / `resume_music` / `stop_music` | — | 暂停 / 继续 / 停止 |
| `next_music` / `prev_music` | — | 切歌 |
| `music_status` | — | 当前播放状态（含「被语音暂停」提示） |
| `search_music` | `query`, `limit` | 只搜索不播放 |
| `list_playlists` | — | 歌单列表 |
| `play_playlist` | `name` 或 `id` | 播放整张歌单 |
| `set_volume` | `level` | 音量 0–100 |
| `set_quality` | `quality` | `standard` / `high` / `lossless` |
| `set_mode` | `mode` | `sequential` / `loop` / `single` / `shuffle` |

## HTTP API 摘要

完整契约（参数、响应示例、状态码）见 **[docs/API.md](docs/API.md)**。
所有响应为 JSON：成功 `{"ok": true, ...}`，失败 `{"ok": false, "error": "...", "code": "..."}`。

| 分组 | 主要接口 |
|---|---|
| 系统 | `GET /health`、`GET /version`、`GET /state`、`GET/POST /settings` |
| 播放 | `GET /now`、`GET /queue`、`POST /play`、`POST /play_url`、`POST /pause`、`POST /resume`、`POST /stop`、`POST /next`、`POST /prev`、`POST /seek`、`POST /volume` |
| 歌单 | `GET /playlists`、`GET /playlist?id=`、`POST /playlist/{create,rename,delete,add,add_local,remove,reorder,play}` |
| 网易云 | `GET /account`、`POST /account/qr`、`GET /account/qr/status`、`GET /qr.png`、`POST /account/logout`、`GET /netease/playlists`、`POST /netease/{import,sync,sync_all}`、`GET /netease/sync_all/status` |
| 本地曲库 | `POST /local/scan`、`GET /local/tracks`、`POST /local/play`、`POST /local/add`、`GET /media/<id>?t=<token>` |
| 下载 | `GET /download/jobs`、`POST /download/{playlist,song,retry,cancel}` |
| 设备 | `GET /devices`、`POST /devices/scan`、`POST /devices/select` |
| 兼容 | `GET /search`、`GET/POST /pause`、`/resume`、`/stop`、`/next`、`/prev`、`/volume`、`/mode`、`/quality`、`/quiet`、`/play`、`/playlist/*`、`/netease/*`、`/local/*`、`/download/*`、`/media/*`、`/account*` 等 v2 路径 |

- 设置 `UI_PASSWORD` 后，除 `/`、静态资源、`/health`、`/version`、`/media/*` 外均需 HTTP Basic Auth；
- `/media/*` 使用媒体令牌 `?t=`（由密码 + 曲目 id 派生），供 DLNA 渲染器直接取流；
- 变更类请求（POST）会做同源校验；查询用 GET、变更用 POST。

```bash
curl -s http://127.0.0.1:8010/health | python3 -m json.tool
curl -s "http://127.0.0.1:8010/play" -X POST -H 'Content-Type: application/json' -d '{"q":"晴天"}'
curl -s "http://127.0.0.1:8010/state" | python3 -m json.tool
curl -s "http://127.0.0.1:8010/playlist/play" -X POST -H 'Content-Type: application/json' -d '{"id":1,"i":0}'
```

## MCP 接入示例

OpenClaw / 任意支持 stdio MCP 的客户端，注册一个 server 即可：

```json
{
  "mcp": {
    "servers": {
      "music-hub": {
        "transport": "stdio",
        "command": "python3",
        "args": ["/opt/xiaoai-music-hub/mcp_server.py"],
        "env": {
          "MUSIC_HUB_URL": "http://127.0.0.1:8010",
          "MUSIC_HUB_PASSWORD": ""
        }
      }
    }
  }
}
```

- `MUSIC_HUB_URL`：音乐中枢地址，默认 `http://127.0.0.1:8010`；
- `MUSIC_HUB_PASSWORD`：可选，与中枢 `UI_PASSWORD` 一致即可（HTTP Basic Auth，用户名任意）。

调试（直接喂协议行）：

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 mcp_server.py
```

## 常见问题（FAQ）

**1. 我对小爱说「暂停」，为什么中枢不自动播下一首？**
这是 v3 的预期行为：小爱语音暂停在多数设备上表现为 `STOPPED`，和「自然播完」状态相同。
v3 只有在拿到「确实播到末尾」的正证据时才续播，否则判定为**外部暂停**并记录中断态：
网页显示「音箱已被语音/其他控制器暂停 · 继续播放」，说「继续播放」（或点横幅、`POST /resume`）
会从原位置续播。若你希望旧行为，可显式设置 `AUTO_ADVANCE_UNKNOWN_DURATION=1`（不推荐）。

**2. VIP 曲目 / 版权曲目播不了？**
- 需要网易云账号本身有 VIP；cookie 失效时网易云返回 `-110`，重新扫码登录即可；
- 无版权 / 已下架曲目拿不到直链，接口返回 `409 {"code":"no_url"}`，语音侧会提示换一首；
- 已下载到本地的曲目不受影响（本地优先）。

**3. 音箱不可达 / 接口 503？**
确认音箱与中枢在同一局域网、IP 正确（`XIAOAI_IP` / 网页设备页）、音箱已开启「DLNA / 局域网播放」类选项
（不同固件叫法不同；部分型号需在小爱 App 里开启）。也可以 `curl http://<音箱IP>:9999/description.xml`
粗略判断 9999 端口是否可达。

**4. 播放本地曲库没有声音 / 转码失败？**
- 需要安装 `ffmpeg`（`apt install ffmpeg` / `brew install ffmpeg`）；缺省时 flac/m4a/wav 无法转 mp3；
- 检查 `LAN_IP`：媒体 URL 拼的是这个地址，音箱必须能访问；多网卡 / VPN 环境务必显式设置。

**5. 端口 8010 被占用？**
换端口启动：`PORT=8011 python3 app.py`，并同步修改 MCP 的 `MUSIC_HUB_URL` 与反向代理配置。

**6. 局域网 IP 探测错误 / 有 VPN？**
v3 优先用 `LAN_IP` 环境变量，其次对音箱 IP 做 UDP 路由探测；但多网卡、VPN（utun）环境仍可能选错。
请在启动前显式 `export LAN_IP=192.168.x.x`（Docker 部署填宿主机 IP），启动日志会打印最终选中的地址与依据。

**7. 旧的 `xiaoai_music.py` 还能用吗？**
能启动但会向 stderr 打印弃用提示，它只是转发到新入口。请改用 `python3 app.py` /
`python3 -m xiaoai_hub` / `xiaoai-music-hub`；旧的环境变量（`XIAOAI_IP` / `PORT` / `MUSIC_DIR`）继续生效，
旧 `music.db` 自动迁移不丢数据。

更多排障见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

## 开发与测试

```bash
make venv      # 创建 .venv
make dev       # 安装运行 + 开发依赖（pytest / pytest-cov / ruff）
make test      # pytest（tests/）
make cov       # 覆盖率报告
make lint      # ruff check
make fmt       # ruff check --fix + ruff format
make run       # 启动服务
make clean     # 清理构建与缓存产物
```

也可以直接：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
```

测试完全离线：`tests/fakes.py` 提供可脚本化的假 DLNA 渲染器、假网易云、假曲库与假时钟，
`tests/conftest.py` 的 `tmp_hub` 夹具为每个用例创建独立数据目录和 SQLite。
开发约定见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)，架构细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 目录结构

```
.
├── app.py                     # 正式入口（薄壳，转发 xiaoai_hub.app:main）
├── xiaoai_music.py            # v2 旧入口兼容壳（弃用提示）
├── mcp_server.py              # MCP stdio 兼容壳（实现见 xiaoai_hub/mcp.py）
├── netease_login.py           # 命令行扫码登录兼容壳
├── xiaoai_hub/                # 主程序包
│   ├── app.py                 # 组装依赖、启动 HTTP、优雅退出
│   ├── config.py              # 静态配置（环境变量 → Settings）
│   ├── db.py                  # SQLite、Schema、版本化迁移
│   ├── prefs.py               # 运行时偏好（音质/模式/免打扰/设备/密码）
│   ├── logging_setup.py       # 日志初始化
│   ├── errors.py              # 异常体系 → HTTP 状态码
│   ├── security.py            # 媒体令牌 / Basic Auth / 同源校验
│   ├── netease.py             # 网易云接口（会话/缓存/重试/降级）
│   ├── dlna.py                # UPnP 渲染器（SOAP/发现/能力探测）
│   ├── library.py             # 曲库扫描/标签/转码/流式输出
│   ├── downloader.py          # 下载队列（持久化/并发/续传/取消/错误分类）
│   ├── player.py              # 播放调度 + watchdog 状态机
│   ├── mcp.py                 # MCP stdio server
│   ├── login_cli.py           # 命令行扫码登录
│   ├── api/                   # HTTP 接口层（router/server/context/routes）
│   └── web/                   # 前端（index.html + app.js + style.css，零构建）
├── deploy/                    # systemd / Dockerfile / docker-compose / env 示例
├── docs/                      # DESIGN.md / API.md / ARCHITECTURE.md / DEVELOPMENT.md / TROUBLESHOOTING.md
├── tests/                     # pytest（fixtures + fakes，全离线）
├── pyproject.toml             # 项目元数据 / 依赖 / ruff / pytest 配置
├── requirements*.txt          # 运行 / 开发依赖
├── Makefile                   # 常用开发命令
├── AGENTS.md                  # 后续维护 agent 约定
└── CHANGELOG.md               # 变更日志（Keep a Changelog）
```

## 免责声明

- 本项目通过**网易云音乐的非公开（私有）接口**获取搜索、播放直链与歌单数据，
  接口随时可能变更、限流或失效，因此**不保证长期可用**；
- 仅供**个人学习与自用**：请使用你自己的账号，不要对外提供服务，不要用于商业用途或批量抓取；
- 请尊重版权，支持正版音乐；VIP / 付费内容请通过官方渠道购买；
- 使用本项目（包括因接口变更、账号风控、设备兼容等）产生的一切后果由使用者自行承担，作者不承担任何责任。

## 许可证

[MIT](LICENSE)
