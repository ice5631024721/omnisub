#!/usr/bin/env bash
# omnisub.py 的确定性自检（零 LLM、零计费；除场景 11 外零网络——场景 11 故意用一把坏 key
# 调一次模型，只为证明 --doctor 不会假绿，调用必然失败、不产生费用）。
#
# 为什么单独存在：skill-up 的用例是"agent 级"契约测试，agent 可能绕开被测路径
# （实测旧代码下 agent 自己补 --source asr、或把陈旧文件挪走，就能拿到正确结果），
# 也可能超时。这几条实测缺陷必须有一条**走产出路径、无法绕开**的闸门来守。
#
# 覆盖：显式参数的权威性（--refresh-source/--asr-json 不被自家产物架空）、单条 cue 不崩、
#       片库只多出一个 .ass、源语言判定与语言对解析、单语输出不覆盖双语成品；场景 8/9/10/11 覆盖
#       注入污染的拒写、退化输入不甩栈、外挂字幕三种真实格式、--doctor 不许假绿。
# 翻译方向与 ASS 样式由同目录的 selftest-langs.py 覆盖（那部分必须直接查请求体与样式字段：
# 端到端跑一遍看不出来 —— 旧版把方向写死在提示词里，退出码照样是 0、日志照样正常）。
#
# 用法：
#   bash evals/fixtures/scripts/selftest-omnisub.sh [被测脚本路径]
#   PYTHON=/abs/path/python3 bash evals/fixtures/scripts/selftest-omnisub.sh
# 退出码 0 = 全部通过；非 0 = 有用例失败（会打印 ❌ 行）。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
SCRIPT=${1:-$ROOT/scripts/omnisub.py}
PY=${PYTHON:-python3}
MKV=$ROOT/evals/fixtures/repos/video-guard/sample.mkv
MKV_EMB=$ROOT/evals/fixtures/repos/video-embedded/sample.mkv

[ -f "$SCRIPT" ] || { echo "找不到被测脚本：$SCRIPT"; exit 2; }
[ -f "$MKV" ] || { echo "找不到夹具视频：$MKV"; exit 2; }

# 解释器版本闸门：本机默认 python3 是 3.9（/usr/bin/python3），缺 Path.write_text(newline=)
# 等 3.10 API，会产出一片"假红"——真故障会被当成环境噪声。宁可在这里明确报错。
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "需要 Python 3.10+，当前 $PY 是 $("$PY" -V 2>&1)。"
  echo "请用 PYTHON=/abs/path/python3.12 bash $0 指定 3.10+ 解释器。"
  exit 2
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0

mkws() {   # $1=目录：视频 + 陈旧的自家产物 + 合成 ASR 结果
  local d=$1
  rm -rf "$d"; mkdir -p "$d"
  cp "$MKV" "$d/sample.mkv"
  cat > "$d/sample.source.srt" <<'SRT'
1
00:00:00,500 --> 00:00:02,000
STALE_SOURCE_MARKER must not survive a refresh.

2
00:00:03,000 --> 00:00:04,600
STALE_SOURCE_MARKER second stale line.
SRT
  echo '{"cues": 2, "limited": false, "origin": "内嵌字幕轨 idx=2 srt/eng"}' > "$d/sample.source.json"
  cat > "$d/sample.asr.json" <<'JSON'
{"transcripts": [{"channel_id": 0, "content_duration_in_milliseconds": 7000, "sentences": [
 {"begin_time": 500, "end_time": 2000, "sentence_id": 1, "text": "FROM_ASR_JSON_ALPHA the quick brown fox."},
 {"begin_time": 3000, "end_time": 4600, "sentence_id": 2, "text": "FROM_ASR_JSON_BETA jumps over the lazy dog."}]}]}
JSON
  touch "$d"/sample.*
}

ok()  { pass=$((pass+1)); echo "  ✅ $1"; }
bad() { fail=$((fail+1)); echo "  ❌ $1"; }

