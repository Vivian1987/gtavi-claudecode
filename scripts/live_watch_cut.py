"""Continuously watch a live stream recording and prepare ready-made 1080x1920 clips.

Runs alongside live.py (either spawning it or attaching to an existing job with --watch-only).
For each new spike_<n>.mp4 produced by live.py:
  1. Waits for the file to finish writing and skips Twitch commercial break screens.
  2. Transcribes speech with faster-whisper (word timestamps).
  3. Builds a clean title/hook via moments.build_hooks and flags potential story spoilers.
  4. Cuts a 1080x1920 vertical clip via cut.py (--layout react with fallback to blur).
  5. Verifies the output with check.py and copies passing clips to --out-dir.
  6. Appends clip details + ready-to-post caption to jobs/<id>/ready_clips.md and keeps watching.

usage:
  python scripts/live_watch_cut.py https://www.twitch.tv/caseoh_ --cam 0.0000:0.1250:0.2094:0.2778
  python scripts/live_watch_cut.py https://www.twitch.tv/caseoh_ --watch-only --cam 0.0000:0.1250:0.2094:0.2778
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import clip_filename, ffprobe, job_dir, load_json, save_json, tool
from moments import build_hooks

SCRIPTS_DIR = Path(__file__).parent
SPOILER_RE = re.compile(r"\b(rip|r\.i\.p|died|dead|death|ending|finale|killed|betray)\b", re.I)
_WHISPER_MODEL = None


def get_whisper():
    """Lazily load and cache the faster-whisper small model."""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    from faster_whisper import WhisperModel
    try:
        import ctranslate2
        cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        cuda = False
    try:
        _WHISPER_MODEL = WhisperModel("small", device="cuda" if cuda else "cpu",
                                      compute_type="float16" if cuda else "int8")
    except RuntimeError:
        _WHISPER_MODEL = WhisperModel("small", device="cpu", compute_type="int8")
    return _WHISPER_MODEL


def wait_stable(path: Path, checks: int = 3, delay: float = 1.5) -> bool:
    """Wait until a newly created mp4 stops growing."""
    last = -1
    stable = 0
    for _ in range(20):
        if not path.exists():
            return False
        sz = path.stat().st_size
        if sz > 0 and sz == last:
            stable += 1
            if stable >= checks:
                return True
        else:
            stable = 0
        last = sz
        time.sleep(delay)
    return path.exists() and path.stat().st_size > 0


def is_ad_screen(video_path: Path) -> bool:
    """Detect uniform Twitch commercial-break / intermission slates."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return False
    dur = float(ffprobe(video_path).get("format", {}).get("duration") or 30.0)
    edge_scores = []
    for frac in (0.25, 0.5, 0.75):
        cap.set(cv2.CAP_PROP_POS_MSEC, dur * frac * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150) > 0
        edge_scores.append(float(edges.mean()))
    cap.release()
    # Twitch commercial slates have very sparse edges (< 0.008) compared to gameplay (> 0.02)
    return bool(edge_scores) and max(edge_scores) < 0.009


