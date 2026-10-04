#!/usr/bin/env python3
"""视频 → 任意语言对的双语 ASS 字幕，时间轴与视频一一对应。

任意源语言 → 任意语言对（默认 en,zh）：**译文在上、原文沉底**——中文片出英上中下、
英文片出中上英下（用户截图参照）。在上的那行用醒目的 Upper 样式（6.5% 画面高、加粗、纯白），
在下的用 Lower（4.0%、常规、暖白）。源语言若在语言对里，该行直接复用原文，不翻译、不花钱。

复用优先（不重复造轮子）：能拿到现成字幕就不转写——
  1. 内嵌字幕轨（MKV/MP4 的 srt/ass 文本轨，含 SDH）→ ffprobe 探轨 + ffmpeg 抽取，零 ASR 成本
  2. 同目录外挂字幕（<视频基名>.srt / .ass / .vtt）→ 直接复用
  3. 都没有才转写（ASR）→ ffmpeg 抽 16k 单声道 + bl 异步 filetrans 拿句级/词级时间戳
再翻译：默认云端直连 dashscope（qwen3.7-flash，显式关思考），--backend local 可切本地 llama.cpp / mlx-lm
的 OpenAI 兼容服务。

为什么出 ASS 而不是 SRT：SubRip 格式**没有任何样式位**，字号/颜色/加粗/描边都无处安放，
"上面的字幕更醒目"这类要求只能由 ASS 承载（每行一个样式，正文里用 \\r 按行切换）。

工具链：ffmpeg / ffprobe（**走 PATH 优先，再按平台兜底**：macOS 常见 /opt/homebrew、Linux /usr/bin、
Windows C:\\ffmpeg\\bin）、bailian CLI（bl，ASR 与云端翻译；Windows 上是 bl.cmd，过 cmd /c 执行）、
可选 llama-server（本地翻译）。
注意：qwen-mt-turbo 与 gummy-* 于 2026-10-10 下线。qwen-mt-* 整系已于 2026-10-04 撤出
可选项（用户令），现在只有通用指令模型一条翻译路径。

跑法：
  python omnisub.py <video> [--out <dir>] [--source auto|embedded|sidecar|asr]
      [--source-lang auto|en|zh|ja|ko|fr|…] [--subtitles en,zh] [--sub-index N]
      [--asr-model ...] [--chat-model qwen3.7-flash] [--backend cloud|local] [--local-server URL]
      [--asr-json <已有.json>] [--no-translate] [--batch 20] [--workers 4]
      [--refresh-source] [--verify-sync auto|on|off] [--limit N] [--no-log]
      [--cache-dir <中间产物目录>]
  （--target-lang 已废弃，等价于 --subtitles <源语言>,<目标语言>，仅为兼容保留）

产出：**视频目录只多出一个 <视频基名>.ass**（双语，或 --no-translate 时的单语）——
  中间产物（<基名>.source.srt / .source.json / .asr.json / .<lang>.json）默认写进平台缓存目录
  （macOS ~/Library/Caches/omnisub/、Linux $XDG_CACHE_HOME/omnisub/、
  Windows %LOCALAPPDATA%\\omnisub\\Cache），可用 --cache-dir 改；这样既能"重切不重付"，
  又不往片库里堆文件。
与视频同名同目录即被 mpv / IINA / VLC / MPC-HC / PotPlayer / Infuse 自动加载。
媒体库（Plex / Jellyfin / Emby）对外挂 ASS 支持不一，必要时由用户自行改扩展名或重新封装。
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import urllib.request
import time
from pathlib import Path

def _tool_candidates(name: str) -> list[str]:
    """按平台给出可执行文件的兜底路径（先看 PATH，找不到才看这里）。"""
    home = Path.home()
    out: list[str] = []
    if sys.platform == "darwin":
        out += [f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"]
    elif os.name == "nt":
        out += [rf"C:\ffmpeg\bin\{name}.exe", rf"C:\Program Files\ffmpeg\bin\{name}.exe"]
    else:
        out += [f"/usr/bin/{name}", f"/usr/local/bin/{name}", f"/snap/bin/{name}"]
    out += [str(home / ".local/bin" / name), str(home / "bin" / name)]
    return out


def find_tool(name: str, aliases: tuple[str, ...] = ()) -> str:
    """定位外部可执行文件：**优先 PATH**（macOS/Linux/Windows 通用），再按平台兜底。

    aliases 用于 Windows 的 shim 后缀（npm 装出来的是 `bl.cmd` 而不是 `bl`）。
    都没有就原样返回名字，让子进程抛出"找不到命令"，比在这里编一个假路径清楚。
    """
    for candidate in (name, *aliases):
        found = shutil.which(candidate)
        if found:
            return found
    for cand in _tool_candidates(name):
        if Path(cand).exists():
            return cand
    return name


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
VERSION = "1.2.0"
SKILL_DEST_DEFAULT = Path.home() / ".dsh/skills/omnisub"   # DSH 加载技能的目录（--install 目标）


def code_rev() -> str:
    """本脚本内容的短指纹。

    启动横幅里打印它；与 `--install` 打印的指纹一致，就说明 DSH 里跑的就是当前这份代码。
    以前确认这件事靠人工 `md5` 比对"仓库副本 vs 技能副本"——那是流程长，不是严谨。
    程序只要不改代码就永远自洽，所以把这一步固化进程序，不再让 Agent 手工做。
    """
    try:
        return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()[:8]
    except OSError:
        return "unknown"


ASR_MODEL_DEFAULT = "qwen-audio-3.1-asr-flash-filetrans"
# 翻译默认模型 = qwen3.7-flash（2026-09-28 用户定版：走 dashscope API 按量流量，key 仍是
# ~/.agentmemory/.env 那把；token-plan 不含此模型，实测 404 Model not exist）。
# 为什么换：qwen-mt-flash 是 MT 专用模型，对"透明型习语"只会字面直译——
# "if it's not one thing, it's another" → 「要不是一件事，就是另一件事」；且加风格指令 /
# 原生 translation_options.domains / 升 qwen-mt-plus 全部无效（同日 A/B 实测，见 ASR-API.md）。
# qwen3.7-flash + 风格指令实测意译（「麻烦事一桩接一桩，没完没了」），30 条整批 30/30
# 标记协议服从、7.5 s/批；0.2/0.4 元每百万 tokens ≈ 0.01 元/集。qwen3-max 本项目禁用（用户令）。
# qwen-mt-* 整系已于 2026-10-04 撤出可选项（用户令），传了会被 main() 明确拒绝。
# 保留这段 A/B 记录是因为它就是"为什么撤"的证据，不是给后人复用的入口。
CHAT_MODEL_DEFAULT = "qwen3.7-flash"
LOCAL_SERVER_DEFAULT = "http://127.0.0.1:8080"
DELIM = "\n|||\n"


def local_prompt(src_lang: str, tgt_lang: str) -> str:
    """本地后端的 Hy-MT2 分隔符模板。方向必须跟着语言对走，不能写死中文。"""
    return (f"请将以下{lang_name(src_lang)}文本准确翻译为{lang_name(tgt_lang)}。"
            "你必须在译文中保留等量的分隔符 ||| ，绝对不可遗漏、转义或翻译该符号，"
            "并注意分隔符的位置。\n\n")



TEXT_SUB_CODECS = {"srt", "subrip", "ass", "ssa", "mov_text", "webvtt", "text"}
SIDECAR_EXTS = (".srt", ".ass", ".ssa", ".vtt")
MAX_LINE_CHARS = 42          # 仅 ASR 路线需要重切时用
MAX_CUE_MS = 7000
MIN_CUE_MS = 800
# 与下一句紧贴时优先让位给下一句；让不出 MIN_READABLE_MS 就退回 MIN_CUE_MS
# （宁可 0.5s 极短重叠，也不出 0.2s 闪现——实测 E08 全片 684 条里只触发 1 次）
MIN_READABLE_MS = 400
MIN_GAP_MS = 40
SOURCE_SUFFIX = ".source"     # 原文缓存：<基名>.source.srt / .source.json（自家中间产物）
MONO_SUFFIX = ".mono"         # 只出原文时的单语产物：<基名>.mono.ass
REQUESTS_PER_MIN = 50   # 数字沿用 qwen-mt-flash 时代的实测限额（60 次/分钟 + 3.5 万 token/分钟）留安全余量；
                        # qwen3.7-flash 的官方限额未重新查证，改模型时这个数要重新确认
TRANSLATE_BATCH = 30   # 每批条数。实测（E04，279 单元，--no-cache A/B）：20 条/批 18.0–18.5s、
                       # 30 条/批 12.7–13.8s——批量大 → 请求少 → 更快贴到 50 次/分钟限速下限。
                       # 40 条/批的旧结论（"大量触发二分"）是**漏末行标记**造成的，那个毛病已修
                       # （见 _ask_cloud：只缺末行就单独补问），但 30 已足够快，不再往上试。
TRANSLATE_TIMEOUT = 180   # bl --timeout：实测 8 并发下偶发 ETIMEDOUT，给足时间
TRANSLATE_ATTEMPTS = 3    # 单批最多重试次数（失败后二分）
ASR_TIMEOUT = 600
ASR_MP3_KBPS = 32   # 送 ASR 的音频：16kHz 单声道有损。实测整片 58.6 分钟 FLAC 126.6MB / MP3 14MB，
                    # 上传体积差 9 倍，而上传占转写阶段大头；ASR 不需要无损，--audio-lossless 可回退
ASR_PROBE_SECONDS = 180   # 源语言探测样本长度（见 probe_source_lang）
ASR_PROBE_SKIP = 30       # 探测样本跳过片头（音乐/静音）
ASR_CHUNK_DEFAULT = 0     # 分片并行转写：0=按片长自动（长片 4 段，短片 1 段），见 resolve_asr_chunks
ASR_CHUNK_AUTO = 4        # 自动模式下的段数
ASR_CHUNK_MIN_SECONDS = 600   # 短于 10 分钟不切片：段太小，切片/上传开销盖过并行收益


# 各 ASR 模型的**单次请求音频上限**（实测得来，不是抄的）：
#   · qwen-audio-3.1-asr-flash（同步版）**300 秒**——2026-09-28 实测：5 分钟的切片直接报
#     `AUDIO_DURATION_TOO_LONG: audio duration (300001.0ms) over service process (300s)`。
#   · filetrans 系（qwen-audio-3.1-asr-flash-filetrans 等异步录音文件识别）按"录音文件"处理，
#     整集直送即可，这里给一个宽上限、不做约束。
# 为什么必须有这张表：同步版 300 秒上限撞上"自动 4 段"（50 分钟片 = 750 秒/段）会**每段都失败**，
# 而失败报文是服务端错误、不属于配置类，旧版会当成网络抖动去重试——白等又白花钱。
ASR_MODEL_MAX_SECONDS = {
    "qwen-audio-3.1-asr-flash": 300.0,
    "qwen-audio-asr": 300.0,
    "qwen-audio-asr-latest": 300.0,
}
ASR_MODEL_DEFAULT_MAX_SECONDS = 0.0        # 0 = 不认识就不拦（保持现状）


def resolve_asr_chunks(want: int, duration: float, model: str = "") -> int:
    """把 `--asr-chunk` 解析成实际段数（0=自动），并**按模型单次上限兜底**。

    自动取 4 段的实测依据（E04 同集同提示 A/B）：ASR 55.7s → 24.0s（2.3×）、整程 118.5s → 96.0s；
    质量上词级相似度 0.985（差异为 ASR 随机性，三个分片边界附近无截断），且 E02 的整句语言漂移
    从 22/373 条降到 0 条。

    模型上限那一层（2026-09-28 加）：`--asr-model qwen-audio-3.1-asr-flash` 这类同步模型单请求
    只吃 300 秒，而"自动 4 段"对 50 分钟的片是 750 秒/段 → **每段都会被服务端拒**
    （AUDIO_DURATION_TOO_LONG）。所以：
      · 自动（want=0）：段数取到能满足上限为止（50 分钟片 → 11 段，273 秒/段）；
      · 显式给了太小的段数：**当场报错并给出建议值**，不等到 11 个请求全烧完才失败。
    """
    limit = ASR_MODEL_MAX_SECONDS.get(model, ASR_MODEL_DEFAULT_MAX_SECONDS)
    need = int(duration // limit) + 1 if (limit > 0 and duration > limit) else 0
    if want and want > 0:
        if need and duration / want > limit:
            raise SystemExit(
                f"[asr] --asr-chunk {want} 与模型上限冲突：{model} 单次只吃 {limit:.0f} 秒，"
                f"而 {duration:.0f} 秒的片切成 {want} 段是 {duration / want:.0f} 秒/段。\n"
                f"[asr] 改成 --asr-chunk {need}（或更大），或换回异步的 filetrans 模型"
                f"（qwen-audio-3.1-asr-flash-filetrans，整集直送）。")
        return want
    auto = ASR_CHUNK_AUTO if duration >= ASR_CHUNK_MIN_SECONDS else 1
    if need and need > auto:
        print(f"[asr] {model} 单次上限 {limit:.0f} 秒 → 自动段数 {auto} 提到 {need}"
              f"（{duration / need:.0f} 秒/段）", flush=True)
        return need
    return auto

# ---------------- 语言：任意源语言 → 任意语言对的双语字幕 ----------------
# 交付契约：**译文在上（醒目）、原文沉底**（display_order()）；--subtitles 的顺序决定译文行之间的
# 次序，源不在语言对里时即整体行序。中文片→英上中下、英文片→中上英下（用户截图参照）。
# 源语言若出现在语言对里，该行直接用原文（不翻译、不花钱）；不在则整对都翻译。
LANGS_DEFAULT = ("en", "zh")
LANG_NAMES = {
    "zh": "简体中文", "en": "英文", "ja": "日文", "ko": "韩文", "fr": "法文",
    "de": "德文", "es": "西班牙文", "ru": "俄文", "pt": "葡萄牙文", "it": "意大利文",
    "ar": "阿拉伯文", "th": "泰文", "vi": "越南文", "id": "印尼文", "tr": "土耳其文",
    "hi": "印地文", "nl": "荷兰文", "pl": "波兰文", "sv": "瑞典文", "ms": "马来文",
    "ta": "泰米尔文", "te": "泰卢固文", "kn": "卡纳达文", "ml": "马拉雅拉姆文",
    "bn": "孟加拉文", "gu": "古吉拉特文", "pa": "旁遮普文", "or": "奥里亚文",
    "si": "僧伽罗文", "my": "缅甸文", "km": "高棉文", "lo": "老挝文",
    "ka": "格鲁吉亚文", "hy": "亚美尼亚文", "am": "阿姆哈拉文", "bo": "藏文", "mn": "蒙古文",
}
# ffprobe 的 ISO639-2/B、bl 的 ISO639-1、带地区码的标签都要能认
LANG_ALIASES = {
    "zh-cn": "zh", "zh-hans": "zh", "chs": "zh",
    "chi": "zh", "zho": "zh", "cmn": "zh",
    "eng": "en", "en-us": "en", "en-gb": "en",
    "jpn": "ja", "jp": "ja", "ja-jp": "ja",
    "kor": "ko", "ko-kr": "ko",
    "fre": "fr", "fra": "fr", "deu": "de", "ger": "de", "spa": "es", "rus": "ru",
    "por": "pt", "ita": "it", "ara": "ar", "tha": "th", "vie": "vi", "ind": "id",
    "tur": "tr", "hin": "hi", "nld": "nl", "dut": "nl", "pol": "pl", "swe": "sv",
    "msa": "ms", "may": "ms",
}
# 繁体中文单独成一个码：旧版把 zh-TW/zh-Hant 一律并成 zh，于是用户要繁体、拿到简体，
# 全程没有任何提示。归成 zh-Hant 后它就是一个独立目标，提示词写"繁體中文"。
LANG_HANT = {"zh-tw", "zh-hk", "zh-mo", "zh-hant", "zh-hant-tw", "cht"}
LANG_NAMES["zh-Hant"] = "繁體中文"
# 有**独占文字区**的语言：能用 Unicode 区块直接判定，不需要语言模型。
# ko 必须排在 ja/zh 之前（谚文与汉字互不相交，但 ja 的判据要用到汉字，顺序不能乱）。
SCRIPT_LANGS = {
    "ko": re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]"),   # 谚文
    "ja": re.compile(r"[\u3040-\u309f\u30a0-\u30ff]"),                   # 平假名/片假名
    "ru": re.compile(r"[\u0400-\u04ff]"),                                 # 西里尔
    "ar": re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]"),   # 阿拉伯
    "he": re.compile(r"[\u0590-\u05ff]"),                                 # 希伯来
    "el": re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]"),                  # 希腊
    "th": re.compile(r"[\u0e00-\u0e7f]"),                                 # 泰文
    "hi": re.compile(r"[\u0900-\u097f]"),                                 # 天城文（印地/尼泊尔）
    "bn": re.compile(r"[\u0980-\u09ff]"),                                 # 孟加拉
    "pa": re.compile(r"[\u0a00-\u0a7f]"),                                 # 古尔穆奇（旁遮普）
    "gu": re.compile(r"[\u0a80-\u0aff]"),                                 # 古吉拉特
    "or": re.compile(r"[\u0b00-\u0b7f]"),                                 # 奥里亚
    "ta": re.compile(r"[\u0b80-\u0bff]"),                                 # 泰米尔
    "te": re.compile(r"[\u0c00-\u0c7f]"),                                 # 泰卢固
    "kn": re.compile(r"[\u0c80-\u0cff]"),                                 # 卡纳达
    "ml": re.compile(r"[\u0d00-\u0d7f]"),                                 # 马拉雅拉姆
    "si": re.compile(r"[\u0d80-\u0dff]"),                                 # 僧伽罗
    "lo": re.compile(r"[\u0e80-\u0eff]"),                                 # 老挝
    "bo": re.compile(r"[\u0f00-\u0fff]"),                                 # 藏文
    "my": re.compile(r"[\u1000-\u109f]"),                                 # 缅甸
    "ka": re.compile(r"[\u10a0-\u10ff]"),                                 # 格鲁吉亚
    "hy": re.compile(r"[\u0530-\u058f]"),                                 # 亚美尼亚
    "am": re.compile(r"[\u1200-\u137f]"),                                 # 阿姆哈拉（埃塞俄比亚）
    "km": re.compile(r"[\u1780-\u17ff]"),                                 # 高棉
    "mn": re.compile(r"[\u1800-\u18af]"),                                 # 蒙古
    "zh": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]"),                  # 汉字（放最后：日文含汉字）
}
# 拉丁字母语言共用字母区：区块分不出英/法/德/西，只能用英文虚词密度筛一轮（见 detect_lang）
LATIN_LANGS = {"en", "fr", "de", "es", "pt", "it", "nl", "pl", "sv", "tr", "vi", "id", "ms"}
# 明显不属于英语的高频虚词：用来兜住"样本太短、虚词密度判不出来"的拉丁字母文本。
# 只收**不会与英语撞车**的词（die/am/man/per/con 这类双语词一律不收）。
NON_EN_MARKERS = re.compile(
    r"\b(?:le|les|des|une|vous|nous|est|pas|pour|dans|avec|qui|comment|merci|bonjour|"
    r"der|und|ist|nicht|ich|wir|auch|mit|für|eine|das|"
    r"los|las|una|para|como|pero|muy|"
    r"della|sono|gli|het|een|niet|dat|voor|zijn|"
    r"não|mas|muito|bir|ve|için|ama|của|và|là|không|"
    r"yang|dan|tidak|untuk|dengan)\b", re.I)
EN_STOPWORDS = re.compile(
    r"\b(?:the|and|you|your|that|this|it|is|are|was|were|be|to|of|in|on|at|for|with|from|"
    r"have|has|had|do|does|did|not|but|they|them|we|us|he|she|his|her|our|their|there|here|"
    r"what|when|where|why|who|how|will|would|can|could|should|just|like|about|out|up|down|"
    r"all|one|know|get|got|go|going|want|need|see|think|say|said|right|okay|yeah|good|no|yes)\b",
    re.I)
# ASR 的**整句语言漂移**判据（见 source_drift）：罗曼语/德语的高频虚词。
# 只收"英语句子几乎不会出现"的词；la/en/ne 这类短词也收（靠 EN_STRONG 的"且"兜住误报）。
DRIFT_MARKERS = re.compile(
    r"\b(?:el|la|los|las|una|un|del|con|por|para|que|es|son|está|están|más|pero|como|muy|también|"
    r"le|les|des|du|une|et|sont|dans|avec|pour|qui|ils|elles|nous|vous|pas|ne|mais|tout|cette|ces|"
    r"der|die|das|und|ist|sind|nicht|mit|für|eine|ich|wir|auch)\b", re.I)
# 漂移判据只认**强英语虚词**：no / one / all / so / as / like 这些与西语/德语撞车，
# 用它们当"这是英语"的证据会把 `no mucho pero suficiente…` 这类真漂移整条放过（实测漏 4 条）。
EN_STRONG = re.compile(
    r"\b(?:the|and|of|to|in|is|are|was|were|that|this|it|for|with|from|they|their|them|"
    r"you|your|he|she|his|her|we|our|us|but|not|have|has|had|will|would|can|could|should|"
    r"there|here|what|when|where|why|who|how)\b", re.I)


def normalize_lang(code: str | None) -> str:
    """3 字母 / 带地区码 / 大小写不一的语言标签 → 统一的语言码。

    只对一个语言保留区分度：繁体中文（zh-TW / zh-HK / zh-Hant / cht）归成 `zh-Hant`，
    而不是并进 `zh`（=简体）——否则用户点名要繁体却拿到简体，且全程无声。
    """
    c = (code or "").strip().lower().replace("_", "-")
    if not c or c == "und":            # und = ffprobe 的"未定义"，按未知处理
        return ""
    if c in LANG_HANT:
        return "zh-Hant"
    return LANG_ALIASES.get(c, c.split("-")[0])


def lang_name(code: str) -> str:
    """给 MT 提示词用的语言名。认不出的语言直接回落到语言码本身（模型多数也能懂）。"""
    c = normalize_lang(code)
    if not c:
        return "原文"          # 源语言没判出来时的兜底措辞（不能写"英文"，那是在撒谎）
    return LANG_NAMES.get(c, code)


def parse_langs(spec: str) -> tuple[str, ...]:
    """`en,zh` / `en zh` → ('en','zh')。校验非空、无重复。"""
    out = [normalize_lang(x) for x in re.split(r"[,\s]+", spec or "") if x.strip()]
    out = [c for c in out if c]
    if not out:
        raise SystemExit(f"语言对解析为空：{spec!r}")
    dup = {c for c in out if out.count(c) > 1}
    if dup:
        raise SystemExit(f"语言对里有重复语言：{'、'.join(sorted(dup))}（{spec!r}）")
    return tuple(out)


def looks_like_lang(text: str, lang: str) -> bool:
    """粗判"这段文本是不是已经是该语言"。

    只用于两件事：① 决定某个语言是否可以直接复用原文；② 判断译文是否漏译。

    判据分两层：目标语言有**独占文字区**（韩/日/俄/阿/希/泰/天城文/汉字）就看该文字是否出现；
    拉丁字母语言（英/法/德/西…）则要求"有拉丁字母且没有其他文字区"。
    旧版只认 zh/en，于是 `--subtitles en,ja` 这类非中英目标下**每条译文都被判成漏译**：
    白跑两轮补译，真漏译还修不好（补译结果被同一条判据丢弃）。
    """
    t = text or ""
    base = normalize_lang(lang).split("-")[0]
    if base in SCRIPT_LANGS:
        if base == "ja":
            # 日文必然混假名：只有汉字时更可能是中文，不认作日文
            return bool(SCRIPT_LANGS["ja"].search(t))
        if base == "zh":
            return bool(SCRIPT_LANGS["zh"].search(t)) and not SCRIPT_LANGS["ja"].search(t)
        return bool(SCRIPT_LANGS[base].search(t))
    if base in LATIN_LANGS:
        return bool(re.search(r"[A-Za-z]", t)) and not any(p.search(t) for p in SCRIPT_LANGS.values())
    return False


# "这条不是空/纯符号"的判据：任意文字或数字都算有内容（韩/俄/阿/泰/纯假名日文全算）。
# 旧版写死 [0-9A-Za-z\u4e00-\u9fff]，把**非拉丁非汉字**的字幕整片当符号丢掉 ——
# 韩语 ASR 会直接"没有切出任何字幕条"退出，任意语言的支持在三条来源路径上全部失效。
HAS_CONTENT_CHAR = re.compile(r"[^\W_]", re.UNICODE)
# 只要"字"、不含数字：用于判断"这句值不值得翻译/补译"（纯号码行不必补译）
HAS_WORD_CHAR = re.compile(r"[^\W\d_]", re.UNICODE)
# 渲染用：**数字也算内容**（`100 000` 是有效字幕行，不能当纯符号丢掉）。
# 判据与"是不是纯符号行"同源（任意文字或数字都算有内容），只是这里不能把数字排除掉。
HAS_ALNUM = re.compile(r"[^\W_]", re.UNICODE)
# "这份字幕里有没有中日韩文字 / 有没有成串的拉丁词" —— 用来判"是不是真双语"（见 looks_bilingual）。
# 拉丁这一侧要**成串**（≥2 个字母）才算，免得中文行里夹一个字母就把纯中文字幕判成双语。
CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")


def detect_lang(texts: list[str]) -> tuple[str, bool]:
    """源语言兜底判定 → (语言码, 是否可信)。只在 --source-lang auto 且轨道没标签时用。

    **可信** = 由独占文字区判定（韩/日/俄/阿/希/泰/天城文/汉字），区块不会骗人。
    拉丁字母区无法由区块区分英/法/德/西（共用字母），只能用英文虚词密度筛一轮：
      · 像英文 → ("en", True)
      · 明显不像 → ("", False) —— 语言对里**每种语言都会被真正翻译**。返回空串是刻意的：
        旧版把一切非中日文本都判成 en，于是法文视频的"英文行"直接就是法文原文（实测证伪了
        "任意语言都能出中英"这句话），而且没有任何提示。
    样本太短（<30 词）时判不出外语，按英文放行——少花一次翻译钱，且短样本影响面极小。
    """
    sample = " ".join(t for t in texts if t)[:4000]
    if not sample:
        return "", False
    for code, pattern in SCRIPT_LANGS.items():
        if pattern.search(sample):
            return code, True
    words = re.findall(r"[A-Za-z][A-Za-z']*", sample)
    if len(words) < 30:
        # 样本太短，密度判不出来：先排除"看得出不是英文"的，再按英文放行（少花一次翻译钱）
        return ("", False) if NON_EN_MARKERS.search(sample) else ("en", True)
    density = len(EN_STOPWORDS.findall(sample)) / len(words)
    return ("en", True) if density >= 0.12 else ("", False)


def find_bl() -> str:
    """定位 bailian CLI（跨平台，复用 find_tool 的"PATH 优先 + 平台兜底"）。

    Windows 上 npm -g 装出来的是 `bl.cmd`：CreateProcess 不能直接执行 .cmd，
    必须过 `cmd /c`（见 `_bl_argv`）。
    """
    exe = find_tool("bl", ("bl.cmd", "bl.bat", "bl.exe"))
    if exe != "bl":
        return exe
    home = Path.home()
    appdata = os.environ.get("APPDATA")
    node_roots = [home / ".local/share/fnm/node-versions"]
    candidates: list[Path] = []
    if os.name == "nt" and appdata:
        candidates += [Path(appdata) / "npm" / "bl.cmd", Path(appdata) / "npm" / "bl"]
        node_roots.append(Path(appdata) / "fnm" / "node-versions")
    candidates.append(home / ".local/bin/bl")
    for root in node_roots:
        if root.exists():
            candidates += sorted(root.glob("*/installation/bin/bl"))
    for cand in candidates:
        if Path(cand).exists():
            return str(cand)
    raise SystemExit("找不到 bl（bailian-cli）：npm install -g bailian-cli（见 ASR-API.md）")


def _bl_argv(exe: str) -> list[str]:
    """Windows 的 .cmd/.bat shim 要先过 cmd /c，POSIX 下原样返回。"""
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe]
    return [exe]


def tool_env() -> dict:
    """给子进程用的 PATH：前置 ffmpeg/bl 所在目录与常见全局 bin。

    DSH 里 bash 子进程的 PATH 可能只有 /usr/bin:/bin:/usr/sbin:/sbin，
    而 ffsubsync 这类工具内部是按名字调 `ffmpeg` 的 —— 不前置就会静默失败。
    跨平台三点：分隔符用 os.pathsep（Windows 是 `;`）、Windows 补 %APPDATA%\\npm、
    目录**一律从工具实际所在位置推导**（不按版本号排序猜 fnm 目录：本机装过
    v24.13.0/v24.15.0/v26.7.0，挑"最新"会把 bl 的 `#!/usr/bin/env node` 换成另一个
    node 运行时；bl 所在目录同时也放着配套的 node，跟它走才对）。
    """
    env = os.environ.copy()
    parts: list[str] = []
    ffmpeg_dir = os.path.dirname(FFMPEG)
    if ffmpeg_dir:
        parts.append(ffmpeg_dir)
    parts.append(str(Path.home() / ".local/bin"))
    appdata = os.environ.get("APPDATA")
    if os.name == "nt" and appdata:
        parts.append(str(Path(appdata) / "npm"))
    try:
        parts.append(str(Path(find_bl()).parent))     # bl 与配套 node 同目录
    except SystemExit:
        pass
    env["PATH"] = os.pathsep.join(parts + [env.get("PATH", "")])
    return env


def default_cache_root() -> Path:
    """中间产物的默认缓存根目录（按平台惯例）。

    为什么不全写在视频目录：那些 `.source.srt` / `.source.json` / `.asr.json` / `.zh.json`
    都是中间产物，用户真正要的只有 `<基名>.ass` 一个文件（2026-09-27 明确要求）。
    放在这里既能"重切不重付"，又不污染片库目录。
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
        return Path(base) / "omnisub" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/omnisub"
    return Path(os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")) / "omnisub"


def bl_env() -> dict:
    """bl 专用环境（等价于 tool_env，保留名字是因为调用点多）。"""
    return tool_env()


def api_key(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("DASHSCOPE_API_KEY"):
        return os.environ["DASHSCOPE_API_KEY"]
    env_path = Path.home() / ".agentmemory/.env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("找不到 API key：传 --api-key 或设 DASHSCOPE_API_KEY（见 ASR-API.md）")


# ---------------- 0. 配置边界：哪些要用户配、哪些程序自己扛 ----------------
# 分工写死在程序里，出错时直接打印出来，省掉"Agent 猜 / 用户问"这一轮：
#   用户配：百炼 API key（一次）、模型开通与额度（一次）
#   程序扛：PATH/工具发现、编码参数、缓存与续跑、并发与限速、重试与二分、自检与配置指引
CONFIG_ERRORS = (
    (re.compile(r"invalid.?api.?key|incorrect api key|\b401\b|unauthor", re.I),
     "API key 无效 / 未授权",
     "把有效 key 写进 ~/.agentmemory/.env 的 OPENAI_API_KEY=，或运行时传 --api-key，或导出 DASHSCOPE_API_KEY"),
    (re.compile(r"arrearage|overdue|insufficient.?balance|欠费|余额不足", re.I),
     "账号欠费 / 余额不足",
     "登录阿里云百炼控制台（bailian.console.aliyun.com）充值"),
    (re.compile(r"freetieronly|free tier only|free quota exhausted|仅使用免费额度|免费额度耗尽", re.I),
     "该模型的「免费额度用完即停」开着（与余额无关）",
     "服务端开关原文叫「免费额度用完即停」：开着时免费额度一耗尽就**直接停服**、不看付费余额。"
     "去百炼控制台免费额度页（bailian.console.aliyun.com/cn-beijing/costing-balance/free-quota）"
     "把该模型这一行的开关关掉（或「批量操作 → 一键关闭所有模型」）；换 key 无用，"
     "要换的是这个开关。官方文档：help.aliyun.com/zh/model-studio/model-usage-statistics"),
    (re.compile(r"model.?not.?exist|model.?not.?found|not.?activated|未开通|未授权模型", re.I),
     "模型未开通 / 不存在",
     "在百炼控制台开通该模型，或用 --asr-model / --chat-model 换一个已开通的"),
    (re.compile(r"access.?denied|forbidden|\b403\b|permission", re.I),
     "这把 key 无权访问该模型",
     "在百炼控制台为该 key 开通/授权对应模型"),
    (re.compile(r"throttl|\b429\b|rate.?limit|requests? too many", re.I),
     "触发限流（临时）",
     "无需配置：等一会儿重跑即可，缓存会续跑；也可调小 --rpm / --workers"),
)


def diagnose_service(text: str) -> tuple[str, str] | None:
    """把服务端报错归类成"要用户配什么"。认出就返回 (问题, 配置动作)，认不出返回 None。"""
    for pattern, what, how in CONFIG_ERRORS:
        if pattern.search(text or ""):
            return what, how
    return None


def config_hint_lines(stage: str, detail: str) -> list[str]:
    """把"哪个阶段失败了 + 服务端报文"翻成可打印的配置指引（也供 --doctor 复用）。"""
    hit = diagnose_service(detail)
    out = [f"{stage} 失败"]
    if hit:
        out.append(f"原因：{hit[0]}")
        out.append(f"需要用户配置：{hit[1]}")
    else:
        out.append(f"服务端返回：{detail.strip()[:300]}")
        out.append("认不出是哪类配置问题：若是网络抖动直接重跑（缓存会续跑）；"
                   "若持续报错，请核对 key 与模型开通状态。")
    return out


def fail_config(stage: str, detail: str) -> None:
    """配置类错误：立即终止并给出配置指引（不二分、不重试、不烧钱）。永不正常返回。"""
    print(f"\n[config] {stage} 失败 → 已终止，不再重试。", file=sys.stderr)
    for line in config_hint_lines(stage, detail)[1:]:
        print(f"[config] {line}", file=sys.stderr)
    raise SystemExit(2)


def install_skill(dest: Path = SKILL_DEST_DEFAULT) -> None:
    """把本技能从源码**整树同步**到 DSH 技能目录（幂等）。

    为什么是"整树"而不是一份写死的清单（2026-09-28 实测教训）：旧版只拷
    SKILL.md/README.md/ASR-API.md/COMMERCIAL.md/LICENSE/.gitignore + scripts/omnisub.py +
    tools/ + 启动器，**完全不拷 evals/**——于是安装树里的 evals/ 停在很早的一版：既缺评测
    超时修复（500 秒级用例被 295 s 切断），也缺 DSH_BASE_URL 修复（key 与端点不同域 → 6/6 全 401）。
    用户从安装副本跑评测就会撞上已经修好的问题。**清单一定会过时，整树同步不会。**

    排除项只有构建/编辑器垃圾（__pycache__、*.pyc、.DS_Store、.git）；目标里多出来的
    文件**只警告不删除**（那是用户的目录，删东西不该是安装的副作用）。
    """
    src = Path(__file__).resolve().parent.parent
    dest = dest.expanduser().resolve()
    skip_dirs = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", "node_modules", ".venv"}
    skip_names = {".DS_Store"}
    copied = 0
    wanted: set[Path] = set()
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in skip_dirs for part in rel.parts):
            continue
        if path.name in skip_names or path.name.startswith("._") or path.suffix in (".pyc", ".pyo"):
            continue
        target = dest / rel
        wanted.add(rel)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    if (dest / "omnisub").exists():
        (dest / "omnisub").chmod(0o755)
    if not (dest / "scripts" / "omnisub.py").exists():
        print(f"[install] ⚠️ 源码目录不完整：{src / 'scripts/omnisub.py'} 不存在", file=sys.stderr)
    stale = []
    for path in dest.rglob("*"):
        rel = path.relative_to(dest)
        if any(part in skip_dirs for part in rel.parts) or path.name in skip_names:
            continue
        if path.is_file() and rel not in wanted:
            stale.append(rel)
    rev = hashlib.sha256((dest / "scripts" / "omnisub.py").read_bytes()).hexdigest()[:8]
    print(f"[install] 源   {src}")
    print(f"[install] 目标 {dest}（{copied} 个文件，整树同步"
          f"{'，含 omnisub 启动器' if (dest / 'omnisub').exists() else ''}）")
    if stale:
        print(f"[install] ⚠️ 目标里还有 {len(stale)} 个源目录已没有的文件（未删除，请自行确认）："
              f"{', '.join(str(x) for x in stale[:5])}"
              f"{' …' if len(stale) > 5 else ''}", file=sys.stderr)
    print(f"[install] 已安装 omnisub v{VERSION} rev={rev} —— 之后运行时横幅会打印同一指纹")


def doctor(asr_model: str = ASR_MODEL_DEFAULT, chat_model: str = CHAT_MODEL_DEFAULT) -> int:
    """环境自检：**真执行**二进制（不是 `command -v` 看一眼），并分列"用户要配的"。

    存在的意义是替代"先跑 60 秒小样看看通不通"这种人工前置：配置对不对由程序说，
    服务端能不能用由真实运行时兜（报错即 fail_config 给指引）。
    """
    print(f"[doctor] omnisub v{VERSION} rev={code_rev()}  {Path(__file__).resolve()}")
    ok = True
    for name, exe in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
        try:
            proc = subprocess.run([exe, "-version"], capture_output=True, text=True, env=tool_env())
            head = ((proc.stdout or proc.stderr).splitlines() or [""])[0]
            good = proc.returncode == 0
            print(f"[doctor] {name:7s} {'✅' if good else '❌'} {exe}  {head[:52]}")
            ok &= good
        except OSError as exc:
            print(f"[doctor] {name:7s} ❌ 无法执行 {exe}（{exc}）")
            ok = False
    try:
        exe = find_bl()
        proc = subprocess.run(_bl_argv(exe) + ["--version"], capture_output=True, text=True, env=bl_env())
        good = proc.returncode == 0
        print(f"[doctor] bl      {'✅' if good else '❌'} {exe}  {(proc.stdout or proc.stderr).strip()[:32]}")
        ok &= good
    except SystemExit as exc:
        print(f"[doctor] bl      ❌ {exc}")
        ok = False
    key = ""
    try:
        key = api_key(None)
        print(f"[doctor] API key ✅ 已找到（{key[:6]}…{key[-4:]}，来自环境变量或 ~/.agentmemory/.env）")
    except SystemExit as exc:
        print(f"[doctor] API key ❌ {exc}")
        ok = False
    if ok and key:
        ok &= doctor_probe_models(key, asr_model, chat_model)
    print("[doctor] 需要用户配置：① 百炼 API key ② 对应模型已开通且有额度（上面两行）")
    print("[doctor] 程序自扛：PATH/工具发现、音频编码与前处理、缓存续跑、并发限速、重试与自检")
    print(f"[doctor] {'✅ 环境可用' if ok else '❌ 有缺项，按上面提示配置后重跑'}")
    return 0 if ok else 2


def doctor_probe_models(key: str, asr_model: str, chat_model: str) -> bool:
    """**实测**两个模型各调用一次——doctor 的绿字必须是验证过的，不能只看"key 存在"。

    实测教训（2026-09-28）：doctor 打"✅ 环境可用"，而 ASR 实际被「免费额度用完即停」挡着
    （403 AllocationQuota.FreeTierOnly）；用户照绿字排查方向全错、还去充了值。
    成本：一次两个字的 chat + 一次 0.5 秒静音的 ASR，**分文级别**。
    """
    good = True
    try:
        if _needs_direct_http(chat_model):
            # doctor 必须走与生产相同的传输（qwen3 系直连+关思考），否则绿字验的不是出货路径
            raw, err = _http_chat([{"role": "user", "content": "回复 ok"}], key, chat_model, 60)
            detail = err or raw
            probe_ok = bool(raw.strip()) and '"error"' not in detail[:400]
            service = "翻译（dashscope chat）"
        else:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8") as fh:
                json.dump([{"role": "user", "content": "回复 ok"}], fh, ensure_ascii=False)
                msg_file = fh.name
            proc = subprocess.run(_bl_argv(find_bl()) + [
                "text", "chat", "--model", chat_model, "--messages-file", msg_file, "--api-key", key,
                "--output", "json", "--quiet", "--timeout", "60"],
                capture_output=True, text=True, env=bl_env())
            detail = (proc.stderr or "") + (proc.stdout or "")
            probe_ok = proc.returncode == 0 and proc.stdout.strip() and '"error"' not in detail[:400]
            service = "翻译（bl text chat）"
        if probe_ok:
            print(f"[doctor] 翻译模型 ✅ {chat_model} 实测可调用")
        else:
            good = False
            print(f"[doctor] 翻译模型 ❌ {chat_model} 实测失败")
            for line in config_hint_lines(service, detail):
                print(f"[doctor]   {line}")
    except (OSError, SystemExit) as exc:
        good = False
        print(f"[doctor] 翻译模型 ❌ 无法探测（{exc}）")
    try:
        with tempfile.TemporaryDirectory() as td:
            probe = Path(td) / "probe.mp3"
            made = subprocess.run(
                [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                 "-t", "0.5", "-c:a", "libmp3lame", "-b:a", "32k", str(probe)],
                capture_output=True, text=True, env=tool_env())
            if made.returncode != 0 or not probe.exists():
                print(f"[doctor] 转写模型 ⏭ 跳过（本地造不出 0.5 秒探测音频：{made.stderr[:60]}）")
            else:
                out = Path(td) / "probe.json"
                proc = subprocess.run(_bl_argv(find_bl()) + [
                    "speech", "recognize", "--url", str(probe), "--model", asr_model,
                    "--out", str(out), "--output", "json", "--api-key", key, "--timeout", "120"],
                    capture_output=True, text=True, env=bl_env())
                detail = (proc.stdout or "") + (proc.stderr or "")
                if proc.returncode == 0 and '"error"' not in detail[:400]:
                    print(f"[doctor] 转写模型 ✅ {asr_model} 实测可调用")
                else:
                    good = False
                    print(f"[doctor] 转写模型 ❌ {asr_model} 实测失败")
                    for line in config_hint_lines("转写（bl speech recognize）", detail):
                        print(f"[doctor]   {line}")
    except (OSError, SystemExit) as exc:
        good = False
        print(f"[doctor] 转写模型 ❌ 无法探测（{exc}）")
    return good


def _take_lock(lock: Path, what: str) -> Path:
    """取一把"持有者死了就接管"的进程级锁；被活着的进程持有时退出并说明怎么处理。"""
    for _ in (1, 2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"pid={os.getpid()} started={time.strftime('%F %T')}".encode())
            os.close(fd)
            atexit.register(lambda: lock.unlink(missing_ok=True))
            return lock
        except FileExistsError:
            try:
                holder = lock.read_text(encoding="utf-8").strip()
            except OSError:
                holder = ""
            m = re.search(r"pid=(\d+)", holder)
            alive = False
            if m:
                try:
                    os.kill(int(m.group(1)), 0)
                    alive = True
                except (OSError, ValueError):
                    alive = False
            if alive:
                raise SystemExit(
                    f"[lock] {what} 正被另一个 omnisub 处理（{holder}）→ 已终止。\n"
                    f"[lock] 锁文件：{lock}\n"
                    f"[lock] 并发跑会互相覆盖 ASR / 译文 / 成品；等它跑完，"
                    f"或确认进程已死后删掉 {lock.name} 再跑。")
            print(f"[lock] 发现失效锁（{holder or '无内容'}）→ 接管：{lock.name}", flush=True)
            lock.unlink(missing_ok=True)
    raise SystemExit("[lock] 无法取得锁")


def acquire_lock(cache_root: Path, stem: str) -> Path:
    """同一部片的并发保护（进程级锁）。

    实测踩过：两个 omnisub 同时处理同一部片会共用同一份缓存（`.asr.json` / `.source.srt` /
    `.<语言>.json`）并互相覆盖——现象是"同一份 ASR 数据，一次算 364 条、另一次算 373 条"，
    成品与缓存都可能是对方写到一半的状态。并发不是用户的错，但检测是程序的责任。
    """
    return _take_lock(cache_root / f"{stem}.lock", "这部片的缓存")


def acquire_out_lock(out_dir: Path, stem: str) -> Path:
    """成品锁：锁的是**成品路径**，锁文件放在共享锁目录。

    缓存锁只覆盖缓存目录——两个进程只要 `--cache-dir` 不同（评测/CI 常这么传，用户也可能自己
    分目录），缓存锁就各锁各的，却仍会写同一个 `<基名>.ass`。实测（2026-09-27）：本进程跑完的
    成品被另一个进程覆盖，验收被迫整体重做。成品路径是最终事实，它必须自己有一把锁。

    锁文件**不放在片库**（用户要求视频目录只多出一个 .ass，锁文件即使是临时的也不该出现在那儿），
    按成品绝对路径哈希放进 <缓存根>/locks/，与 `--cache-dir` 无关 → 换缓存目录也能互相挡住。
    """
    key = hashlib.sha1(str(out_dir / f"{stem}.ass").encode("utf-8")).hexdigest()[:12]
    # 锁目录要容错：`--cache-dir` 的隔离意义就是"不碰 HOME"（CI/沙箱），这里若无条件
    # mkdir(HOME/...) 就会在干活之前因 HOME 只读直接崩。退到系统临时目录，再不行就跳过锁。
    for base in (default_cache_root() / "locks", Path(tempfile.gettempdir()) / "omnisub-locks"):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return _take_lock(base / f"out-{key}.lock", "这部片的成品")
        except OSError:
            continue
    print("[lock] 拿不到共享锁目录 → 跳过成品锁（并发写同一成品的风险由调用方承担）",
          file=sys.stderr)
    return None


def atomic_write_text(path: Path, text: str) -> None:
    """同目录写临时文件 + fsync + os.replace：读者永远看不到写了一半的文件。

    旧的 `Path.write_text` 是先截断再写，另一个进程/用户在写入窗口里读到的是半个文件
    （实测：并发的第二次跑覆盖成品时，同一个路径先后读到 62572 与 62775 两个版本）。
    os.replace 在同一文件系统内是原子的：读者要么看到旧的、要么看到新的。
    """
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def asr_cache_ok(video: Path, asr_json: Path, meta_path: Path, model: str,
                 want_lang: str = "auto", want_chunks: int = 1) -> bool:
    """已有 ASR 结果能否复用。

    重跑一次转写 = 重读整片 + 重上传 + 重新付费（实测 58.6 分钟片 131.7s），能省必须省。
    守卫与源字幕复用同源：必须比视频新、模型一致、时长一致（1s 容差）、元数据完好、
    且结果里真有句子。缺元数据一律不复用——宁可贵一次，也不能拿别的片的转写冒充。
    """
    if not (asr_json.exists() and meta_path.exists()):
        return False
    if asr_json.stat().st_mtime < video.stat().st_mtime:
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        data = json.loads(asr_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(meta, dict) or meta.get("model") != model:
        return False
    # 分片数必须一致：4 段并行转出来的结果与整片一次不是同一份（句/时间戳都可能不同），
    # 缺这个键的旧缓存按 1 段算——这样"整片一次"的缓存不会被 --asr-chunk 4 静默复用。
    if int(meta.get("chunks") or 1) != max(1, int(want_chunks)):
        print(f"[asr] 已有转写是 {int(meta.get('chunks') or 1)} 段切分，本次要 {want_chunks} 段 → 不复用",
              file=sys.stderr)
        return False
    # 缓存是"没给语言提示"时转的，而这次明确了语言 → 必须重转：
    # 实测不给提示会整句漂移到西语/法语（14/282 句），复用这份结果等于让"按建议重跑"失效。
    cached_lang = str(meta.get("lang") or "").strip().lower()
    if want_lang.strip().lower() not in ("", "auto") and cached_lang in ("", "auto"):
        print("[asr] 已有转写是「未指定语言」时产生的，本次指定 "
              f"{want_lang} → 重新转写（不复用可能漂移的结果）", file=sys.stderr)
        return False
    if not ((data.get("transcripts") or [{}])[0].get("sentences")):
        return False
    try:
        dur = float((ffprobe_json(video).get("format") or {}).get("duration") or 0)
        if dur and abs(float(meta.get("duration") or 0) - dur) > 1.0:
            return False
    except (TypeError, ValueError):
        return False
    return True


# ---------------- 时间工具 ----------------
def ts(ms: int) -> str:
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_ts(text: str) -> int:
    fields = re.split(r"[:]", text.strip())
    if len(fields) == 2:                    # WebVTT 允许省略小时位：MM:SS,mmm
        h, m, rest = "0", fields[0], fields[1]
    elif len(fields) == 3:
        h, m, rest = fields
    else:
        raise ValueError(f"无法解析时间戳：{text!r}")
    s, _, ms = rest.replace(".", ",").partition(",")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int((ms or "0").ljust(3, "0")[:3])


# ---------------- 1a. 内嵌字幕轨（ffprobe 探轨 + ffmpeg 抽取）----------------
def ffprobe_json(video: Path) -> dict:
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-probesize", "20M", "-analyzeduration", "20M", "-show_entries",
         "stream=index,codec_type,codec_name,width,height:stream_tags=language,title:format=duration",
         "-of", "json", str(video)],
        capture_output=True, text=True, env=tool_env())
    if proc.returncode != 0:
        raise SystemExit(f"ffprobe 失败：{proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout or "{}")


def embedded_tracks(video: Path) -> list[dict]:
    tracks = []
    for st in ffprobe_json(video).get("streams") or []:
        if st.get("codec_type") != "subtitle":
            continue
        tags = st.get("tags") or {}
        codec = (st.get("codec_name") or "").lower()
        tracks.append({"index": st.get("index"), "codec": codec,
                       "lang": (tags.get("language") or "").lower(),
                       "title": tags.get("title") or "",
                       "text_based": codec in TEXT_SUB_CODECS})
    return tracks


def pick_embedded_track(pool: list[dict], prefer_lang: str | None) -> dict:
    """按 --source-lang 挑轨：先比完整标签，再退到**主语言**比。

    轨标签常是 ISO639-2（chi/zho/eng），用户写的是 zh/en；而 --source-lang zh-TW 归一后是
    `zh-Hant`，若只比完整标签就一条都匹配不上，会静默退回第一条轨（实测退回英文轨）。
    """
    if not pool:
        raise SystemExit("视频里没有可用字幕轨")
    want = normalize_lang(prefer_lang)
    if not want:
        return pool[0]
    exact = next((t for t in pool if normalize_lang(t["lang"]) == want), None)
    if exact is not None:
        return exact
    base = want.split("-")[0]
    return next((t for t in pool if normalize_lang(t["lang"]).split("-")[0] == base), None) or pool[0]


def extract_embedded(video: Path, index: int | None, prefer_lang: str | None) -> tuple[list[dict], dict]:
    tracks = embedded_tracks(video)
    if not tracks:
        raise SystemExit("视频里没有字幕轨")
    track = None
    if index is not None:
        track = next((t for t in tracks if t["index"] == index), None)
        if track is None:
            raise SystemExit(f"没有 idx={index} 的字幕轨")
    else:
        pool = [t for t in tracks if t["text_based"]] or tracks
        track = pick_embedded_track(pool, prefer_lang)
    if not track["text_based"]:
        raise SystemExit(f"字幕轨 idx={track['index']} 是图形字幕（{track['codec']}），需要先 OCR")
    with tempfile.TemporaryDirectory(prefix="omnisub-sub-") as tmp:
        raw_srt = Path(tmp) / "sub.srt"
        proc = subprocess.run([FFMPEG, "-v", "error", "-y", "-probesize", "20M", "-analyzeduration", "20M",
                               "-i", str(video),
                               "-map", f"0:{track['index']}", "-c:s", "srt", str(raw_srt)],
                              capture_output=True, text=True)
        if proc.returncode != 0 or not raw_srt.exists():
            raise SystemExit(f"ffmpeg 抽字幕失败：{proc.stderr.strip()[:200]}")
        cues = read_srt(raw_srt)
    return cues, {"index": track["index"], "codec": track["codec"],
                  "language": track["lang"], "title": track["title"]}


# ---------------- 1b. 外挂字幕 ----------------
def looks_bilingual(path: Path) -> bool:
    """一份字幕是不是**真的双语**（同时含 CJK 文字与拉丁文字）——"它还能不能当原文用"。

    必须防住：成品 <视频基名>.ass/.srt 就落在视频目录，正是 sidecar 的首个命中路径；
    不防就会把成品当原文回读（再把中文翻一遍）、或覆盖掉真正的英文外挂字幕。

    2026-09-27 修（实测踩坑）：旧判据是"含汉字的条数 >30%"，于是**纯中文字幕**（中文片 + 中文 srt，
    最常见的场景之一）被判成双语成品 → `[src] 跳过 sample.srt：它看起来是双语成品` → 落到 ASR
    （配额用尽时整跑直接失败）。判据改为"**同时**有明显 CJK 与明显拉丁文字"才算双语：
    纯中文、纯英文、纯日文都能当原文用，而 en+zh 那种真·双语外挂字幕依旧会被跳过。
    """
    try:
        cues = read_ass(path) if path.suffix.lower() in (".ass", ".ssa") else read_srt(path)
    except (OSError, ValueError):
        return False
    if not cues:
        return False
    cjk = sum(1 for c in cues if CJK_CHAR.search(c["text"]))
    latin = sum(1 for c in cues if LATIN_WORD.search(c["text"]))
    return cjk / len(cues) > 0.3 and latin / len(cues) > 0.3


def has_own_mark(path: Path) -> bool:
    """读文件头认"这是我们自己产出的成品"。

    成品名就是 <视频基名>，与"用户放的外挂字幕"同名同扩展名，靠文件名分不出来，
    所以在 ASS 头部写产出标记（Title/注释行）当指纹。只读头 2KB，避免整片读盘。

    两道收紧：① 只认 ASS/SSA —— 我们只出 ASS，SRT/VTT 不可能带我们的标记；
    ② 按**整行**匹配那两个标记行，而不是搜子串 —— 否则真外挂字幕的台词里出现 "omnisub"
    就会被判成"自家产物"跳过，回落 ASR 白花钱且日志误导。
    """
    if path.suffix.lower() not in (".ass", ".ssa"):
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(2048)
    except OSError:
        return False
    marks = {f"; Generated by {ASS_MARK}", f"Title: {ASS_MARK}"}
    return any(line.strip() in marks for line in head.splitlines()[:40])


def _is_own_artifact(cand: Path, video: Path) -> bool:
    """`<基名>.source.srt` / `<基名>.mono.ass` 是本技能自己写出的中间产物，不是"别人的外挂字幕"。

    旧版本把它们写在视频目录里（现在默认落缓存目录，但片库里可能还留着历史文件），
    且是纯英文 → 能绕过 looks_bilingual 守卫，被 sidecar 兜底 glob（`<基名>*`）
    当成外挂字幕读回来。后果实测：修好切分器后重跑，--refresh-source 与 --asr-json 全被
    架空，成品仍是旧的无标点文本 —— 必须显式排除。

    成品本身（<基名>.ass/.srt，名字与视频同名）靠产出标记识别，见 has_own_mark()。
    """
    if cand.suffix.lower() not in SIDECAR_EXTS:
        return False
    if cand.stem in (f"{video.stem}{SOURCE_SUFFIX}", f"{video.stem}{MONO_SUFFIX}"):
        return True
    return has_own_mark(cand)


def sidecar_path(video: Path) -> Path | None:
    for ext in SIDECAR_EXTS:
        exact = video.with_suffix(ext)
        if exact.exists():
            # 精确同名是最先命中的路径，必须是**两道**守卫：产出标记 + 双语外观。
            # 只查 looks_bilingual（靠"含大量汉字"）时，语言对不含中文（如 en,ko）的成品
            # 会被当外挂字幕读回来 —— 与历史上 .source.srt 劫持同一类漏洞，只是换了入口。
            if _is_own_artifact(exact, video):
                print(f"[src] 跳过 {exact.name}：本技能自己产出的成品/中间产物（不是外挂原文）", flush=True)
                continue
            if looks_bilingual(exact):
                print(f"[src] 跳过 {exact.name}：它看起来是双语成品（不是原文）", flush=True)
                continue
            return exact
    stem = video.stem
    for cand in sorted(video.parent.glob(f"{stem}*")):
        if cand.suffix.lower() in SIDECAR_EXTS and cand != video:
            if _is_own_artifact(cand, video):
                print(f"[src] 跳过 {cand.name}：本技能自己的中间产物（原文缓存），不是外挂字幕", flush=True)
                continue
            if looks_bilingual(cand):        # 兜底路径同样要防"吃自己的产出"
                print(f"[src] 跳过 {cand.name}：它看起来是双语成品（不是原文）", flush=True)
                continue
            return cand
    return None


_SIDE_HTML_TAG = re.compile(r"</?(?:i|b|u|s|em|strong|font|ruby|rt|rb|c|v|lang|span)\b[^>]*>", re.I)
_SIDE_ASS_TAG = re.compile(r"\{[^}]*\}")
_SIDE_ENTITIES = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                  ("&#39;", "'"), ("&apos;", "'"), ("&nbsp;", " "))