check_guard() {   # $1=目录 $2=场景名：成品必须来自 ASR JSON，中间产物必须落在缓存目录
  local d=$1 tag=$2 origin
  grep -q FROM_ASR_JSON_ALPHA "$d/sample.ass" 2>/dev/null && ok "\${tag}：成品来自 ASR JSON" || bad "\${tag}：成品不是 ASR JSON"
  grep -q STALE_SOURCE_MARKER "$d/sample.ass" 2>/dev/null && bad "\${tag}：被自家 .source.srt 劫持（STALE 进了成品）" || ok "\${tag}：未被自家产物劫持"
  origin=$(cd "$d" && $PY -c 'import json;print(json.load(open(".cache/sample.source.json")).get("origin",""))' 2>/dev/null)
  case "$origin" in
    "ASR 转写"*) ok "\${tag}：缓存里的 origin=$origin" ;;
    *)           bad "\${tag}：缓存里的 origin=\${origin}（期望 ASR 转写；没落到 --cache-dir 也算失败）" ;;
  esac
}

echo "== 场景 1：--refresh-source --asr-json --no-translate --no-log --cache-dir .cache =="
mkws "$TMP/s1"
(cd "$TMP/s1" && $PY "$SCRIPT" sample.mkv --refresh-source --asr-json sample.asr.json --no-translate --no-log --cache-dir .cache >/dev/null 2>&1) \
  && ok "场景 1：退出码 0" || bad "场景 1：非 0 退出"
check_guard "$TMP/s1" "场景 1"

echo "== 场景 2：同目录重跑，只给 --asr-json（不加 --refresh-source）—— 显式参数必须仍然说了算 =="
(cd "$TMP/s1" && $PY "$SCRIPT" sample.mkv --asr-json sample.asr.json --no-translate --no-log --cache-dir .cache >/dev/null 2>&1) \
  && ok "场景 2：退出码 0" || bad "场景 2：非 0 退出"
check_guard "$TMP/s1" "场景 2"

echo "== 场景 3：只有 1 条 cue —— 对齐自检不得崩（旧版 max() on empty → ValueError）=="
mkws "$TMP/s3"
cat > "$TMP/s3/one.json" <<'JSON'
{"transcripts": [{"content_duration_in_milliseconds": 7000, "sentences": [
 {"begin_time": 500, "end_time": 2000, "sentence_id": 1, "text": "SINGLE_CUE_ONLY."}]}]}
JSON
out=$(cd "$TMP/s3" && $PY "$SCRIPT" sample.mkv --refresh-source --asr-json one.json --no-translate --no-log --cache-dir .cache 2>&1)
rc=$?
if [ "$rc" -eq 0 ] && ! grep -q Traceback <<<"$out"; then ok "场景 3：单条 cue 正常退出"; else bad "场景 3：崩溃或被 Traceback 打断（exit=\${rc}）"; fi
grep -q SINGLE_CUE_ONLY "$TMP/s3/sample.ass" 2>/dev/null && ok "场景 3：产出 1 条字幕" || bad "场景 3：没产出字幕"

echo "== 场景 4：默认缓存时，视频目录只许多出一个 .ass（中间产物不许留在片库）=="
d=$TMP/s4; rm -rf "$d"; mkdir -p "$d/home" "$d/video"; cp "$MKV_EMB" "$d/video/sample.mkv"
(cd "$d/video" && HOME="$d/home" $PY "$SCRIPT" sample.mkv --no-translate --no-log >/dev/null 2>&1) \
  && ok "场景 4：退出码 0" || bad "场景 4：非 0 退出"
leaked=$(cd "$d/video" && ls -A | grep -v -E '^(sample\.mkv|sample\.ass)$' || true)
[ -z "$leaked" ] && ok "场景 4：视频目录只有 原视频 + sample.ass" || bad "场景 4：视频目录多了：$(echo "$leaked" | tr '\n' ' ')"
cached=$(cd "$d/home" && find . -name "sample.source.srt" | head -1)
[ -n "$cached" ] && ok "场景 4：中间产物落在默认缓存目录（\${cached}）" || bad "场景 4：默认缓存目录里没有中间产物"

