"""Find the best clip moments in a video or VOD.

Signals per second: loudness spikes, YouTube "most replayed" heatmap, live-chat spikes
(YouTube VOD chat replay or a TwitchDownloader chat.json), story/tip semantic cues,
speech cadence, and hype words in the transcript.

By default for remote URLs, downloads audio-only first (saving ~95% bandwidth/disk)
so cut.py can fetch only the winning 1080p clip segments on demand.
Writes jobs/<id>/candidates.md + candidates.json.

usage: python moments.py <url-or-file> [--top 8] [--min 15] [--max 40] [--min-score 0]
                         [--chat chat.json] [--max-minutes 0] [--audio-only | --full-video]
"""
import argparse, json, math, re, subprocess, sys, wave
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from check import banned_in
from common import ffprobe, is_url, job_dir, load_json, run, save_json, tool, ytdlp_base, ytdlp_json
from cut import clean

HYPE = re.compile(
    r"\b(no way|oh my god|omg|what|wait|bro|let'?s go|holy|insane|crazy|yo+|wtf|dude|look at|nah|run|go go)\b",
    re.I,
)
STORY_CUE = re.compile(
    r"\b(secret|hidden|turns out|finally|final mission|nobody noticed|realized|actually|truth|wait until|watch this|first time|ending|plot twist|easter egg)\b",
    re.I,
)
TIP_CUE = re.compile(
    r"\b(how to|fastest way|best way|trick|unlock|location|money|tutorial|guide|make sure|don'?t forget|secret spot)\b",
    re.I,
)
CHAT_HYPE = re.compile(r"(LMAO|LOL|KEKW|OMEGALUL|W+\b|CLIP|💀|😂|🔥|NO WAY|HOLY)", re.I)
_FILLER_PREFIX = re.compile(r"^(um+|uh+|so yeah|like|okay so|yo|hey guys)\b[\s,]*", re.I)


def _find_audio_file(d: Path) -> Path | None:
    for p in sorted(d.glob("source_audio.*")):
        if p.suffix not in (".part", ".ytdl") and p.is_file():
            return p
    return None


def _probe_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            [tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if r.returncode != 0:
            return 0.0
        return float(json.loads(r.stdout).get("format", {}).get("duration") or 0)
    except Exception:
        return 0.0


def _clear_analysis_cache(d: Path):
    for name in ("audio.wav", "transcript.json"):
        (d / name).unlink(missing_ok=True)


def fetch(src, d: Path, max_min: int, audio_only: bool):
    info = {}
    vid = None
    audio_src = None
    if is_url(src):
        info = ytdlp_json(src)
        save_json(
            d / "info.json",
            {
                k: info.get(k)
                for k in (
                    "id", "title", "uploader", "channel", "uploader_id",
                    "webpage_url", "duration", "view_count", "upload_date",
                    "heatmap", "is_live", "was_live",
                )
            },
        )
        full_dur = float(info.get("duration") or 0)
        target_dur = (min(full_dur, max_min * 60) if max_min > 0 else full_dur) if full_dur > 0 else 0.0
        if audio_only:
            audio_src = _find_audio_file(d)
            if audio_src is not None and target_dur > 0 and _probe_duration(audio_src) < target_dur - 15:
                audio_src.unlink(missing_ok=True)
                _clear_analysis_cache(d)
                audio_src = None
            if audio_src is None:
                _clear_analysis_cache(d)
                out_tpl = str(d / "source_audio.%(ext)s")
                args = ytdlp_base() + ["-f", "ba[ext=m4a]/ba/b", "-o", out_tpl]
                if max_min > 0 and full_dur > max_min * 60:
                    args += ["--download-sections", f"*0-{max_min * 60}"]
                run(args + [src])
                audio_src = _find_audio_file(d)
                if audio_src is None:
                    sys.exit("Audio-only download completed without producing source_audio.*")
        else:
            vid = d / "source.mp4"
            if vid.exists() and target_dur > 0 and _probe_duration(vid) < target_dur - 15:
                vid.unlink(missing_ok=True)
                _clear_analysis_cache(d)
            if not vid.exists():
                _clear_analysis_cache(d)
                args = ytdlp_base() + [
                    "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
                    "--merge-output-format", "mp4", "-o", str(vid),
                ]
                if max_min > 0 and full_dur > max_min * 60:
                    args += ["--download-sections", f"*0-{max_min * 60}"]
                run(args + [src])
            audio_src = vid

        if info.get("was_live") and not list(d.glob("chat*.live_chat.json")):
            run(
                ytdlp_base() + ["--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", str(d / "chat"), src],
                check=False,
            )
    else:
        vid = Path(src).resolve()
        audio_src = vid
        save_json(d / "info.json", {"title": vid.stem, "webpage_url": str(vid)})
    return vid, audio_src, info


def audio_rms(media_src: Path, d: Path):
    wav = d / "audio.wav"
    if not wav.exists():
        run([tool("ffmpeg"), "-y", "-v", "error", "-i", str(media_src), "-ac", "1", "-ar", "16000", str(wav)])
    with wave.open(str(wav)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if len(x) == 0:
        return wav, np.zeros(1, dtype=np.float32)
    if len(x) < 16000:
        rms = np.array([np.sqrt((x ** 2).mean() + 1e-9)], dtype=np.float32)
    else:
        n = len(x) // 16000
        rms = np.sqrt((x[: n * 16000].reshape(n, 16000) ** 2).mean(1) + 1e-9)
    return wav, 20 * np.log10(rms)


def transcribe(wav: Path, d: Path):
    out = d / "transcript.json"
    if out.exists():
        return load_json(out)
    from faster_whisper import WhisperModel
    try:
        import ctranslate2
        cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        cuda = False
    with wave.open(str(wav)) as w:  # pass samples directly (16 kHz mono), no extra decoder needed
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768

    def run_on(device):
        model = WhisperModel("small", device=device, compute_type="float16" if device == "cuda" else "int8")
        segs, _ = model.transcribe(pcm, word_timestamps=True, vad_filter=True)
        return [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)} for s in segs for w in (s.words or [])]

    try:
        words = run_on("cuda" if cuda else "cpu")
    except RuntimeError as e:  # GPU present but CUDA libraries missing: fall back to CPU
        print(f"GPU transcription unavailable ({str(e)[:80]}), using CPU")
        words = run_on("cpu")
    save_json(out, words)
    return words


