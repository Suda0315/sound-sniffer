# Sound Sniffer

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)

浏览器音频流嗅探器。接管一个 Chrome 实例，监听它的网络请求，把所有音频类型的响应列出来，点一下存成本地文件。

老牌插件 Sound Pirate（声海盗）被下架后的本地复刻版 —— 同样的原理，自己掌控。

---

## 快速开始

**从 GitHub 拿代码**：

```bash
git clone https://github.com/Suda0315/sound-sniffer.git
cd sound-sniffer
pip install -r requirements.txt
```

**跑起来**：

1. 双击 `start.bat`（Windows），或 `python app.py`
2. 会自动弹出一个受控 Chrome 窗口 + 控制面板网页
3. 两条路，挑一条：
   - **嗅探**：在受控 Chrome 里打开网页、点播放 → 音频流自己冒出来 → 点「保存」
   - **链接**：把页面链接粘到控制面板顶部输入框、回车 → 走 yt-dlp

文件都落在 `downloads/` 目录。

---

## 两条路，各管一摊

| | 嗅探 | 链接 |
|---|---|---|
| 用法 | 浏览器里播放，自动列出来 | 粘贴 URL，回车 |
| 靠什么 | CDP 监听网络响应 | yt-dlp |
| 适合 | 播客、电台、独立站、小众社区 —— 那些直接发 mp3/m4a/m3u8 的 | YouTube、B站、Vimeo、Twitter 这类用分片加载的 |
| 不适合 | 分片站点（抓到也是几秒一段的碎片） | 需要动态交互才出音频的页面 |

**为什么 YouTube 嗅探不到**：它用 MSE（Media Source Extensions）把音视频切成几秒一段，靠 Service Worker 在后台请求。页面级监听看不到这些请求，就算看到了也是几百个碎片，拼不成一首歌。所以 YouTube 请走链接通道。

控制面板会自动检测你打开的站点，遇到分片站点就弹提示条告诉你改用链接。

---

## 它能抓什么

| 类型 | 举例 | 处理方式 |
|---|---|---|
| 直链音频文件 | `.mp3` `.m4a` `.flac` `.wav` `.ogg` `.opus` | 直接下载，带原站请求头和 cookie |
| HLS 明文流 | `.m3u8` 播放列表 | ffmpeg 合并分片，可直出 mp3 |
| DASH 明文流 | `.mpd` | 同上 |

**抓不到的（工具不做）**：Widevine / FairPlay 之类的 DRM 加密流。那种内容在浏览器里解密后才出声，网络层抓下来只有密文，解不开也不需要解开。

判定规则在 `app.py` 的 `classify()`：只看 `Content-Type` 和 URL 扩展名，视频流（`.mp4`/`.webm`）不抓。

---

## 功能

- 实时列表：每 1.2 秒刷新，显示类型、来源站点、大小、命中次数
- **链接抓取**：粘贴 URL 走 yt-dlp，支持代理、自动带 cookie 重试
- 分片站点提示：检测到 YouTube/B站 会提示改用链接通道
- 过滤框：按 URL 或站点名筛选
- 统一转 mp3：勾选后所有下载自动转成 320kbps mp3（需要 ffmpeg）
- 登录态：下载时会从浏览器取当前 cookie，带权限的流也能存
- 下载进度：直链显示实时字节数，转码显示状态
- 端口自动避让：8765 被占用就往后找空闲端口

---

## 依赖

Python 3.10+，以及：

```bash
pip install fastapi uvicorn httpx playwright
```

浏览器用**本机已装的 Chrome**（优先）或 Edge，不需要下载 Playwright 的 chromium。

**yt-dlp（链接通道需要）**：项目自带一份在 `bin/yt-dlp.exe`，优先用；没有就自动找系统里的。

**版本就是生命线**：yt-dlp 落后一两个月，YouTube 就开始返回 403 Forbidden。这不是配置问题，是版本问题。定期更新：

```bash
# 7897 换成你自己的代理端口
curl -x http://127.0.0.1:7897 -L -o bin/yt-dlp.exe \
  https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe
```

**ffmpeg（可选但推荐）**：处理 m3u8 和转 mp3 需要它。启动时自动检测两个位置 —— 环境变量 `FFMPEG_PATH`，以及 winget 装的 FFmpeg。没有 ffmpeg 也能跑，只是 m3u8 流和 mp3 转换不可用。

