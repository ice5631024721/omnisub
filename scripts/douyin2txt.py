#!/usr/bin/env python3
"""douyin2txt.py — 抖音链接 → 文字（omnisub 抖音分支的固化实现）

一条命令完成：triage → 分享页解析 → 下载（必须带 cookie）→ 劣化文件守卫
→ 提音 MP3 → ASR 转写 → 覆盖守卫 → 汇总输出。

2026-09-29 实测踩坑固化（四个都真实发生过）：
  1. 【下载必须带 cookie】无 cookie 时 CDN 返回劣化损坏文件：HTTP 200、
     Content-Length 与实收一致（下载器认为成功），但容器时长 < 分享页元数据、
     视频流 NAL 全错、音频实际可解时长远小于标称。带 cookie 下载则完整。
  2. 【afconvert 不可信】对劣化文件 afconvert 静默"成功"并按索引填充静音到
     标称时长，不报任何错——时长判据只有 ffprobe 可靠。
  3. 【bl 同步 ASR 模型不能传本地文件】qwen-audio-3.1-asr-flash 走 bl 会把
     本地路径直接交给服务端下载 → FILE_DOWNLOAD_FAILED；DashScope 兼容模式
     不支持该模型（404）。可用路线是异步 filetrans：上传内置。
     2026-10-04 起本分支与本地视频分支统一为 qwen-audio-3.1-asr-flash-filetrans
     （原为 fun-asr，同音频实测费用约 7.8 倍：6.9s 音频 0.00152 元 vs 0.00020 元）。
     仍固定提 MP3 32k——同日实测该模型 WAV 也能过，但覆盖守卫用
     `文件大小 × 8 ÷ 32` 反推时长、依赖已知码率，换 WAV 会把守卫算废。
  4. 【key 不会自动找到】bl 报 "No API key found"：key 必须显式传入
     （--api-key / DASHSCOPE_API_KEY / ~/.agentmemory/.env 的 OPENAI_API_KEY=）。

用法：
  python3 douyin2txt.py "<抖音分享链接>" [--out <正文输出文件>] [--api-key <key>]
  python3 douyin2txt.py "<链接>" --images     # 图片/轮播帖：只下载图片并打印路径
退出码：0 成功；2 配置类错误（cookie 失效/key 缺失）；1 其他失败。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
COOKIE_FILE = Path.home() / ".dsh" / "douyin-cookies.txt"
AGENTMEMORY_ENV = Path.home() / ".agentmemory" / ".env"
TIMING_LOG = Path.home() / ".dsh" / "omnisub-timing.log"
# 容器时长与分享页元数据的容差（秒）；超过即判劣化文件
DURATION_TOLERANCE_S = 2.0
TMP_PREFIX = "dy2t_"
# 异步 filetrans 模型（上传内置、长音频整片直送）。与 omnisub.py 的
# ASR_MODEL_DEFAULT 同名，2026-10-04 由 fun-asr 统一过来。改动请连带 SKILL.md。
ASR_MODEL = "qwen-audio-3.1-asr-flash-filetrans"

# ---- cookie 发送范围白名单（2026-10-04 安全审计 F2）----
# 为什么要有：play_addr / images 的 URL 取自**远端页面内容**，而下载时会把用户的抖音
# session cookie 一起发过去。以前"cookie 只发给抖音自己的域"这条不变量只靠上游行为维持、
# 代码里没钉——将来换分享 host、加跳转或上游返回第三方 URL，都会静默变成凭据外泄。
# 现在把它钉在代码里：不在名单内就**明确报错**，绝不静默降级成"不带 cookie 试一下"
# （那只会得到一个劣化文件，然后被时长守卫报成一句看不懂的"疑似劣化文件"）。
COOKIE_HOST_SUFFIXES = (
    "iesdouyin.com",     # 分享页本身
    "douyin.com",
    "douyinvod.com",
    "douyinpic.com",
    "snssdk.com",        # 实测：play_addr 实际落在这里（aweme.snssdk.com，2026-10-04 取自
    #                      本机当日 3 个不同 aweme_id 的真实分享页响应，三者一致）
    "amemv.com",
    "byteimg.com",
    "bytecdn.cn",
    "zjcdn.com",
)


def cookie_host_allowed(url: str) -> bool:
    """host 是否在 cookie 白名单内。

    边界必须写成「完全相等 或 以 '.'+域名 结尾」：直接 endswith("snssdk.com") 会放过
    evil-snssdk.com 与 snssdk.com.attacker.com 这两类仿冒（下面 selftest 有对应用例）。
    """
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in COOKIE_HOST_SUFFIXES)

FNM_GLOB = os.path.join(
    os.path.expanduser("~"), ".local/share/fnm/node-versions/*/installation/bin"
)


def die(msg: str, code: int = 1) -> None:
    print(f"[douyin2txt] ❌ {msg}", file=sys.stderr)
    raise SystemExit(code)


def load_cookies() -> str:
    if not COOKIE_FILE.exists():
        die(
            f"cookie 文件不存在：{COOKIE_FILE}。"
            "请提供一份 DevTools curl 里的 -b 串（单行 k=v; k=v 格式）写入该文件。"
        )
    return COOKIE_FILE.read_text(encoding="utf-8").strip()


def find_tool(name: str) -> str:
    """DSH 环境 PATH 极简：按名字找不到就补常见前缀目录。"""
    p = shutil.which(name)
    if p:
        return p
    for cand in (
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
    ):
        if os.path.exists(cand):
            return cand
    if name == "bl":
        import glob as _g

        hits = sorted(_g.glob(os.path.join(FNM_GLOB, "bl")))
        if hits:
            return hits[-1]
    die(f"找不到工具 {name}（PATH 极简环境，且常见前缀目录里也没有）")


def tool_env() -> dict:
    """子进程 env：前置 fnm node bin（bl 是 #!/usr/bin/env node 的 shim）。"""
    env = dict(os.environ)
    extra = FNM_GLOB
    import glob as _g

    hits = _g.glob(FNM_GLOB)
    if hits:
        extra = hits[-1]
    env["PATH"] = extra + os.pathsep + env.get("PATH", "")
    return env