echo "== 场景 5：源语言判定与语言对（任意语言的入口）=="
d=$TMP/s5; rm -rf "$d"; mkdir -p "$d"; cp "$MKV" "$d/sample.mkv"
cat > "$d/cn.json" <<'JSON'
{"transcripts": [{"content_duration_in_milliseconds": 7000, "sentences": [
 {"begin_time": 500, "end_time": 2000, "sentence_id": 1, "text": "就算是下地狱，上司也会比我们更深一层。"},
 {"begin_time": 3000, "end_time": 4600, "sentence_id": 2, "text": "有他们在前面，会不会走得快一点？"}]}]}
JSON
out=$(cd "$d" && $PY "$SCRIPT" sample.mkv --refresh-source --asr-json cn.json --no-translate --no-log --cache-dir .cache 2>&1)
grep -q "源语言 zh" <<<"$out" && ok "场景 5：中文转写被判定为 zh 源（文本启发式）" \
  || bad "场景 5：源语言判定不是 zh —— $(grep -o '\[lang\].*' <<<"$out" | head -1)"
grep -q "输出 en,zh" <<<"$out" && ok "场景 5：默认语言对是 en,zh（英上中下）" \
  || bad "场景 5：默认语言对不是 en,zh —— $(grep -o '\[lang\].*' <<<"$out" | head -1)"
out=$(cd "$d" && $PY "$SCRIPT" sample.mkv --asr-json cn.json --no-translate --no-log --cache-dir .cache --subtitles zh,ja 2>&1)
# 中文源 + 语言对 zh,ja：zh 是原文 → 沉底；ja 是译文 → 在上。行序不变量的直接断言。
grep -q "输出 ja,zh" <<<"$out" && ok "场景 5：--subtitles zh,ja 生效，且原文(zh)沉底、译文(ja)在上" \
  || bad "场景 5：--subtitles/行序不变量没生效 —— $(grep -o '\[lang\].*' <<<"$out" | head -1)"
out=$(cd "$d" && $PY "$SCRIPT" sample.mkv --asr-json cn.json --no-translate --no-log --cache-dir .cache --subtitles en,en 2>&1)
grep -q "重复语言" <<<"$out" && ok "场景 5：--subtitles 里的重复语言被拒绝" \
  || bad "场景 5：重复语言没被拒绝"

echo "== 场景 6：已有双语成品时，--no-translate 不覆盖（改写 .mono.ass）=="
d=$TMP/s6; rm -rf "$d"; mkdir -p "$d"; cp "$MKV" "$d/sample.mkv"
cat > "$d/sample.ass" <<'ASS'
[Script Info]
; Generated by omnisub
Title: omnisub
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Upper,PingFang SC,54,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,40,40,32,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.50,0:00:02.00,Upper,,0,0,0,,{\rUpper}KEEP_ME
ASS
(cd "$d" && $PY "$SCRIPT" sample.mkv --refresh-source --asr-json "$TMP/s3/one.json" --no-translate --no-log --cache-dir .cache >/dev/null 2>&1)
grep -q KEEP_ME "$d/sample.ass" 2>/dev/null && ok "场景 6：双语成品未被单语输出覆盖" || bad "场景 6：双语成品被覆盖了"
[ -f "$d/sample.mono.ass" ] && ok "场景 6：单语输出写到 sample.mono.ass" || bad "场景 6：没有产出 .mono.ass"