def transcribe_clip(video_path: Path, scratch_wav: Path) -> list[dict]:
    """Extract 16kHz mono wav and return word-level timestamps."""
    subprocess.run(
        [tool("ffmpeg"), "-y", "-v", "error", "-i", str(video_path), "-ac", "1", "-ar", "16000", str(scratch_wav)],
        check=True,
    )
    with wave.open(str(scratch_wav)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if len(pcm) < 1600:
        return []
    model = get_whisper()
    try:
        segs, _ = model.transcribe(pcm, word_timestamps=True, vad_filter=True)
    except RuntimeError:
        from faster_whisper import WhisperModel
        global _WHISPER_MODEL
        _WHISPER_MODEL = WhisperModel("small", device="cpu", compute_type="int8")
        segs, _ = _WHISPER_MODEL.transcribe(pcm, word_timestamps=True, vad_filter=True)
    return [{"w": wd.word.strip(), "s": round(wd.start, 2), "e": round(wd.end, 2)}
            for seg in segs for wd in (seg.words or []) if wd.word.strip()]


def normalize_clip(src_mp4: Path, norm_mp4: Path) -> tuple[int, int, float]:
    """Re-encode spike to consistent CFR mp4 so ffprobe and decoded frames match."""
    probe = ffprobe(src_mp4)
    dur = float(probe.get("format", {}).get("duration") or 0)
    subprocess.run(
        [tool("ffmpeg"), "-y", "-v", "error", "-i", str(src_mp4),
         "-vf", "fps=30,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-c:a", "aac", "-b:a", "160k", str(norm_mp4)],
        check=True,
    )
    return 1280, 720, dur


def parse_spike_meta(spikes_md: Path, spike_name: str) -> dict:
    """Read rate and top chat words for spike_name from live/spikes.md."""
    if not spikes_md.exists():
        return {}
    for ln in spikes_md.read_text(encoding="utf-8", errors="ignore").splitlines():
        if f"| {spike_name} |" in ln:
            parts = [p.strip() for p in ln.strip("|").split("|")]
            if len(parts) >= 5:
                return {"utc": parts[0], "offset": parts[1], "rate": parts[2], "words": parts[3]}
    return {}


def build_title(text: str, channel: str, meta: dict) -> str:
    """Create a concise, clean reaction title for the clip."""
    info = {"channel": channel, "title": f"{channel} GTA 6 Live"}
    why = ["chat spike"] if meta else []
    primary, _ = build_hooks(text, info, why)
    if primary and "CRAZIEST" not in primary and "NOBODY EXPECTED" not in primary:
        return primary[:48]
    if meta.get("words"):
        return f"{channel} Chat LOST IT in GTA 6"
    return f"{channel} Reacts in GTA 6"


def process_spike(spike_path: Path, job: Path, channel: str, url: str, cam: str,
                  campaign: Path | None, out_dir: Path | None) -> None:
    """Process one spike_<n>.mp4 into a verified 1080x1920 vertical clip."""
    m = re.search(r"spike_(\d+)\.mp4$", spike_path.name)
    if not m:
        return
    num = int(m.group(1))
    print(f"[watch-cut] processing {spike_path.name}...", flush=True)

    if is_ad_screen(spike_path):
        print(f"[watch-cut] skipped {spike_path.name}: Twitch ad/intermission slate", flush=True)
        return

    work = job / "spike_work"
    work.mkdir(exist_ok=True)
    norm_mp4 = work / f"norm_{num}.mp4"
    wav_path = work / f"spike_{num}.wav"

    _, _, dur = normalize_clip(spike_path, norm_mp4)
    if dur < 7.0:
        print(f"[watch-cut] skipped {spike_path.name}: too short ({dur:.1f}s)", flush=True)
        return
    clip_end = round(min(dur, 30.0), 2)

    words = transcribe_clip(norm_mp4, wav_path)
    transcript_text = " ".join(w["w"] for w in words).strip()
    meta = parse_spike_meta(job / "live" / "spikes.md", spike_path.name)

    # Prepare job metadata for cut.py
    spike_job = work / f"job_{num}"
    spike_job.mkdir(exist_ok=True)
    save_json(spike_job / "transcript.json", words)
    title = build_title(transcript_text, channel, meta)
    save_json(spike_job / "candidates.json", {
        "source": str(norm_mp4),
        "duration": dur,
        "info": {"title": f"{channel} Live", "channel": channel, "webpage_url": url},
        "candidates": [{"n": num, "start": 0.0, "end": clip_end, "score": 5.0, "why": ["chat spike"], "hook": title}],
    })

    caps_mode = "word" if len(words) >= 3 else "none"
    cut_cmd = [
        sys.executable, str(SCRIPTS_DIR / "cut.py"), str(spike_job),
        "--pick", str(num), "--layout", "react", "--cam", cam,
        "--title", title, "--title-style", "box", "--captions", caps_mode,
    ]
    r = subprocess.run(cut_cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        # Automatic fallback to blur layout when no webcam/gameplay split is available
        blur_cmd = [
            sys.executable, str(SCRIPTS_DIR / "cut.py"), str(spike_job),
            "--pick", str(num), "--layout", "blur", "--hook", title, "--captions", caps_mode,
        ]
        r = subprocess.run(blur_cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            print(f"[watch-cut] cut failed for {spike_path.name}: {r.stderr[-300:]}", flush=True)
            return

    clip_name = clip_filename(channel, num, title)
    clip_json_name = clip_filename(channel, num, title, ext="json")
    rendered = spike_job / "edits" / clip_name
    rendered_json = spike_job / "edits" / clip_json_name
    caption = f"{title}\n\n🎥 @{channel}\n#GTA6 #GTAVI #GamingClips"

    check_cmd = [sys.executable, str(SCRIPTS_DIR / "check.py"), str(rendered), "--caption", caption]
    if campaign and campaign.exists():
        check_cmd += ["--campaign", str(campaign)]
    chk = subprocess.run(check_cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if chk.returncode != 0:
        print(f"[watch-cut] check failed for {spike_path.name}:\n{chk.stdout}", flush=True)
        return

    edits_dir = job / "edits"
    edits_dir.mkdir(exist_ok=True)
    final_mp4 = edits_dir / clip_name
    shutil.copy2(rendered, final_mp4)
    if rendered_json.exists():
        shutil.copy2(rendered_json, edits_dir / clip_json_name)

    copied_to = None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / clip_name
        shutil.copy2(rendered, dest)
        copied_to = str(dest)

    spoiler_hit = bool(SPOILER_RE.search(transcript_text + " " + meta.get("words", "")))
    flag = " ⚠️ POSSIBLE SPOILER (review before posting)" if spoiler_hit else ""
    ready_md = job / "ready_clips.md"
    if not ready_md.exists():
        ready_md.write_text("# Ready-Made Live Clips\n\n", encoding="utf-8")
    entry = (
        f"## Spike #{num}{flag}\n"
        f"- **File:** `{final_mp4}`" + (f" (copy: `{copied_to}`)" if copied_to else "") + "\n"
        f"- **Stream offset:** {meta.get('offset', '?')} ({meta.get('rate', '?')} msgs/10s; top words: {meta.get('words', '')})\n"
        f"- **Title:** {title}\n"
        f"- **Transcript:** {transcript_text or '(no speech)'}\n"
        f"- **Check:** PASS (1080x1920, 30fps, audio, {clip_end:.1f}s)\n\n"
    )
    with open(ready_md, "a", encoding="utf-8") as fh:
        fh.write(entry)
    print(f"[watch-cut] READY -> {final_mp4}{flag}", flush=True)


def infer_channel(url: str) -> str:
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", url)
    return m.group(1) if m else "creator"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--minutes", type=float, default=720)
    ap.add_argument("--ratio", type=float, default=2.0)
    ap.add_argument("--cam", default="auto")
    ap.add_argument("--channel", default="")
    ap.add_argument("--campaign", default="vice-city-daily/campaigns.md")
    ap.add_argument("--out-dir", default="vice-city-daily/media/edits")
    ap.add_argument("--watch-only", action="store_true", help="attach to already-running live.py job without spawning another")
    ap.add_argument("--poll", type=float, default=10.0)
    a = ap.parse_args()

    job = job_dir(a.url)
    live_dir = job / "live"
    live_dir.mkdir(exist_ok=True)
    channel = a.channel or infer_channel(a.url)
    campaign = Path(a.campaign) if a.campaign else None
    out_dir = Path(a.out_dir) if a.out_dir else None

    state_file = job / "processed_spikes.json"
    done = set(load_json(state_file).get("done", [])) if state_file.exists() else set()

    rec_proc = None
    if not a.watch_only:
        rec_cmd = [sys.executable, str(SCRIPTS_DIR / "live.py"), a.url,
                   "--minutes", str(a.minutes), "--ratio", str(a.ratio)]
        rec_proc = subprocess.Popen(rec_cmd)

    print(f"[watch-cut] watching {live_dir} (channel=@{channel}, watch_only={a.watch_only})", flush=True)
    try:
        while True:
            spikes = sorted(live_dir.glob("spike_*.mp4"),
                            key=lambda p: int(re.search(r"(\d+)", p.stem).group(1)))
            for sp in spikes:
                if sp.name in done:
                    continue
                if wait_stable(sp):
                    try:
                        process_spike(sp, job, channel, a.url, a.cam, campaign, out_dir)
                    except Exception as ex:
                        print(f"[watch-cut] error on {sp.name}: {ex}", flush=True)
                    done.add(sp.name)
                    save_json(state_file, {"done": sorted(done)})
            if rec_proc is not None and rec_proc.poll() is not None:
                # Restart recorder if stream is still live or exit cleanly if finished
                print(f"[watch-cut] live.py exited ({rec_proc.returncode}); restarting in 10s...", flush=True)
                time.sleep(10)
                rec_proc = subprocess.Popen([sys.executable, str(SCRIPTS_DIR / "live.py"), a.url,
                                             "--minutes", str(a.minutes), "--ratio", str(a.ratio)])
            time.sleep(a.poll)
    except KeyboardInterrupt:
        print("[watch-cut] stopping...", flush=True)
    finally:
        if rec_proc is not None and rec_proc.poll() is None:
            rec_proc.terminate()


if __name__ == "__main__":
    main()