def chat_series(d: Path, n: int, chat_arg, info):
    """Messages per second from YouTube live_chat json lines or a TwitchDownloader chat.json."""
    c = np.zeros(n)
    yt = list(d.glob("chat*.live_chat.json"))
    if yt:
        for line in yt[0].read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                a = json.loads(line)["replayChatItemAction"]
                t = int(a["videoOffsetTimeMsec"]) / 1000
                txt = json.dumps(a["actions"], ensure_ascii=False)
            except Exception:
                continue
            if 0 <= t < n:
                c[int(t)] += 2 if CHAT_HYPE.search(txt) else 1
    elif chat_arg:
        js = load_json(chat_arg)
        for m in js.get("comments", []):
            t = m.get("content_offset_seconds", 0)
            body = (m.get("message") or {}).get("body", "")
            if 0 <= t < n:
                c[int(t)] += 2 if CHAT_HYPE.search(body) else 1
    return c


def z(a):
    a = np.asarray(a, dtype=float)
    if a.std() < 1e-9:
        return np.zeros_like(a)
    return (a - np.median(a)) / (a.std() + 1e-9)


def smooth(a, k):
    k = max(1, int(k))
    return np.convolve(a, np.ones(k) / k, mode="same")


def snap_boundaries(words: list[dict], peak: float, dur: float, min_len: float = 15.0, max_len: float = 40.0) -> tuple[float, float]:
    """Snap clip boundaries to natural sentence starts/pauses and keep up to a 1s reaction tail."""
    raw_s0 = max(0.0, peak - 0.55 * max_len)
    raw_e0 = min(dur, raw_s0 + max_len)

    valid_words = [w for w in words if w.get("w")]
    win_lo, win_hi = max(0.0, raw_s0 - 4.0), min(dur, raw_e0 + 4.0)
    ctx_indices = [k for k, w in enumerate(valid_words) if w["e"] >= win_lo and w["s"] <= win_hi]
    if not ctx_indices:
        latest_start = max(0.0, dur - min_len) if dur >= min_len else 0.0
        s0 = min(raw_s0, latest_start)
        e0 = min(dur, max(raw_e0, s0 + min_len))
        return round(s0, 2), round(min(e0, s0 + max_len), 2)

    # Precompute sentence starts across all valid words (including pauses crossing the context window edge)
    is_start = [False] * len(valid_words)
    if valid_words[0]["s"] <= 1.0:
        is_start[0] = True
    for k in range(1, len(valid_words)):
        gap = valid_words[k]["s"] - valid_words[k - 1]["e"]
        if gap >= 0.45 or valid_words[k - 1]["w"][-1:] in ".?!":
            is_start[k] = True

    sent_starts = [k for k in ctx_indices if is_start[k]]
    if not sent_starts:
        sent_starts = [ctx_indices[0]]

    latest_start = max(0.0, min(peak - 2.0, dur - min_len if dur >= min_len else peak))
    best_start_k = None
    best_dist = 1e9
    for k in sent_starts:
        st_time = valid_words[k]["s"]
        if st_time > latest_start:
            continue
        dist = abs(st_time - raw_s0)
        if dist < best_dist:
            best_dist = dist
            best_start_k = k

    if best_start_k is None:
        s0 = max(0.0, min(raw_s0, latest_start))
        scan_start_k = next((k for k in ctx_indices if valid_words[k]["e"] > s0), ctx_indices[0])
    else:
        s0 = max(0.0, valid_words[best_start_k]["s"] - 0.25)
        scan_start_k = best_start_k

    # Choose natural sentence end after peak + min_len, keeping reaction tail before the next word
    min_end_target = min(dur, max(s0 + min_len, peak + 2.5))
    max_end_target = min(dur, s0 + max_len)
    best_end_time = None

    for k in range(scan_start_k, ctx_indices[-1] + 1):
        cur_w = valid_words[k]
        next_gap = (valid_words[k + 1]["s"] - cur_w["e"]) if k + 1 < len(valid_words) else 1.5
        is_boundary = cur_w["w"][-1:] in ".?!" or next_gap >= 0.45
        tail = 1.0 if next_gap >= 1.1 else min(max(0.0, next_gap - 0.05), max(0.15, next_gap - 0.1))
        candidate_end = min(dur, cur_w["e"] + tail)
        if is_boundary and min_end_target <= candidate_end <= max_end_target:
            if best_end_time is None:
                best_end_time = candidate_end
            if cur_w["w"][-1:] in ".?!" or next_gap >= 1.0:
                best_end_time = candidate_end
                break

    if best_end_time is None:
        in_win = [k for k in range(scan_start_k, ctx_indices[-1] + 1) if valid_words[k]["e"] + 0.1 <= max_end_target]
        if in_win:
            last_k = in_win[-1]
            last_w = valid_words[last_k]
            next_gap = (valid_words[last_k + 1]["s"] - last_w["e"]) if last_k + 1 < len(valid_words) else 1.5
            tail = 1.0 if next_gap >= 1.1 else min(max(0.0, next_gap - 0.05), max(0.15, next_gap - 0.1))
            cand_e0 = min(dur, max_end_target, last_w["e"] + tail)
            if cand_e0 - s0 >= min_len:
                e0 = cand_e0
            else:
                e0 = min(dur, max(s0 + min_len, min(max_end_target, raw_e0)))
        else:
            e0 = min(dur, max(s0 + min_len, min(max_end_target, raw_e0)))
    else:
        e0 = best_end_time

    if dur >= min_len and (e0 - s0) < min_len:
        if s0 + min_len <= dur:
            e0 = s0 + min_len
        else:
            e0 = dur
            s0 = max(0.0, dur - min_len)
    e0 = min(e0, s0 + max_len)
    return round(s0, 2), round(e0, 2)


