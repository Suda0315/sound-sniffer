"""
Sound Sniffer - 浏览器音频流嗅探器

原理：接管一个 Chrome 实例，监听 CDP 的 Network.responseReceived 事件，
把所有音频类型的网络响应列出来，点击即可保存成本地文件。

只处理你自己浏览器里合法播放到的内容：直链音频文件、明文 HLS 分片。
不绕过任何 DRM / 加密接口 —— 那种流抓下来也是密文，工具不做。
"""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"
STATIC = BASE / "static"
PROFILE = BASE / ".chrome-profile"
DOWNLOADS.mkdir(exist_ok=True)

PORT_START = 8765


# ---------------------------------------------------------------- ffmpeg 定位
def _startupinfo():
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si


def _usable(path) -> bool:
    """真的能跑才算数 —— WindowsApps 下的商店占位符必须排除"""
    path = str(path)
    if "WindowsApps" in path:
        return False
    try:
        r = subprocess.run([path, "-version"], capture_output=True,
                           timeout=8, startupinfo=_startupinfo())
        return r.returncode == 0
    except Exception:
        return False


def find_ffmpeg() -> str | None:
    cands: list[Path] = []
    env = os.environ.get("FFMPEG_PATH")
    if env:
        cands.append(Path(env))
    local = Path(os.path.expandvars(r"%LOCALAPPDATA%"))
    cands += [
        local / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1-full_build/bin/ffmpeg.exe",
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"),
    ]
    # winget 的版本目录名不固定，扫一遍兜底
    root = local / "Microsoft/WinGet/Packages"
    if root.exists():
        cands += sorted(root.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"), reverse=True)
    w = _which("ffmpeg")
    if w:
        cands.append(Path(w))

    seen: set[str] = set()
    for c in cands:
        key = str(c)
        if key in seen or not c.exists():
            continue
        seen.add(key)
        if _usable(c):
            return key
    return None


def _which(name: str) -> str | None:
    from shutil import which

    return which(name)


FFMPEG = find_ffmpeg()


def find_ytdlp() -> str | None:
    cands: list[Path] = []
    env = os.environ.get("YTDLP_PATH")
    if env:
        cands.append(Path(env))
    cands.append(BASE / "bin" / "yt-dlp.exe")  # 项目自带的独立版优先
    w = _which("yt-dlp")
    if w:
        cands.append(Path(w))
    for pyver in ("Python310", "Python311", "Python312", "Python313"):
        cands.append(Path(os.path.expandvars(rf"%LOCALAPPDATA%\Programs\Python\{pyver}\Scripts\yt-dlp.EXE")))
        cands.append(Path(os.path.expandvars(rf"%APPDATA%\Python\{pyver}\Scripts\yt-dlp.EXE")))

    seen: set[str] = set()
    for c in cands:
        key = str(c)
        if key in seen or not c.exists() or "WindowsApps" in key:
            continue
        seen.add(key)
        try:
            r = subprocess.run([key, "--version"], capture_output=True,
                               timeout=15, startupinfo=_startupinfo())
            if r.returncode == 0 and r.stdout.strip():
                return key
        except Exception:
            continue
    return None


YTDLP = find_ytdlp()

CONFIG_FILE = BASE / "config.json"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


CONFIG = _load_config()

# ---------------------------------------------------------------- 类型判定
AUDIO_EXT = {
    "mp3", "m4a", "aac", "flac", "wav", "ogg", "oga", "opus",
    "wma", "aiff", "aif", "amr", "ape", "mka", "m4b", "dsf",
}
HLS_MIME = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "application/vnd.apple.mpegurl.audio",
    "application/dash+xml",
}
HLS_EXT = {"m3u8", "mpd"}


def classify(url: str, mime: str) -> str | None:
    """返回 'hls' / 'audio' / None"""
    m = (mime or "").split(";")[0].strip().lower()
    path = urllib.parse.urlparse(url).path.lower()
    ext = path.rsplit(".", 1)[-1] if "." in path else ""

    if m in HLS_MIME or ext in HLS_EXT:
        return "hls"
    if m.startswith("audio/"):
        return "audio"
    if ext in AUDIO_EXT and m in ("", "application/octet-stream", "binary/octet-stream"):
        return "audio"
    if ext in AUDIO_EXT and m.startswith("video/"):
        # 有些站点用 mp4 容器装纯音频
        return "audio"
    return None