def http_get(url: str, *, cookie: str = "", referer: str = "", binary: bool = False):
    # cookie 发送范围的**唯一收口点**：调用方不需要各自记得判断，将来新增调用点也不会漏。
    if cookie and not cookie_host_allowed(url):
        die(
            f"拒绝把 cookie 发往非白名单域：{urlparse(url).hostname or url!r}。"
            f"当前白名单 COOKIE_HOST_SUFFIXES = {COOKIE_HOST_SUFFIXES}，"
            "定义在 scripts/douyin2txt.py 顶部。抖音若启用了新 CDN 域，"
            "确认该域确属抖音所有后再加进去——这是有意卡住的，别绕过。",
            code=2,
        )
    req = urllib.request.Request(url, headers={"User-Agent": IPHONE_UA})
    if cookie:
        req.add_header("Cookie", cookie)
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", errors="replace")


def resolve_share_url(link: str) -> tuple[str, str, str]:
    """短链 → (kind, id, 直达页 URL)。"""
    req = urllib.request.Request(link, headers={"User-Agent": IPHONE_UA}, method="HEAD")
    eff = link
    with urllib.request.urlopen(req, timeout=30) as r:
        eff = r.url
    m = re.search(r"(video|note)/(\d+)", eff)
    if not m:
        die(f"链接未解析出 video/note ID：{eff}")
    return m.group(1), m.group(2), eff


def parse_share_page(html: str) -> dict:
    m = re.search(r"_ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not m:
        die("分享页没有 _ROUTER_DATA（页面结构变更或被风控）")
    data = json.loads(m.group(1))
    for _k, v in (data.get("loaderData") or {}).items():
        if "page" in _k and isinstance(v, dict):
            items = (v.get("videoInfoRes") or {}).get("item_list") or []
            if items:
                return items[0]
    die(
        "item_list 为空 = cookie 失效。请提供一份新的 DevTools curl 的 -b 串覆盖 "
        f"{COOKIE_FILE}",
        code=2,
    )


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe_duration(video: Path) -> float:
    ffprobe = find_tool("ffprobe")
    p = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video),
        ]
    )
    try:
        return float(p.stdout.strip())
    except ValueError:
        die(f"ffprobe 读不出容器时长（文件损坏）：{p.stderr[:200]}")


