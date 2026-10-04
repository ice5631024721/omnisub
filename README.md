# omnisub

视频 / 图片 → 文字与字幕。一个面向 AI Agent 的 **Agent Skill**（DSH / Claude Skill 格式）。

| 输入 | 处理路径 |
| --- | --- |
| 抖音链接 · 有声视频 | 分享页取无水印直链 → 下载 → 转 16k 单声道 → **分段并行 ASR** |
| 抖音链接 · 单图 / 轮播图 | 逐张下载 → 视觉读图，**图中文字一并转出** |
| **本地视频（MKV / MP4…）** | **复用优先**出**双语 ASS 字幕**：内嵌字幕轨 → 同目录外挂字幕 → 都没有才 ASR |

## 本地视频 → 任意语言对的双语 ASS

- **任意源语言 → 任意语言对**（`--subtitles`，默认 `en,zh`）。
- **行序不变量：译文在上（醒目）、原文沉底** —— 中文片出英上中下，英文片出中上英下。
  在上的行用 `Upper` 样式，在下的用 `Lower`；源语言若在语言对里，该行直接复用原文，不翻译、不花钱。
- **能拿到现成字幕就绝不转写**：人工字幕的时间轴比 ASR 更准，且省掉整片转写成本。
- **只出 ASS**：SRT 没有任何样式位，放不下"上面那行更醒目"这类要求（见下）。
- **片库里只多出一个文件**，中间产物一律进缓存目录。

| 源语言 | `--subtitles en,zh`（默认）的实际行为 |
| --- | --- |
| 英文 | 上行 = 中文译文（大而粗），下行 = 英文原文 |
| 中文 | 上行 = 英文译文（大而粗），下行 = 中文原文 |
| 日 / 韩 / 俄 / 阿 / 泰… | 两种目标语言都真翻译（原文不显示） |
| 法 / 德 / 西…（拉丁字母语系） | 自动模式分不出具体语种时记作"未知"并两种都翻；要准确请显式传 `--source-lang fr` |

源语言判定：有独占文字区的语言（日 / 韩 / 俄 / 阿 / 泰 / 汉字…）由 Unicode 区块直接判定；
拉丁语系之间区块分不出来，自动模式用英文虚词密度筛一轮，明显不像英文就记作"未知"
（宁可多翻一种，也不把法文原文填进英文行）。繁体中文是独立目标：`zh-TW` / `zh-Hant` → `繁體中文`。

### 原文来源优先级

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 内嵌字幕轨 | 容器里的 srt / ass / 文本轨，直接复用原时间轴 |
| 2 | 同目录外挂字幕 | `<视频基名>.srt` / `.ass` / `.vtt`；`auto` 时用 ffsubsync 校验时间轴，带两层守卫：缩放不像帧率比就拒绝改写，真要改写则对校正后的结果复检、不通过即回滚 |
| 3 | ASR 转写 | 前两者都没有才走。语言提示三级取：显式 `--source-lang` → 音轨 `language` 标签（免费）→ 180 s 样本探测；抽成 16 kHz 单声道 MP3 后**按片长自动切 4 段并行、边抽边转**；`bl speech recognize` 异步 filetrans 返回句级与词级时间戳 |

`--asr-chunk` 可调分片数（`0` = 自动）。分片不是越多越快：请求数增加会摊薄收益，且跨片上下文变短会
影响边界处质量。`--asr-chunk 8` 与自动值做过对照，默认值更快也更好，保持默认即可。

### 翻译

拿到原文后批量**并行**翻译，30 条/批 × 4 并发，走编号标记协议。默认模型 `qwen3.7-flash`
（直连 dashscope HTTP 并显式 `enable_thinking:false`——Qwen3 服务端默认开思考、bl 传不了关闭字段，
实测漏发慢 66 倍）。`qwen-mt` 系已于 2026-10-04 撤出可选项，`--chat-model qwen-mt-*` 会被直接拒绝。
提示词带风格指令（习语意译、禁止逐字直译）：qwen-mt 对 "if it's not one thing, it's another" 这类
透明型习语只会直译（「要不是一件事，就是另一件事」），qwen3.7-flash 意译（「麻烦事一桩接一桩」）。

关键设计是**按整句翻译再切回 cue**：ASR 切出的"半句话"直接送 MT，模型会把相邻两句合并、并按顺序
重新编号，合并点之后整段错位。先合并成完整句子再翻，模型就没有可合并的对象；译文再按各 cue 的
源文长度切回。**翻译方向由语言对决定并写进请求体**，不写死在提示词里。

## 安装

```bash
cp -r omnisub ~/.dsh/skills/          # 或放到你的 skills 目录
~/.dsh/skills/omnisub/omnisub --doctor   # 环境自检：真执行 ffmpeg/bl + 查 key + 实测两个模型各一次
```