def guess_ext(url: str, mime: str) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    path = urllib.parse.urlparse(url).path.lower()
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    if ext in AUDIO_EXT:
        return ext
    if ext in HLS_EXT or m in HLS_MIME:
        return "m4a"
    table = {
        "audio/mpeg": "mp3", "audio/mp3": "mp3",
        "audio/mp4": "m4a", "audio/x-m4a": "m4a",
        "audio/aac": "aac", "audio/aacp": "aac",
        "audio/flac": "flac", "audio/x-flac": "flac",
        "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
        "audio/ogg": "ogg", "audio/opus": "opus", "audio/vorbis": "ogg",
        "audio/webm": "webm", "audio/x-ms-wma": "wma",
    }
    return table.get(m, "audio")


# ---------------------------------------------------------------- 全局状态
LOCK = threading.Lock()
TRACKS: dict[str, dict] = {}
ORDER: list[str] = []
JOBS: dict[str, dict] = {}
JOB_ORDER: list[str] = []
STATE = {"browser": "starting", "ffmpeg": bool(FFMPEG), "ffmpeg_path": FFMPEG, "profile": str(PROFILE)}

PW_TASKS: queue.Queue = queue.Queue()


def add_track(url: str, mime: str, kind: str, page_url: str, page_title: str, req_headers: dict) -> None:
    key = url
    with LOCK:
        for tid, t in TRACKS.items():
            if t["url"] == key:
                t["hits"] += 1
                t["last_seen"] = time.time()
                return
        tid = uuid.uuid4().hex[:10]
        TRACKS[tid] = {
            "id": tid,
            "url": url,
            "mime": mime,
            "kind": kind,
            "ext": guess_ext(url, mime),
            "page": page_url,
            "title": page_title,
            "size": int((req_headers or {}).get("content-length") or 0) or None,
            "headers": {
                k: v
                for k, v in (req_headers or {}).items()
                if k.lower() in ("referer", "user-agent", "cookie", "authorization", "range")
            },
            "first_seen": time.time(),
            "last_seen": time.time(),
            "hits": 1,
        }
        ORDER.append(tid)
        if len(ORDER) > 500:
            dead = ORDER.pop(0)
            TRACKS.pop(dead, None)


# ---------------------------------------------------------------- 文件名
BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_name(name: str, limit: int = 80) -> str:
    name = urllib.parse.unquote(name)
    name = BAD_CHARS.sub("_", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "audio"
    return name[:limit]


def name_from_url(url: str, fallback: str, ext: str) -> str:
    path = urllib.parse.urlparse(url).path
    base = path.rsplit("/", 1)[-1] if "/" in path else path
    base = base.rsplit(".", 1)[0] if "." in base else base
    base = safe_name(base)
    if not base or len(base) < 2:
        base = safe_name(fallback) or "audio"
    return f"{base}.{ext}"


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suffix, i = p.stem, p.suffix, 2
    while True:
        cand = p.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1


# ---------------------------------------------------------------- 下载
def header_string(headers: dict, page_url: str) -> str:
    h = dict(headers or {})
    if "Referer" not in h and "referer" not in h and page_url:
        h["Referer"] = page_url
    h.setdefault("User-Agent", UA)
    lines = [f"{k}: {v}" for k, v in h.items() if v]
    return "\r\n".join(lines) + "\r\n"


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def fresh_cookies(url: str) -> str | None:
    """在 Playwright 线程里取当前浏览器的 cookie，保证登录态有效"""
    def job(ctx):
        try:
            cs = ctx.cookies(url)
            return "; ".join(f"{c['name']}={c['value']}" for c in cs)
        except Exception:
            return None

    return call_in_pw(job)


def call_in_pw(fn, timeout: float = 15):
    if not PW_READY.is_set():
        return None
    box: dict = {}
    ev = threading.Event()
    PW_TASKS.put((fn, box, ev))
    ev.wait(timeout)
    return box.get("result")


def build_headers(track: dict) -> dict:
    h = dict(track.get("headers") or {})
    ck = fresh_cookies(track["url"])
    if ck:
        h["Cookie"] = ck
    h["User-Agent"] = h.get("User-Agent") or UA
    if not h.get("Referer") and not h.get("referer"):
        h["Referer"] = track.get("page") or ""
    return h


def download_direct(track: dict, dest: Path, job: dict) -> None:
    h = build_headers(track)
    timeout = httpx.Timeout(30.0, read=120.0)
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=h) as cli:
        with cli.stream("GET", track["url"]) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            job["total"] = total or job.get("total") or 0
            job["status"] = "downloading"
            got = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(65536):
                    f.write(chunk)
                    got += len(chunk)
                    job["bytes"] = got


