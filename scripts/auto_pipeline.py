"""Fully Automated End-to-End Pipeline for GTA 6 Clip Desk.

1. Discovers top trending GTA 6 videos.
2. Identifies peak hype/loudness/chat moments.
3. Automatically cuts 1080x1920 vertical clips with captions & hook boxes.
4. Verifies clips against safety, length, and campaign rules.
5. Schedules them at the algorithmically optimal posting times and frequency.
6. Sends the video to your Telegram for 1-click [Approve] / [Decline].
7. Alerts you (or posts) when the scheduled posting window arrives.

usage:
  python scripts/auto_pipeline.py --once
  python scripts/auto_pipeline.py --loop --interval-hours 3
"""
import argparse, json, os, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path
try:
    import urllib3.util.connection as urllib_conn
    urllib_conn.HAS_IPV6 = False
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).parent))
from common import load_json, run, save_json, ytdlp_json
from smart_queue import add_to_queue, get_next_optimal_slot, load_queue
from social_config import load_social_config
from telegram_bot import TelegramDesk, load_config

PROCESSED_FILE = Path("jobs/processed.json")


def load_processed():
    if not PROCESSED_FILE.exists():
        return set()
    try:
        data = json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
        return set(data.get("processed", []))
    except Exception:
        return set()


def mark_processed(url_or_id):
    s = load_processed()
    s.add(str(url_or_id))
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_FILE.write_text(json.dumps({"processed": list(s)}, indent=2), encoding="utf-8")


def find_next_trending_video(query="gta 6", max_hours=24):
    """Finds the highest-velocity trending video that hasn't been clipped yet."""
    processed = load_processed()
    q = query.replace(" ", "+")
    upload_sort = "&sp=CAI%253D"
    search_url = f"https://www.youtube.com/results?search_query={q}{upload_sort}"
    
    print(f"Scanning trending videos for '{query}'...")
    try:
        flat = ytdlp_json(search_url, ["--flat-playlist", "--playlist-end", "20"])
    except Exception as e:
        print(f"Failed to scan trends: {e}")
        return None

    entries = [e for e in flat.get("entries", []) if e.get("url") and "/shorts/" not in e["url"]]
    
    for entry in entries:
        url = entry.get("url")
        vid_id = entry.get("id") or url
        if vid_id in processed or url in processed:
            continue
        return url

    return None


def run_pipeline_step(cmd, desc):
    print(f"\n--- {desc} ---")
    print(f"Executing: {' '.join(cmd)}")
    res = subprocess.run([sys.executable] + cmd, check=False)
    if res.returncode != 0:
        print(f"Step failed with code {res.returncode}")
        return False
    return True


def extract_hook_from_candidate(candidate, info):
    """Generates an engaging 1-second hook text based on candidate speech or title."""
    text = candidate.get("text", "").strip()
    title = info.get("title", "")
    
    # Priority: short punchy candidate sentence
    if text:
        first_sentence = text.split(".")[0].split("?")[0].split("!")[0]
        words = first_sentence.split()
        if 3 <= len(words) <= 8:
            return " ".join(words).upper()

    # Fallback to punchy title segment
    clean_title = title.split("|")[0].split("-")[0].strip()
    return clean_title[:45].upper() if clean_title else "GTA 6 INSANE MOMENT"


