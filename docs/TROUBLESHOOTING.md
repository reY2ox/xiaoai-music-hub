# 排障手册（Troubleshooting）

> 本文按「症状 → 可能原因 → 排查/解决」组织。先看第 1 节的快速自检，再对号入座。
> 设计原理见 [ARCHITECTURE.md](ARCHITECTURE.md)，接口见 [API.md](API.md)。

---

## 1. 快速自检（30 秒）

```bash
# 服务活着吗？音箱可达吗？下载/DB 是否正常？
curl -s http://127.0.0.1:8010/health | python3 -m json.tool

# 当前播放状态（含 interrupted 外部中断信息）
curl -s http://127.0.0.1:8010/state | python3 -m json.tool

# 网易云登录态
curl -s http://127.0.0.1:8010/account | python3 -m json.tool

# 启动日志（重点看「媒体 URL 地址」「监听地址」「音箱默认地址」三行）
journalctl -u xiaoai-music-hub -n 100 --no-pager     # systemd
# 或前台运行：python3 app.py --verbose
```

| 检查项 | 正常表现 |
|---|---|
| `GET /health` | `ok: true`，`device.reachable: true`，`db.ok: true` |
| 启动日志 | 打印 `媒体 URL 地址 <局域网IP>（来源：...）` |
| `GET /state` | `player.device.reachable: true`，`player.interrupted` 通常为 `null` |
| `GET /account` | `logged_in: true`（VIP 曲目还需要账号本身有 VIP） |

---

## 2. 常见问题表

### 2.1 音箱 / 媒体

| 症状 | 可能原因 | 排查与解决 |
|---|---|---|
| 调用播放接口返回 `503 {"code":"device_unavailable"}`；`/health` 里 `device.reachable=false` | 音箱离线 / IP 写错 / 中枢与音箱不在同一网段 / 路由器 AP 隔离 / 音箱未开启 DLNA | ① `ping <音箱IP>`、`curl http://<音箱IP>:9999/description.xml` 看 9999 端口是否通；② 网页「设备」页扫描并重新选择设备（`POST /devices/scan`、`POST /devices/select`）；③ 检查 `XIAOAI_IP` 是否笔误；④ 关闭路由器「AP 隔离 / 访客网络」；⑤ 部分固件需在小爱 App 里打开「DLNA / 局域网播放」类开关；⑥ 重启音箱 |
| 音箱能播放但**本地曲库没声音**、媒体请求 403 | 设置密码后媒体令牌不匹配（v2 bug）；`LAN_IP` 不是音箱可达地址；反向代理丢了 query | ① v3 已修复「player 取流不带 token」，确认运行的是 v3；② 启动日志确认 `媒体 URL 地址` 是局域网 IP，必要时显式 `LAN_IP=192.168.x.x`；③ 手动验证令牌：`curl -sv "http://<本机IP>:8010/media/1?t=<token>"`（token 由密码+曲目 id 派生，网页播放时可见 URL）；④ 若套了 Nginx，确认 `/media/` 保留 `?t=` 查询串并允许 `Range` |
| 播放卡顿 / 中途停止 / 日志 `BrokenPipe` | 音箱缓冲小、WiFi 信号差、转码 CPU 吃满、媒体 URL 网络不通 | ① 优先用 mp3（`standard`/`high`）而非无损；② 检查 `/media` 从音箱所在网段是否可达；③ 转码请求：首次播放 flac 会先生成缓存，稍等再播；④ 有线网络或靠近路由器 |
| `Range` 请求返回 200 而不是 206；拖进度条无效 | 客户端/代理不支持 Range 或服务端未走 `library.stream_file` | 用 `curl -H "Range: bytes=0-1023" -o /dev/null -w '%{http_code}' <媒体URL>` 验证，正常应为 206/416；若经过反向代理，需放行 `Range` 与 `Accept-Ranges` |
| 播放界面进度跳变 / 时长显示 00:00 | 设备不回报 `TrackDuration`（部分型号） | 正常现象；自动续播依赖「播到末尾」的正证据，时长未知时默认**不续播**（见 2.4） |

### 2.2 网易云账号 / 接口

