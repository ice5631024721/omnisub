# ASR API：qwen-audio-3.1-asr-flash-filetrans

## Key

```bash
KEY=$(grep -E "^OPENAI_API_KEY=" ~/.agentmemory/.env | cut -d= -f2)
```

## 首选：bailian CLI（接受本地文件，实测路径）

未装则先装：`export PATH="$HOME/.local/share/fnm/node-versions/v24.15.0/installation/bin:$PATH" && npm install -g bailian-cli`

```bash
export PATH="$HOME/.local/share/fnm/node-versions/v24.15.0/installation/bin:$PATH"
bl speech recognize --url /tmp/dy_audio.m4a \
  --model qwen-audio-3.1-asr-flash-filetrans --output json
```

结果在返回 JSON 的文本字段（transcript/text），失败时 JSON 含 code/message。

## 备选：原生异步 API

`filetrans` 走异步任务（需公网可访问的 file_urls；本地文件此路不通，仅当音频已有公网 URL 时用）：

```bash
curl -s -X POST https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen-audio-3.1-asr-flash-filetrans","input":{"file_urls":["<PUBLIC_URL>"]}}'
# 轮询：GET https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}
```

## 兜底：OpenAI 兼容同步端点

CLI 与异步 API 均不可用时尝试（模型名不带 filetrans）：

```bash
curl -s -X POST https://dashscope.aliyuncs.com/compatible-mode/v1/audio/transcriptions \
  -H "Authorization: Bearer $KEY" -F model=qwen3-asr-flash -F file=@/tmp/dy_audio.m4a
```

注意：此路曾对 `qwen3-asr-flash` 返回 404（模型路由可能仅开放部分端点）——失败就回到 CLI 路径，不要反复重试。

## 计费事实

- filetrans 输入 0.8 元/百万 tokens（三条 ASR lane 最便宜）
- 免费额度：filetrans 36,000 秒；qwen-audio-3.1 系列每模型另计 100 万 token（至 2026-12-21）
- 实测量级：≈0.0012–0.013 元/分钟音频

## 实测约束（2026-09-26 验证）

- `bl speech recognize` **同步模式上限 300 秒音频**，超出报 `audio duration over service process (300s)`——长音频按 ≤280 秒/片切分，各片**并行**转写后按序拼接
- **bl 输出流反直觉：转写正文在 stdout，`[Model:...]` banner 在 stderr**。收正文用 `2>/dev/null` 直接拿 stdout；`--output json` 在当前版本不可靠，按纯文本收
- 本机 `ffmpeg`/`ffprobe` 已于 2026-09-27 用 `brew reinstall ffmpeg` 修好（均 9.0.2，`/opt/homebrew/bin`）；音视频一律走它们，**不用 PyAV**（`afconvert` 读不了 MKV）

## 整片带时间戳转写（字幕用，2026-09-27 实测）

要时间轴就别走同步模式：**异步模型**（`*-filetrans` / `fun-asr` / `paraformer-*`）接受整片音频，并可用 `--out` 落 JSON。**本 skill 两条分支（抖音 / 本地视频）统一走 `qwen-audio-3.1-asr-flash-filetrans`**——2026-10-04 抖音分支由 fun-asr 换入，理由见下。

```bash
bl speech recognize --url /tmp/e02_16k.flac \
  --model qwen-audio-3.1-asr-flash-filetrans --language en \
  --out /tmp/e02_asr.json --output json --api-key "$KEY"
```

- 实测：43.8 分钟音频（16 kHz 单声道 FLAC，48 MB）**62 秒**返回，**无 300 秒限制**
- JSON 结构：`transcripts[0].sentences[]`，每句含 `begin_time`/`end_time`（毫秒）、`text`、`sentence_id`，以及**词级** `words[]`（`begin_time`/`end_time`/`text`/`punctuation`/`confidence`）——**字幕文本取句级 `text`，`words[]` 只用来定时间**：标点单独在 `punctuation` 里，词还会被拆开（`gl`+`enn`）或缺前导空格（`in`+`90`），拼词当文本必错（实测见 SKILL.md 的坑）
- 覆盖率自检：末句 `end_time` 应接近容器时长（实测 2626.8s vs 2628s），并确认相邻句之间没有 >15s 的空隙
- 解音轨/抽字幕用 `ffmpeg`：`~/.local/bin/python3 scripts/omnisub.py …`（`ffprobe` 探轨、`ffmpeg -map 0:<idx> -c:s srt` 抽字幕、`-vn -ac 1 -ar 16000 -c:a flac` 抽音轨）

## 翻译：bl text chat（字幕双语化用）

```bash
bl text chat --model qwen-mt-flash --messages-file /tmp/msg.json \
  --api-key "$KEY" --output json --quiet
```