def extract_audio_mp3(video: Path, out_mp3: Path) -> None:
    ffmpeg = find_tool("ffmpeg")
    p = run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "32k",
            str(out_mp3),
        ]
    )
    if p.returncode != 0 or not out_mp3.exists() or out_mp3.stat().st_size < 10_000:
        die(f"提音失败：{p.stderr[:300]}")


def resolve_api_key(cli_key: str | None) -> str:
    if cli_key:
        return cli_key
    env = os.environ.get("DASHSCOPE_API_KEY", "")
    if env:
        return env
    if AGENTMEMORY_ENV.exists():
        for line in AGENTMEMORY_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    die(
        "找不到百炼 API key。传 --api-key，或设 DASHSCOPE_API_KEY，"
        f"或在 {AGENTMEMORY_ENV} 写 OPENAI_API_KEY=",
        code=2,
    )


def asr_transcribe(mp3: Path, key: str, asr_json_out: Path) -> str:
    """异步 filetrans（qwen-audio-3.1-asr-flash-filetrans）：上传内置，长音频整片直送。

    2026-10-04 由 fun-asr 换入，与本地视频分支统一。实测 6.9s MP3 32k 走通，
    返回带 properties.original_duration_in_milliseconds（覆盖守卫要用）。
    """
    bl = find_tool("bl")
    # key 走**环境变量**而不是 --api-key：argv 在进程表里对同机其他用户可见
    #（macOS/Linux 的 ps 默认就能看到别人的完整命令行）。2026-10-04 实测 bl 认
    # DASHSCOPE_API_KEY（显式 env -u OPENAI_API_KEY 仍能跑通），所以这里等价但更安全。
    # tool_env() 是 os.environ 的副本，后写覆盖前写：--api-key 传的值仍然优先。
    env = tool_env()
    env["DASHSCOPE_API_KEY"] = key
    p = run(
        [
            bl,
            "speech",
            "recognize",
            "--url",
            str(mp3),
            "--model",
            ASR_MODEL,
            "--language",
            "zh",
            "--output",
            "text",
            "--out",
            str(asr_json_out),
        ],
        env=env,
        timeout=600,
    )
    if p.returncode != 0:
        die(f"ASR 转写失败（{ASR_MODEL}）：{p.stderr[-400:]}")
    if not asr_json_out.exists():
        die("bl 未产出 --out JSON")
    d = json.loads(asr_json_out.read_text(encoding="utf-8"))
    props = d.get("properties") or {}
    got_ms = props.get("original_duration_in_milliseconds") or 0
    want_ms = int(mp3.stat().st_size * 8 / 32)  # 32kbps
    if got_ms and abs(got_ms - want_ms) > DURATION_TOLERANCE_S * 1000 * 2:
        die(
            f"转写覆盖不足：服务端收到 {got_ms/1000:.1f}s，音频实际 {want_ms/1000:.1f}s"
            "（上传被截断）"
        )
    texts = [t.get("text", "") for t in d.get("transcripts") or []]
    return "\n".join(x for x in texts if x).strip()