| 症状 | 可能原因 | 排查与解决 |
|---|---|---|
| 播放在线歌曲返回 `409 {"code":"no_url"}`；日志出现 `-110` | 未登录 / cookie 失效 / 被风控 / 需要 VIP / 该曲无版权 | ① `GET /account` 确认登录态；失效则重新扫码：网页「登录」或 `python3 netease_login.py`；② VIP 曲目需要账号本身是 VIP；③ 无版权 / 已下架曲目无法播放，换一首或先下载到本地；④ 稍后重试或切换网络（避免机房 IP 被风控） |
| 扫码后**二维码不显示**（`/qr.png` 404 或页面空白） | `segno` 未安装；`qr_start()` 请求网易云失败；超时（约 5 分钟）过期；浏览器缓存 | ① `pip install segno`（`python3 app.py` 启动日志会警告）；② `GET /account/qr/status` 看 `status/message`（`error/expired` 有原因）；③ 重新点「登录」触发 `POST /account/qr`；④ 服务器无法访问 `music.163.com` 时扫码必失败，先在服务器上 `curl -I https://music.163.com/`；⑤ 无头服务器直接用 `python3 netease_login.py --qr /tmp/qr.png` 把二维码文件拉回本地扫 |
| 扫码成功但歌单不显示 / 歌单播放 403 | 歌单属于其他网易云账号（`owner_uid` 隔离）；导入未完成 | ① 换回导入该歌单的账号登录；② 重新导入 / 同步（`POST /netease/import`、`POST /netease/sync`）；③ 看 `GET /netease/playlists` 是否有数据 |
| 接口返回 `502 {"code":"upstream_error"}` | 网易云接口抖动 / 被限流 / 私有接口变更 | 重试；`NETEASE_TIMEOUT` 可适当加大；若长期如此可能私有接口已变更，关注项目 issue |
| 歌单超过 1000 首只同步了一部分 | v2 的分页 bug（v3 已修复） | 确认 v3；重新 `POST /netease/sync_all` |

### 2.3 下载

| 症状 | 可能原因 | 排查与解决 |
|---|---|---|
| 下载任务 `failed`，`error` 含 `no url` / VIP / 版权 | 无直链、需要 VIP、版权受限、该曲已下架 | ① 检查登录态与 VIP；② 换音质档位重试（`standard` 有时可用）；③ 无版权曲目只能换源；④ 不要反复重试永久性错误（v3 已不重试） |
| 下载卡住 `progress` 不动 | 网络中断 / 代理超时 / 网易云限速；线程被占用 | ① `GET /download/jobs` 看 `status=running` 与字节数；② 检查磁盘空间与 `MUSIC_DIR` 写权限；③ `POST /download/cancel` 后 `POST /download/retry`；④ 重启服务会自动把 `running` 恢复为 `pending` 并续传 `.part` |
| 下载成功但文件是 HTML / 无法播放 | v2 只按大小判断有效性（v3 已加 `Content-Type` + 魔数校验） | 升级 v3；删除坏文件后重新下载；确认 `ffmpeg` 存在（写标签需要） |
| 磁盘被转码缓存占满 | `CACHE_DIR` 缓存超过上限才会自动清理 | 设置 `CACHE_MAX_MB`（默认 2048）；手动清 `CACHE_DIR`；网页「本地曲库」里删除缓存 |
| 下载完成但曲库看不到 | 未执行入库（v3 下载完成自动 `scan_file`）；文件不在 `MUSIC_DIR` 下 | 检查任务 `path` 是否位于 `MUSIC_DIR`；手动 `POST /local/scan`；看日志是否有标签/转码警告 |

### 2.4 语音暂停 / 自动续播（v3 重点）

| 症状 | 可能原因 | 排查与解决 |
|---|---|---|
| 对小爱说「暂停」后中枢**自动播下一首**（停不下来） | ① 运行的是 v2 旧版；② 显式设置了 `AUTO_ADVANCE_UNKNOWN_DURATION=1` | ① 升级到 v3（判定规则见 ARCHITECTURE 第 3 节）；② 去掉该环境变量（默认 0）；③ 用 `AUTO_ADVANCE_UNKNOWN_DURATION=0` 重启 |
| 语音暂停后**不再自己恢复**，界面显示「已被语音暂停」 | 这是 v3 的**预期行为**：外部暂停不会被中枢抢回 | 说「继续播放」、点网页横幅，或 `POST /resume`；中枢会用记录的 `uri + position` 重推流并 Seek |
| 用小爱说「继续播放」后横幅不消失 | 设备切换了媒体/URI（`NO_MEDIA_PRESENT`）或状态没回到 `PLAYING` | 点横幅「继续播放」（等价 `/resume`）；或重新点歌；检查 `GET /now` 的 `state/url` |
| 一首歌播完**不自动切下一首** | ① 设备不报时长且兜底开关关闭（默认）；② 处于免打扰时段；③ 单曲播放不挂队列；④ `manual_stop=true` | ① 了解取舍：无时长设备开启 `AUTO_ADVANCE_UNKNOWN_DURATION=1` 会把「语音暂停」误判为播完，不建议；② 检查 `quiet_hours`；③ 从歌单进入播放才会续播；④ `POST /stop` 后需重新播放 |
| 停止后过一会儿自己又响了 | 旧版 watchdog 抢播（v3 已修复）；或小爱自己的闹钟/语音内容 | 升级 v3；看 `/state` 是否为 `interrupted`；必要时 `POST /stop` |
| 自动续播偶发连跳两首 | v2 竞态（v3 已用锁 + 宽限期修复） | 升级 v3；抓 `poll_once` 相关 INFO 日志 |

### 2.5 依赖 / 环境 / 部署