def clean_sidecar_text(text: str) -> str:
    r"""外挂字幕正文里的**标记不是内容**，必须剥掉再送翻译/写成品。

    实测必要性（2026-09-28）：带 `<i>Italic line</i>` 与 `{\an8}` 的 SRT 原样进了成品——
    `<i>`/`<b>`/`&amp;` 直接显示成字面量，ASS 覆盖块被转义成 `\{\an8\}` 也照显示（反斜杠本义）。
    这些在真实外挂字幕里极常见（iTunes/YouTube 导出的 SRT、播放器导出的 ASS），
    不剥就会污染译文（模型会把 `<i>` 当正文翻译）。
    """
    t = _SIDE_HTML_TAG.sub("", text or "")
    t = _SIDE_ASS_TAG.sub("", t)
    for ent, ch in _SIDE_ENTITIES:
        t = t.replace(ent, ch)
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def read_srt(path: Path) -> list[dict]:
    cues = []
    text = path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        idx = 0
        if re.fullmatch(r"\d+", lines[0].strip()):
            idx = 1                      # SRT 的序号行
        if idx >= len(lines) or "-->" not in lines[idx]:
            # WebVTT 允许"标识符行 + 时间轴行"（cue id，YouTube 导出必备形状）。
            # 旧版只认第一行，于是带 id 的块被**整块丢弃**——实测 2 条的 vtt 只出 1 条，
            # 这是"静默少字幕"，比报错更危险。
            if idx + 1 < len(lines) and "-->" in lines[idx + 1]:
                idx += 1
            else:
                continue
        left, _, right = lines[idx].partition("-->")
        body = clean_sidecar_text("\n".join(lines[idx + 1:]))
        if body:
            cues.append({"begin": parse_ts(left), "end": parse_ts(right.split()[0]), "text": body})
    return normalize_cues(cues)