抖音分支需要登录态 cookie（分享页 SSR 才内嵌视频数据）。从浏览器 DevTools 复制请求的 `-b` 串，
写入单行 `k=v; k=v`：

```bash
printf '%s' 'ttwid=...; passport_csrf_token=...' > ~/.dsh/douyin-cookies.txt
chmod 600 ~/.dsh/douyin-cookies.txt
```

## 用法

```bash
omnisub "<视频>" --out <输出目录> \
  [--source auto|embedded|sidecar|asr] [--source-lang auto|en|zh|ja|ko|fr|…] \
  [--subtitles en,zh] [--sub-index N] [--chat-model qwen3.7-flash] [--backend cloud|local] \
  [--asr-chunk 0|1|4|N] [--asr-workers 4] [--asr-json <已有.json>] \
  [--refresh-source] [--verify-sync auto|on|off] [--limit N] [--audio-lossless] \
  [--allow-desync] [--no-repair-desync] [--keep-overlaps] [--cache-dir <目录>] [--no-log]

omnisub --doctor     # 环境自检
omnisub --install    # 装/更新到 ~/.dsh/skills/omnisub（整树同步，打印代码指纹）
```

`omnisub` 是**启动器**：它按序找一个 3.10+ 的解释器再 exec 真脚本（`scripts/omnisub.py` 只用标准库）。
要直接调脚本，用 `python3 scripts/omnisub.py`，把 `PYTHONPATH` 留给系统默认即可。

**产出**：视频同目录的 `<视频基名>.ass`。

**中间产物**（原文 `.source.srt`、元数据、转写 `.asr.json`、按语言分的译文 `.json`）写进平台缓存目录
（macOS `~/Library/Caches/omnisub/`，Linux `$XDG_CACHE_HOME/omnisub/`，Windows `%LOCALAPPDATA%\omnisub\Cache`），
`--cache-dir` 可改。既保住"重切不重付"，又不往片库里堆文件；第二次跑同一部片子会复用转写与译文。

## 为什么是 ASS

SRT 只有序号、时间轴和纯文本，没有样式位——字号、颜色、加粗、描边都无处安放。往 SRT 里塞
`<font color>` 是播放器私有行为，支持参差不齐，不能当交付标准。所以本工具只出 ASS/SSA。

| 样式 | 用在哪 | 主色 | 字号 | 字重 | 描边 |
| --- | --- | --- | --- | --- | --- |
| `Upper` | 译文行（在上） | 纯白 `#FFFFFF` | 6.5% 画面高 | 加粗 | 3/1080 |
| `Lower` | 原文行（在下） | 暖白 `#F0EDE6` | 4.0% 画面高 | 常规 | 2/1080 |

两行写进**同一个 Dialogue 事件**、用 `\N` 换行并按行切样式，所以它们天然是一个整体：
居中堆叠与底部定位交给 libass，换字号或换分辨率都不会散。字号按画面高度等比缩放，1080p / 2160p 都合身。
`--subtitles` 支持 N 种语言：首行 `Upper`，其余全部 `Lower`。

**交付约定**：与视频**同名同目录**是跨平台通用的自动加载约定，主流桌面播放器（mpv / IINA / VLC /
MPC-HC / PotPlayer / Infuse）零配置即可加载。不去写任何播放器的私有目录——播放器不止一种，
替用户选播放器是越界。媒体库（Plex / Jellyfin / Emby）对外挂 ASS 的支持度不一，需要时自行转 SRT
（会丢样式）或封装进容器。

## 环境要求与平台支持

- `python3` 3.10+（脚本只用标准库）
- **`ffmpeg` / `ffprobe`**：探轨、抽内嵌字幕、抽音轨。按 PATH 优先解析，再按平台兜底
  （macOS `/opt/homebrew/bin`、Linux `/usr/bin`、Windows `C:\ffmpeg\bin`），不写死单一路径。
- **`bl`（bailian CLI）** 与一个可用的 ASR / 文本模型额度，配置见 [`ASR-API.md`](ASR-API.md)。

| 平台 | 本地视频 → 字幕 | 抖音链接分支 |
| --- | --- | --- |
| macOS | ✅ 实测（中 / 英双向，含整集长片） | ✅ 实测（依赖系统原生 `afconvert`） |
| Linux | ✅ 同一 ffmpeg 链路，脚本无 macOS 专属调用 | ⚠️ 需把 `afconvert` 换成 `ffmpeg`，未实测 |
| Windows | ⚠️ 工具发现与 `.cmd` 调用按官方约定写好，未实测 | ⚠️ 同上，未实测 |

> 只承诺实测过的组合。任何平台排查的第一步都是确认 `ffprobe` / `bl` **能被直接执行**（而不是只 `which` 到）。

