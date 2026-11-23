# xiaoai-music-hub

把小米 / 小爱音箱当成一台普通 DLNA 播放器来用的音乐中枢。

不买小爱音乐会员、不改音箱固件、不依赖米家 App——用网易云自己的账号听歌，走 DLNA 直推给音箱。

```
浏览器 / OpenClaw / 语音助手
        │  HTTP
        ▼
   xiaoai-music-hub  ── 网易云私有 API（搜索 / 取直链 / 歌单 / 扫码登录）
        │  DLNA SOAP（AVTransport）
        ▼
   小爱音箱（UPnP MediaRenderer）
```

## 项目结构

```
.
├── app.py                入口：建表、起线程、启动 HTTP
├── core/
│   ├── config.py         常量、设置、音质档位
│   ├── dlna.py           UPnP 控制（播放/暂停/音量/状态/发现）
│   ├── netease.py        网易云接口（搜索/取链/账号/歌单）
│   ├── library.py        本地曲库（扫描/转码/流输出）
│   ├── downloader.py     下载队列（持久化/并发/重试/续传）
│   ├── player.py         播放调度（队列/模式/续播/暂停恢复）
│   └── api.py            HTTP 路由（薄层）
├── web/index.html        前端
└── mcp_server.py         语音接口
```

改哪里：换音源改 `netease.py`；播放行为改 `player.py`；下载策略改 `downloader.py`；页面改 `web/index.html`。

## 特性

- **点歌**：搜网易云，取 320k 直链，推给音箱播放
- **歌单**：本地歌单 + 网易云歌单导入 / 增量同步
- **账号**：网页扫码登录，歌单按账号隔离，退出登录即隐藏
- **本地曲库**：扫描目录、自动转码（flac/m4a/wav → mp3）、HTTP Range 输出
- **下载**：整张歌单或单曲下载到本地，写入 ID3 标签后自动入库
- **播放模式**：顺序 / 列表循环 / 单曲循环 / 随机（随机模式下整张播放也从随机曲目开始）
- **多设备**：SSDP 自动发现局域网 MediaRenderer，可切换
- **断点续播**：小爱不支持 DLNA Pause，暂停时记忆进度，继续时重新拉流并 Seek 回原位
- **自动续播**：三种判定（状态变化 / 进度到尾 / 进度归零），带新鲜度与夜间免打扰保护
- **语音接入**：内置 MCP server，可被 OpenClaw 等 agent 调用点歌

## 快速开始

```bash
git clone https://github.com/reY2ox/xiaoai-music-hub.git
cd xiaoai-music-hub

# 1. 改音箱地址（默认 192.168.3.81）
export XIAOAI_IP=192.168.3.81

# 2. 启动
python3 xiaoai_music.py      # 监听 :8010
```

打开 `http://<你的IP>:8010/`，右上角扫码登录网易云，然后搜歌播放。

### systemd

```ini
[Unit]
Description=Xiaoai DLNA Music Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/xiaoai-music
Environment=XIAOAI_IP=192.168.3.81
ExecStart=/usr/bin/python3 /opt/xiaoai-music/xiaoai_music.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## 配置

| 环境变量 / 设置 | 默认 | 说明 |
|---|---|---|
| `XIAOAI_IP` | 192.168.3.81 | 音箱地址 |
| `MUSIC_DIR` | `/root/music` | 本地曲库目录（代码内常量，可改） |
| `quiet_hours` | `23:30-08:00` | 夜间免打扰，期间不自动续播；`/quiet?off=1` 关闭 |

凭据文件默认写到 `./secrets/netease.cookie`（代码里是绝对路径，部署时按需调整）。

## 音质

默认 320k，可在页面或接口切换：

| 档位 | 参数 | 说明 |
|---|---|---|
| `standard` | 192k mp3 | 省流量 |
| `high` | 320k mp3 | 默认 |
| `lossless` | FLAC | 需 VIP；实测小爱 LX06 可直解 |

```bash
curl "http://127.0.0.1:8010/quality"            # 查看
curl "http://127.0.0.1:8010/quality?set=lossless"
```

下载可单独指定音质：`/download/song?...&quality=lossless`

## 下载队列

任务持久化在 SQLite，服务重启自动恢复；并发 2（`config.DOWNLOAD_CONCURRENCY`），失败重试 2 次，支持 `.part` 断点续传。

```bash
curl "http://127.0.0.1:8010/download/playlist?id=3"   # 整张入队
curl "http://127.0.0.1:8010/download/jobs"            # 队列状态
curl "http://127.0.0.1:8010/download/retry"           # 重试失败
```

## HTTP API

播放控制
- `GET /now` 当前曲目与进度
- `GET /stop` `/pause` `/resume`
- `GET /volume?level=0-100`
- `GET /next` `/prev`
- `GET /mode?set=sequential|loop|single|shuffle`

搜索与播放
- `GET /search?q=`
- `GET /play?q=&i=`

歌单
- `GET /playlists` `/playlist?id=` `/playlist/create?name=`
- `GET /playlist/add?id=&song_id=&name=&artist=&album=`
- `GET /playlist/remove?id=&track=` `/playlist/delete?id=`
- `GET /playlist/play?id=&i=&whole=1`

网易云
- `GET /account` `/account/qr` `/account/qr/status` `/qr.png` `/account/logout`
- `GET /netease/playlists` `/netease/import?id=` `/netease/sync?id=`
- `GET /netease/sync_all` `/netease/sync_all/status`

本地曲库
- `GET /local/scan` `/local/tracks?q=` `/local/play?id=` `/local/add?id=&track=`
- `GET /media/<id>?t=<token>`（Range 206）
- `GET /local/download?id=` `/local/download/song?song_id=` `/local/download/status`

设备
- `GET /devices` `/devices/scan` `/devices/select?ip=`

免打扰
- `GET /quiet` `/quiet?set=22:00-07:00` `/quiet?off=1`

## 语音接入（MCP）

`mcp_server.py` 是一个 stdio MCP server，暴露 `play_music` / `stop_music` / `pause_music` / `music_status` / `set_volume`。

OpenClaw 里注册：

```json
{
  "mcp": {
    "servers": {
      "music-hub": {
        "transport": "stdio",
        "command": "python3",
        "args": ["/opt/xiaoai-music/mcp_server.py"]
      }
    }
  }
}
```

## 已知限制

- 网易云 VIP 曲目需要有效 `MUSIC_U`；失效时接口返回 `-110`
- 播放直链约 20 分钟过期，每次播放前现取
- 小爱这类 DLNA 渲染器不支持 Pause，暂停是「记位置 + 重拉流 + Seek」模拟的，恢复有 1–2 秒停顿
- 部分设备不返回总时长，自动续播靠「进度归零」兜底
- 网易云私有接口随时可能变，登录态也可能失效，需要重新扫码

## 许可

MIT