def download_hls(track: dict, dest: Path, job: dict, to_mp3: bool) -> None:
    if not FFMPEG:
        raise RuntimeError("需要 ffmpeg 才能处理 m3u8/dash 流")
    h = build_headers(track)
    hdr = header_string(h, track.get("page") or "")
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-nostdin",
           "-headers", hdr, "-i", track["url"], "-vn"]
    if to_mp3:
        cmd += ["-c:a", "libmp3lame", "-b:a", "320k", str(dest)]
    else:
        cmd += ["-c", "copy", "-bsf:a", "aac_adtstoasc", str(dest)]
    job["status"] = "converting"
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError((proc.stderr or "").strip()[-400:] or "ffmpeg 失败")


def to_mp3_file(src: Path, job: dict) -> Path:
    if not FFMPEG:
        return src
    dst = src.with_suffix(".mp3")
    job["status"] = "converting"
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-nostdin", "-i", str(src),
           "-vn", "-c:a", "libmp3lame", "-b:a", "320k", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not dst.exists():
        return src
    try:
        src.unlink()
    except OSError:
        pass
    return dst


def run_job(job_id: str, track_id: str, to_mp3: bool) -> None:
    job = JOBS[job_id]
    try:
        with LOCK:
            track = TRACKS.get(track_id)
        if not track:
            raise RuntimeError("音频记录已过期，请刷新页面重新捕获")

        if track["kind"] == "hls":
            # m3u8 的 URL 通常是 playlist.m3u8 这种没信息量的名字，优先用页面标题
            ext = "mp3" if to_mp3 else "m4a"
            base = safe_name(track.get("title") or urllib.parse.urlparse(track["url"]).netloc)
            fname = f"{base or 'stream'}.{ext}"
            dest = unique_path(DOWNLOADS / fname)
            job["file"] = dest.name
            download_hls(track, dest, job, to_mp3)
        else:
            # 先用真实格式落盘，再按需转 mp3，避免后缀和数据对不上
            real = track["ext"]
            fname = name_from_url(
                track["url"],
                track.get("title") or urllib.parse.urlparse(track["url"]).netloc,
                real,
            )
            dest = unique_path(DOWNLOADS / fname)
            job["file"] = dest.name
            download_direct(track, dest, job)
            if to_mp3 and FFMPEG and real != "mp3":
                dest = to_mp3_file(dest, job)
                job["file"] = dest.name

        job["status"] = "done"
        job["size"] = dest.stat().st_size
        job["path"] = str(dest)
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)[:500]


def start_job(track_id: str, to_mp3: bool) -> str:
    jid = uuid.uuid4().hex[:10]
    with LOCK:
        JOBS[jid] = {
            "id": jid, "track": track_id, "status": "queued", "kind": "sniff",
            "bytes": 0, "total": 0, "size": 0, "file": "", "path": "",
            "error": "", "note": "", "percent": 0, "started": time.time(),
        }
        JOB_ORDER.append(jid)
        if len(JOB_ORDER) > 200:
            JOBS.pop(JOB_ORDER.pop(0), None)
    threading.Thread(target=run_job, args=(jid, track_id, to_mp3), daemon=True).start()
    return jid


RETRY_HINTS = ("sign in to confirm", "bot", "po_token", "po token")


def _ytdlp_cmd(url: str, to_mp3: bool, proxy: str, with_cookies: bool) -> list[str]:
    out_tpl = str(DOWNLOADS / "%(title).80s [%(id)s].%(ext)s")
    cmd = [YTDLP, "--newline", "-o", out_tpl, "--no-playlist", "-f", "bestaudio/best"]
    if to_mp3:
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "320K"]
    if proxy:
        cmd += ["--proxy", proxy]
    if with_cookies:
        cmd += ["--cookies-from-browser", "chrome"]
    cmd.append(url)
    return cmd