> `bl` 是 npm shim（`#!/usr/bin/env node`）。PATH 里没有 node 时它会**静默空返回**（exit 127），
> 表现为"翻译批次全部返回 0 条却无报错"。脚本内置 `bl_env()` 会把 `bl` 所在目录前置进子进程 PATH；
> 自行调用 `bl` 时请一并处理。

## 内置闸门与自检

交付路径里有一道**零成本对齐闸门**：译文与原文逐条对位（句末标点不变量 + 长度自洽性），
检出疑似整体错位就自动对那批单元逐条重译并复检；复核不过则**不写盘**、以非零退出码结束，
盘上保留上一版。`--allow-desync` 可在确认内容可接受后强制交付（日志仍会如实报告错位）。

抓不住的那一类（"标点模式恰好一致"的内容错位）由**地面真值审计工具**兜：

```bash
python3 tools/deep-align-check.py "<视频或 .ass>"     # 4 个连续窗口 × 10 条
```

它一条 cue 一次请求（单条不可能被合并或重编号）、取**连续**窗口（等距抽样会把连续错位打散），
每条与"同条 / 上一条 / 下一条"三种假设比较。

仓库还带两条**不依赖 agent 的确定性闸门**（零计费，走产出路径、无法绕开）：

```bash
PYTHON=/abs/path/python3 bash evals/fixtures/scripts/selftest-omnisub.sh   # 端到端可观测的契约
PYTHON=/abs/path/python3 evals/fixtures/scripts/selftest-langs.py          # 端到端看不出来的
```

前者覆盖显式参数权威性、单条 cue、片库整洁、源语言判定、退化输入不甩栈、三种真实外挂字幕格式、
`--doctor` 不许假绿、`--install` 整树同步；后者覆盖翻译方向真的进了请求体（旧版写死在提示词里，
产出"中文重复两遍"的假双语而退出码仍是 0）、漏译判据的方向性，以及 ASS 样式字段。
评测套件（skill-up，全部离线）见 [`evals/README.md`](evals/README.md)。

## 安全与隐私

| 做法 | 原因 |
| --- | --- |
| cookie 不落仓库，只从 `~/.dsh/douyin-cookies.txt`（`chmod 600`）读取，且必须由用户手动提供 | 会话 cookie 等同账号凭据 |
| API key 不落任何文件，从既有环境变量或用户自己的配置文件读取 | 避免在 skill 里新增一份密钥副本 |
| **不出货任何字幕 / 转写正文**：仓库只有工具与文档 | 影视与他人作品的字幕属于受版权保护的内容 |
| 评测夹具全部合成（占位作者、`example.invalid` 直链） | 真实分享页含第三方 PII，不适合公开 |
| `.gitignore` 兜底忽略 cookie / 日志 / 字幕与转写产物 / 评测 workspace | 防止运行一次就把凭据或他人内容提交上去 |

两点使用者自查：① 抖音分支下载的媒体落在 `/tmp`，属于临时目录但不自动清理（步骤开头会清场，
若你的机器有同名前缀程序请改路径）；② 工具会把提取到的正文原样返回，**不做版权判断**，
处理他人作品时请自行确认使用范围。

## 已知边界

- **拉丁语系之间的自动源语言判定**只能给出"未知"，需要准确语种请显式传 `--source-lang`。
- **对齐闸门的长度信号有盲区**：源文长度均匀的区段、或整体错位占比过半时，它是失明的——
  这是"事后检测"的固有上限，主要防线是写入缓存前的入批校验；要 100% 确认请跑地面真值审计。
- **一句话跨两条 cue** 时，若译文语序与源文不同（例如中文把 "won't mate" 放到句尾），
  任何连续切分都无法同时对上两条源文；这是跨语言语序差异，不是缺陷。
- 抖音分支依赖登录态 cookie 与平台风控，属于**人工冒烟**范围，不适合当 CI 门禁。

## 目录结构

```
SKILL.md                 # Skill 本体：triage 规则、三条分支、输出模板、计时约定
ASR-API.md               # ASR 与翻译通道配置
COMMERCIAL.md            # 双许可说明
LICENSE                  # AGPL-3.0 原文
omnisub                  # 启动器（自动找 3.10+ 解释器）
scripts/omnisub.py       # 本地视频 → 双语 ASS
tools/deep-align-check.py# 地面真值对齐审计
evals/                   # skill-up 评测套件 + 离线确定性闸门 + 合成夹具
```

## License

**双许可**：开源使用 [AGPL-3.0](LICENSE)（免费；分发或提供网络服务时须同样开源）；
**商业使用需购买授权**（闭源集成、SaaS 运营、企业内部商业流程等）。详见 [`COMMERCIAL.md`](COMMERCIAL.md)。