echo '== 场景 8：注入"平移污染"的译文缓存 → 闸门必须抓住且**拒写**（对照：对齐缓存必须通过）=='
d=$TMP/s8; rm -rf "$d"; mkdir -p "$d"; cp "$MKV" "$d/sample.mkv"
$PY - "$d" "$SCRIPT" <<'PYFIX'
# 造夹具：40 句长度参差的转写 + 一份"对齐"译文缓存 + 一份"20 个单元各装下一条译文"的污染缓存。
# 必须离线：真实修复路径会发 MT 请求，所以这一场用 --no-repair-desync，只验"抓 + 拒写"。
import hashlib, importlib.util, json, sys
from pathlib import Path
d, script = Path(sys.argv[1]), Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("omnisub_under_test", script)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
sents, t = [], 500
for i in range(40):
    words = 3 + (i * 7) % 14                      # 3-16 词：长度参差是长度判据能做统计的前提
    # 间隔 2 s / 时长 0.8 s：间隔大于 cue 合并阈值（700ms），否则相邻句会被并成一条 cue，
    # 单元数直接腰斩（实测 40 句 → 20 条），"局部平移"就没法与"整片平移"区分了。
    sents.append({"begin_time": t, "end_time": t + 800, "sentence_id": i + 1,
                  "text": ("word " * words).strip().capitalize() + " sentence."})
    t += 2000
asr = d / "big.asr.json"
json.dump({"transcripts": [{"channel_id": 0, "sentences": sents}]},
          open(asr, "w"), ensure_ascii=False)
cues = m.cues_from_asr(m.sentences_of(json.load(open(asr, encoding="utf-8"))))
spans = m.translation_units(cues)
ul = [" ".join(cues[j]["text"].strip() for j in range(a, b)) for a, b in spans]
fp = hashlib.sha256("\n".join(ul).encode()).hexdigest()[:16]
aligned = ["译" * max(2, int(0.34 * len(x))) + "。" for x in ul]
shifted = list(aligned)
for ui in range(10, min(30, len(aligned) - 1)):   # 注入：这一段各装了下一条的译文（局部污染）
    shifted[ui] = aligned[ui + 1]


def dump(path, arr):
    json.dump({"fingerprint": fp, "count": len(ul), "model": m.CHAT_MODEL_DEFAULT,
               "src_lang": "en", "target_lang": "zh", "backend": "cloud", "translations": arr},
              open(path, "w", encoding="utf-8"), ensure_ascii=False)


dump(d / "aligned.zh.json", aligned)
dump(d / "shifted.zh.json", shifted)
print(f"  夹具：{len(ul)} 个单元（指纹 {fp}）")
PYFIX
run_case() {   # $1=缓存的译文文件名  $2=输出目录名
  # **必须把夹具译文缓存放进 --cache-dir**：不传 --cache-dir 时脚本去默认缓存根找
  # <基名>.<语言>.json，夹具会被静默忽略、于是它自己联网重译一遍（本闸门要求零网络零计费）。
  local c=$1 o=$2
  rm -rf "$d/.cache" "$d/$o"; mkdir -p "$d/.cache" "$d/$o"
  cp "$d/$c" "$d/.cache/sample.zh.json"
  (cd "$d" && $PY "$SCRIPT" sample.mkv --asr-json big.asr.json --source-lang en \
      --no-repair-desync --no-log --cache-dir .cache --out "$o" > "$o.log" 2>&1)
  echo $?
}
rc=$(run_case aligned.zh.json o_ok)
[ "$rc" = "0" ] && ok "场景 8 对照：对齐缓存 → 闸门通过（退出 0）" || bad "场景 8 对照：对齐缓存被判失败（rc=\${rc}）"
[ -f "$d/o_ok/sample.ass" ] && ok "场景 8 对照：成品已写出" || bad "场景 8 对照：没写出成品"
grep -q "复用译文缓存" "$d/o_ok.log" && ok "场景 8 对照：夹具译文缓存被复用（零网络）" \
  || bad "场景 8 对照：夹具缓存没被复用（脚本自己联网重译了，这一场就不是零网络闸门了）"