def read_ass(path: Path) -> list[dict]:
    cues = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        begin, end, body = parts[1], parts[2], parts[9]
        body = re.sub(r"\\[Nn]", "\n", body)
        body = clean_sidecar_text(body)
        if body:
            cues.append({"begin": parse_ts(begin.replace(".", ",")), "end": parse_ts(end.replace(".", ",")), "text": body})
    return normalize_cues(cues)


# ---------------- 1d. 同步校验（ffsubsync）----------------
def find_ffsubsync() -> list[str] | None:
    """优先用 PATH 里的 ffsubsync；没有就用 uv 临时环境跑（本机 uv 在 ~/.local/bin）。"""
    exe = shutil.which("ffsubsync")
    if exe:
        return [exe]
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    if Path(uv).exists():
        return [uv, "run", "--quiet", "--with", "ffsubsync", "ffsubsync"]
    return None


# 合法的"帧率换算"比值：25↔24、25↔23.976、24↔23.976、30↔25 等常见组合。
# 只在这附近才认为 scale 是帧率差异；别的值八成是 VAD 锁错（实测 0.9420 谁都不是）。
FFSYNC_SCALE_HINTS = (1.0, 1.0417, 1.0427, 1.001, 1.2, 0.8333, 0.96, 0.959)
FFSYNC_MIN_SECONDS = 600   # 短于 10 分钟不校验时间轴：短片稀疏对白上 ffsubsync 会锁错（实测见 verify_sync）


def _ffsubsync_once(video: Path, srt_in: Path, work: Path, tag: str) -> dict | None:
    """跑一次 ffsubsync，返回 {offset, scale, out}（out 是它写出的校正文件）。"""
    cmd = find_ffsubsync()
    if cmd is None:
        return None
    out = work / f"synced-{tag}.srt"
    proc = subprocess.run(cmd + [str(video), "-i", str(srt_in), "-o", str(out), "--gss"],
                          capture_output=True, text=True, env=tool_env())
    m_off = re.search(r"offset seconds:\s*(-?[\d.]+)", proc.stdout + proc.stderr)
    m_scale = re.search(r"framerate scale factor:\s*([\d.]+)", proc.stdout + proc.stderr)
    if not m_off or not out.exists():
        detail = (proc.stderr or proc.stdout or "")[-300:].replace("\n", " ")
        print(f"[ffsync] 校验未完成（exit={proc.returncode}）：{detail}", file=sys.stderr)
        return None
    return {"offset": float(m_off.group(1)),
            "scale": float(m_scale.group(1)) if m_scale else 1.0, "out": out}


def _ffsync_clean(offset: float, scale: float) -> bool:
    """判定"已经同步"的容差：偏移 ≤0.3s 且缩放 ≈1（±0.002）。"""
    return abs(offset) <= 0.3 and abs(scale - 1.0) <= 0.002


def _scale_plausible(scale: float) -> bool:
    """缩放是否像"帧率换算"（只认已知比值附近）——不是就当 VAD 锁错。"""
    return any(abs(scale - hint) <= 0.01 for hint in FFSYNC_SCALE_HINTS)


def verify_sync(video: Path, source_srt: Path, work_dir: Path, force: bool = False) -> dict | None:
    """用音频 VAD + FFT 校验字幕是否真的对得上视频，**并校验"校正后的结果真的同步"**。

    实测（本机，43.8 分钟 1080p）：正对照（内嵌字幕）报 offset 0.000 / scale 1.000；
    反证（人为把字幕挪 +3.5s）报 offset -3.500 —— 说明这道闸门真能抓偏移。

    2026-09-27 新增两层守卫（实测踩坑，代价是"把对的改坏"）：一份**帧级精确对齐**的 3 分钟侧车
    被判"不同步"，ffsubsync 报 `offset +25.270s、scale 0.9420` 并据此改写了原文——首条从 10.64s
    挪到 35.29s，成品跟着错。旧实现只要 `|offset|>0.3 或 |scale-1|>0.002` 就无条件接受，
    既没有合理性先验，也不检验校正结果。现在：
      ① **先验**：scale 必须落在已知帧率比附近，否则判定锁错 → 拒绝改写；
      ② **结果校验**：真要改写时对**校正后的文件**再跑一次，复检仍不同步就回滚、保留原字幕。
    只有确实要改写时才多花这一次（约 2–13 s）。

    另：**片子短于 `FFSYNC_MIN_SECONDS` 直接跳过**（`force=True` 可强制）——ffsubsync 靠音频 VAD
    做互相关，短片对白稀疏时锁不准：实测同一份**帧级精确对齐**的 3 分钟侧车，两次跑分别被报成
    `+25.270s/0.9420` 与 `+12.610s/1.0030`（真值 0），而 58 分钟整片报 `-3.600s/1.0000`（真值 -3.5）。
    """
    if not force:
        try:
            dur = video_duration(video)
        except SystemExit:
            dur = 0.0
        if 0 < dur < FFSYNC_MIN_SECONDS:
            print(f"[ffsync] 片长 {dur:.0f}s < {FFSYNC_MIN_SECONDS}s，跳过时间轴校验"
                  f"（ffsubsync 在短片稀疏对白上会锁错：实测 3 分钟片段把帧级对齐的字幕报成 +25.27s；"
                  f"要强制校验传 --verify-sync on）", file=sys.stderr)
            return None
    first = _ffsubsync_once(video, source_srt, work_dir, "first")
    if first is None:
        print("[ffsync] 未找到 ffsubsync 或校验未完成，跳过时间轴校验", file=sys.stderr)
        return None
    offset, scale = first["offset"], first["scale"]
    info = {"offset": offset, "scale": scale, "fixed": False, "rejected": ""}
    if _ffsync_clean(offset, scale):
        print(f"[ffsync] ✅ 同步正常（offset {offset:+.3f}s、scale {scale:.4f}）", flush=True)
        first["out"].unlink(missing_ok=True)
        return info
    if not _scale_plausible(scale):
        info["rejected"] = f"scale {scale:.4f} 不是常见帧率比"
        print(f"[ffsync] ⚠️ ffsubsync 报 offset {offset:+.3f}s、scale {scale:.4f}，但缩放不是任何"
              f"常见帧率比 → **判定为锁错，保留原字幕、不改写**"
              f"（实测：一份帧级对齐的 3 分钟侧车会被报成 +25.270s/0.9420）", file=sys.stderr)
        first["out"].unlink(missing_ok=True)
        return info
    second = _ffsubsync_once(video, first["out"], work_dir, "recheck")
    if second is not None and _ffsync_clean(second["offset"], second["scale"]):
        shutil.copy2(first["out"], source_srt)
        info["fixed"] = True
        print(f"[ffsync] ⚠️ 字幕与音轨不同步（offset {offset:+.3f}s、scale {scale:.4f}）→ 已校正"
              f"，且**复检通过**（复检 offset {second['offset']:+.3f}s）", flush=True)
    else:
        info["rejected"] = "校正后复检仍不同步"
        got = f"{second['offset']:+.3f}s" if second else "复检跑不起来"
        print(f"[ffsync] ⚠️ 检出不同步（offset {offset:+.3f}s）但**校正后复检仍不同步**（{got}）"
              f" → 回滚，保留原字幕（宁可不改，也不能把对的改坏）", file=sys.stderr)
    first["out"].unlink(missing_ok=True)
    if second is not None:
        second["out"].unlink(missing_ok=True)
    return info


