#!/usr/bin/env python3
"""
Gemini 配音生成器 — 为 H.E.A.R.T. 讲义 HTML 生成高质量中文配音，并把音频嵌回 HTML。

用法：
  pip install requests lameenc
  export GEMINI_API_KEY="你的key"          # Windows: set GEMINI_API_KEY=你的key
  python gemini_voiceover.py eph2-reconciliation-heart.html

可选参数：
  --dry-run            只列出分段与字数，不调用 API
  --model MODEL        默认 gemini-3.1-flash-tts-preview
  --narrator VOICE     讲解声音（默认 Sulafat 温暖）
  --scripture VOICE    经文朗读声音（默认 Charon 沉稳）
  --prayer VOICE       祷告声音（默认 Vindemiatrix 温柔）
  --only 3,7           只重新生成指定分段（其余用缓存）
  --mp3                另外导出一个完整 MP3（可发微信群、离线听）

输出：
  <原名>-voiced.html   已嵌入配音、可直接打开或上传发布
  <原名>-full.mp3      （加 --mp3 时）
  tts_cache/           每段音频缓存；中途失败重跑会自动跳过已完成的段落
"""
import argparse, base64, hashlib, json, os, re, sys, time
import requests, lameenc

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RATE = 24000  # Gemini TTS 输出：24kHz / 16-bit / 单声道 PCM

STYLE = {
    "title": ("Warm, welcoming small-group leader opening a Bible study evening. "
              "Unhurried, gentle authority, a natural pause after the title."),
    "narration": ("A warm, thoughtful Chinese small-group Bible study leader explaining to friends in a living room. "
                  "Conversational but clear; not preachy, not theatrical. Natural pauses at commas and full stops."),
    "debate": ("A thoughtful teacher posing hard questions, then explaining each thinker's idea. "
               "Let each question land with a short pause before the answer. Calm, intelligent, engaged."),
    "scripture": ("Reverent public Scripture reading in a church. Slow, clear, dignified; "
                  "each verse given room to breathe. No dramatization."),
    "prayer": ("Quiet, sincere, heartfelt prayer spoken to God in a small group. "
               "Slower pace, soft and tender, humble; never performative."),
}

def build_prompt(kind, text):
    return (
        "Synthesize speech for the transcript below. Read ONLY the transcript, never these notes.\n\n"
        "### DIRECTOR'S NOTES\n"
        f"Style: {STYLE.get(kind, STYLE['narration'])}\n"
        "Accent: Standard Mandarin Chinese (Putonghua), clear and natural, as spoken by an educated "
        "Chinese speaker living in Vancouver.\n"
        "Pronunciation: Read Bible references like 以弗所书2章14节 naturally. Read punctuation as pauses, not words.\n\n"
        "### TRANSCRIPT\n" + text
    )

def call_tts(key, model, voice, prompt, tries=6):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    wait = 8
    for n in range(1, tries + 1):
        try:
            r = requests.post(API.format(model=model), json=body, timeout=300,
                              headers={"x-goog-api-key": key, "Content-Type": "application/json"})
        except requests.RequestException as e:
            print(f"    网络错误：{e}；{wait}s 后重试 ({n}/{tries})"); time.sleep(wait); wait = min(wait * 2, 120); continue
        if r.status_code == 200:
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                data = next(p["inlineData"]["data"] for p in parts if "inlineData" in p)
                return base64.b64decode(data)
            except Exception:
                print(f"    返回里没有音频（可能被判为文本输出），重试 ({n}/{tries})")
        elif r.status_code in (429, 500, 502, 503, 504):
            ra = r.headers.get("retry-after")
            w = int(ra) if ra and ra.isdigit() else wait
            print(f"    HTTP {r.status_code}，{w}s 后重试 ({n}/{tries})"); time.sleep(w); wait = min(wait * 2, 120); continue
        else:
            sys.exit(f"API 错误 HTTP {r.status_code}：{r.text[:500]}")
        time.sleep(wait); wait = min(wait * 2, 120)
    sys.exit("多次重试仍失败，请稍后再跑（已完成的段落会保留在 tts_cache/）。")