rc=$(run_case shifted.zh.json o_bad)
[ "$rc" != "0" ] && ok "场景 8 污染：闸门判失败并拒写（退出 \${rc}）" || bad "场景 8 污染：**平移污染被放过**（退出 0）"
grep -qE "长度平移段|单元收口错位 [1-9]" "$d/o_bad.log" && ok "场景 8 污染：日志给出错位证据" || bad "场景 8 污染：日志没有错位证据"
[ -f "$d/o_bad/sample.ass" ] && bad "场景 8 污染：**写出了坏成品**" || ok "场景 8 污染：未写成品（盘上保留上一版）"
grep -q "复用译文缓存" "$d/o_bad.log" && ok "场景 8 污染：夹具译文缓存被复用（零网络，未偷偷重译）" \
  || bad "场景 8 污染：夹具缓存没被复用"
rm -f "$d/sample.zh.json"
echo "== 场景 9：退化输入不得甩栈 + --allow-desync 必须能强制交付 =="
d=$TMP/s9; rm -rf "$d"; mkdir -p "$d"; cp "$MKV" "$d/sample.mkv"
printf '{"transcripts":[{"sentences":' > "$d/trunc.asr.json"
printf '{"transcripts":[]}' > "$d/empty.asr.json"
printf 'not json at all' > "$d/junk.asr.json"
deg() {   # $1=标签 $2=asr 文件名
  local tag=$1 f=$2 out rc
  out=$(cd "$d" && $PY "$SCRIPT" sample.mkv --asr-json "$f" --no-translate --no-log --cache-dir .c 2>&1)
  rc=$?
  [ "$rc" != "0" ] && ok "场景 9 \${tag}：非零退出（rc=\${rc}）" || bad "场景 9 \${tag}：**退化输入被当成成功**"
  if grep -qi "traceback" <<<"$out"; then
    bad "场景 9 \${tag}：**甩了 Python 栈**（应给明确报错）"
  else
    ok "场景 9 \${tag}：明确报错、未甩栈"
  fi
}
deg "截断 JSON" trunc.asr.json
deg "非法 JSON" junk.asr.json
deg "空转写" empty.asr.json
out=$(cd "$d" && $PY "$SCRIPT" sample.mkv --asr-json trunc.asr.json --no-translate --no-log --cache-dir .c 2>&1)
grep -q "不是合法 JSON" <<<"$out" && ok "场景 9：报错文案指出"不是合法 JSON"" || bad "场景 9：报错文案没说明原因"
out=$(cd "$d" && $PY "$SCRIPT" sample.mkv --asr-json empty.asr.json --no-translate --no-log --cache-dir .c 2>&1)
grep -q "没有任何句子" <<<"$out" && ok "场景 9：空转写被明确拒绝" || bad "场景 9：空转写没被明确拒绝"
# --allow-desync：复用场景 8 的污染夹具 —— 闸门判失败时用户必须能强制交付（且日志照说错位）
if [ -d "$TMP/s8" ] && [ -f "$TMP/s8/shifted.zh.json" ]; then
  e=$TMP/s9b; rm -rf "$e"; mkdir -p "$e/.cache" "$e/o"
  cp "$MKV" "$e/sample.mkv"; cp "$TMP/s8/big.asr.json" "$e/"
  cp "$TMP/s8/shifted.zh.json" "$e/.cache/sample.zh.json"
  out=$(cd "$e" && $PY "$SCRIPT" sample.mkv --asr-json big.asr.json --source-lang en \
      --no-repair-desync --allow-desync --no-log --cache-dir .cache --out o 2>&1)
  rc=$?
  [ "$rc" = "0" ] && ok "场景 9 --allow-desync：强制交付成功（rc=0）" || bad "场景 9 --allow-desync：逃生舱失效（rc=\${rc}）"
  [ -f "$e/o/sample.ass" ] && ok "场景 9 --allow-desync：成品已写出" || bad "场景 9 --allow-desync：没写成品"
  grep -q "整体错位" <<<"$out" && ok "场景 9 --allow-desync：日志仍如实报告错位" || bad "场景 9 --allow-desync：交付时没报告错位"