def main() -> None:
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("link")
    ap.add_argument("--out", help="正文写入该文件（默认只打印）")
    ap.add_argument("--api-key")
    ap.add_argument("--images", action="store_true", help="只下载图片（图片/轮播帖）")
    args = ap.parse_args()

    cookie = load_cookies()
    tmp = Path(tempfile.gettempdir())
    # mkdtemp 而不是 tmp/f"{TMP_PREFIX}{pid}"。理由按平台分（别照抄一句"世界可读"就
    # 当成到处成立——2026-10-04 实测纠正过一次，见下）：
    #   Linux：TMPDIR 通常未设置 → gettempdir() 返回 /tmp（1777）。旧写法在那里既是可预测
    #     又是全局可读，同机攻击者可预建同名目录或在里面放符号链接（write_bytes /
    #     ffmpeg -y 都跟随符号链接）诱导覆写任意可写文件，转写正文也人人可读。
    #     **本机 macOS 无法实测这一条**，是按 Python 标准行为推断。
    #   macOS：TMPDIR 指向每用户目录，实测 /var/folders/…/T 本身已是 0700，别人连列目录
    #     都做不到 —— 所以这里旧写法的"全局可读"**不成立**，只剩同用户可预测这一条（价值有限，
    #     同用户本就能读你的数据）。
    # mkdtemp 一次解决两者：原子创建、名字不可预测、自身 0700。
    # 刻意**不删** work：图片分支打印的图片路径、视频分支打印的 workdir 都是文档化产出，
    # 删了就是毁约。这里只保证它私密。
    work = Path(tempfile.mkdtemp(prefix=TMP_PREFIX, dir=tmp))

    t = {}
    t["triage"] = time.time() - t0
    _p0 = time.time()
    kind, aweme_id, _eff = resolve_share_url(args.link)
    html = http_get(
        f"https://www.iesdouyin.com/share/{kind}/{aweme_id}/", cookie=cookie
    )
    item = parse_share_page(html)
    desc = (item.get("desc") or "").strip()
    t["triage"] = time.time() - _p0

    # ---------- 图片/轮播分支 ----------
    images = item.get("images") or []
    if images or args.images:
        if not images:
            die("该帖没有 images 字段（是视频帖？去掉 --images 重跑）")
        paths = []
        for i, img in enumerate(images, 1):
            url = (img.get("url_list") or [""])[0]
            if not url:
                continue
            dst = work / f"img_{i}.jpeg"
            dst.write_bytes(
                http_get(url, cookie=cookie, referer="https://www.iesdouyin.com/", binary=True)
            )
            paths.append(str(dst))
        print(json.dumps({"type": "images", "desc": desc, "images": paths}, ensure_ascii=False, indent=2))
        return

    # ---------- 视频分支 ----------
    va = (item.get("video") or {}).get("play_addr") or {}
    urls = va.get("url_list") or []
    if not urls:
        die("item 里没有 video.play_addr.url_list")
    play = urls[0].replace("playwm", "play")
    dur_ms = (item.get("video") or {}).get("duration") or 0

    _p = time.time()
    video = work / "video.mp4"
    data = http_get(
        play, cookie=cookie, referer="https://www.iesdouyin.com/", binary=True
    )
    video.write_bytes(data)
    t["dl"] = time.time() - _p

    if video.stat().st_size < 100_000:
        die("下载文件 <100KB：直链过期（302 落到错误页），重跑一次取新直链")

    # 【核心守卫】劣化文件检测：无 cookie / CDN 抽风时返回的文件容器时长会
    # 明显小于分享页元数据（实测 209.2s vs 255.7s，且 afconvert 不报错）。
    container = probe_duration(video)
    if dur_ms and abs(container - dur_ms / 1000) > DURATION_TOLERANCE_S:
        die(
            f"疑似劣化文件：容器时长 {container:.1f}s ≠ 元数据 {dur_ms/1000:.1f}s。"
            "多为 cookie 失效/CDN 风控导致——更新 cookie 后重跑（下载必须带 cookie）。"
        )

    _p = time.time()
    mp3 = work / "audio.mp3"
    extract_audio_mp3(video, mp3)
    t["conv"] = time.time() - _p

    _p = time.time()
    key = resolve_api_key(args.api_key)
    asr_json = work / "asr.json"
    text = asr_transcribe(mp3, key, asr_json)
    t["asr"] = time.time() - _p

    total = time.time() - t0
    print(
        json.dumps(
            {
                "type": "video",
                "aweme_id": aweme_id,
                "desc": desc,
                "duration_s": round(container, 1),
                "text": text,
                "timing": {k: round(v, 1) for k, v in t.items()} | {"total": round(total, 1)},
                "workdir": str(work),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[douyin2txt] 正文已写入 {args.out}", file=sys.stderr)

    try:
        with TIMING_LOG.open("a", encoding="utf-8") as f:
            f.write(
                "\t".join(
                    [
                        time.strftime("%Y-%m-%d"),
                        aweme_id,
                        f"triage={t['triage']:.0f}",
                        f"dl={t['dl']:.0f}",
                        f"conv={t['conv']:.0f}",
                        f"asr={t['asr']:.0f}",
                        f"total={total:.0f}",
                        f"chars={len(text)}",
                    ]
                )
                + "\n"
            )
    except OSError:
        pass


if __name__ == "__main__":
    main()