def _run_ytdlp(cmd: list[str], job: dict):
    """跑一遍 yt-dlp，返回 (退出码, 末尾输出行, 输出文件路径)"""
    dest = None
    tail: list[str] = []
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", startupinfo=_startupinfo(),
    )
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        if len(tail) > 12:
            tail.pop(0)
        if "Destination:" in line:
            dest = line.split("Destination:", 1)[1].strip()
        m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if m:
            job["percent"] = float(m.group(1))
        job["note"] = line[:160]
    return proc.wait(), tail, dest


def run_fetch_job(job_id: str, url: str, to_mp3: bool, proxy: str) -> None:
    """走 yt-dlp 抓链接 —— YouTube、B站这类用 MSE 分片加载的站点只能靠这条路"""
    job = JOBS[job_id]
    started = time.time()
    try:
        if not YTDLP:
            raise RuntimeError("没有找到 yt-dlp，执行 pip install yt-dlp 后重启")

        job["status"] = "downloading"
        code, tail, dest = _run_ytdlp(_ytdlp_cmd(url, to_mp3, proxy, False), job)
        # YouTube 现在常弹人机验证，撞上就带上自己的登录态再试一次
        if code != 0 and any(h in "\n".join(tail).lower() for h in RETRY_HINTS):
            job["note"] = "遇到人机验证，带浏览器 cookie 重试一次…"
            code, tail, dest = _run_ytdlp(_ytdlp_cmd(url, to_mp3, proxy, True), job)

        if code != 0:
            raise RuntimeError("\n".join(tail[-6:])[-500:] or f"yt-dlp 退出码 {code}")

        if not dest or not Path(dest).exists():
            cands = [p for p in DOWNLOADS.iterdir()
                     if p.is_file() and p.stat().st_mtime > started - 5]
            if cands:
                dest = str(max(cands, key=lambda p: p.stat().st_mtime))
        if not dest or not Path(dest).exists():
            raise RuntimeError("yt-dlp 报告成功但没找到输出文件")

        job["file"] = Path(dest).name
        job["path"] = str(dest)
        job["size"] = Path(dest).stat().st_size
        job["percent"] = 100
        job["note"] = ""
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)[:500]


def start_fetch_job(url: str, to_mp3: bool, proxy: str) -> str:
    jid = uuid.uuid4().hex[:10]
    with LOCK:
        JOBS[jid] = {
            "id": jid, "track": "", "status": "queued", "kind": "fetch",
            "bytes": 0, "total": 0, "size": 0, "file": "", "path": "",
            "error": "", "note": "", "percent": 0, "url": url,
            "started": time.time(),
        }
        JOB_ORDER.append(jid)
        if len(JOB_ORDER) > 200:
            JOBS.pop(JOB_ORDER.pop(0), None)
    threading.Thread(target=run_fetch_job, args=(jid, url, to_mp3, proxy), daemon=True).start()
    return jid


# ---------------------------------------------------------------- Playwright
PW_READY = threading.Event()


def hook_page(page, ctx) -> None:
    try:
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")

        def on_response(ev):
            try:
                resp = ev.get("response", {})
                url = resp.get("url", "")
                if not url or url.startswith(("data:", "blob:")):
                    return
                mime = resp.get("mimeType", "")
                kind = classify(url, mime)
                if not kind:
                    return
                status = resp.get("status", 200)
                if status and status >= 400:
                    return
                req_h = resp.get("requestHeaders") or {}
                try:
                    title = page.title()
                except Exception:
                    title = ""
                add_track(url, mime, kind, page.url, title, req_h)
            except Exception:
                pass

        cdp.on("Network.responseReceived", on_response)
    except Exception:
        pass