装 ffmpeg：

```bash
winget install Gyan.FFmpeg
```

---

## 目录结构

```
sound-sniffer/
├── app.py              # 后端：CDP 嗅探引擎 + 下载器 + API
├── static/
│   ├── index.html      # 控制面板
│   └── guide.html      # 受控浏览器启动时打开的说明页
├── downloads/          # 存下来的音频
├── bin/
│   └── yt-dlp.exe     # 自带的独立版，优先使用
├── config.json         # 代理等设置（自动生成）
├── .chrome-profile/    # 独立浏览器配置（首次启动自动创建）
├── start.bat           # 一键启动
└── requirements.txt
```

## 架构

```
Chrome 窗口（受控）
   │  CDP: Network.responseReceived
   ▼
app.py 捕获引擎 ──► TRACKS（内存，最多 500 条）
   │                    │
   │                    ▼
   │              FastAPI /api/state ──► 控制面板（1.2s 轮询）
   │
   └─► 点「保存」► 下载线程 ──► httpx（直链）/ ffmpeg（HLS）──► downloads/

粘贴链接 ──► yt-dlp（可带代理 / cookie）────────────────────────► downloads/
```

Playwright 跑在独立线程，下载请求通过任务队列回到 Playwright 线程拿最新 cookie，保证登录态有效。

---

## 常见问题

**浏览器没启动 / 状态显示 error**
检查是否装了 Chrome 或 Edge。都没有的话执行 `playwright install chromium` 装一个。

**播放了但列表没东西**
- 有些站点要手动点播放才会请求音频，光打开页面不算
- 视频站点的音频和视频是合流的（`.mp4`），工具不抓视频
- 如果是加密流，那就不是这个工具该干的活

**YouTube / B站 抓不到**
这是设计如此，不是坏了 —— 分片站点请用顶部输入框粘链接走 yt-dlp。

**yt-dlp 报 Sign in to confirm you're not a bot**
YouTube 的人机验证。工具会自动带浏览器 cookie 重试一次。还不行就在受控 Chrome 里登录 Google 账号再试。

**下载失败：HTTP Error 403 Forbidden**
九成是 yt-dlp 版本太老（见上面"版本就是生命线"）。更新完基本就好。剩下的是代理出口 IP 被 YouTube 标记 —— 在 Clash 里换个节点。

**YouTube 连不上 / 超时**
需要代理。在控制面板的代理框里填 `http://127.0.0.1:7897` 这样的地址，会记住。端口不确定的话在 Clash 的设置页看，或者扫一下本机开着的端口。

**需要登录的站点**
受控 Chrome 用的是**独立配置**，登录状态是空的，需要在那个窗口里重新登录一次。

**想换 ffmpeg**
设置环境变量 `FFMPEG_PATH` 指向你的 ffmpeg.exe。

**关掉命令行窗口**
Chrome 会一起关掉，正常。

---

## 更新日志

### v0.2.1 · 2026-09-29
- 内置独立版 yt-dlp 到 `bin/`，不再依赖系统 Python 环境
- `find_ytdlp()` 优先用项目自带的，找不到才回退系统
- 实测通过：YouTube 单曲 → mp3（263 秒 / 3.99MiB）

### v0.2.0 · 2026-09-29
- 新增链接抓取通道（yt-dlp）：YouTube / B站 这类分片站点的正解
- 代理设置持久化到 `config.json`
- 检测到分片站点时自动弹提示，引导改用链接通道
- 撞上 YouTube 人机验证会自动带浏览器 cookie 重试一次
- 状态条增加 yt-dlp 指示灯

### v0.1.0 · 2026-09-29
- CDP 网络响应嗅探，支持直链音频与 HLS/DASH
- 控制面板：实时列表、过滤、下载进度、打开目录
- ffmpeg 自动定位（排除 WindowsApps 商店占位符）
- 统一转 mp3、cookie 透传、端口自动避让

---

## 边界声明

这个工具只处理你自己有权限听到的内容：自己买过的、创作者开放下载的、公有领域的、自己上传的。别拿它去干不该干的事。DRM 部分工具从设计上就不支持，不是技术不够，是不做。