else
  bad "场景 9：找不到场景 8 的夹具（$TMP/s8）"
fi
echo "== 场景 10：外挂字幕格式（CRLF / 标记与实体 / WebVTT cue 标识符）=="
d=$TMP/s10; rm -rf "$d"; mkdir -p "$d"
for n in a b c; do cp "$MKV" "$d/$n.mkv"; done
printf '1\r\n00:00:00,500 --> 00:00:02,000\r\nCRLF_ONE line.\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,600\r\nCRLF_TWO line.\r\n' > "$d/a.srt"
printf '1\n00:00:00,500 --> 00:00:02,000\n<i>TAG_ITALIC</i> and {\\an8} override, Tom &amp; Jerry.\n\n2\n00:00:03,000 --> 00:00:04,600\n<b>TAG_BOLD</b> tail.\n' > "$d/b.srt"
printf 'WEBVTT\n\n00:00:00.500 --> 00:00:02.000\nVTT_ONE cue.\n\ncue-2\n00:00:03.000 --> 00:00:04.600 align:start position:10%%%%\nVTT_TWO cue.\n' > "$d/c.vtt"
for n in a b c; do (cd "$d" && $PY "$SCRIPT" "$n.mkv" --refresh-source --no-translate --no-log --cache-dir .c >/dev/null 2>&1); done
grep -q CRLF_ONE "$d/a.ass" 2>/dev/null && ok "场景 10：CRLF 换行的 SRT 正常解析" || bad "场景 10：CRLF 的 SRT 解析失败"
grep -q CRLF_TWO "$d/a.ass" 2>/dev/null && ok "场景 10：CRLF 的两条都在" || bad "场景 10：CRLF 漏条"
grep -q "<i>\|<b>" "$d/b.ass" 2>/dev/null && bad "场景 10：**HTML 标签原样进了成品**（会显示成字面量）" || ok "场景 10：HTML 标签已剥离"
grep -q "an8" "$d/b.ass" 2>/dev/null && bad "场景 10：**ASS 覆盖块原样进了成品**" || ok "场景 10：ASS 覆盖块已剥离"
grep -q "&amp;\|&lt;\|&gt;" "$d/b.ass" 2>/dev/null && bad "场景 10：**HTML 实体没解码**（&amp; 会显示成字面量）" || ok "场景 10：HTML 实体已解码"
grep -q TAG_ITALIC "$d/b.ass" 2>/dev/null && grep -q TAG_BOLD "$d/b.ass" 2>/dev/null && ok "场景 10：标签内的正文被保留" || bad "场景 10：连正文一起剥掉了"
grep -q VTT_ONE "$d/c.ass" 2>/dev/null && ok "场景 10：WebVTT 首条解析" || bad "场景 10：WebVTT 首条丢失"
grep -q VTT_TWO "$d/c.ass" 2>/dev/null && ok "场景 10：WebVTT 带 cue 标识符的条目也在（旧版整块丢弃 → 只出 1 条）" || bad "场景 10：**WebVTT 带标识符的条目被丢弃**"
cnt=$(grep -ac "^Dialogue:" "$d/c.ass" 2>/dev/null || echo 0)
[ "$cnt" = "2" ] && ok "场景 10：WebVTT 条数与原文一致（2/2）" || bad "场景 10：WebVTT 条数 $cnt ≠ 2（静默少字幕）"
echo "== 场景 11：--doctor 不得假绿（坏 key 必须判不可用）=="
d=$TMP/s11; rm -rf "$d"; mkdir -p "$d"
out=$(cd "$d" && $PY "$SCRIPT" --doctor --api-key sk-definitely-invalid-key 2>&1)
rc=$?
[ "$rc" != "0" ] && ok "场景 11：坏 key 时 doctor 非零退出（rc=\${rc}）" || bad "场景 11：**坏 key 却报环境可用**（假绿）"
grep -q "环境可用" <<<"$out" && bad "场景 11：坏 key 仍打「✅ 环境可用」" || ok "场景 11：坏 key 不打「环境可用」"
grep -qE "实测失败|无法探测" <<<"$out" && ok "场景 11：doctor 给出「实测失败」这一层结论（不是只看 key 在不在）" \
  || bad "场景 11：doctor 没有模型实测这一层（旧版只查 key 存在 → 实际调用被服务端拒绝也报绿）"