def pw_worker() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        STATE["browser"] = "error: 缺少 playwright，执行 pip install playwright"
        return

    with sync_playwright() as p:
        ctx = None
        last = ""
        launch_args = ["--disable-blink-features=AutomationControlled"]
        opts = dict(
            headless=False,
            args=launch_args,
            ignore_default_args=["--enable-automation"],
            user_data_dir=str(PROFILE),
            viewport=None,
            no_viewport=True,
        )
        for channel in ("chrome", "msedge", None):
            try:
                kw = dict(opts)
                if channel:
                    kw["channel"] = channel
                ctx = p.chromium.launch_persistent_context(**kw)
                STATE["browser"] = f"connected:{channel or 'bundled'}"
                break
            except Exception as e:  # noqa: BLE001
                last = str(e)[:160]
                continue
        if ctx is None:
            STATE["browser"] = f"error: 无法启动浏览器 {locals().get('last', '')}"
            return

        def attach(pg):
            hook_page(pg, ctx)

        for pg in ctx.pages:
            attach(pg)
        ctx.on("page", attach)

        if not ctx.pages:
            pg = ctx.new_page()
            attach(pg)

        # 打开本地说明页，用户从这里开始浏览
        try:
            ctx.pages[0].goto(f"http://127.0.0.1:{STATE['port']}/guide")
        except Exception:
            pass

        PW_READY.set()
        while True:
            try:
                fn, box, ev = PW_TASKS.get(timeout=0.3)
                try:
                    box["result"] = fn(ctx)
                except Exception as e:  # noqa: BLE001
                    box["result"] = None
                ev.set()
            except queue.Empty:
                if not ctx.pages:
                    break


# ---------------------------------------------------------------- API
app = FastAPI(title="Sound Sniffer")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/guide")
def guide():
    return FileResponse(STATIC / "guide.html")


@app.get("/api/state")
def api_state():
    with LOCK:
        now = time.time()
        tracks = [dict(TRACKS[t]) for t in ORDER][::-1]
        jobs = [dict(JOBS[j]) for j in JOB_ORDER][::-1][:40]
    urls = call_in_pw(lambda ctx: [pg.url for pg in ctx.pages]) or []
    sites = sorted({urllib.parse.urlparse(u).netloc for u in urls if u.startswith("http")})
    return JSONResponse({
        "browser": STATE["browser"],
        "ffmpeg": STATE["ffmpeg"],
        "ffmpeg_path": STATE["ffmpeg_path"],
        "ytdlp": bool(YTDLP),
        "ytdlp_path": YTDLP,
        "config": CONFIG,
        "sites": sites,
        "tracks": tracks,
        "jobs": jobs,
        "download_dir": str(DOWNLOADS),
        "now": now,
    })


@app.post("/api/fetch")
def api_fetch(payload: dict = Body(...)):
    url = (payload.get("url") or "").strip()
    if not url:
        return JSONResponse({"error": "链接是空的"}, status_code=400)
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "只认 http/https 链接"}, status_code=400)
    return JSONResponse({"job": start_fetch_job(url, bool(payload.get("to_mp3")),
                                                CONFIG.get("proxy", ""))})


@app.post("/api/config")
def api_config(payload: dict = Body(...)):
    if "proxy" in payload:
        CONFIG["proxy"] = (payload.get("proxy") or "").strip()
        _save_config(CONFIG)
    return JSONResponse({"ok": True, "config": CONFIG})


@app.post("/api/download/{track_id}")
def api_download(track_id: str, to_mp3: bool = False):
    return JSONResponse({"job": start_job(track_id, to_mp3)})


@app.post("/api/clear")
def api_clear():
    with LOCK:
        keep = set()
        for j in JOBS.values():
            if j["status"] in ("queued", "downloading", "converting"):
                keep.add(j["track"])
        for tid in list(TRACKS):
            if tid not in keep:
                TRACKS.pop(tid, None)
        ORDER[:] = [t for t in ORDER if t in TRACKS]
    return JSONResponse({"ok": True})


@app.post("/api/open-folder")
def api_open_folder():
    try:
        os.startfile(str(DOWNLOADS))  # noqa: S606
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)})
    return JSONResponse({"ok": True})


app.mount("/files", StaticFiles(directory=str(DOWNLOADS)), name="files")


def free_port(start: int) -> int:
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def main() -> None:
    port = free_port(PORT_START)
    STATE["port"] = port
    if port != PORT_START:
        print(f"[提示] 端口 {PORT_START} 被占用，改用 {port}")
    threading.Thread(target=pw_worker, daemon=True).start()

    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    print("=" * 56)
    print(" Sound Sniffer 已启动")
    print(f" 控制面板: {url}")
    print(f" 保存目录: {DOWNLOADS}")
    print(f" ffmpeg  : {FFMPEG or '未找到（m3u8 与 mp3 转换不可用）'}")
    print(f" yt-dlp  : {YTDLP or '未找到（链接抓取不可用，pip install yt-dlp）'}")
    if CONFIG.get("proxy"):
        print(f" 代理    : {CONFIG['proxy']}")
    print("=" * 56)
    threading.Timer(1.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