def _trim_hook(text: str, limit: int = 52) -> str:
    s = " ".join((text or "").split())
    if len(s) <= limit:
        return s
    cut_str = s[: limit + 1]
    if " " in cut_str:
        trimmed = cut_str.rsplit(" ", 1)[0].rstrip(" ,;:-")
        if trimmed:
            return trimmed
    return s[:limit]


def _is_clean_hook(text: str) -> bool:
    return bool(text) and "*" not in text and not banned_in(text)


def build_hooks(text: str, info: dict, why: list[str]) -> tuple[str, list[str]]:
    """Generate 3 distinct hook styles and pick the best primary hook (<= 52 chars)."""
    channel = (info.get("channel") or info.get("uploader") or "THE STREAMER").strip()
    title = (info.get("title") or "").strip()
    clean_title = clean(re.sub(r"^[^\w#@]+", "", title).split("|")[0].split("(")[0].strip(), profanity=False)

    # 1. Punchy quote from transcript (skipping segments with masked profanity or banned terms)
    quote_hook = ""
    for seg in re.split(r"[.?!]+", text or ""):
        cleaned = clean(_FILLER_PREFIX.sub("", seg.strip()), profanity=False)
        words = cleaned.split()
        if 3 <= len(words) <= 9:
            candidate = _trim_hook(" ".join(words).upper(), 48)
            if len(candidate.split()) >= 3 and _is_clean_hook(candidate):
                quote_hook = candidate
                break
    if not quote_hook and text:
        words = clean(_FILLER_PREFIX.sub("", text.strip()), profanity=False).split()[:7]
        if words:
            candidate = _trim_hook(" ".join(words).upper(), 48)
            if _is_clean_hook(candidate):
                quote_hook = candidate

    # 2. Curiosity / stakes hook
    why_str = " ".join(why).lower()
    if "tip" in why_str or TIP_CUE.search(text or ""):
        curiosity_hook = "GTA 6 SECRET NOBODY TOLD YOU"
    elif "story" in why_str or STORY_CUE.search(text or ""):
        curiosity_hook = "WAIT UNTIL WHAT HAPPENS NEXT"
    elif "chat spike" in why_str:
        curiosity_hook = "CHAT LOST IT AT THIS MOMENT"
    else:
        curiosity_hook = "NOBODY EXPECTED THIS IN GTA 6"

    # 3. Context / stakes hook
    if clean_title and len(clean_title) <= 44 and _is_clean_hook(clean_title):
        context_hook = _trim_hook(clean_title.upper(), 48)
    else:
        short_ch = _trim_hook(clean(channel, profanity=False), 18).upper() or "THE STREAMER"
        context_hook = _trim_hook(f"{short_ch}'S CRAZIEST GTA 6 MOMENT", 48)
        if not _is_clean_hook(context_hook):
            context_hook = "THE CRAZIEST GTA 6 MOMENT"

    hooks = []
    for h in (curiosity_hook, quote_hook, context_hook):
        h = _trim_hook(h, 52)
        if h and h not in hooks:
            hooks.append(h)

    primary = quote_hook if (quote_hook and len(quote_hook.split()) >= 4) else (hooks[0] if hooks else "GTA 6 INSANE MOMENT")
    return _trim_hook(primary, 52), hooks