def process_video(url, campaign_file=None):
    mark_processed(url)
    
    # Step 1: Moments finder
    print(f"\n[1/4] Running moments finder on {url}...")
    run_res = subprocess.run([sys.executable, "scripts/moments.py", url, "--top", "5", "--max-minutes", "60"],
                             check=False, stdout=subprocess.PIPE, text=True)
    if run_res.returncode != 0:
        print("Failed to analyze video moments.")
        return None

    # Locate job directory from output
    job_dir = None
    for line in run_res.stdout.splitlines():
        if line.startswith("job: "):
            job_dir = Path(line.replace("job: ", "").strip())
            break
            
    if not job_dir or not (job_dir / "candidates.json").exists():
        print("No job directory or candidates.json found.")
        return None

    cand_data = load_json(job_dir / "candidates.json")
    candidates = cand_data.get("candidates", [])
    if not candidates:
        print("No high-confidence moments detected in video.")
        return None

    info = cand_data.get("info", {})
    channel = info.get("channel") or info.get("uploader") or "creator"
    source_title = info.get("title", "GTA 6 Clip")

    # Step 2: Try cutting top candidate
    top_cand = candidates[0]
    hook = extract_hook_from_candidate(top_cand, info)
    print(f"\n[2/4] Cutting top moment #{top_cand['n']} with hook: '{hook}'...")

    cut_cmd = ["scripts/cut.py", str(job_dir), "--pick", str(top_cand["n"]), "--hook", hook, "--layout", "facecam"]
    res = subprocess.run([sys.executable] + cut_cmd, check=False)
    if res.returncode != 0:
        # Fallback without facecam
        print("Facecam layout failed, falling back to blur background...")
        cut_cmd = ["scripts/cut.py", str(job_dir), "--pick", str(top_cand["n"]), "--hook", hook, "--layout", "blur"]
        res = subprocess.run([sys.executable] + cut_cmd, check=False)
        if res.returncode != 0:
            print("Failed to render clip.")
            return None

    clip_mp4 = job_dir / "edits" / f"clip_{top_cand['n']}.mp4"
    if not clip_mp4.exists():
        print(f"Rendered clip not found at {clip_mp4}")
        return None

    # Step 3: Rule check
    print(f"\n[3/4] Verifying clip rules for {clip_mp4}...")
    check_cmd = [sys.executable, "scripts/check.py", str(clip_mp4)]
    if campaign_file and Path(campaign_file).exists():
        check_cmd += ["--campaign", str(campaign_file)]
    
    check_res = subprocess.run(check_cmd, check=False)
    if check_res.returncode != 0:
        print("Clip failed pre-post quality or safety checks.")
        return None

    # Step 4: Add to Smart Queue & Send to Telegram
    print(f"\n[4/4] Scheduling & Dispatching to Telegram...")
    caption = (
        f"{hook}!\n\n"
        f"What do you think of this? Drop your thoughts below 👇\n\n"
        f"🎥 Credit: @{channel}\n"
        f"#GTA6 #GTAVI #RockstarGames #Gaming #ViceCity"
    )

    item = add_to_queue(
        job_id=job_dir.name,
        clip_file=clip_mp4,
        hook=hook,
        caption=caption,
        credit=channel,
        source_url=url,
        platforms=load_social_config().get("default_platforms"),
    )

    try:
        desk = TelegramDesk()
        desk.send_video_for_approval(item)
        print(f"Successfully queued and sent to Telegram! Next optimal slot: {item['scheduled_time']}")
        return item
    except Exception as e:
        print(f"Could not send to Telegram: {e}")
        print("Check that TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in bot_config.json")
        return item


def telegram_listener_daemon():
    """Background thread polling for Approve/Decline clicks & alerting when posts are due."""
    try:
        desk = TelegramDesk()
        print("[Telegram Listener] Started listening for approvals...")
        offset = None
        while True:
            offset = desk.process_updates(offset)
            desk.notify_scheduled_posts()
            time.sleep(3)
    except Exception as e:
        print(f"[Telegram Listener Error]: {e}")


def main():
    ap = argparse.ArgumentParser(description="Automated GTA 6 Clip Production & Scheduling Desk")
    ap.add_argument("--once", action="store_true", help="Run one discovery and clip generation cycle")
    ap.add_argument("--loop", action="store_true", help="Run indefinitely on a schedule")
    ap.add_argument("--interval-hours", type=float, default=3.0, help="Hours between automated scans")
    ap.add_argument("--url", help="Process a specific video URL directly instead of scanning trends")
    ap.add_argument("--campaign", default="campaigns.md", help="Path to campaigns.md file")
    a = ap.parse_args()

    # Start Telegram callback listener in background thread
    t = threading.Thread(target=telegram_listener_daemon, daemon=True)
    t.start()

    if a.url:
        process_video(a.url, a.campaign)
        if a.loop or a.once:
            print("Finished processing provided URL. Listener is active. Press Ctrl+C to exit.")
            while True:
                time.sleep(1)
        return

    while True:
        print(f"\n=======================================================")
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting Trend & Clipping Cycle")
        print(f"=======================================================")
        
        target_url = find_next_trending_video()
        if target_url:
            print(f"Found target trending video: {target_url}")
            process_video(target_url, a.campaign)
        else:
            print("No new trending videos found at this moment.")

        if a.once:
            print("\nRun completed (--once). Keeping Telegram listener alive for 2 minutes or until Ctrl+C...")
            try:
                time.sleep(120)
            except KeyboardInterrupt:
                pass
            break

        sleep_seconds = int(a.interval_hours * 3600)
        print(f"\nNext automated scan in {a.interval_hours} hours ({sleep_seconds}s). Sleeping...")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