# ---------------- 1c. ASR 路线（ffmpeg 抽音轨）----------------
def audio_track_lang(video: Path) -> str:
    """音轨的 `language` 标签 → 语言提示（**免费**，不发任何请求）。

    实测本片库 6 集的 MKV 音轨都带 `TAG:language=eng`。有标签就不必做 180s 样本探测
    （探测实测 6.3–9.2s，且要打一次 ASR 请求）：提示一样、成本为 0。
    标签缺失或是 und/zxx 时返回 ""，由调用方回落样本探测（保持原行为，不猜）。
    """
    try:
        for s in (ffprobe_json(video).get("streams") or []):
            if s.get("codec_type") != "audio":
                continue
            tag = str(((s.get("tags") or {}).get("language") or "")).strip().lower()
            if tag and tag not in ("und", "unknown", "zxx", "mul"):
                return normalize_lang(tag).split("-")[0]
    except (SystemExit, ValueError, TypeError):
        return ""
    return ""


def probe_source_lang(video: Path, work: Path, key: str, model: str) -> str:
    """取片头一段音频快速转写 → 判定源语言，用作整片 ASR 的**语言提示**。

    为什么值得多花这一下（2026-09-27 实测：同一份 58.6 分钟整片音频、同一模型，只差语言提示）：
      · **不给提示**：282 句里 **14 句整句漂移**成西班牙语/法语（中文行跟着漂，成品"原文行"不再是原文）
      · **给 `--language en`**：283 句，**0 句漂移**
    而默认的 `--source-lang auto` 恰恰什么提示都不给 → 默认路径本身有坑，把它固化掉，不留给用户发现。
    样本 180s ≈ 1.4MB / 约 6s，远小于整片转写（58 分钟 ≈ 60s + 14MB），更小于事后重跑。
    判不出源语言就**不给提示**（保持原行为，不猜）。
    """
    sample, out = work / "probe.mp3", work / "probe.json"
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-ss", str(ASR_PROBE_SKIP), "-t", str(ASR_PROBE_SECONDS),
         "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "libmp3lame", "-b:a", f"{ASR_MP3_KBPS}k", str(sample)],
        capture_output=True, text=True)
    if proc.returncode != 0 or not sample.exists():
        print(f"[asr] 源语言探测取样失败（{proc.stderr.strip()[:80]}）→ 整片不给语言提示", file=sys.stderr)
        return ""
    try:
        transcribe(sample, out, key, model, "auto")
    except SystemExit:
        return ""
    try:
        texts = [s["text"] for s in sentences_of(json.loads(out.read_text(encoding="utf-8")))]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        # 探测只是"锦上添花"：读不通就当判不出，绝不能把整跑带崩（并发写到半写文件时实测会撞）
        print("[asr] 源语言探测结果不可读 → 整片不给语言提示", file=sys.stderr)
        return ""
    lang, sure = detect_lang(texts)
    if lang and sure:
        print(f"[asr] 源语言探测：{ASR_PROBE_SECONDS}s 样本 → {lang}（作为整片语言提示；"
              f"实测不给提示会整句漂移到别的语言）", flush=True)
        return lang
    print("[asr] 源语言探测判不出 → 整片不给语言提示（保持原行为）", flush=True)
    return ""


def audio_cache_path(cache_root: Path, stem: str, lossless: bool) -> Path:
    """送 ASR 的音频缓存路径（参数写进文件名，改了参数自然不会串用）。

    抽音轨要读完 4.69 GB 外挂机械盘（实测 15–33s，冷盘更久）。ASR 缓存命中时整段跳过，
    但只要 ASR 缓存失效（换模型/换分片数/失败重跑）就得把整片再读一遍。
    把 14MB 的 MP3 留在缓存目录 → 重跑 0 秒拿到音频（6 集共 ~84MB，用户可随时删）。
    """
    tag = f"{ASR_MP3_KBPS}k" if not lossless else "flac"
    ext = ".mp3" if not lossless else ".flac"
    return cache_root / f"{stem}.16k-mono-{tag}{ext}"


def transcribe(audio: Path, out_json: Path, key: str, model: str, lang: str) -> dict:
    """lang 传 auto/空 → 不给语言提示，由模型自行判定（任意语言场景的默认行为）。

    `--language` 在 bl 里是**语言提示**而非必填项；给的提示错了反而会伤识别率，
    所以 auto 时宁可不给，让用户按需显式指定。
    """
    # bl 的 --language 只传**主语言码**：示例与实测都是 zh/en/ja 这种两字母码，
    # zh-Hant 之类的变体没验过，传它属于拿付费接口做实验。
    hint = "" if (lang or "").strip().lower() in ("", "auto") else normalize_lang(lang).split("-")[0]
    argv = ["speech", "recognize", "--url", str(audio), "--model", model,
            "--out", str(out_json), "--output", "json",
            "--api-key", key, "--timeout", str(ASR_TIMEOUT)]
    if hint:
        argv += ["--language", hint]
    print(f"[asr] {model} ← {audio.name}"
          + (f"（语言提示 {hint}）" if hint else "（不给语言提示，自动判定）"), flush=True)
    proc = subprocess.run(
        _bl_argv(find_bl()) + argv,
        capture_output=True, text=True, env=bl_env(),
    )
    if proc.returncode != 0 or not out_json.exists():
        detail = proc.stdout[-2000:] + proc.stderr[-2000:]
        sys.stderr.write(detail)
        # 报错就终止：认得出来的配置类错误直接给"要配什么"，不在这里反复重试烧时间
        fail_config("ASR 转写（bl speech recognize）", detail)
    return json.loads(out_json.read_text(encoding="utf-8"))


def offset_sentences(data: dict, offset_ms: int) -> list[dict]:
    """把一份转写结果的句级/词级时间整体平移（分片合并用）。

    **必须原样保留 bl 的字段名 `begin_time`/`end_time`**：这份合并结果会被当成
    `bl speech recognize` 的产物重新读回（`sentences_of` 读的就是 begin_time）。
    实测踩过：先写成 begin/end（内部命名）→ 读回后每句时间都是 0，整片时间轴只剩
    词级时间戳兜底；一旦某句没有 words，时间轴就塌到 0:00。
    """
    out = []
    for sent in sentences_of(data):
        words = []
        for w in (sent.get("words") or []):
            w = dict(w)
            for k in ("begin_time", "end_time"):
                if isinstance(w.get(k), (int, float)):
                    w[k] = int(w[k]) + offset_ms
            words.append(w)
        out.append({"begin_time": sent["begin"] + offset_ms, "end_time": sent["end"] + offset_ms,
                    "text": sent["text"], "words": words})
    return out


def video_duration(video: Path) -> float:
    """片长（秒）。ffprobe 只读文件头，便宜。"""
    return float((ffprobe_json(video).get("format") or {}).get("duration") or 0)


def extract_audio_chunk(video: Path, out: Path, start: float, dur: float, lossless: bool) -> None:
    """抽一段音轨：`-ss` 在 -i 之前（快进到关键帧再解码），`-t 0` 表示到片尾。"""
    enc = ["-c:a", "flac"] if lossless else ["-c:a", "libmp3lame", "-b:a", f"{ASR_MP3_KBPS}k"]
    argv = [FFMPEG, "-v", "error", "-y"]
    if start > 0:
        argv += ["-ss", f"{start:.3f}"]
    if dur > 0:
        argv += ["-t", f"{dur:.3f}"]
    argv += ["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", *enc, str(out)]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise SystemExit(f"ffmpeg 抽音轨失败（{start:.0f}s 起）：{proc.stderr.strip()[:200]}")


def transcribe_streaming(video: Path, cache_root: Path, stem: str, duration: float, key: str,
                         model: str, lang: str, chunks: int, workers: int,
                         lossless: bool, lang_provider=None) -> tuple[dict, float, float]:
    """**边抽边转**：切出第 i 段就立刻把第 i 段送 ASR，抽音轨与转写重叠。

    实测（E04–E06，4 段并行）：整程 79–96s 里"抽音轨 30–34s"与"转写 19–24s"是两段串行的大头，
    而它们用的是**不同资源**（外置机械盘 I/O vs 云端 ASR），重叠后整程接近"只付抽音轨那一段
    + 最后一段的转写"。片段常驻缓存目录，重跑 0 秒拿到音频。

    `lang_provider` 惰性给语言提示（在第一个分片的转写线程里触发）：样本探测要 6–9s，
    放在这里就能与抽音轨重叠，而不是白等一段。分片数、模型、提示都写进 meta，复用守卫据此把关。

    返回 (合并后的转写 JSON, 抽音轨墙钟, 转写尾段墙钟)。两个时间**不重叠**，相加=该段墙钟。
    """
    from concurrent.futures import ThreadPoolExecutor

    if duration <= 0:
        if chunks > 1:
            print("[asr] ffprobe 报不出片长 → 退回整片一次转写（分片长度无从计算）", file=sys.stderr)
        chunks = 1
    info = ffprobe_json(video)
    stream = next((x for x in (info.get("streams") or []) if x.get("codec_type") == "audio"), None)
    if stream is None:
        raise SystemExit(f"{video} 里没有音频流")
    print(f"[audio] {stream.get('codec_name')} → 16kHz 单声道 "
          f"{'FLAC 无损' if lossless else f'MP3 {ASR_MP3_KBPS}k'}，切 {chunks} 段"
          f"（{duration:.0f}s；有损 32k 实测比 FLAC 小 9 倍、上传快得多）", flush=True)

    tag = f"{ASR_MP3_KBPS}k" if not lossless else "flac"
    ext = ".mp3" if not lossless else ".flac"
    seg = duration / chunks if chunks > 1 else 0.0
    parts: list[tuple[Path, float]] = []
    for i in range(chunks):
        name = (f"{stem}.16k-mono-{tag}{ext}" if chunks == 1
                else f"{stem}.16k-mono-{tag}.part{i:02d}{ext}")
        parts.append((cache_root / name, i * seg))

    results: list[list[dict] | None] = [None] * len(parts)
    t_extract = 0.0

    def transcribe_part(i: int) -> None:
        p, start = parts[i]
        out = cache_root / f"{p.stem}.asr.json"
        lang_i = lang
        if not lang_i and lang_provider is not None:
            lang_i = lang_provider()          # 探测与抽音轨重叠：在第一个分片要转写时才解析提示
        print(f"[asr] 分片 {i + 1}/{len(parts)} 转写 ← {p.name}（{start:.0f}s 起）", flush=True)
        transcribe(p, out, key, model, lang_i)
        results[i] = offset_sentences(json.loads(out.read_text(encoding="utf-8")), int(start * 1000))
        out.unlink(missing_ok=True)

    phase0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(parts)))) as pool:
        futures = []
        for i, (p, start) in enumerate(parts):
            if not (p.exists() and p.stat().st_mtime >= video.stat().st_mtime):
                t0 = time.time()
                extract_audio_chunk(video, p, start, seg, lossless)
                t_extract += time.time() - t0
            elif i == 0:
                print(f"[audio] 复用缓存音频 {p.name}（比视频新 → 跳过整片读盘）", flush=True)
            futures.append(pool.submit(transcribe_part, i))   # 立刻送，不等后面的段
        for f in futures:
            f.result()
    t_phase = time.time() - phase0

    merged: list[dict] = []
    for r in results:
        merged.extend(r or [])
    merged.sort(key=lambda s: (s["begin_time"], s["end_time"]))
    # 覆盖度校验：**丢一段不能被静默交付**。分片转写"rc=0 + 0 句"是可能的（服务端空结果、
    # 切片截断），而一旦写进 meta，这个残缺结果会被 asr_cache_ok 永久复用（它只要求"整份
    # 文件有 ≥1 句"），成品就少掉十几分钟字幕、只留一行"⚠️ 大空隙"。静音段真的会是 0 句，
    # 所以这里明说怎么办：音频已缓存，重跑很便宜；确实无对白就用 --asr-chunk 1。
    empty = [i + 1 for i, r in enumerate(results) if not r]
    if empty and len(parts) > 1:
        raise SystemExit(
            f"[asr] 分片 {empty} 没产出任何句子（共 {len(parts)} 段，每段覆盖 {seg:.0f}s）"
            f"→ 拒绝把这个残缺结果写进缓存。\n"
            f"[asr] 音频片段已缓存在 {cache_root}，直接重跑即可（不再读盘、不再切片）；\n"
            f"[asr] 若这一段确实没有对白，用 --asr-chunk 1 走整片一次转写。")
    print(f"[asr] {len(parts)} 段并行转写完成 → 合并 {len(merged)} 句"
          f"（各段句数 {[len(r or []) for r in results]}；"
          f"抽音轨与转写已重叠：抽 {t_extract:.1f}s + 尾段 {max(0.0, t_phase - t_extract):.1f}s"
          f" = {t_phase:.1f}s）", flush=True)
    return {"transcripts": [{"sentences": merged}]}, t_extract, max(0.0, t_phase - t_extract)


def sentences_of(data: dict) -> list[dict]:
    global _STALE_SENTENCE_WARNED
    out = []
    stale = 0
    for sent in (data.get("transcripts") or [{}])[0].get("sentences") or []:
        text = (sent.get("text") or "").strip()
        if not text:
            continue
        begin = int(sent.get("begin_time") or 0)
        end = int(sent.get("end_time") or 0)
        words = sent.get("words") or []
        if not (begin or end) and words:
            # 旧格式（内部命名 begin/end）或缺句级时间的转写：句级时间会全是 0，整片时间轴只剩
            # 词级时间戳兜底——**某句没有 words 时就塌到 0:00**。实测 E04–E06 的缓存就是这样
            # （由已删除的旧分片路径写出），而缓存兼容检查只看"有没有句子"，会一直复用，
            # 所以这里既要补回来（只填缺失值、不改已有值），也要让用户看见。
            stale += 1
            ws = [w for w in words if isinstance(w.get("begin_time"), (int, float))]
            if ws:
                begin = int(min(w["begin_time"] for w in ws))
                end = int(max(int(w.get("end_time") or w["begin_time"]) for w in ws))
        out.append({"begin": begin, "end": end, "text": text, "words": words})
    if stale and not _STALE_SENTENCE_WARNED:
        _STALE_SENTENCE_WARNED = True
        print(f"[asr] ⚠️ 这份转写有 {stale} 句缺句级时间（旧格式/损坏）→ 已用词级时间戳补回；"
              f"要彻底修就重转写：--refresh-source", file=sys.stderr)
    out.sort(key=lambda s: (s["begin"], s["end"]))
    return out


def _canonical_text(text: str) -> str:
    """句级 text 才是可靠文本（标点、空格、词形都对），压缩空白后返回。"""
    return re.sub(r"\s+", " ", (text or "").strip())


def _align_words(words: list[dict], canonical: str) -> list[list[int]] | None:
    """把词对齐到句级文本，返回 [[begin_ms, end_ms, c0, c1], ...]，c0/c1 是 canonical 的字符区间。

    为什么要对齐（E08 实测，旧版两个坑都踩了）：ASR 的 words[] 两个方向都不可靠——
      拆词（须无空格相接）："gl"+"enn"、"pr"+"ou"+"der"、"9"+"0"、"er"+"ie"
      缺空格（须有空格）  ："in"+"90"、"restaurant"+"You"、"day"+"90"、"And"+"I"
    本地规则分不出这两种（旧版直接 join → "in90" 共 28 条；只补空格 → "gl enn" 更多）。
    只有句级 text 知道空格在哪：**显示文本一律取自 canonical**，words[] 只贡献时间戳——
    在 canonical 的"字母数字投影"上顺序匹配每个词，上面两种怪癖都能自然对齐。
    任一词匹配不上、区间不单调、或覆盖不足 90% 时返回 None（调用方退回比例切分）。
    """
    if not canonical or not words:
        return None
    proj = [(i, ch.lower()) for i, ch in enumerate(canonical) if ch.isalnum()]
    if not proj:
        return None
    out: list[list[int]] = []
    cursor = 0
    for word in words:
        token = [ch.lower() for ch in (word.get("text") or "") if ch.isalnum()]
        if not token:                        # 纯标点词（"'" / ","）→ 并进前一个词的区间
            if out:
                end = out[-1][3]
                while end < len(canonical) and not canonical[end].isalnum() and canonical[end] != " ":
                    end += 1
                out[-1][3] = end
            continue
        idx, hit, first = cursor, 0, None
        while idx < len(proj) and hit < len(token):
            if proj[idx][1] == token[hit]:
                if first is None:
                    first = proj[idx][0]
                hit += 1
            idx += 1
        if hit < len(token) or first is None or (out and first < out[-1][3]):
            return None
        out.append([int(word.get("begin_time") or 0), int(word.get("end_time") or 0),
                    first, proj[idx - 1][0] + 1])
        cursor = idx
    if not out:
        return None
    seen = bytearray(len(canonical))
    for span in out:
        for i in range(max(0, span[2]), min(len(canonical), span[3])):
            seen[i] = 1
    if sum(seen[i] for i, _ in proj) < 0.9 * len(proj):
        return None
    return out


def _text_windows(text: str, max_chars: int) -> list[tuple[int, int]]:
    """把整句切成 ≤max_chars 的字符窗口：先按标点小句打包，超长小句再按空格均衡硬切。

    旧版按字符数贪心硬切，句尾常剩 1–3 个词的孤儿条（"out."、"that out."），
    碎片单独送 MT 会被脑补成别的意思；按小句打包后切点基本落在标点上。
    """
    atoms: list[tuple[int, int]] = []
    for m in re.finditer(r"[^,;:.!?…]*[,;:.!?…]+|[^,;:.!?…]+$", text):
        seg = m.group()
        lead = len(seg) - len(seg.lstrip())
        trail = len(seg) - len(seg.rstrip())
        if m.end() - trail > m.start() + lead:
            atoms.append((m.start() + lead, m.end() - trail))
    if not atoms:
        atoms = [(0, len(text))]

    packed: list[tuple[int, int]] = []
    cur0 = cur1 = -1
    for a0, a1 in atoms:
        if cur0 < 0:
            cur0, cur1 = a0, a1
        elif a1 - cur0 <= max_chars:
            cur1 = a1
        else:
            packed.append((cur0, cur1)); cur0, cur1 = a0, a1
    if cur0 >= 0:
        packed.append((cur0, cur1))

    out: list[tuple[int, int]] = []
    for c0, c1 in packed:
        seg = text[c0:c1]
        if len(seg) <= max_chars:
            out.append((c0, c1))
            continue
        n = (len(seg) + max_chars - 1) // max_chars      # 单个小句仍超长 → 均衡硬切
        start = 0
        for i in range(1, n):
            ideal = len(seg) * i // n
            lo, hi = start + 1, len(seg) - 1
            # 在整个小句里找离 ideal 最近的空格（不在 ideal 附近截断，否则会切进词里）
            cuts = [p for p in range(lo, hi + 1) if seg[p] == " "]
            cut = min(cuts, key=lambda p: (abs(p - ideal), p)) if cuts else min(max(ideal, lo), hi)
            out.append((c0 + start, c0 + cut)); start = cut
        out.append((c0 + start, c1))
    return [(a, b) for a, b in out if text[a:b].strip()]