| 症状 | 可能原因 | 排查与解决 |
|---|---|---|
| 启动日志警告「未找到 ffmpeg」，无损/flac 播放失败、下载标签写入失败 | 系统缺 ffmpeg | Debian/Ubuntu：`sudo apt install ffmpeg`；macOS：`brew install ffmpeg`；Docker 镜像已内置；安装后重启服务 |
| 启动报 `ModuleNotFoundError: segno` / 扫码不可用 | 未安装依赖 | `pip install -e .` 或 `pip install segno`；确认用的是 `.venv` 里的 python |
| 启动报 `Address already in use`（端口占用） | 8010 已被其他进程占用 | `lsof -i :8010`（macOS）/ `ss -ltnp \| grep 8010`（Linux）找到进程；`PORT=8011 python3 app.py` 换端口，并同步修改 MCP `MUSIC_HUB_URL`、反向代理、systemd 环境文件 |
| 媒体 URL 是 `10.x` / `172.x` / `utun` 地址，本地曲库播放失败 | 多网卡 / VPN / 容器导致 LAN IP 自动探测选错 | 显式 `export LAN_IP=192.168.x.x`（必须是音箱能访问的宿主机局域网 IP）；Docker bridge 模式必须填宿主机 IP（见 `deploy/docker-compose.yml` 注释）；启动日志 `媒体 URL 地址 ...（来源：env:LAN_IP）` 可确认 |
| `database is locked` | 同时跑了多个实例写同一个 `music.db`；备份/同步工具占用；数据库放在 NFS/SMB 上 | ① `ps aux | grep app.py` 杀掉重复进程（systemd + 手动各跑一份最常见）；② v3 已设 `busy_timeout=30s`；③ 停止服务后再备份/移动 `music.db*`；④ 不要把 DB 放网络文件系统 |
| systemd 启动失败（`status=203/EXEC`、路径不存在） | `ExecStart` 的 venv python 路径或工作目录不对；`/etc/xiaoai-music-hub.env` 权限/语法错误 | `systemctl status xiaoai-music-hub -l`；确认 `/opt/xiaoai-music-hub/.venv/bin/python` 存在；`EnvironmentFile` 按 `KEY=VALUE` 一行一个、不要加 `export`；确认 `User=xiaoai` 对数据目录有写权限 |
| Docker 容器 `unhealthy` | `/health` 不通（端口/绑定错误）、启动即崩（缺依赖/数据目录权限） | `docker logs xiaoai-music-hub`；确认 `PORT=8010`、`DATA_DIR=/data`；`-v` 挂载目录对 UID 10001 可写；healthcheck 走 `127.0.0.1:8010/health` |
| 网页打不开（连接被拒绝） | 服务绑定在 `127.0.0.1`；防火墙；用了 https 但服务是 http | `XIAOAI_HOST=0.0.0.0`；放行 8010/tcp；通过 `http://<IP>:8010/` 访问（反向代理终止 TLS 时注意保留 `Range`/`Authorization`） |
| 设置 `UI_PASSWORD` 后浏览器一直弹登录框 | 期望行为：除 `/`、静态资源、`/health`、`/version`、`/media/*` 外均需 Basic Auth | 输入密码；脚本调用用 `-u user:password`（用户名任意）；忘记密码可改环境变量或数据库 `settings.ui_password` |
| MCP 语音助手报「连不上音乐中枢」 | 中枢未启动 / `MUSIC_HUB_URL` 错 / 中枢开了密码但没配 `MUSIC_HUB_PASSWORD` | 先 `curl $MUSIC_HUB_URL/health`；检查 MCP 客户端 env；密码与 `UI_PASSWORD` 一致 |
| MCP 返回 `409` 类错误但文案友好 | v3 已把 409/404/503 转成中文提示 | 若仍看到 `HTTP Error` 说明跑的是旧 `mcp_server.py`，更新后重启语音助手 |
| 自动续播在夜里不工作 | 免打扰时段（默认 `23:30-08:00`）内不自动续播 | 网页设置里改 `quiet_hours` 或置空关闭；`GET /quiet` 查看 |

---

## 3. 抓取诊断信息（提 issue 时请附上）

```bash
# 1. 版本与健康
curl -s http://127.0.0.1:8010/version
curl -s http://127.0.0.1:8010/health

# 2. 启动日志（含 LAN IP 探测来源、设备、鉴权状态）
journalctl -u xiaoai-music-hub -n 200 --no-pager
# 或：python3 app.py --verbose 2>&1 | tee /tmp/hub.log

# 3. 最近运行日志（视图一：环境变量 LOG_FILE 指定的文件；视图二：journald / 前台输出）
tail -n 200 "${LOG_FILE:-/dev/null}"

# 4. 环境信息
python3 -V; uname -a; ffmpeg -version | head -1
```

**注意**：提交日志/截图前，请删除 `secrets/`、cookie、`UI_PASSWORD`、内网 IP 等敏感信息。