def trim_silence(pcm, thresh=350, keep_ms=180):
    """去掉首尾过长的静音，保留一点呼吸感。"""
    import array
    s = array.array("h"); s.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big": s.byteswap()
    i, j = 0, len(s) - 1
    while i < j and abs(s[i]) < thresh: i += 1
    while j > i and abs(s[j]) < thresh: j -= 1
    pad = RATE * keep_ms // 1000
    i, j = max(0, i - pad), min(len(s), j + pad)
    out = s[i:j]
    if sys.byteorder == "big": out.byteswap()
    return out.tobytes()

def to_mp3(pcm, kbps=40):
    enc = lameenc.Encoder()
    enc.set_bit_rate(kbps); enc.set_in_sample_rate(RATE); enc.set_channels(1); enc.set_quality(2)
    return enc.encode(pcm) + enc.flush()

def main():
    ap = argparse.ArgumentParser(description="用 Gemini TTS 为讲义 HTML 配音")
    ap.add_argument("html")
    ap.add_argument("--model", default="gemini-3.1-flash-tts-preview")
    ap.add_argument("--narrator", default="Sulafat")
    ap.add_argument("--scripture", default="Charon")
    ap.add_argument("--prayer", default="Vindemiatrix")
    ap.add_argument("--only", default="")
    ap.add_argument("--mp3", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    html = open(a.html, encoding="utf-8").read()
    m = re.search(r'(<script[^>]*id="tts-manifest"[^>]*>)(.*?)(</script>)', html, re.S)
    if not m: sys.exit("找不到 tts-manifest，请使用带配音播放器的讲义 HTML。")
    man = json.loads(m.group(2).replace("<\\/", "</"))
    paras, chunks = man["paras"], man["chunks"]
    voice_for = {"scripture": a.scripture, "prayer": a.prayer}

    total = 0
    for c in chunks:
        c["_text"] = "\n".join(paras[p]["text"] for p in c["paras"])
        total += len(c["_text"])
    print(f"共 {len(chunks)} 段，{total} 字（约 {total/4.2/60:.0f} 分钟）  模型：{a.model}")
    if a.dry_run:
        for c in chunks:
            print(f"  [{c['id']:>2}] {c['kind']:<9} {voice_for.get(c['kind'], a.narrator):<12} {len(c['_text']):>4}字  {c['sec']}")
        return

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key: sys.exit("请先设置环境变量 GEMINI_API_KEY。")
    only = {int(x) for x in a.only.split(",") if x.strip()}
    os.makedirs("tts_cache", exist_ok=True)
    full = bytearray(); gap = b"\x00\x00" * int(RATE * 0.7)

    for c in chunks:
        voice = voice_for.get(c["kind"], a.narrator)
        prompt = build_prompt(c["kind"], c["_text"])
        h = hashlib.sha1(f"{a.model}|{voice}|{prompt}".encode()).hexdigest()[:16]
        path = os.path.join("tts_cache", f"{c['id']:02d}_{h}.pcm")
        if os.path.exists(path) and c["id"] not in only:
            pcm = open(path, "rb").read(); tag = "缓存"
        else:
            print(f"  [{c['id']:>2}/{len(chunks)-1}] 生成中… {c['sec']}（{c['kind']}，{voice}）")
            pcm = trim_silence(call_tts(key, a.model, voice, prompt))
            open(path, "wb").write(pcm); tag = "完成"
            time.sleep(2)
        dur = len(pcm) / 2 / RATE
        c["audio"] = "data:audio/mpeg;base64," + base64.b64encode(to_mp3(pcm)).decode()
        c["dur"] = round(dur, 2)
        full += pcm + gap
        print(f"  [{c['id']:>2}] {tag}  {dur:5.1f}s")

    for c in chunks: c.pop("_text", None)
    js = json.dumps(man, ensure_ascii=False).replace("</", "<\\/")
    out_html = html[: m.start(2)] + js + html[m.end(2):]
    base = os.path.splitext(a.html)[0]
    out = base + "-voiced.html"
    open(out, "w", encoding="utf-8").write(out_html)
    print(f"\n✓ 已输出 {out}（{os.path.getsize(out)/1e6:.1f} MB）")
    if a.mp3:
        mp3 = base + "-full.mp3"
        open(mp3, "wb").write(to_mp3(bytes(full), 48))
        print(f"✓ 已输出 {mp3}（{os.path.getsize(mp3)/1e6:.1f} MB，{len(full)/2/RATE/60:.1f} 分钟）")

if __name__ == "__main__":
    main()