def _split_long(sent: dict, max_ms: int, max_chars: int) -> list[dict]:
    """把一条 ASR 句切成若干条字幕：文本取自句级 text，时间来自对齐后的词。

    长度预算（max_chars，≈两行）与时长预算（max_ms）双约束；切点优先落在标点上。
    """
    words = [w for w in sent["words"]
             if (w.get("text") or "").strip() or (w.get("punctuation") or "").strip()]
    canonical = _canonical_text(sent.get("text"))
    if not canonical:
        return []
    span_ms = max(1, sent["end"] - sent["begin"])
    spans = _align_words(words, canonical)
    if spans is None:
        # 回退：词层缺失或对不上 → 只按文本切，时间在句内按**字符数比例**摊
        # （旧版按条数均摊且不看时长预算，慢语速/长停顿会切出 20 s 的长条）
        n_time = max(1, (span_ms + max_ms - 1) // max_ms)
        eff = max(20, len(canonical) // n_time)
        chunks = [(canonical[a:b].strip(), b - a)
                  for a, b in _text_windows(canonical, min(max_chars, eff))]
        chunks = [(t, w) for t, w in chunks if t]
        if not chunks:
            return []
        total_chars = sum(w for _, w in chunks) or 1
        out, acc = [], 0
        for text, width in chunks:
            begin = sent["begin"] + span_ms * acc // total_chars
            acc += width
            out.append({"begin": begin, "end": sent["begin"] + span_ms * acc // total_chars,
                        "text": text, "words": []})
        return out

    pieces: list[dict] = []
    for c0, c1 in _text_windows(canonical, max_chars):
        inner = [s for s in spans if s[2] < c1 and s[3] > c0]
        if not inner:
            continue
        begin, end = inner[0][0], inner[-1][1]
        if end - begin <= max_ms:                    # 时长也在预算内 → 一条
            pieces.append({"begin": begin, "end": end, "text": canonical[c0:c1].strip()})
            continue
        # 慢语速/长停顿：窗口时长超标 → 用词再按时长均分
        groups: list[list[list[int]]] = [[inner[0]]]
        for span in inner[1:]:
            # 切点必须落在"空格之后"的词首：ASR 会把 don't 拆成 don/'/t，
            # 允许在 t 处切就会得到 "I don'" ‖ "t know what to say."
            at_word_start = span[2] == 0 or canonical[span[2] - 1] == " "
            if span[1] - groups[-1][0][0] > max_ms and at_word_start:
                groups.append([span])
            else:
                groups[-1].append(span)
        bounds = [c0] + [g[0][2] for g in groups[1:]] + [c1]
        for i, group in enumerate(groups):
            text = canonical[bounds[i]:bounds[i + 1]].strip()
            if text:
                pieces.append({"begin": group[0][0], "end": group[-1][1], "text": text})
    return pieces


def cues_from_asr(sentences: list[dict]) -> list[dict]:
    pieces: list[dict] = []
    for sent in sentences:
        if not sent["text"]:
            continue
        if sent["end"] <= sent["begin"]:
            sent["end"] = sent["begin"] + 1000
        for piece in _split_long(sent, MAX_CUE_MS, MAX_LINE_CHARS * 2):
            if piece["text"]:
                pieces.append(piece)
    merged: list[dict] = []
    for piece in pieces:
        if merged:
            prev = merged[-1]
            joined_len = len(prev["text"]) + 1 + len(piece["text"])
            if (joined_len <= MAX_LINE_CHARS * 2 and piece["end"] - prev["begin"] <= MAX_CUE_MS
                    and piece["begin"] - prev["end"] < 700):
                prev["text"] = f"{prev['text']} {piece['text']}".strip()
                prev["end"] = piece["end"]
                continue
        merged.append(dict(piece))
    return normalize_cues(merged)


# ---------------- 共用：清洗时间轴 ----------------
KEEP_OVERLAPS = False   # 由 --keep-overlaps 设置；normalize_cues 读取（见其 docstring）


def normalize_cues(cues: list[dict], keep_overlaps: bool | None = None) -> list[dict]:
    """丢掉空/纯符号条，补最短时长，保证不重叠、留最小间隔。

    `keep_overlaps=True`（或全局 `KEEP_OVERLAPS`，由 `--keep-overlaps` 设置）时**跳过"压到
    下一句之前"这一步**。2026-09-27 修：这个开关原先只关掉了下游的 `clamp_overlaps`，而重叠
    在**读取阶段就被这里修掉了** → 开关是无效的（实测：一份故意重叠 2 处的侧车，加不加
    `--keep-overlaps` 产出都是 0 重叠，而 `[check] 钳掉 N 处` 永远不出现）。显式参数必须说了算。
    """
    if keep_overlaps is None:
        keep_overlaps = KEEP_OVERLAPS
    clean = []
    for cue in sorted(cues, key=lambda c: c["begin"]):
        text = cue.get("text", "").strip()
        if not text or not HAS_CONTENT_CHAR.search(text):
            continue
        if cue.get("end") is None or cue["end"] <= cue["begin"]:
            cue["end"] = cue["begin"] + 1500
        cue["text"] = text
        clean.append(cue)
    for i, cue in enumerate(clean):
        if cue["end"] - cue["begin"] < MIN_CUE_MS:
            cue["end"] = cue["begin"] + MIN_CUE_MS
        nxt = clean[i + 1] if i + 1 < len(clean) else None
        if nxt and not keep_overlaps and cue["end"] > nxt["begin"] - MIN_GAP_MS:
            cap = nxt["begin"] - MIN_GAP_MS
            # 优先不重叠：只要还留得住 MIN_READABLE_MS 的可读时长，就压到下一句开始之前。
            # 旧版写 max(begin+MIN_CUE_MS, cap)，下一句紧贴时会反推出 120–360ms 重叠
            #（E08 实测 3 处：条 46/340/510）。
            cue["end"] = cap if cap >= cue["begin"] + MIN_READABLE_MS else cue["begin"] + MIN_CUE_MS
    return clean


# ---------------- 2. 翻译 ----------------
# 翻译单元：**整句送翻译**，再把整句译文切回各条 cue。
# 为什么必须有这一层：ASR 的长句会被 _split_long 切成多条 cue，若把"半句话"直接送 MT，
# 模型会把相邻两半合并成一条译文并按顺序重新编号——实测 20 行只回 18 个标记，
# [[3]] 装的是第 4 行的译文，合并点之后整段错位（中英两行对不上）。旧代码靠"对不上就二分"
# 歪打正着地修好，代价是每批 2–7 次请求（384 条实测 83.8s，顶到 50 次/分钟限流）。
# 单元化之后输入天然是完整句子，模型没有可合并的对象，请求数≈批数。
SENT_END = re.compile(r"[.?!…。？！][\"'”’)\]]*$")
UNIT_MAX_CUES = 3          # 无标点长句的兜底上限：再多就拆单元（拆了也不怕，二分兜底）
UNIT_MAX_MS = 14000
UNIT_MAX_CHARS = 120
SPLIT_AFTER = "，。、；：！？…,.;:!? "


def translation_units(cues: list[dict]) -> list[tuple[int, int]]:
    """把 cue 归并成"翻译单元"（完整句），返回 [start, end) 区间列表。

    纯结构判据，不依赖 ASR 元数据：**累积到出现句末标点就收口**；没有标点的长句用
    条数/时长/字数三个上限兜底。每个 cue 恰好属于一个单元、文本恰好被拼接一次。
    """
    units: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(cues) + 1):
        done = i == len(cues) or bool(SENT_END.search(cues[i - 1]["text"].strip()))
        over = (i - start >= UNIT_MAX_CUES
                or cues[i - 1]["end"] - cues[start]["begin"] >= UNIT_MAX_MS
                or sum(len(c["text"]) for c in cues[start:i]) >= UNIT_MAX_CHARS)
        if done or over:
            units.append((start, i))
            start = i
    return units


def split_translation(text: str, weights: list[int]) -> list[str]:
    """整句译文 → 按成员 cue 的源文字长度切回多条；优先切在标点/空格之后。

    长度比例来自源文（各 cue 的字符数），切口在目标位置附近 ±6 字符内挑标点；
    挑不到就按比例硬切。**任何一条都不许为空**——空行等于屏幕上少一行字。
    """
    k = len(weights)
    text = (text or "").strip()
    if k <= 1:
        return [text]
    if not text:
        return [""] * k
    total = sum(weights) or k
    targets: list[int] = []
    acc = 0
    for i in range(k - 1):
        acc += weights[i]
        targets.append(int(round(len(text) * acc / total)))
    cuts: list[int] = []
    for idx, target in enumerate(targets):
        lo = (cuts[-1] + 1) if cuts else 1
        hi = len(text) - (k - 1 - idx)          # 给后面每条至少留 1 个字符
        best = max(lo, min(target, hi))
        for d in range(0, 7):
            cands = [c for c in (target - d, target + d) if lo <= c <= hi
                     and text[c - 1] in SPLIT_AFTER]
            if cands:
                best = cands[0]
                break
        # 不做"更宽窗口找句末标点"的兜底（2026-09-28 试过又回退）：`。` 本来就在 SPLIT_AFTER 里、
        # ±6 已经覆盖，把窗口放宽到 ±0.8×段长只会**让切点跑到更远的句号上**，可能把某条 cue
        # 压成很短的一条；而它对审计里那条告警（E01 cue#39/40）毫无帮助——那条是**同句跨两条 cue +
        # 跨语言语序差异**造成的：中文把"won't mate"放在最后，任何连续切分都对不上两条源文。
        cuts.append(best)
    pieces = [text[:cuts[0]]]
    pieces += [text[cuts[i]:cuts[i + 1]] for i in range(len(cuts) - 1)]
    pieces.append(text[cuts[-1]:])
    return [p.strip() for p in pieces]


class _RateLimiter:
    """按"每分钟 N 次"节流。百炼侧的分钟级限额，超了就是一串 429 退避（比限速更慢）。

    具体配额随模型变：REQUESTS_PER_MIN 的注释记着换模型时要重新确认。"""

    def __init__(self, rpm: int) -> None:
        self._interval = 60.0 / max(1, rpm)
        self._lock = threading.Lock()
        self._next_at = time.monotonic()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                time.sleep(self._next_at - now)
                now = time.monotonic()
            self._next_at = now + self._interval


_LIMITER: "_RateLimiter | None" = None

# 目标：**少往返 + 不错位**。实测（qwen-mt-flash，20 条/批）单批约 1.8–2 s；
# 逐行翻译要 765 次往返且触发 429 限流，所以批量优先、错位才二分、补译也成批。
def _translate_local(batch: list[str], server: str, timeout: int,
                     src_lang: str, tgt_lang: str) -> list[str] | None:
    """本地后端：llama.cpp / mlx-lm 的 OpenAI 兼容 /v1/chat/completions（Hy-MT2 官方分隔符模板）。"""
    prompt = local_prompt(src_lang, tgt_lang) + DELIM.join(batch)
    payload = {"model": "local", "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.7, "top_p": 0.6, "top_k": 20,
               "repetition_penalty": 1.05, "max_tokens": 4096, "stream": False}
    req = urllib.request.Request(server.rstrip("/") + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"[mt] 本地服务失败：{type(exc).__name__} {str(exc)[:110]}", file=sys.stderr)
        return None
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    parts = [x.strip() for x in text.split("|||")]
    return parts if len(parts) == len(batch) else None


def style_note(src: str, tgt: str) -> str:
    """通用指令模型的风格指令，拼进每条 user 消息末尾。

    这是选通用指令模型而非 MT 专用模型的收益来源：qwen-mt 系对提示词里的自由文本指令
    无动于衷（实测加风格指令与不加一字不差），而通用指令模型加了才会把习语意译——
    实测（2026-09-28，qwen3.7-flash）"if it's not one thing, it's another" →
    「麻烦事一桩接一桩，没完没了」。方向与语言名进模板，zh→en 等方向同样适用。
    """
    return (f"\n\n翻译要求：这是影视对白（口语）。遇到{src}习语或惯用表达必须意译成{tgt}里"
            "对应的自然说法，禁止逐字直译；译文要像母语者平时说话那样自然。")


# qwen3 系的服务端**默认开思考**：实测同一句翻译，不发 enable_thinking 字段 = 39.4 s、
# 2353 输出 tokens（其中 6877 字是思考）；显式 False = 0.6 s、10 tokens（2026-09-28）。
# bl text chat 只有 --enable-thinking（开启用），请求体根本不带该字段（--dry-run 实证）
# → qwen3 系必须直连 HTTP 关思考，不能走 bl（否则每批慢 10-60 倍，整集翻译多花几分钟）。
DASHSCOPE_CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


def _needs_direct_http(chat_model: str) -> bool:
    """哪些模型 bl 传不了必需参数、必须直连。目前只有 qwen3 系（关思考字段）。"""
    return chat_model.startswith("qwen3")


def _http_chat(messages: list[dict], key: str, chat_model: str, timeout: int) -> tuple[str, str]:
    """直连 OpenAI 兼容端点发一次 chat。返回 (raw_body, err_detail)；成功时 err 为空。"""
    body = {"model": chat_model, "messages": messages, "temperature": 0.3,
            "max_tokens": 4096, "enable_thinking": False}   # Qwen3 默认开思考，必须显式关
    req = urllib.request.Request(
        DASHSCOPE_CHAT_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace"), ""
    except Exception as exc:
        detail = ""
        if isinstance(exc, urllib.error.HTTPError):
            try:
                detail = exc.read().decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            detail = detail or f"HTTP {exc.code} {exc.reason}"
        else:
            detail = f"{type(exc).__name__} {str(exc)[:160]}"
        return "", detail[-500:]


def _cloud_payload(batch: list[str], chat_model: str, system: str,
                   src_lang: str, tgt_lang: str) -> list[dict]:
    """构造请求体。请求形态是**单条 user 消息**（system 参数不进请求，不引入未测过的变量）。

    翻译方向由 src_lang/tgt_lang 决定 —— 旧版把"把英文翻译成简体中文"写死在提示词里，
    于是 --target-lang en 完全失效（拿中文当输入也照样要求译成中文），
    实测产出是"中文原样重复两遍"的假双语。方向必须进提示词，且要与语言对一致。
    这一条由 selftest-langs.py 钉成闸门，改这里必须重跑它。
    """
    src, tgt = lang_name(src_lang), lang_name(tgt_lang)
    # 编号标记协议：模型偶尔把一句拆成两条（实测 20 条回 23/25 条），二分永远不收敛；
    # 带 [[n]] 标记就能把拆出来的片段按标记归位，一次请求拿全，不用反复二分。
    # 解析侧本就按 [[n]] 切片、与模型无关，实测 30 条整批 30/30 标记、顺序正确（2026-09-28）。
    note = style_note(src, tgt)
    if len(batch) == 1:
        return [{"role": "user", "content":
                 f"把下面这句{src}翻译成{tgt}，只输出译文：\n" + batch[0] + note}]
    marked = "\n".join(f"[[{i + 1}]] {x}" for i, x in enumerate(batch))
    return [{"role": "user", "content":
             f"把下面每一行{src}翻译成{tgt}。必须原样保留每行开头的编号标记 [[n]]，"
             "一个标记对应一条译文，不要合并或拆分编号，**最后一行也要单独给出它的编号**：\n"
             + marked + note}]


def _ask_cloud(batch: list[str], key: str, chat_model: str, system: str,
               timeout: int = TRANSLATE_TIMEOUT,
               src_lang: str = "en", tgt_lang: str = "zh") -> list[str] | None:
    """一次云端请求。返回 None = 失败或条数/编号不符（交由上层二分），绝不猜测对齐关系。"""
    payload = _cloud_payload(batch, chat_model, system, src_lang, tgt_lang)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        msg_file = fh.name
    try:
        raw = ""
        direct = _needs_direct_http(chat_model)
        service = "翻译（dashscope chat）" if direct else "翻译（bl text chat）"
        for attempt in range(1, TRANSLATE_ATTEMPTS + 1):
            if direct:
                # qwen3 系：bl 发不了 enable_thinking 字段（服务端默认开思考，实测慢 10-60 倍）
                raw, err = _http_chat(payload, key, chat_model, timeout)
                ok = bool(raw.strip()) and not err
            else:
                proc = subprocess.run(
                    _bl_argv(find_bl()) + ["text", "chat", "--model", chat_model,
                                           "--messages-file", msg_file, "--api-key", key,
                                           "--output", "json", "--quiet", "--timeout", str(timeout)],
                    capture_output=True, text=True, env=bl_env())
                raw = proc.stdout
                err = "" if proc.returncode == 0 else (proc.stderr or raw)
                ok = proc.returncode == 0 and raw.strip()
            if ok:
                break
            detail = (err or raw)[-200:].replace("\n", " ")
            # 配置类错误（key 无效/欠费/模型未开通）→ 立刻终止并给配置指引，不进重试与二分
            if diagnose_service(detail) and not diagnose_service(detail)[0].startswith("触发限流"):
                fail_config(service, detail)
            print(f"[mt] 第 {attempt}/{TRANSLATE_ATTEMPTS} 次失败：{detail}", file=sys.stderr)
            if attempt < TRANSLATE_ATTEMPTS:
                time.sleep(2 * attempt)          # 429 限流时退避
        else:
            return None
        if '"error"' in raw[:200] and '"code"' in raw[:600]:
            if diagnose_service(raw[:600]):
                fail_config("翻译（bl text chat）", raw[:600])
            return None

        content: str | None = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
            return [x.strip() for x in parsed]
        if isinstance(parsed, dict):
            node = (parsed.get("choices") or [{}])[0].get("message", {}) if parsed.get("choices") else parsed
            content = node.get("content") if isinstance(node, dict) else None
            content = content if isinstance(content, str) else json.dumps(node, ensure_ascii=False)
        elif isinstance(parsed, str):
            content = parsed
        else:
            content = raw
        if len(batch) == 1:                        # 单行：直接取正文，不必是 JSON
            text = (content or "").strip()
            if text.startswith("[") :
                try:
                    arr = json.loads(text, strict=False)
                    return [str(arr[0]).strip()] if isinstance(arr, list) and arr else None
                except json.JSONDecodeError:
                    pass
            return [text] if text else None
        # 编号标记协议解析：文本按 [[n]] 切片，同编号的片段合并（模型拆句时仍能归位）
        marked_text = content or ""
        found = list(re.finditer(r"\[\[\s*(\d+)\s*\]\]", marked_text))
        if found and len(batch) > 1:
            slots: dict[int, list[str]] = {}
            for pos, m in enumerate(found):
                n = int(m.group(1))
                end = found[pos + 1].start() if pos + 1 < len(found) else len(marked_text)
                piece = marked_text[m.end():end].strip()
                if piece:
                    slots.setdefault(n, []).append(piece)
            if all(i + 1 in slots for i in range(len(batch))):
                got = [" ".join(slots[i + 1]).strip() for i in range(len(batch))]
                if length_shift_suspected([len(x) for x in batch], [len(x) for x in got]):
                    # 编号齐全 ≠ 内容对位：模型可以合并两句、再把另一句拆成两条，编号数照样对得上。
                    print(f"[mt] {len(batch)} 条编号齐全但**长度自洽性判为整体平移** → 二分",
                          file=sys.stderr)
                    return None
                return got
            # 标记不全 → 必须二分，**不能"只补缺的那几条"**（2026-09-27 实测反例）：
            # 模型是把"半句话"和它的下半句合并成一条译文、然后**按顺序重新编号**——
            # 实测 20 行只回 18 个标记，[[3]] 里装的是第 4 行的译文，合并点之后整段错位。
            # 补缺口只能补回末尾那几条，中间那 16 条的错位会原样留在成品里（中英文对不上）。
            # 二分之所以能修好：批变小以后模型不再合并，单条更无从合并，递归必然收敛。
            missing = [i + 1 for i in range(len(batch)) if i + 1 not in slots]
            if missing == [len(batch)]:
                # 模型的稳定毛病：**总漏最后一行**（提示词已三次强调"最后一行也要单独给出编号"，
                # 实测 batch=20 的批次里它照样漏）。二分修它要 log2(n) 层 ×2 次请求
                # （实测 20→10→5→3 共 ~8 次），而"只缺最后一行"是**无歧义**的：
                # 前 n-1 条已按标记各就各位，最后一条单独一问即可（单条请求结构上不可能错位）。
                # 实测：E04 翻译 26.8s → 18.1s。
                # **只缺末行 ≠ 只有末行有问题**（2026-09-27 实测回归）：模型把第 4、5 行合并进
                # 同一个 [[4]]、其后整体前移、末尾那条被挤掉，于是"缺的"确实只有最后一行，
                # 但前面 29 条已经全部错位。旧版据此直接收下，污染了一个批次的 15 个单元、
                # 成品里出现"中文提前一条"，而标点闸门看不见（该段每句都以句号结尾）。
                head = [" ".join(slots[i + 1]).strip() for i in range(len(batch) - 1)]
                if length_shift_suspected([len(x) for x in batch[:-1]], [len(x) for x in head]):
                    print(f"[mt] {len(batch)} 条只缺末行，但长度自洽性判为**整体平移**"
                          f"（模型合并了中间两句）→ 二分（不补末行）", file=sys.stderr)
                    return None
                tail = _ask_cloud([batch[-1]], key, chat_model, system, timeout, src_lang, tgt_lang)
                if tail and (tail[0] or "").strip():
                    print(f"[mt] {len(batch)} 条只缺末行标记 → 单独补问 1 次（不必二分）", file=sys.stderr)
                    return ([" ".join(slots[i + 1]).strip() for i in range(len(batch) - 1)]
                            + [tail[0].strip()])
            print(f"[mt] 标记不全（{len(batch)} 条缺 {len(missing)} 个：{missing[:5]}）→ 二分"
                  f"（合并会整体错位，只能二分修正）", file=sys.stderr)
            return None
        match = re.search(r"\[.*\]", content or "", re.S)
        if not match:
            return None
        try:
            arr = json.loads(match.group(0), strict=False)   # 模型常在字符串里漏转义换行
        except json.JSONDecodeError:
            return None
        return [str(x).strip() for x in arr] if isinstance(arr, list) else None
    finally:
        os.unlink(msg_file)


def _translate_batch(batch: list[str], key: str, chat_model: str, system: str,
                     timeout: int = TRANSLATE_TIMEOUT, backend: str = "cloud",
                     local_server: str = LOCAL_SERVER_DEFAULT,
                     src_lang: str = "en", tgt_lang: str = "zh") -> list[str]:
    """一批 → 译文列表，长度恒等于输入。

    条数不符就二分（递归到单行），**绝不末尾补空**——补空等于把整批译文按错位映射出去。
    也不"只补缺的那几条"：模型合并半句后会重新编号，缺口之后的条目全是错位的（见 _ask_cloud）。
    """
    if not batch:
        return []
    if _LIMITER is not None and backend != "local":
        _LIMITER.wait()
    out = (_translate_local(batch, local_server, timeout, src_lang, tgt_lang) if backend == "local"
           else _ask_cloud(batch, key, chat_model, system, timeout, src_lang, tgt_lang))
    if out is not None and len(out) == len(batch):
        return [str(x) for x in out]
    if len(batch) == 1:
        print(f"[mt] 单行仍失败：{batch[0][:40]!r}", file=sys.stderr)
        return [out[0] if out else ""]
    mid = len(batch) // 2
    print(f"[mt] {len(batch)} 条只回 {len(out) if out else 0} 条 → 二分", file=sys.stderr)
    kw = dict(key=key, chat_model=chat_model, system=system, timeout=timeout, backend=backend,
              local_server=local_server, src_lang=src_lang, tgt_lang=tgt_lang)
    return _translate_batch(batch[:mid], **kw) + _translate_batch(batch[mid:], **kw)


def missing_indices(lines: list[str], translations: list[str], tgt_lang: str) -> list[int]:
    """哪些行没译成目标语言（漏条 / 原样回吐）——补译与补译后复核共用同一判据。

    判据必须**跟着目标语言走**：旧版写死"译文里没有汉字就是漏译"，在 zh→en 方向下
    每条正确的英文译文都不含汉字 → 全片被判漏译，白跑两轮补译，而补译结果又因同一条
    汉字判据被丢弃。纯符号行（♪♪♪）不算可翻译行，避免无谓补译。
    """
    return [i for i, t in enumerate(translations)
            if HAS_WORD_CHAR.search(lines[i] or "") and not looks_like_lang(t or "", tgt_lang)]


def repair_missing(lines: list[str], translations: list[str], key: str, chat_model: str,
                   system: str, timeout: int, backend: str, local_server: str,
                   src_lang: str = "en", tgt_lang: str = "zh", rounds: int = 2) -> list[str]:
    """成批补译（每批 10 条，最多 rounds 轮），不逐行。

    漏译判据必须**跟着目标语言走**：旧版写死"译文里没有汉字就是漏译"，在 zh→en 方向下
    每条正确的英文译文都不含汉字 → 全片被判为漏译，白跑两轮补译（数百次请求），
    而补译结果又因同一条汉字判据被丢弃 —— 纯烧钱且永不收敛。
    """
    for rnd in range(1, rounds + 1):
        bad = missing_indices(lines, translations, tgt_lang)
        if not bad:
            return translations
        print(f"[mt] 第 {rnd} 轮补译：{len(bad)} 条", flush=True)
        for start in range(0, len(bad), 10):
            idx = bad[start:start + 10]
            fixed = _translate_batch([lines[i] for i in idx], key, chat_model, system,
                                     timeout, backend, local_server, src_lang, tgt_lang)
            for i, t in zip(idx, fixed):
                if looks_like_lang(t or "", tgt_lang):
                    translations[i] = t
    return translations


def system_for(src_lang: str, tgt_lang: str) -> str:
    """翻译系统提示词（translate 与 repair_missing 必须用同一份，否则补译风格会漂）。

    注意：当前云端路径**不发送 system 角色**（请求形态是单条 user 消息，见 _cloud_payload），
    所以这份文本的实际消费者是本地后端与可能的未来路径。别因为"看着没被用上"就删——
    translate 与 repair_missing 共用它，是"补译风格不漂"的依据。
    """
    return (f"You are a professional subtitle translator. Translate each {lang_name(src_lang)} line "
            f"into {lang_name(tgt_lang)}. Keep the same order and count. "
            f"Output ONLY a JSON array of strings.")


def translate(lines: list[str], key: str, chat_model: str, src_lang: str, tgt_lang: str,
              workers: int = 4, batch_size: int = TRANSLATE_BATCH,
              timeout: int = TRANSLATE_TIMEOUT, partial_path: Path | None = None,
              fingerprint: str = "", backend: str = "cloud",
              local_server: str = LOCAL_SERVER_DEFAULT, rpm: int = REQUESTS_PER_MIN,
              no_cache: bool = False) -> list[str]:
    """批量翻译；批间并行，输出严格保持输入顺序。

    增量缓存：每完成一批就把该批结果写进 partial_path（按指纹+批大小校验），
    重跑时只补缺失批次——长片翻译中途失败不再从零开始。

    `no_cache=True`（`--no-cache`）时必须**连这份续跑缓存也不读**：实测（2026-09-27）
    上次崩在中途留下的 partial 会被静默复用（日志 `[mt] 续跑：复用已完成 1/1 批`），
    `--no-cache` 名义上"强制重新翻译"却一次请求都没发——显式参数被架空。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    global _LIMITER
    _LIMITER = _RateLimiter(rpm)

    system = system_for(src_lang, tgt_lang)
    batches = [lines[i:i + batch_size] for i in range(0, len(lines), batch_size)]
    results: list[list[str] | None] = [None] * len(batches)
    lock = threading.Lock()

    def persist() -> None:
        if partial_path is None:
            return
        payload = {"fingerprint": fingerprint, "batch_size": batch_size, "count": len(lines),
                   "batches": {str(i): r for i, r in enumerate(results) if r is not None}}
        partial_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    if partial_path is not None and partial_path.exists() and not no_cache:
        try:
            cached = json.loads(partial_path.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fingerprint and cached.get("batch_size") == batch_size:
                for key_idx, value in (cached.get("batches") or {}).items():
                    i = int(key_idx)
                    if 0 <= i < len(batches) and isinstance(value, list) and len(value) == len(batches[i]):
                        results[i] = value
                reused = sum(1 for r in results if r is not None)
                if reused:
                    print(f"[mt] 续跑：复用已完成 {reused}/{len(batches)} 批", flush=True)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    todo = [i for i, r in enumerate(results) if r is None]
    done = len(batches) - len(todo)
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_translate_batch, batches[i], key, chat_model, system, timeout,
                                   backend, local_server, src_lang, tgt_lang): i for i in todo}
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()
                with lock:
                    done += 1
                    persist()
                print(f"[mt] {done}/{len(batches)} 批（{min(done * batch_size, len(lines))}/{len(lines)} 条）",
                      flush=True)

    translations: list[str] = []
    for r in results:
        translations.extend(r or [])
    return translations[:len(lines)]


# ---------------- 3. 写 SRT ----------------
def write_source_srt(cues: list[dict], path: Path) -> None:
    """写原文缓存 `<基名>.source.srt` —— 中间产物，不是交付物。

    交付物是 .ass（见 write_ass）。这份 SRT 只服务两件事：源字幕复用（省掉整片读盘）
    与 ffsubsync 同步校验。因此**不做换行包装**：ASS 侧由 libass 按画面宽度自动折行，
    这里预先硬折只会把多余的换行带进成品。
    """
    blocks = [
        f"{i}\n{ts(cue['begin'])} --> {ts(cue['end'])}\n{cue['text'].strip()}"
        for i, cue in enumerate(cues, 1)
    ]
    atomic_write_text(path, "\n\n".join(blocks) + "\n")
    print(f"[srt] {len(cues)} 条 → {path}", flush=True)


# ---------------- 3b. 写 ASS（唯一交付物）----------------
# 为什么不能再出 SRT：SubRip 只有序号/时间轴/纯文本，**格式本身没有任何样式位**，
# 字号、颜色、加粗、描边都无处安放；往 SRT 里塞 <font color> 是播放器私有行为、
# 支持参差不齐，不能当交付标准。要"上面的字幕更醒目"就只能出 ASS。
ASS_MARK = "omnisub"                # 产出标记：把自己产出的成品从"外挂字幕"候选里排除
ASS_FONT_DEFAULT = "PingFang SC"    # 中文首选；缺字体时 libass 按系统默认回落
# 「上面的字幕更醒目」的落地（方案 A 高对比白系，2026-09-28 按用户截图参照收紧）：
#   Upper = 译文行（在上）：纯白 #FFFFFF、6.5% 画面高、加粗、描边更粗 → 醒目
#   Lower = 原文行（在下）：暖白 #F0EDE6（BGR 即 E6EDF0）、4.0% 画面高、常规字重 → 退到背景
# 截图参照的两行字号比约 1.6:1；旧版 5.0/4.35（1.15:1）被用户判为"不明显"。
# 谁在上由 display_order() 决定（译文在上、原文沉底），样式只认"位置"，不认具体语言。
# (样式名, 主色 &HAABBGGRR, 字号/画面高, 粗体开关, 描边/画面高)
ASS_STYLES = (
    ("Upper", "&H00FFFFFF", 0.0650, -1, 0.0032),
    ("Lower", "&H00E6EDF0", 0.0400, 0, 0.0020),
)


def ass_time(ms: int) -> str:
    """ASS 时间戳：H:MM:SS.cc（厘秒）。"""
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, cs = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{cs // 10:02d}"


def ass_escape(text: str) -> str:
    """ASS 正文转义。

    花括号是覆盖标签的定界符（正文里的 `{` 会被当成标签头），反斜杠是转义符。
    顺序有讲究：**先转义反斜杠、再包花括号**，反过来的话自己加的那批反斜杠会被二次转义。
    """
    t = (text or "").replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return "\\N".join(line.strip() for line in t.splitlines() if line.strip())


def write_ass(cues: list[dict], path: Path, lines_by_cue: list[list[str]],
              width: int = 1920, height: int = 1080) -> None:
    """写双语 ASS：每条的 N 行放进**同一个 Dialogue 事件**，用 \\N 换行、\\r 按行切样式。

    为什么不用"每行一个事件 + 各自 MarginV"：那要手算两行的行高差，字号或分辨率一变就错位，
    而本项目的字号是按画面高度比例算的，换片就得重算。单事件方案里两行天然是一个整体，
    居中堆叠与底部定位全交给 libass，换字号/换分辨率都不会散。
    行序 = lines_by_cue 里的顺序（由 display_order() 给出：译文在上、原文沉底）：
    第一条在最上、用 Upper 样式，其余用 Lower。
    """
    w = max(320, int(width or 1920))
    h = max(240, int(height or 1080))
    sizes = [max(12, round(h * ratio)) for _, _, ratio, _, _ in ASS_STYLES]
    outlines = [max(1, round(h * ratio)) for _, _, _, _, ratio in ASS_STYLES]
    margin_v = max(10, round(h * 0.030))

    lines: list[str] = [
        "[Script Info]",
        f"; Generated by {ASS_MARK}",
        f"Title: {ASS_MARK}",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    for (name, colour, _, bold, _), size, outline in zip(ASS_STYLES, sizes, outlines):
        # Alignment=2（底部居中）；两个样式用同一个 MarginV，单事件里才不会互相打架
        lines.append(f"Style: {name},{ASS_FONT_DEFAULT},{size},{colour},{colour},&H00000000,"
                     f"&H80000000,{bold},0,0,0,100,100,0,0,1,{outline},1,2,40,40,{margin_v},1")
    lines += ["", "[Events]",
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    rendered = 0
    width_rows = 0
    dropped_rows = 0
    merged_cues = 0
    prev_line: int | None = None      # lines 里上一条已写出的 Dialogue 下标（用于把纯符号条并进去）
    for i, cue in enumerate(cues):
        raw = lines_by_cue[i] if i < len(lines_by_cue) else []
        # 逐行过滤，但**保留原槽位下标**：样式是按位置给的（槽 0 = Upper），删掉一行后
        # 若重新编号，原文行会被渲染成醒目的 Upper（实测这就是"…"那类条目的成因）。
        slots = [(j, r.strip()) for j, r in enumerate(raw)
                 if (r or "").strip() and HAS_ALNUM.search(r)]
        if not slots:
            # 整条只剩纯符号（"…" / "。" / "♪♪♪"，实测全片 0 条）：不出一条只闪符号的字幕，
            # 把它的结束时间并进上一条，**时间轴不留洞**（丢时间比丢一行更难查）。
            dropped_rows += 1
            if prev_line is not None:
                head = lines[prev_line].split(",")
                head[2] = ass_time(cue["end"])
                lines[prev_line] = ",".join(head)
                merged_cues += 1
            continue
        dropped_rows += len(raw) - len(slots)
        width_rows = max(width_rows, len(slots))
        body = "\\N".join(
            f"{{\\r{ASS_STYLES[min(j, len(ASS_STYLES) - 1)][0]}}}{ass_escape(text)}"
            for j, text in slots)
        lines.append(f"Dialogue: 0,{ass_time(cue['begin'])},{ass_time(cue['end'])},"
                     f"{ASS_STYLES[0][0]},,0,0,0,,{body}")
        prev_line = len(lines) - 1
        rendered += 1
    atomic_write_text(path, "\n".join(lines) + "\n")
    note = ""
    if dropped_rows or merged_cues:
        note = (f"（省略 {dropped_rows} 行纯符号；{merged_cues} 条整条并入上一条——"
                f"只含「…」「。」这类无信息行不出现在屏幕上）")
    print(f"[ass] {rendered} 条 × 最多 {width_rows} 行 → {path}{note}", flush=True)


_STALE_SENTENCE_WARNED = False   # 旧格式转写的告警只打一次（sentences_of 一次运行里会被调多次）
T0 = time.time()
T = {"probe": 0.0, "lang": 0.0, "audio": 0.0, "asr": 0.0, "mt": 0.0, "write": 0.0}


def clamp_overlaps(cues: list[dict], min_ms: int = 200) -> int:
    """钳掉相邻条的重叠：`a.end > b.begin` 时优先收 a 的尾，收不下就推 b 的头。

    ASR 的词级时间戳天然会小幅交叠（实测 E02 cue#56「她」与 #57 重叠 0.6s：同一位置两条
    事件一起渲染，屏幕上短暂叠字）。时间轴以"不重叠"为准；做了多少处如实报数
    （`--keep-overlaps` 可关）。实在两边都太短时只收尾，保证条数不变——**绝不丢字幕条**。
    """
    fixed = 0
    for a, b in zip(cues, cues[1:]):
        if a["end"] <= b["begin"]:
            continue
        trim_end = b["begin"] - 1
        if trim_end - a["begin"] >= min_ms:
            a["end"] = trim_end
        elif b["end"] - (a["end"] + 1) >= min_ms:
            b["begin"] = a["end"] + 1
        else:
            a["end"] = max(a["begin"] + 1, trim_end)
        fixed += 1
    return fixed


SYNC_MIN_RUN = 3          # 逐条不一致的**最长连续段**达到它 → 判整体错位（散落的标点差异不算）
SYNC_UNIT_FLOOR = 6       # 单元收口错位判失败的**下限**：实测对齐成品的噪声上限是 2 条
SYNC_UNIT_RATIO = 0.02    # 单元收口错位判失败的占比上限（长片用比例，短片用下限兜）
SYNC_REPAIR_PASSES = 3    # 对齐闸门自愈的迭代上限（实测 1 轮不够覆盖整段污染）


def length_shift_suspected(src_lens: list[int], tgt_lens: list[int]) -> bool:
    """用"长度自洽性"判断内容是否被**整体平移一条**（零成本、与语言无关）。

    对齐时"译文长/源文长"这个比值高度稳定；一旦模型把相邻两句合并进同一个编号、其后整体前移，
    该比值就会跳变，而"译文长 ÷ **下一条**源文长"反而更稳。实测（en→zh，30 条一批）：
      · 对齐批：own MAD **0.027** vs 平移假设 MAD **0.206**（比值 7.6）
      · 合并批：own MAD **0.134** vs 平移假设 MAD **0.054**（比值 0.40）   ← 4 次复现都一致
    判据 = "own 自身足够离散" 且 "平移假设明显更紧"；两侧各有近 19× 余量，故不敏感于语言/长度。
    """
    n = min(len(src_lens), len(tgt_lens))
    if n < 6:
        return False
    src = [max(1, x) for x in src_lens[:n]]
    tgt = [max(1, x) for x in tgt_lens[:n]]
    own = [t / s for t, s in zip(tgt, src)]
    shift = [tgt[i] / src[i + 1] for i in range(n - 1)]

    def mad(xs: list[float]) -> float:
        med = statistics.median(xs)
        return statistics.median([abs(x - med) for x in xs])

    own_mad, shift_mad = mad(own), mad(shift)
    # 主判据是"平移假设明显更紧"；下限只用来防"所有行长度几乎相同"的退化场景
    # （那种情况两个假设都是噪声，比大小没意义）。下限取 3% 中位数：实测真实干净批
    # own MAD 0.027 > 0.010 仍会进入比例比较，但因 shift MAD 0.206 远大于 own×0.8 而不触发。
    return own_mad > 0.03 * statistics.median(own) and shift_mad < own_mad * 0.8


def sync_faults(cues: list[dict], spans: list[tuple[int, int]], pieces: dict[str, list[str]],
                unit_lines: list[str], src_lang: str) -> dict:
    """译文行与原文行是否逐条对得上 —— **零成本结构判据**（不发任何请求）。

    为什么必须有它：编号标记协议只校验"编号齐全"，而模型**可以在编号齐全的同时把内容整体错位**
    （少装一条、另一条拆成两条，编号数照样对得上；本机在批次里实测复现过两次）。这类错位
    `report_qa` 看不见（它只查空行），脚本会以退出码 0 交出"中英对不上"的成品。
    实测代价（2026-09-27）：一次并发产物整体提前一条 cue，靠人工逐条复译才查出来——
    50 次/分钟限速下花了 7 分钟。这一步必须是程序的责任，不该由人眼兜。

    判据用**与语言无关的结构信号**——句末标点：
      · 单元收口：单元源文以句末标点收尾时，落在该单元**最后一条 cue** 上的译文也必须收尾。
        错位时这条 cue 拿到的是**下一个单元**的中间片段，天然不以句末标点结束。
      · 逐条一致：源文是否句末 ↔ 译文是否句末应一致；统计**最长连续不一致段**
        （错位是"成段"的，改写是"散落"的——这正是区分二者的关键）。
    """
    targets = [lang for lang in pieces if lang != src_lang]
    n = len(cues)
    bad_units: set[int] = set()
    run_best = 0
    run_total = 0
    run_span: tuple[int, int] | None = None
    for lang in targets:
        arr = pieces.get(lang) or []
        if len(arr) != n:
            continue
        for ui, (a, b) in enumerate(spans):
            if not SENT_END.search((unit_lines[ui] or "").strip()):
                continue                 # 单元是被长度上限切开的，不要求收口
            if not SENT_END.search((arr[b - 1] or "").strip()):
                bad_units.add(ui)
        run = 0
        for j in range(n):
            s_fin = bool(SENT_END.search((cues[j]["text"] or "").strip()))
            t_fin = bool(SENT_END.search((arr[j] or "").strip()))
            if s_fin != t_fin:
                run_total += 1
            run = run + 1 if s_fin != t_fin else 0
            if run > run_best:
                run_best, run_span = run, (j - run + 1, j)
    # **长度自洽性滚动扫描**：整体平移一条时，标点信号可能被"每句都以句号结尾"完全掩盖
    #（实测 E04 有 25 个单元被平移，标点判据全绿 → 结构闸门漏过，靠地面真值审计才发现）。
    # 判别量是**离散度**而不是中位偏移：平移后"译文长 ÷ 源文长"的中位数期望不变（长度可比），
    # 但离散度明显变大。实测（E04 真实数据）：干净时逐条比值 MAD 0.038–0.048，
    # 平移 25 个单元后局部 MAD 翻 2.5 倍以上；六集干净数据**零命中**，人造 10/25/30 条平移都能抓到。
    # 取"局部 8 条 MAD > 2.5 × 全局 MAD"且**连续 ≥4 条**才算（散点是正常改写噪声）。
    # 已知边界（如实标注）：源文长度**均匀**的区段里平移不产生离散度 → 这条信号会失明；
    # 真正的防线是 `_ask_cloud` 的**入批校验**（预防污染），这条是给"已被污染的缓存"兜底。
    shift_runs = 0
    if targets:
        arr0 = pieces.get(targets[0]) or []
        if len(arr0) == n:
            u_src = [len(unit_lines[ui] or "") for ui in range(len(spans))]
            u_tgt = [sum(len(arr0[j] or "") for j in range(a, b)) for a, b in spans]
            ratios = [u_tgt[i] / max(1, u_src[i]) for i in range(len(spans))]

            def _mad(xs: list[float]) -> float:
                med = statistics.median(xs)
                return statistics.median([abs(x - med) for x in xs])

            half, need = 4, 4                          # 局部窗 8 条，连续 ≥4 条才算成段
            local_mads: dict[int, float] = {}
            for i in range(half, len(ratios) - half):
                local_mads[i] = _mad(ratios[i - half:i + half + 1])
            # 基数用**局部 MAD 的中位数**（自校准），而不是全片 MAD：全片 MAD 会被污染区自己抬高，
            # 实测"平移占多数"时会把判据钝化。基线取中位数则天然来自未被污染的那部分。
            base = statistics.median(list(local_mads.values())) if local_mads else 0.0
            if base > 0:
                flagged: set[int] = set()
                for i, lm in local_mads.items():
                    if lm > 2.5 * base:
                        flagged.add(i)
                # 连续 ≥`need` 个命中窗口才算"成段"；命中后**标记整段的覆盖范围**
                #（窗口中心 ±half），而不是只标最后几条——污染区是整段，修复也该修整段。
                run_start: int | None = None
                for i in range(len(ratios) + 1):
                    if i in flagged:
                        if run_start is None:
                            run_start = i
                    elif run_start is not None:
                        if i - run_start >= need:
                            shift_runs += 1
                            for ui in range(max(0, run_start - half), min(len(spans), i + half + 1)):
                                bad_units.add(ui)
                        run_start = None
    # 修复范围 = 收口错位的单元 ∪ 最长连续段覆盖的单元（两者都指向"内容被挪走了"的那批单元）
    repair = set(bad_units)
    if run_span:
        for ui, (a, b) in enumerate(spans):
            if a <= run_span[1] and b - 1 >= run_span[0]:
                repair.add(ui)
    units = max(1, len(spans))
    # 两段判据：**触发修复要敏感、判失败要保守**。
    #   suspicious：单元收口错位 ≥1 条、或连续不一致 ≥2 条 → 值得花几条单条请求重译这片区域。
    #              误报的代价只是几次请求（单条重译只会更准，不会更差），所以可以敏感。
    #   ok       ：连续不一致 ≥3 条、或单元收口错位 ≥3 条（长片按 1% 兜）才算"确实错位"。
    # 干净基线（E02 全片实测）：标点不一致 3/373、最长连续段 1 条、单元收口错位 0/273。
    # 判失败要**远离噪声**：实测对齐成品的单元收口错位是 0–2 条（E01–E06 各 0–1；另一部
    # zh→en 片 2 条，两处都是"中文句末『。』被 qwen-mt 译成英文逗号"这种正常改写）。
    # 阈值贴到 3 会让第 3 条噪声就把好成品判成 exit 1。修复触发仍然敏感（suspicious，见下），
    # 失败判定则取 max(6, 2%)，真错位（成段不一致）由 run 判据兜住。
    unit_limit = max(SYNC_UNIT_FLOOR, int(units * SYNC_UNIT_RATIO))
    ok = run_best < SYNC_MIN_RUN and len(bad_units) < unit_limit
    # 修复触发也要求"成段"证据：**散落的 1–2 条标点差异是正常改写**（实测 qwen-mt 会把 ASR
    # 小句末的「。」译成英文逗号），为它重译只会让已交付文本无理由漂移、还每次白花请求。
    # 真错位有 run_best>=2 的连续不一致；单元收口错位要 ≥3 条才算证据。
    suspicious = run_best >= 2 or len(bad_units) >= 3 or shift_runs > 0
    return {"ok": ok, "suspicious": suspicious, "units_bad": len(bad_units), "run": run_best,
            "shift_windows": shift_runs,
            "unit_limit": unit_limit, "miss": run_total, "bad_units": sorted(bad_units),
            "repair_units": sorted(repair), "units": units, "no_target": not targets}


def report_sync(info: dict, cues: list[dict], spans: list[tuple[int, int]]) -> None:
    """把对齐闸门的结论打出来（PASS/FAIL 都要有据可查）。"""
    if info.get("no_target"):
        return
    detail = (f"标点不一致 {info.get('miss', 0)}/{len(cues)} 条 · 最长连续不一致段 {info['run']} 条"
              f" · 单元收口错位 {info['units_bad']}/{info['units']}")
    if info.get("shift_windows"):
        detail += f" · 长度平移段 {info['shift_windows']} 段"
    mark = "✅ 译文与原文逐条对齐" if info["ok"] else "❌ 译文与原文存在整体错位"
    print(f"[sync] {mark}（{detail}）", flush=True)


def retranslate_units(indices: list[int], unit_lines: list[str], rows_by_lang: dict[str, list[str]],
                      src_lang: str, key: str, chat_model: str, timeout: int,
                      backend: str, local_server: str) -> int:
    """把被判错位的单元**逐条单独重译**（batch=1）。

    单条请求里模型无从"合并两行再重新编号"——这正是错位的成因，所以单条是**结构上不可能错位**的
    重译路径。返回成功替换的条数（按单元 × 目标语言计）。
    """
    fixed = 0
    global _LIMITER
    if _LIMITER is None and backend != "local":
        # 译文整份命中缓存时 translate() 从不被调用 → 限速器还是 None。
        # 而"缓存命中 + 闸门可疑"正是最需要节流的场景（一次要补几十上百条），
        # 不限速就是背靠背直冲 60 次/分钟接口 → 429 风暴。
        _LIMITER = _RateLimiter(REQUESTS_PER_MIN)
    for ui in indices:
        line = unit_lines[ui]
        if not (line or "").strip():
            continue
        for lang, rows in rows_by_lang.items():
            if lang == src_lang or ui >= len(rows):
                continue
            # 走 _translate_batch：它已经处理了 local 后端与本机限速（旧版无条件 _ask_cloud，
            # --backend local 的用户会被拿去发云端请求并计费，离线环境还会直接失败）
            out = _translate_batch([line], key, chat_model, system_for(src_lang, lang), timeout,
                                   backend, local_server, src_lang, lang)
            if out and (out[0] or "").strip():
                rows[ui] = out[0].strip()
                fixed += 1
    return fixed


def dump_translation_cache(cache_path: Path, lines: list[str], translations: list[str],
                           chat_model: str, src_lang: str, tgt_lang: str, backend: str,
                           fingerprint: str) -> None:
    """译文缓存落盘（原子写）。字段是复用守卫的判据，别删：count/fingerprint/model/src_lang。"""
    atomic_write_text(cache_path, json.dumps(
        {"fingerprint": fingerprint, "count": len(lines), "model": chat_model,
         "src_lang": src_lang, "target_lang": tgt_lang, "backend": backend,
         "translations": translations}, ensure_ascii=False))


def check_timeline(cues: list[dict], video: Path, partial: bool = False) -> None:
    """对齐自检：覆盖率 / 大空隙 / 重叠。这是"字幕与视频一一对应"的机器判据。

    ASR 或外挂字幕都可能只覆盖片段，人工看不出来；这里直接给结论并落进日志。
    """
    try:
        duration = float((ffprobe_json(video).get("format") or {}).get("duration") or 0)
    except (SystemExit, ValueError, TypeError):
        return
    if duration <= 0:
        return
    if not cues:
        print("[check] 对齐自检：没有切出任何字幕条", flush=True)
        return
    last_end = cues[-1]["end"] / 1000.0
    tail_gap = duration - last_end
    gaps = [b["begin"] - a["end"] for a, b in zip(cues, cues[1:])]
    max_gap = max(gaps) if gaps else 0.0      # 只有 1 条字幕时 gaps 为空（旧版 max() 直接 ValueError）
    big = [g for g in gaps if g > 20000]
    overlaps = [(a, b) for a, b in zip(cues, cues[1:]) if a["end"] > b["begin"] + 1]
    cover = last_end / duration * 100
    flags = []
    if tail_gap > max(30, duration * 0.02) and not partial:   # --limit 小样不该刷覆盖率告警
        flags.append(f"末条到片尾还差 {tail_gap:.0f}s")
    elif last_end > duration * 1.02 and not partial:
        flags.append(f"字幕超出片尾 {last_end - duration:.0f}s（可能拿错集/版本不符）")
    if big and not partial:
        flags.append(f"{len(big)} 处 >20s 空隙")
    if overlaps:
        flags.append(f"{len(overlaps)} 处重叠")
    state = ("（小样，跳过覆盖率判定）" if partial and not flags
             else "⚠️ " + "；".join(flags) if flags else "✅ 覆盖率/空隙/重叠正常")
    print(f"[check] 对齐自检：覆盖到 {cover:.1f}%（末条 {last_end:.1f}s / 片长 {duration:.1f}s）、"
          f"最大空隙 {max_gap / 1000:.1f}s、重叠 {len(overlaps)} 处 → {state}", flush=True)


def source_drift(cues: list[dict], src_lang: str) -> list[int]:
    """原文行里疑似**整句不是源语言**的条目（ASR 语言漂移），只对英语源启用。

    实测（2026-09-27）：一部 58.6 分钟纪录片、373 条里有 **20 条（5.4%）**整句变成西班牙语/法语
    （`Durante el invierno,` / `et ils sont insistant.`），中文行是把这些西语/法语译过来的，
    于是成品里"原文行"不再是原文。同一集用 FLAC 与 MP3、给与不给语言提示各整片跑过，
    都没复现 → 是**长音频上的偶发行为**，防不住，只能跑完自己报出来（这活不该由人眼干）。

    判据：命中罗曼语/德语虚词 **且** 一个强英语虚词都没有（`EN_STRONG`）。英文句子几乎必然含
    the/and/of/to…，所以这个"且"很干净；只用强虚词是为了不放过 `no mucho pero suficiente…`
    这类"唯一像英语的词恰好也是西语词"的句子（实测这么漏过 4 条）。
    """
    if normalize_lang(src_lang or "").split("-")[0] != "en":
        return []
    return [i for i, c in enumerate(cues)
            if DRIFT_MARKERS.search(c["text"]) and not EN_STRONG.search(c["text"])]


def report_qa(cues: list[dict], rows: list[list[str]]) -> bool:
    """成品自检（固化进程序，替代"Agent 打开 ass 抽查"这一步）。

    只判**能机器判的硬缺陷**：条数为 0、有空行（漏译/漏行的直接症状）。
    空隙与重叠由 check_timeline 报（旁白片的长空隙是常态，不是缺陷，不该据此判失败）。
    返回 False 时 main 以退出码 1 结束——交付不出去的东西不该静默返回 0。
    """
    total = len(cues)
    blank = sum(1 for r in rows if any(not (x or "").strip() for x in r))
    print(f"[qa] {total} 条 · 行齐全 {total - blank}/{total} · "
          f"{'✅ 可交付' if total and not blank else '❌ 有空行，需检查'}", flush=True)
    return bool(total) and not blank


def report_timing(stem: str, cues: list[dict], origin: str, write_log: bool = True) -> None:
    """打印阶段耗时；默认追加到 ~/.dsh/omnisub-timing.log（评测/CI 用 --no-log 关掉，别污染宿主状态）。"""
    total = time.time() - T0

    def _fmt(x: float) -> str:
        return f"{x:.1f}"

    print(f"【耗时】探测 {_fmt(T['probe'])}s · 源语言探测 {_fmt(T['lang'])}s · 抽音轨 {_fmt(T['audio'])}s · "
          f"转写 {_fmt(T['asr'])}s · "
          f"翻译 {_fmt(T['mt'])}s · 写盘 {_fmt(T['write'])}s · 总计 {_fmt(total)}s"
          f"（{len(cues)} 条，{origin}）", flush=True)
    if not write_log:
        return
    try:
        log = Path.home() / ".dsh/omnisub-timing.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write("\t".join([
                time.strftime("%Y-%m-%d %H:%M:%S"), f"local:{stem[:40]}",
                _fmt(T["probe"]), _fmt(T["audio"]), _fmt(T["asr"]), _fmt(T["mt"]),
                _fmt(T["write"]), _fmt(total), str(len(cues)), str(sum(len(c["text"]) for c in cues)),
            ]) + "\n")
    except OSError as exc:
        print(f"[timing] 写日志失败：{exc}", file=sys.stderr)


def resolve_source_lang(explicit: str, track_lang: str, texts: list[str]) -> tuple[str, str]:
    """源语言三级回退：显式参数 → 字幕轨语言标签 → 文本启发式。返回 (语言码, 依据)。

    为什么不直接信 ASP/ASR 的返回：`bl speech recognize` 的 JSON 里**没有识别到的语言**
    （实测只有 file_url/properties/transcripts/usage 四个键），内嵌轨又常常不打语言标签
    （实测本机某片 4 条国语音轨全有 chi 标签，而另一部片的字幕轨没有），
    所以只能逐级回退，并把"依据"打出来让用户能判断该不该显式指定。
    """
    want = (explicit or "").strip().lower()
    if want and want != "auto":
        return normalize_lang(explicit), "显式 --source-lang"
    code = normalize_lang(track_lang)
    if code:
        return code, f"字幕轨语言标签 {track_lang}"
    code, confident = detect_lang(texts)
    if not code:
        return "", ("文本看着不是英文、又判不出具体语种 → 按未知处理：语言对里每种语言都会真翻译"
                    "（要更准就别用 auto，显式传 --source-lang <语种>）")
    return code, f"文本启发式自动判定 → {code}（要更准可显式传 --source-lang）"


def display_order(pair: tuple[str, ...], src_lang: str) -> list[str]:
    """行序：译文按语言对顺序在上，**原文沉底**。

    为什么不是"语言对顺序即行序"：两份用户参照互相矛盾——中文片要"英上中下"、
    英文片要"中上英下"（截图参照：上行大而粗的译文、下行小而淡的原文）。
    统一两者的不变量是**译文在上且醒目、原文在下**：源=zh → 英上中下；源=en → 中上英下；
    源不在语言对里（ja 源、或源语言没判出来）→ 全是译文，按语言对顺序。
    """
    rest = [lg for lg in pair if lg != src_lang]
    return rest + ([src_lang] if src_lang in pair else [])


def video_size(video: Path) -> tuple[int, int]:
    """画面尺寸 → ASS 的 PlayResX/Y（字号与描边都按它换算）。取不到就退回 1920x1080。"""
    try:
        data = ffprobe_json(video)
    except (SystemExit, ValueError, TypeError):
        return 1920, 1080
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and stream.get("width") and stream.get("height"):
            return int(stream["width"]), int(stream["height"])
    return 1920, 1080


def translate_for_target(lines: list[str], key: str, src_lang: str, tgt_lang: str,
                         chat_model: str, cache_path: Path, fingerprint: str, *,
                         workers: int, batch_size: int, timeout: int, backend: str,
                         local_server: str, rpm: int, no_cache: bool) -> list[str]:
    """一个目标语言的全部字幕行：缓存命中就用缓存，否则翻译 → 补译 → 落缓存。

    每个目标语言一份缓存（<基名>.<语言>.json），互不覆盖：同一部片既出 en 又出 zh 时，
    两边的翻译进度、计费与重跑都是独立的。
    """
    def dump(payload: list[str]) -> None:
        dump_translation_cache(cache_path, lines, payload, chat_model, src_lang, tgt_lang,
                               backend, fingerprint)

    translations: list[str] | None = None
    if cache_path.exists() and not no_cache:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            same_input = (cached.get("count") == len(lines)
                          and cached.get("fingerprint") == fingerprint
                          and len(cached.get("translations") or []) == len(lines))
            # src_lang 必须显式相等：旧缓存没有这个键，若按"缺省即相同"放行，
            # 同一份文本在 zh→en 与 en→zh 之间会互相串用（实测 E33 就撞上过这个坑）
            same_engine = (cached.get("model") == chat_model
                           and cached.get("backend", "cloud") == backend
                           and cached.get("src_lang") == src_lang)
            if same_input and same_engine:
                translations = [str(x) for x in cached["translations"]]
                print(f"[mt] {tgt_lang}：复用译文缓存 {cache_path.name}"
                      f"（{len(translations)} 条，{chat_model}）", flush=True)
            elif same_input:
                print(f"[mt] {tgt_lang}：缓存来自 {cached.get('model')}/"
                      f"{cached.get('backend') or 'cloud'}/{cached.get('src_lang') or '?'}，"
                      f"与本次 {chat_model}/{backend}/{src_lang} 不符 → 重新翻译", flush=True)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            print(f"[mt] {tgt_lang}：缓存损坏（{type(exc).__name__}）→ 忽略并重译：{cache_path.name}",
                  file=sys.stderr)

    if translations is None:
        translations = translate(lines, key, chat_model, src_lang, tgt_lang,
                                 workers=workers, batch_size=batch_size, timeout=timeout,
                                 partial_path=cache_path, fingerprint=fingerprint,
                                 backend=backend, local_server=local_server, rpm=rpm,
                                 no_cache=no_cache)
        dump(translations)
        print(f"[mt] {tgt_lang}：译文已缓存 → {cache_path.name}（重切/重跑不再重复付费）", flush=True)

    # 补译校验：漏条/回原文的行成批补译（不逐行，避免 429 与慢）
    if missing_indices(lines, translations, tgt_lang):
        before = list(translations)
        translations = repair_missing(lines, translations, key, chat_model,
                                      system_for(src_lang, tgt_lang), timeout, backend,
                                      local_server, src_lang, tgt_lang)
        if translations != before:
            dump(translations)
    left = missing_indices(lines, translations, tgt_lang)
    if left:
        print(f"[mt] {tgt_lang}：补译后仍有 {len(left)} 条不像 {lang_name(tgt_lang)}，已如实保留",
              file=sys.stderr)
    return translations


def main() -> None:
    ap = argparse.ArgumentParser(
        description="视频 → 任意语言对的双语 ASS 字幕（复用优先：内嵌字幕 > 外挂字幕 > ASR）")
    ap.add_argument("video", type=Path, nargs="?", help="视频文件")
    ap.add_argument("--install", action="store_true",
                    help="把本技能安装/更新到 DSH 技能目录后退出（打印代码指纹，便于确认版本）")
    ap.add_argument("--dest", type=Path, default=SKILL_DEST_DEFAULT, help="--install 的目标目录")
    ap.add_argument("--doctor", action="store_true",
                    help="环境自检：真执行 ffmpeg/bl 并检查 API key，打印需配置项后退出")
    ap.add_argument("--out", type=Path, default=None, help="输出目录（默认与视频同目录）")
    ap.add_argument("--cache-dir", type=Path, default=None,
                    help="中间产物目录（默认平台缓存目录，如 ~/Library/Caches/omnisub/；"
                         "视频目录只会多出 <基名>.ass 一个文件）")
    ap.add_argument("--source", choices=["auto", "embedded", "sidecar", "asr"], default="auto",
                    help="字幕来源：auto=按优先级自动（内嵌轨 → 同目录外挂 → ASR，默认）；embedded/sidecar/asr 强制走某一路")
    ap.add_argument("--source-lang", default="auto",
                    help="源语言：auto=自动判定（显式参数 → 字幕轨语言标签 → 文本启发式）；"
                         "也可显式给 en/zh/ja/ko/fr…。非中英源语言建议显式指定，"
                         "ASR 语言提示与翻译方向提示词都会更准")
    ap.add_argument("--subtitles", default=None, metavar="LANG[,LANG...]",
                    help="要产出的字幕语言（逗号分隔），默认 en,zh。行序不变量：**译文在上（醒目）、"
                         "原文沉底**（中文片→英上中下、英文片→中上英下）；顺序只决定译文行之间的次序。"
                         "源语言若在列表中则该行直接复用原文、不翻译。"
                         "支持任意语言对与三行以上，如 ja,en,zh")
    ap.add_argument("--target-lang", default=None,
                    help="[已废弃] 单目标写法，等价于 --subtitles <源语言>,<目标语言>；请改用 --subtitles")
    ap.add_argument("--sub-index", type=int, default=None, help="指定内嵌字幕轨 index")
    ap.add_argument("--asr-model", default=ASR_MODEL_DEFAULT,
                    help=f"ASR 模型（默认 {ASR_MODEL_DEFAULT}）")
    ap.add_argument("--chat-model", default=CHAT_MODEL_DEFAULT,
                    help=f"翻译模型（默认 {CHAT_MODEL_DEFAULT}，直连 dashscope 并显式关思考）。"
                         "qwen-mt 系已于 2026-10-04 撤出，选它会被直接拒绝")
    ap.add_argument("--api-key", default=None,
                    help="百炼 key（默认按 ~/.agentmemory/.env 的 OPENAI_API_KEY → DASHSCOPE_API_KEY 顺序找）")
    ap.add_argument("--asr-json", type=Path, default=None, help="复用已有 ASR 结果（跳过解音轨与转写）")
    ap.add_argument("--asr-chunk", type=int, default=ASR_CHUNK_DEFAULT, metavar="N",
                    help="把音频等分 N 段**边抽边转**（默认 0=自动：长片 4 段、短片 1 段）。"
                         "实测转写是整程最大一段（58.6 分钟片 54–100s），4 段并行后 24s（2.3×），"
                         "整句语言漂移 22/373→0；代价是每段边界可能切到词（实测三个边界均无截断）")
    ap.add_argument("--asr-workers", type=int, default=4, help="分片并行度（默认 4，仅分片>1 时生效）")
    ap.add_argument("--no-translate", action="store_true", help="只出原文单语 ASS（不翻译）")
    ap.add_argument("--no-cache", action="store_true", help="忽略译文缓存，强制重新翻译")
    ap.add_argument("--batch", type=int, default=TRANSLATE_BATCH, help="每次翻译的条数（默认 20；实测 40 会大量触发二分反而更慢）")
    ap.add_argument("--refresh-source", action="store_true",
                    help="强制重新抽字幕（默认会复用比视频新的 .source.srt，省掉整片读盘）")
    ap.add_argument("--verify-sync", choices=["auto", "on", "off"], default="auto",
                    help="用 ffsubsync 校验字幕与音轨是否同步：auto=仅外挂字幕时校验（默认）、on=总是、off=不校验")
    ap.add_argument("--allow-desync", action="store_true",
                    help="对齐闸门判失败时仍以退出码 0 交付（默认：失败即报错退出，绝不静默交出"
                         "中英对不上的成品）")
    ap.add_argument("--no-repair-desync", action="store_true",
                    help="不自动修复错位（默认：检出整体错位就对那批单元逐条单独重译后复检）")
    ap.add_argument("--keep-overlaps", action="store_true",
                    help="保留相邻条重叠（默认钳掉：ASR 词级时间戳会小幅交叠，同位置两条事件会叠字）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条字幕（小样验证用，0=全部）")
    ap.add_argument("--workers", type=int, default=4,
                    help="翻译并发数（默认 4）。实测已是**限速瓶颈**：默认 30 条/批时 4 并发 13.8s、"
                         "8 并发 12.7s、2 并发 17.6s——再往上加收益很小")
    ap.add_argument("--rpm", type=int, default=REQUESTS_PER_MIN, help="每分钟最大请求数（默认 50，官方限额 60）")
    ap.add_argument("--backend", choices=["cloud", "local"], default="cloud",
                    help=f"翻译后端：cloud=直连 dashscope（默认 {CHAT_MODEL_DEFAULT}）；"
                         "local=本地 OpenAI 兼容服务")
    ap.add_argument("--local-server", default=LOCAL_SERVER_DEFAULT,
                    help="本地后端地址，如 llama-server --port 8080 或 mlx_lm.server")
    ap.add_argument("--timeout", type=int, default=TRANSLATE_TIMEOUT, help="单次 bl 调用超时秒数（默认 180）")
    ap.add_argument("--keep-audio", action="store_true",
                    help="音频片段本来就一直留在缓存目录（重跑 0 秒复用，这是提速的关键之一）；此开关只为兼容旧命令，不改变行为")
    ap.add_argument("--audio-lossless", action="store_true",
                    help="送 ASR 的音频用 FLAC 无损（默认 16kHz 单声道 MP3 32k，体积小 9 倍、转写更快）")
    ap.add_argument("--no-probe", action="store_true",
                    help="不先探源语言（默认探：--source-lang auto 时不给语言提示，"
                         "实测会让 ASR 整句漂移到西班牙语/法语）")
    ap.add_argument("--no-log", action="store_true",
                    help="不追加 ~/.dsh/omnisub-timing.log（评测/CI 用，避免污染宿主状态）")
    args = ap.parse_args()
    global KEEP_OVERLAPS              # 必须在读取来源之前生效：normalize_cues 在 read_srt/read_ass 里
    KEEP_OVERLAPS = bool(args.keep_overlaps)   # 就把重叠修掉了，晚设置等于开关无效

    # 版本横幅：省掉"这份代码到底是哪份"的人工比对（--install 也打印同一指纹）
    print(f"[omnisub] v{VERSION} rev={code_rev()}  {Path(__file__).resolve()}", flush=True)
    if args.install:
        install_skill(args.dest)
        return
    if args.doctor:
        raise SystemExit(doctor(args.asr_model, args.chat_model))
    if args.video is None:
        ap.error("缺少视频路径（或改用 --install / --doctor）")

    # qwen-mt-* 已于 2026-10-04 撤出可选项（用户令）。留着它不是"多一个选择"：
    # 它对透明型习语只做字面直译，风格指令与原生 domains 全部无效（ASR-API.md 有 A/B 证据），
    # 走下面的通用路径必然 400（它不吃 enable_thinking、不接受 system 角色）。
    # 明确拒，别让它跑到网络层才炸出一个看不懂的报错。
    if args.chat_model.startswith("qwen-mt"):
        raise SystemExit(
            f"--chat-model {args.chat_model}：qwen-mt 系已于 2026-10-04 撤出，不再是可选翻译模型。"
            f"翻译模型固定用 {CHAT_MODEL_DEFAULT}（直连 dashscope、显式关思考、支持风格指令做习语意译）。"
            "弃用理由与实测证据见 ASR-API.md「纯文本翻译」。"
        )

    video: Path = args.video.expanduser().resolve()
    if not video.exists():
        raise SystemExit(f"找不到视频：{video}")
    out_dir = (args.out or video.parent).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = video.stem
    acquire_out_lock(out_dir, stem)      # 成品路径也要锁：换 --cache-dir 的并发不会互相挡

    cues: list[dict] | None = None
    origin = ""
    track_lang = ""                      # 内嵌轨的语言标签：源语言三级回退的第二级
    # 是否**转写路线**（决定要不要按整句送翻译）：ASR 的 cue 里会有"半句话"，内嵌/外挂字幕是整行。
    # 必须单独记**真来源**（source_origin），不能事后看 origin 字符串 —— 复用缓存的 origin 是
    # "复用的 source.srt"，用字符串判断会让整句优化在**第二次跑时静默失效**（实测：复用那次
    # 翻译了 373 条而不是 313 个单元）。
    asr_route = False
    source_origin = ""                   # 这份 cue 文本**原本**从哪来（会写进 source.json 供下次复用判读）
    # 中间产物（.source.srt / .source.json / .asr.json / 译文缓存）默认落在**缓存目录**，
    # 不再堆在视频旁边——用户要的是"视频目录只多出一个 .ass"。
    # 同一视频的缓存放在 <缓存根>/<基名>-<路径哈希前 8 位>/ 下，换目录的同名视频不会互相串。
    cache_root = (args.cache_dir.expanduser().resolve() if args.cache_dir
                  else default_cache_root() / f"{stem}-{hashlib.sha1(str(video).encode()).hexdigest()[:8]}")
    cache_root.mkdir(parents=True, exist_ok=True)
    acquire_lock(cache_root, stem)      # 同一部片禁止并发跑（会互相覆盖缓存，实测踩过）
    # 片长与分片数尽早定下来：源字幕复用守卫、ASR 缓存守卫、转写与元数据必须用同一个值，
    # 否则"守卫说 1 段、转写按 4 段"这种错配会把缓存语义搞乱。
    _dur_video = video_duration(video)
    asr_chunks = resolve_asr_chunks(args.asr_chunk, _dur_video, args.asr_model)
    # --asr-json 是"我就要用这份转写结果"的显式指令：强制走 ASR 分支，
    # 压过缓存复用、内嵌轨与外挂字幕。否则它会被目录里的产物静默架空——
    # 实测踩过两次：一次是自家 <基名>.source.srt（绕过 looks_bilingual），
    # 一次是上一次 --no-translate 留下的单语成品。显式参数必须说了算。
    if args.asr_json is not None and args.source != "asr":
        print(f"[src] 指定 --asr-json → 强制走 ASR 分支（忽略 --source {args.source} 与目录里的中间产物）",
              flush=True)
        args.source = "asr"
    if args.asr_json is not None and args.asr_chunk:
        print("[src] 提示：--asr-json 直接用给定的转写结果，--asr-chunk 不生效", flush=True)

    # 源字幕复用：抽内嵌字幕要把整片读一遍（2.79 GB 外置机械盘实测 22 s），
    # 上次抽过且比视频新就直接用；--refresh-source 强制重抽。
    cached_source = cache_root / f"{stem}{SOURCE_SUFFIX}.srt"
    source_meta = cache_root / f"{stem}{SOURCE_SUFFIX}.json"
    if (not args.refresh_source and args.source in ("auto", "embedded")
            and cached_source.exists() and cached_source.stat().st_mtime >= video.stat().st_mtime):
        meta = None
        try:
            meta = json.loads(source_meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = None
        cached_cues = read_srt(cached_source)
        # 守卫（失效关闭）：只有"完整抽取 + 元数据可读"才复用。
        # 元数据缺失/损坏时无从判断是否被 --limit 截断 —— 宁可贵 20 秒重抽，也不能静默少出字幕
        #（历史上 6739c59 那版在 --limit 下写截断的 source.srt 且不写 .source.json）。
        if not isinstance(meta, dict):
            print(f"[src] 缺少/损坏 {source_meta.name}，为安全起见不复用", flush=True)
        elif meta.get("limited"):
            print(f"[src] 已有的 {cached_source.name} 来自 --limit 运行，不复用", flush=True)
        elif args.asr_chunk != 0 and int(meta.get("chunks") or 1) != asr_chunks:
            # 分片数不同 → 转写文本不同（实测 4 段并行能把整句语言漂移从 22 条降到 0 条）。
            # 不拦这一条的话 `--asr-chunk` 会被缓存的 source.srt 静默架空（实测踩过：E01/E02/E03
            # 三集都直接复用了 1 段时代的 source.srt，分片参数等于没生效）。
            # **只在用户显式传了 --asr-chunk 时拦**：自动默认（0）不该让既有缓存失效——
            # 实测被这一条害过：配额用尽时（403 FreeTierOnly）连"只想要字幕"的重跑都跑不起来。
            print(f"[src] 已有的 {cached_source.name} 来自 {int(meta.get('chunks') or 1)} 段转写，"
                  f"本次要 {asr_chunks} 段 → 不复用", flush=True)
        elif not cached_cues:
            print(f"[src] {cached_source.name} 为空，不复用", flush=True)
        else:
            cues = cached_cues
            origin = "复用的 source.srt（跳过抽取）"
            # 真来源要**继承**下来（旧缓存可能把"复用"自己写进了 origin，那时用 asr.json 是否存在兜底）
            recorded = str(meta.get("origin") or "")
            if recorded.startswith("复用"):
                recorded = "ASR 转写" if (cache_root / f"{stem}.asr.json").exists() else "未知来源（旧缓存）"
            source_origin = recorded
            asr_route = source_origin.startswith("ASR")
            print(f"[src] 复用 {cached_source.name}（{len(cues)} 条，比视频新，跳过整片读盘；"
                  f"原来源：{source_origin}）", flush=True)
    _stage = time.time()
    if cues is None and args.source in ("auto", "embedded"):
        tracks = embedded_tracks(video)
        text_tracks = [t for t in tracks if t["text_based"]]
        if tracks:
            print(f"[probe] 字幕轨 {len(tracks)} 条：" + "; ".join(
                f"idx={t['index']} {t['codec']}/{t['lang'] or '?'} {t['title']}" for t in tracks), flush=True)
        if text_tracks:
            try:
                cues, info = extract_embedded(
                    video, args.sub_index,
                    None if args.source_lang.strip().lower() == "auto" else args.source_lang)
                if cues:
                    origin = f"内嵌字幕轨 idx={info['index']} {info['codec']}/{info.get('language','?')}"
                    source_origin = origin
                    track_lang = info.get("language") or ""
            except SystemExit as exc:
                if args.source == "embedded":
                    raise
                print(f"[src] 内嵌字幕不可用（{exc}）→ 继续尝试其他来源", file=sys.stderr)
    if cues is None and args.source in ("auto", "sidecar"):
        found = sidecar_path(video)
        if found:
            cues = read_ass(found) if found.suffix.lower() in (".ass", ".ssa") else read_srt(found)
            if cues:
                origin = f"外挂字幕 {found.name}"
                source_origin = origin
    if cues is None and args.source == "auto":
        print("[probe] 没有可用字幕 → 走 ASR 转写", flush=True)
    if cues is None:
        if args.source in ("embedded", "sidecar"):
            raise SystemExit(f"--source {args.source} 但没找到可用字幕")
        key = api_key(args.api_key)
        asr_json = cache_root / f"{stem}.asr.json"
        asr_meta = cache_root / f"{stem}.asr.meta.json"
        with tempfile.TemporaryDirectory(prefix="omnisub-") as tmp:
            if args.asr_json:
                asr_json = args.asr_json.expanduser().resolve()
                if not asr_json.exists():
                    raise SystemExit(f"找不到 --asr-json：{asr_json}")
                print(f"[asr] 复用 {asr_json.name}", flush=True)
            elif asr_cache_ok(video, asr_json, asr_meta, args.asr_model, args.source_lang, asr_chunks):
                # 复用优先的最后一环：抽音轨+上传+转写实测 131.7s，重跑没必要再付一次
                print(f"[asr] 复用 {asr_json.name}（比视频新、同模型、时长一致 → 跳过抽音轨/上传/转写）",
                      flush=True)
            else:
                audio = audio_cache_path(cache_root, stem, args.audio_lossless)
                # 语言提示分三级：显式参数 → **音轨标签（免费）** → 180s 样本探测（要发一次请求）。
                # 探测延迟由 lang_provider 在转写线程里惰性触发，从而与抽音轨重叠（见下）。
                lang_hint = (args.source_lang
                             if args.source_lang.strip().lower() not in ("", "auto") else "")
                if not lang_hint:
                    lang_hint = audio_track_lang(video)
                    if lang_hint:
                        print(f"[asr] 音轨语言标签 → {lang_hint}（免费提示，省掉 180s 样本探测）",
                              flush=True)

                # 惰性 + **去重加锁**：这个 provider 会从每个分片的工作线程里被调用，
                # 不加锁就会并发打 1–4 次付费的样本探测、还并发写同一个 probe.json（读到半写文件
                # → JSONDecodeError 经 future.result() 冒到主线程整跑崩掉）。
                lang_lock = threading.Lock()
                lang_cache: list[str] = []

                def lang_provider() -> str:
                    with lang_lock:
                        if lang_cache:
                            return lang_cache[0]
                        _t0 = time.time()
                        got = ("" if args.no_probe
                               else probe_source_lang(video, Path(tmp), key, args.asr_model))
                        T["lang"] += time.time() - _t0
                        lang_cache.append(got)
                        return got

                duration = video_duration(video)
                merged, t_ext, t_tail = transcribe_streaming(
                    video, cache_root, stem, duration, key, args.asr_model, lang_hint,
                    asr_chunks, args.asr_workers, args.audio_lossless,
                    lang_provider=None if args.no_probe else lang_provider)
                T["audio"] += t_ext              # 抽音轨墙钟
                T["asr"] += t_tail               # 与抽音轨重叠后的尾段（两者相加=该段墙钟）
                atomic_write_text(asr_json, json.dumps(merged, ensure_ascii=False))
                # 元数据是复用守卫的依据：没有它宁可不复用（见 asr_cache_ok）
                atomic_write_text(asr_meta, json.dumps(
                    {"duration": round(duration, 3), "model": args.asr_model,
                     "lang": lang_hint, "audio": audio.suffix.lstrip("."),
                     "chunks": asr_chunks},
                    ensure_ascii=False))
                if args.keep_audio:
                    print(f"[audio] 音频片段已在缓存目录：{audio.parent}", flush=True)
        _stage = time.time()
        # 读转写文件：**损坏要明确报错，不能甩栈**（实测退化输入矩阵：截断的 / 非 JSON 的
        # --asr-json 原来会抛 JSONDecodeError 的 traceback）。三种来源都会走到这里：
        # 显式 --asr-json、缓存命中的 <基名>.asr.json、以及刚写完的那份。写到一半被打断、
        # 磁盘满、下载中断都会留下这种文件。
        try:
            asr_data = json.loads(asr_json.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SystemExit(f"[asr] 读不了转写文件 {asr_json}（{type(exc).__name__}: {exc}）")
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"[asr] 转写文件不是合法 JSON：{asr_json}\n"
                f"[asr] ({exc})\n"
                f"[asr] 常见原因：写入/下载被打断、磁盘满、或传错了文件。"
                f"确认文件完整，或删掉它并用 --refresh-source 重新转写。")
        sentences = sentences_of(asr_data)
        if not sentences:
            raise SystemExit(
                f"[asr] 转写文件里没有任何句子：{asr_json}\n"
                f"[asr] 拒绝用空转写继续（那会产出一份没有字幕的成品）。"
                f"确认文件正确，或用 --refresh-source 重新转写。")
        cues = cues_from_asr(sentences)
        T["asr"] += time.time() - _stage                # 切 cue 计入转写阶段
        origin = "ASR 转写"
        source_origin = origin
        asr_route = True

    T["probe"] = time.time() - _stage
    if args.limit and args.limit > 0:
        cues = cues[:args.limit]
        print(f"[limit] 仅处理前 {len(cues)} 条", flush=True)
    if not cues:
        raise SystemExit("没有切出任何字幕条")
    if not args.keep_overlaps:
        _ov = clamp_overlaps(cues)
        if _ov:
            print(f"[check] 钳掉 {_ov} 处相邻条重叠（时间轴以不重叠为准；--keep-overlaps 可关）", flush=True)
    check_timeline(cues, video, partial=bool(args.limit))
    print(f"[src] {origin}：{len(cues)} 条，{ts(cues[0]['begin'])} → {ts(cues[-1]['end'])}", flush=True)

    source_srt = cache_root / f"{stem}{SOURCE_SUFFIX}.srt"
    write_source_srt(cues, source_srt)
    # 元数据只留"复用守卫"真正要读的字段：cues（空则不复用）与 limited（截断产物永不复用）
    (cache_root / f"{stem}{SOURCE_SUFFIX}.json").write_text(json.dumps(
        # 写**真来源**：写"复用"会让下一次复用丢掉来源判读（实测踩过，整句优化静默失效）
        {"cues": len(cues), "limited": bool(args.limit),
         "chunks": asr_chunks if (source_origin or origin).startswith("ASR") else None,
         "origin": source_origin or origin},
        ensure_ascii=False), encoding="utf-8")

    # 同步校验：外挂/下载来的字幕可能与视频不同版本 —— 用音频对齐验一次（auto 时仅外挂字幕触发）
    if args.verify_sync == "on" or (args.verify_sync == "auto" and origin.startswith("外挂")):
        info = verify_sync(video, source_srt, cache_root,
                           force=(args.verify_sync == "on"))
        if info:
            cues = read_srt(source_srt)          # 以盘上的 source.srt 为准重建时间轴
            verdict = ("已校正（复检通过）" if info["fixed"]
                       else f"未改写：{info['rejected']}" if info.get("rejected") else "无需校正")
            print(f"[ffsync] 时间轴已核对（offset {info['offset']:+.3f}s、scale {info['scale']:.4f}"
                  f"，{verdict}，{len(cues)} 条）", flush=True)

    # ---- 语言对：出几行、行序、以及哪些行需要翻译 ----
    pair = parse_langs(args.subtitles) if args.subtitles else None
    lines = [c["text"] for c in cues]
    src_lang, src_basis = resolve_source_lang(args.source_lang, track_lang, lines)
    if pair is None:
        if args.target_lang:
            # 旧参数兼容：老语义就是"原文行 + 目标语言行"
            pair = tuple(dict.fromkeys(
                p for p in (src_lang, normalize_lang(args.target_lang)) if p))
            print(f"[lang] --target-lang 已废弃 → 按 --subtitles {','.join(pair)} 处理", flush=True)
        else:
            pair = LANGS_DEFAULT
    order = display_order(pair, src_lang)      # 行序唯一事实源：日志与写盘共用
    print(f"[lang] 源语言 {src_lang or '未知'}（{src_basis}）→ 输出 {','.join(order)}"
          f"（{len(order)} 行：译文在上、原文沉底，第一行用醒目样式）", flush=True)
    drift = source_drift(cues, src_lang)
    if drift:
        hint = (f"显式传 --source-lang {src_lang} 后重跑"
                if args.source_lang.strip().lower() in ("", "auto") else "重跑一次")
        print(f"[qa] ⚠️ 原文行疑似整句语言漂移：{len(drift)}/{len(cues)} 条"
              f"（该条原文：{cues[drift[0]]['text'][:40]!r}）\n"
              f"[qa]    这是 ASR 在长音频上的偶发行为（同一集重跑可 0 条），译文会跟着漂；"
              f"要消除就{hint}", flush=True)
    w, h = video_size(video)

    if args.no_translate:
        _stage = time.time()
        final = out_dir / f"{stem}.ass"
        # 只出原文时**只要目标上已有文件就不覆盖**，改写到 <基名>.mono.ass。
        # 旧判据是 `has_own_mark or looks_bilingual`——而 looks_bilingual 在 2026-09-27 被收紧成
        # "真·双语"（见其 docstring），若继续用它当防覆盖判据，一份**纯中文**的既有 .ass
        # （用户自己的中文字幕）就会被单语输出盖掉。防覆盖只该问一件事：**这个路径上已经有东西了吗**。
        if final.exists():
            why = "已是本技能的双语成品" if has_own_mark(final) else "已有同名文件（不是本技能产出）"
            final = out_dir / f"{stem}{MONO_SUFFIX}.ass"
            print(f"[out] {stem}.ass {why} → 本次单语输出写到 {final.name}（不覆盖）", flush=True)
        write_ass(cues, final, [[line] for line in lines], width=w, height=h)
        T["write"] = time.time() - _stage
        ok = report_qa(cues, [[line] for line in lines])
        report_timing(stem, cues, origin, write_log=not args.no_log)
        print(f"[done] {final}")
        if not ok:
            raise SystemExit(1)
        return

    key = api_key(args.api_key)
    # 翻译单元：ASR 路线的 cue 里有"半句话"（长句被切开），整句送翻译才不会被模型合并错位；
    # 内嵌/外挂字幕本来就是整行，一行一个单元，行为与以往一致（见 translation_units）。
    spans = (translation_units(cues) if asr_route
             else [(i, i + 1) for i in range(len(cues))])
    unit_lines = [" ".join(cues[j]["text"].strip() for j in range(a, b)) for a, b in spans]
    if len(unit_lines) != len(cues):
        print(f"[mt] 翻译单元 {len(unit_lines)} 个 / {len(cues)} 条 cue "
              f"（{len(cues) - len(unit_lines)} 条与相邻条同句，合并送翻译后切回）", flush=True)
    fingerprint = hashlib.sha256("\n".join(unit_lines).encode("utf-8")).hexdigest()[:16]
    _stage = time.time()
    rows_by_lang: dict[str, list[str]] = {}
    for lang in pair:
        if lang == src_lang:
            rows_by_lang[lang] = list(unit_lines)   # 源语言行直接用原文：不翻译、不花钱、无缓存
            print(f"[mt] {lang}：源语言 → 直接复用原文，不做翻译", flush=True)
            continue
        rows_by_lang[lang] = translate_for_target(
            unit_lines, key, src_lang, lang, args.chat_model,
            cache_root / f"{stem}.{lang}.json", fingerprint,
            workers=args.workers, batch_size=args.batch, timeout=args.timeout,
            backend=args.backend, local_server=args.local_server, rpm=args.rpm,
            no_cache=args.no_cache)
    T["mt"] = time.time() - _stage

    _stage = time.time()
    final = out_dir / f"{stem}.ass"

    def assemble() -> dict[str, list[str]]:
        """按单元切回每条 cue：lang → 与 cues 等长的片段数组。"""
        out_pieces: dict[str, list[str]] = {}
        for lang in order:
            arr = [""] * len(cues)
            for ui, (a, b) in enumerate(spans):
                weights = [len(cues[j]["text"]) or 1 for j in range(a, b)]
                for j, piece in zip(range(a, b), split_translation(rows_by_lang[lang][ui], weights)):
                    arr[j] = piece
            out_pieces[lang] = arr
        return out_pieces

    pieces_by_lang = assemble()
    sync = sync_faults(cues, spans, pieces_by_lang, unit_lines, src_lang)
    # 修复**迭代到收敛**（最多 SYNC_REPAIR_PASSES 轮）：单轮的修复集合只覆盖"当下被判坏"的单元，
    # 而污染区往往比它大——实测把历史污染（25 个单元）注入缓存后，第一轮只修了 18 个，
    # 剩下的仍然错位 → 复检失败 → 拒写。多轮迭代让"被邻居带偏的单元"在下一轮暴露出来。
    # 只在**有进展**（坏单元数下降）时继续，避免模型一直不收敛时白烧请求。
    for _pass in range(1, SYNC_REPAIR_PASSES + 1):
        if not (sync["suspicious"] and not args.no_repair_desync):
            break
        idx = list(sync["repair_units"])
        if not idx:
            break
        print(f"[sync] ⚠️ 疑似错位（连续不一致 {sync['run']} 条、单元收口错位 "
              f"{sync['units_bad']}/{sync['units']}、长度平移段 {sync.get('shift_windows', 0)} 段）"
              f"→ 第 {_pass}/{SYNC_REPAIR_PASSES} 轮：逐条单独重译 {len(idx)} 个单元复核", flush=True)
        before_bad = sync["units_bad"]
        _t0 = time.time()
        n_fixed = retranslate_units(idx, unit_lines, rows_by_lang, src_lang, key,
                                    args.chat_model, args.timeout, args.backend, args.local_server)
        T["mt"] += time.time() - _t0
        pieces_by_lang = assemble()
        sync = sync_faults(cues, spans, pieces_by_lang, unit_lines, src_lang)
        if n_fixed:
            for lang in order:
                if lang != src_lang:
                    dump_translation_cache(cache_root / f"{stem}.{lang}.json", unit_lines,
                                           rows_by_lang[lang], args.chat_model, src_lang, lang,
                                           args.backend, fingerprint)
            print(f"[sync] 已逐条重译 {n_fixed} 条并回写译文缓存"
                  f"（坏单元 {before_bad} → {sync['units_bad']}）", flush=True)
        if sync["units_bad"] >= before_bad:      # 没进展就不再烧请求
            break
    report_sync(sync, cues, spans)

    if not sync["ok"] and not args.allow_desync:
        # **判在写盘之前**：旧版先 write_ass 再判失败，于是一份被判失败的成品会覆盖掉上一版
        # 好的 <基名>.ass —— 下游只看"文件在不在"的话拿到的就是那份坏成品，闸门只保住了退出码。
        T["write"] = time.time() - _stage
        report_timing(stem, cues, origin, write_log=not args.no_log)
        print("[sync] ❌ 对齐闸门未通过 → **不写成品**，盘上保留上一版"
              "（确认内容可接受再显式传 --allow-desync）", file=sys.stderr)
        raise SystemExit(1)

    rows_by_cue: list[list[str]] = [[pieces_by_lang[lang][j] for lang in order] for j in range(len(cues))]
    write_ass(cues, final, rows_by_cue, width=w, height=h)
    T["write"] = time.time() - _stage

    ok = report_qa(cues, rows_by_cue)
    report_timing(stem, cues, origin, write_log=not args.no_log)
    print(f"[done] {final}")
    if not ok:
        raise SystemExit(1)


if sys.version_info < (3, 10):
    raise SystemExit(
        f"omnisub 需要 Python 3.10+（当前 {sys.version.split()[0]}）。\n"
        "macOS 自带的 /usr/bin/python3 是 3.9，缺 Path.write_text(newline=) 等 3.10 API；\n"
        "请改用 3.10+ 解释器（例如本机 uv 托管的 ~/.local/bin/python3）。")


if __name__ == "__main__":
    main()