def transcript_signals(words: list[dict], n: int):
    """Compute per-second hype, story/semantic, tip/secret, and speech cadence signals."""
    hype = np.zeros(n)
    semantic = np.zeros(n)
    tip_sig = np.zeros(n)
    cadence = np.zeros(n)

    for i, w in enumerate(words):
        if not w.get("w"):
            continue
        idx = int(w["s"])
        if 0 <= idx < n:
            cadence[idx] += 1
            if w["w"][-1:] in "?!":
                cadence[idx] += 1.5
            clean_phrase = re.sub(r"^[^\w]+", "", " ".join(x["w"] for x in words[i : i + 3] if x.get("w")))
            if HYPE.match(clean_phrase):
                hype[idx] += 1
            if STORY_CUE.match(clean_phrase):
                semantic[idx] += 1
            if TIP_CUE.match(clean_phrase):
                tip_sig[idx] += 1

    return hype, semantic, tip_sig, cadence


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--min", type=float, default=15)
    ap.add_argument("--max", type=float, default=40)
    ap.add_argument("--min-score", type=float, default=0.0, help="Drop candidates below this score")
    ap.add_argument("--chat")
    ap.add_argument("--max-minutes", type=int, default=0, help="0 = full video duration")
    mode_grp = ap.add_mutually_exclusive_group()
    mode_grp.add_argument("--audio-only", action="store_true", help="Download audio only for scoring (default for URLs)")
    mode_grp.add_argument("--full-video", action="store_true", help="Download full 1080p video upfront")
    a = ap.parse_args()

    audio_only = bool(a.audio_only or (is_url(a.src) and not a.full_video))
    d = job_dir(a.src)
    vid, audio_src, info = fetch(a.src, d, a.max_minutes, audio_only=audio_only)
    dur = float(ffprobe(audio_src).get("format", {}).get("duration") or info.get("duration") or 0)
    wav, db = audio_rms(audio_src, d)
    n = len(db)
    if dur <= 0 or (a.max_minutes > 0 and dur > n + 5):
        dur = float(n)
    words = transcribe(wav, d)

    # Signals
    loud = smooth(np.clip(z(db - smooth(db, 30)), 0, None), 3)
    hype, semantic, tip_sig, cadence = transcript_signals(words, n)

    hype_s = smooth(hype, 5)
    sem_s = smooth(semantic, 6)
    tip_s = smooth(tip_sig, 6)
    cad_s = smooth(cadence, 5)

    heat = np.zeros(n)
    for h in (info.get("heatmap") or []):
        hs = max(0, min(n, int(h["start_time"])))
        he = max(0, min(n, int(math.ceil(h["end_time"]))))
        if he > hs:
            heat[hs:he] = h["value"]
    chat = chat_series(d, n, a.chat, info)
    chatz = smooth(np.clip(z(smooth(chat, 10)), 0, None), 3)

    w_heat = 2.0 if heat.any() else 0.0
    w_chat = 1.5 if chat.any() else 0.0
    score = (
        1.0 * loud
        + 0.8 * z(hype_s).clip(0)
        + 0.7 * z(sem_s).clip(0)
        + 0.7 * z(tip_s).clip(0)
        + 0.4 * z(cad_s).clip(0)
        + w_heat * z(heat).clip(0)
        + w_chat * chatz
    )

    picks, used = [], np.zeros(n, bool)
    order = np.argsort(-score)
    for p in order:
        if len(picks) >= a.top or score[p] <= 0 or score[p] < a.min_score:
            break
        if used[p]:
            continue
        s0, e0 = snap_boundaries(words, float(p), dur, min_len=a.min, max_len=a.max)
        u0, u1 = max(0, int(s0)), min(n, int(math.ceil(e0)))
        if u1 > u0 and used[u0:u1].any():
            continue

        clip_words = [x for x in words if s0 <= x["s"] < e0]
        cand_score = float(score[p])
        if len(clip_words) < 8 and not (heat[p] > 0.5 or (w_chat and chatz[p] > 1.5)):
            cand_score *= 0.4

        if cand_score < a.min_score:
            used[p] = True
            continue

        if u1 > u0:
            used[u0:u1] = True
        text = " ".join(x["w"] for x in clip_words)
        why = []
        if loud[p] > 1:
            why.append("loudness spike")
        if hype_s[p] > 0:
            why.append("hype words")
        if sem_s[p] > 0:
            why.append("story/payoff cue")
        if tip_s[p] > 0:
            why.append("tip/secret cue")
        if heat[p] > 0.5:
            why.append(f"most replayed {heat[p]:.2f}")
        if w_chat and chatz[p] > 1:
            why.append("chat spike")
        why = why or ["overall energy"]

        primary_hook, hook_options = build_hooks(text, info, why)
        picks.append(
            {
                "n": len(picks) + 1,
                "start": s0,
                "end": e0,
                "peak": int(p),
                "score": round(cand_score, 2),
                "why": why,
                "hook": primary_hook,
                "hooks": hook_options,
                "text": text[:400],
            }
        )

    picks.sort(key=lambda x: x["start"])
    for i, x in enumerate(picks, 1):
        x["n"] = i

    meta = load_json(d / "info.json")
    save_json(
        d / "candidates.json",
        {
            "source": str(vid) if vid else None,
            "audio_source": str(audio_src),
            "info": meta,
            "duration": dur,
            "candidates": picks,
            "signals": {"heatmap": bool(w_heat), "chat": bool(w_chat), "words": len(words)},
        },
    )
    fmt = lambda t: f"{int(t // 3600)}:{int((t % 3600) // 60):02d}:{int(t % 60):02d}" if t >= 3600 else f"{int(t // 60)}:{int(t % 60):02d}"
    md = [
        f"# Candidates: {meta.get('title', '')}",
        f"source: {meta.get('webpage_url', '')}",
        f"signals: loudness, transcript ({len(words)} words), semantic/tip cues"
        + (", most replayed" if w_heat else "")
        + (", chat" if w_chat else ""),
        "",
    ]
    for x in picks:
        md += [
            f"## {x['n']}. {fmt(x['start'])}-{fmt(x['end'])} ({x['end'] - x['start']:.0f}s) score {x['score']}",
            f"why: {', '.join(x['why'])}",
            f"hooks: {' | '.join(x.get('hooks', []))}",
            f"> {x['text']}",
            "",
        ]
    (d / "candidates.md").write_text("\n".join(md), encoding="utf-8")
    print(f"job: {d}\n{len(picks)} candidates -> {d / 'candidates.md'}")


if __name__ == "__main__":
    main()