- `--messages-file` 收 JSON messages 数组（`-` 可走 stdin）；系统提示要求"逐行翻译、顺序与条数同输入、只输出 JSON 数组"
- **输出形态不固定**：实测直接返回模型正文的 JSON 数组（`["译文1","译文2"]`），也可能包成 `{choices:[{message:{content}}]}`——解析要三种都吃（见 `scripts/omnisub.py` 的 `translate()`）
- **批大小 20 最稳**：实测 40 条/批会让模型把短句并进相邻行、条数不符触发大量二分（400 条跑了 142 秒、49 次二分），20 条/批同样内容只要 32 秒。
- 协议用**编号标记**（每行前缀 `[[n]]`，模型拆句也能按标记归位）；条数不符**递归二分**、漏条**成批补译**，绝不「末尾补空」（那会让整批译文错位）。
- 配 4 并发 + **50 次/分钟限速器**（官方限额 60 次/分钟 + 3.5 万 token/分钟；逐行翻译必被 429，退避比限速更慢）；**务必带 `--timeout 180`**。

## 模型选型与费用（2026-09-27 实测，价格取自百炼模型目录）

### 纯文本翻译（字幕翻译用）

| 模型 | 输入 | 输出 | 30 条实测墙钟 | 上下文 | 限流（北京） | 备注 |
|---|---|---|---|---|---|---|
| **qwen3.7-flash**（默认） | 0.2 元/百万 token | 0.4 | **7.5 s**（关思考） | — | 未压测（限速器仍按 50 RPM） | 通用指令模型；**必须直连 HTTP 显式 `enable_thinking:false`**（见下）；token-plan 无此模型（404 实测），走 dashscope 按量流量，key 仍是 `~/.agentmemory/.env` 那把 |
| **qwen-mt-flash**（可选） | 0.7 元/百万 token | 1.95 | **2.69 s** | 输入/输出各 8192，ctx 16384 | RPM 60 / **TPM 35,000** | MT 专用模型；对"透明型习语"字面直译且风格指令无效（见下），`--chat-model qwen-mt-flash` 仍可用 |
| qwen-mt-lite | 0.6 | 1.6 | 1 s | 同左 | 同 flash 量级 | 输出会套 ```json 围栏、说话人标签保留英文 |
| qwen-mt-plus | 1.8 | 5.4 | **3.92 s** | **同 flash（无差别）** | RPM 60 / **TPM 25,000** | 官方定位"旗舰级"，flash 是"轻量级" |
| qwen-mt-uni | 文本 65 / 文档 20 / 图片 32 / 音频 400 元/百万 | 同左 | — | — | — | 多模态统一翻译 |
| ~~qwen-mt-turbo~~ | 0.7 | 1.95 | 1 s | — | — | **2026-10-10 下线** |

**为什么默认换 qwen3.7-flash（2026-09-28 定版，用户确认；qwen3-max 本项目禁用）**：

- **根因（用户报"翻译不地道"的实测归因）**：`if it's not one thing, it's another` 这类**透明型习语**，
  qwen-mt-flash 译成「要不是一件事，就是另一件事」（同义反复的直译）；且三条补救路全部实测无效——
  提示词加风格指令（输出与不加几乎一字不差）、原生 `translation_options.domains`（不改变直译）、
  升 qwen-mt-plus（照样直译）。这是 MT 专用模型的形式对等偏好，**不是提示词能修的**。
- **qwen3.7-flash + 风格指令**（习语按意译、禁止逐字直译）实测意译：「麻烦事一桩接一桩，没完没了」；
  30 条整批 30/30 标记协议服从、7.5 s/批；全片 862 条实测翻译 68.3 s（≈0.01 元/集，比 qwen-mt-flash 还便宜）。
- **代价与坑**：qwen3 系**服务端默认开思考**——实测同一句翻译，不发 `enable_thinking` 字段 = 39.4 s / 2353 输出
  tokens（含 6877 字思考），显式 False = 0.6 s / 10 tokens（**66× 时延差**）。`bl text chat` 只有
  `--enable-thinking`（开启用）、请求体根本不带该字段（`--dry-run` 实证）→ **qwen3 系必须直连
  dashscope compatible-mode HTTP**（`_http_chat`，scripts/omnisub.py）。另一坑：qwen3.7-flash
  **不在 token-plan**（404 Model not exist），不要拿 token-plan 的 key 调它。

**plus vs flash 到底差在哪（2026-09-27 实测，30 条真实纪录片旁白，走生产 `translate()` 路径，qwen3-max 盲评 + 位置互换双评）**：

- **质量：分不出胜负。** 第 1 轮 plus 12 / flash 11 / 平 7，第 2 轮 plus 13 / flash 10 / 平 7；**两轮一致的稳胜只有 plus 6 vs flash 4**，且两轮一致率仅 57%（裁判自身噪声 > 两者差距）。30 条样本量下无统计意义。
- **但 plus 有两处真优势**（出现在稳胜判例里）：① **动物/指代一致性**——`they use my tent as a little poop spot`，flash 写"他**们**"（错，指动物），plus 写"它**们**"；② **陌生词/文化词**——`personia trees too successful`，flash 直译"过于成功了"，plus 译"反而过于繁盛"。
- **吞吐：plus 两头都更差。** 墙钟慢 **1.46×**（3.92s vs 2.69s / 30 条），且**北京区 TPM 只有 25,000（flash 35,000，低 29%）**，RPM 都是 60。批量越大、文本越长，plus 的配额天花板越早撞。
- **上下文与能力完全相同**（最大输入/输出 8192、ctx 16384；都不支持结构化输出 / FC / 批量推理 / 缓存）→ **换 plus 不会让你能塞更大的批**。
- **成本**：一集 765 条 ≈ flash **0.028 元** vs plus **0.076 元**（2.7×）；100 集 2.8 元 vs 7.6 元。
- ⚠️ **区域坑**：新加坡区 plus 是 **18.055 / 54.09 元/百万**（北京价的 **10×**），而 flash 新加坡只 1.68×（1.174 / 3.596）。走国际端点时 plus 的性价比断崖式下跌。

**结论：默认继续用 flash；把 plus 当"精修第二遍"而不是"全量替换"**——只送 QC 没过的那几条（指代、专名、文化词），成本与配额都只花在该花的地方。

**一集 43.8 分钟剧集（765 条字幕）≈ 0.03 元**（输入约 1.2 万 token、输出约 1 万 token）。

### 语音类

| 模型 | 能力 | 价格 | 用法 |
|---|---|---|---|
| **qwen-audio-3.1-asr-flash-filetrans**（默认 ASR） | 识别，**带句级+词级时间戳** | 0.8 / 2.7 元/百万 token | 整片异步，实测 43.8 分钟音频 62 秒返回；一集 ≈0.08 元 |
| qwen-audio-3.0-asr | 识别 | 0.00022 元/秒 | 无时间戳需求时更省 |
| ~~fun-asr~~（2026-10-04 起本 skill 不再用） | 识别，热词 | 0.00022 元/秒 | 与 filetrans 同为异步任务档，时间戳/说话人分离/敏感词过滤能力相同；**贵约 7.8 倍**（见下） |
| qwen3.8-omni-flash | 全模态理解＋**可直接英音→中文** | 0.8 / 2.7 元/百万 token | `bl omni --audio x.wav --text-only --message "翻译成中文"`；**无时间戳，不能做字幕轴** |
| ~~gummy-chat-v1 / gummy-realtime-v1~~ | 语音识别及翻译 | 0.00015 元/秒 | **2026-10-10 下线** |
| qwen3-livetranslate-flash 系列 | 直播/实时翻译 | 音频 10～40 元/百万 token | 实时/流式接口，`bl speech recognize` 调不通 |

#### fun-asr vs filetrans 为什么弃用前者（2026-10-04 同音频 A/B 实测）

同一段 6.92s MP3 32k，两模型走同一条 `bl speech recognize` 命令：

| | usage | 实付 | 耗时 | 识别文本 |
|---|---|---|---|---|
| `fun-asr` | `{}`（按秒计费，不回 token） | 6.9247s × 0.00022 = **0.001523 元** | 3.4s | 一致 |
| `qwen-audio-3.1-asr-flash-filetrans` | `input 188 / output 17` token | 188/1e6×0.8 + 17/1e6×2.7 = **0.000196 元** | 3.1s | 一致 |

≈ **7.8 倍**。附带发现：**本模型 WAV 也能过**（fun-asr 时代"WAV 必 SERVER_ERROR"的约束不适用于它），但抖音分支仍固定提 MP3 32k——覆盖守卫靠 `文件大小 × 8 ÷ 32` 反推时长，换 WAV 会把守卫算废。

未验证项：以上文本一致性是**干净 TTS 语音**上的结果，**不等于**抖音实拍（噪声/BGM/口播）的准确率一致；fun-asr 官方主打噪声鲁棒性，若日后抖音转写质量下降，这是第一个该回滚的点。

### 怎么查某个模型是否要下线

```bash
bl model list --model <model-id> --output json | python3 -c "import json,sys;m=(json.load(sys.stdin).get('items') or [{}])[0];print(m.get('upcomingOfflineAt') or '在售', m.get('announceUrl',''))"
```
目录里带 `--include-deprecated` 可看已下线模型。

### 已知下线批次（2026-07-10 公告，2026-10-10 生效）

`qwen-mt-turbo`、`gummy-chat-v1`、`gummy-realtime-v1` → 替代：文本用 **qwen-mt-flash**，语音直出用 **qwen3.8-omni-flash**。
（公告页 https://www.aliyun.com/notice/118434 正文为 JS 渲染，清单以上面的目录字段为准。）

### 本地翻译后端（离线/隐私场景，可选）

```bash
# llama.cpp（官方 GGUF 量化版，Apple Metal）
llama-server -m <自备的 Hy-MT2-7B GGUF> --port 8080 -ngl 99   # 模型需自行下载（本机评测后已删除）
python scripts/omnisub.py <video> --backend local --local-server http://127.0.0.1:8080
```
本地用 Hy-MT2 官方的「分隔符」提示模板（`|||` 分段），脚本按分隔符切回。实测 Apple M4：Q4_K_M 19.95 tok/s、4.06 GB；MLX 8bit 12.0 tok/s、8.26 GB；质量与云端基本持平（中立裁判 6:6/6:7），但慢 8–25 倍。