grep -q "API key ✅" <<<"$out" && ok "场景 11：key 存在性检查仍在（两层结论分开报）" || bad "场景 11：key 检查丢了"
echo "== 场景 12：--install 必须整树同步（清单式安装会静默过时）=="
d=$TMP/s12; rm -rf "$d"; mkdir -p "$d"
out=$(cd "$d" && $PY "$SCRIPT" --install --dest "$d/skill" 2>&1)
rc=$?
[ "$rc" = "0" ] && ok "场景 12：--install 正常退出" || bad "场景 12：--install 失败（rc=\${rc}）"
verdict=$($PY - "$ROOT" "$d/skill" <<'PYCHK'
# 源目录里每个"受管文件"都必须在安装树里且内容一致。
# 旧版按写死的清单拷（不含 evals/）→ 安装树里的 evals/ 停在旧版：缺评测超时修复、
# 缺 DSH_BASE_URL 修复（key 与端点不同域 → 评测 6/6 全 401）。清单一定会过时。
import hashlib, pathlib, sys
src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
skip = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", "node_modules", ".venv"}


def rels(root):
    out = set()
    for p in root.rglob("*"):
        r = p.relative_to(root)
        if any(x in skip for x in r.parts) or p.name in {".DS_Store"}:
            continue
        if p.name.startswith("._") or p.suffix in (".pyc", ".pyo"):
            continue
        if p.is_file():
            out.add(r)
    return out


a, b = rels(src), rels(dst)
missing = sorted(a - b)
diff = [r for r in sorted(a & b)
        if hashlib.md5((src / r).read_bytes()).hexdigest() != hashlib.md5((dst / r).read_bytes()).hexdigest()]
print(f"源{len(a)} 装{len(b)} 缺{len(missing)} 异{len(diff)} 例子={(missing or diff)[:3]}")
PYCHK
)
echo "  $verdict"
case "$verdict" in
  *"缺0 异0"*) ok "场景 12：安装树与源逐文件一致（整树同步）" ;;
  *)           bad "场景 12：安装树与源不一致 —— $verdict" ;;
esac
[ -f "$d/skill/evals/eval.yaml" ] && ok "场景 12：evals/ 已随安装交付（旧版完全不拷）" || bad "场景 12：**evals/ 没被安装**"
diff -q "$ROOT/evals/eval.yaml" "$d/skill/evals/eval.yaml" >/dev/null 2>&1 \
  && ok "场景 12：安装副本的评测配置与源一致（旧版停在很早的一版 → 评测 401）" \
  || bad "场景 12：安装副本的评测配置是旧的"
[ -d "$d/skill/scripts/__pycache__" ] && bad "场景 12：把 __pycache__ 也装过去了" || ok "场景 12：构建垃圾未被安装"
[ -x "$d/skill/omnisub" ] && ok "场景 12：启动器带可执行位" || bad "场景 12：启动器没有可执行位"
echo "== 场景 7：翻译方向与 ASS 样式（同一闸门的 Python 部分）=="
if [ -f "$HERE/selftest-langs.py" ]; then
  if out=$($PY "$HERE/selftest-langs.py" "$SCRIPT" 2>&1); then
    ok "场景 7：$(tail -1 <<<"$out")"
  else
    bad "场景 7：语言/样式自检失败 —— $(grep '❌' <<<"$out" | head -3 | tr '\n' ' ')"
  fi
else
  bad "场景 7：找不到 selftest-langs.py"
fi

echo
echo "结果：$pass 通过 / $fail 失败"
[ "$fail" -eq 0 ] || exit 1
