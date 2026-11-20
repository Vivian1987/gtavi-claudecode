"""Smart Scheduling Engine for GTA 6 vertical clips.

Calculates optimal posting times based on algorithmic engagement peaks across
TikTok, Instagram Reels, YouTube Shorts, and X.
Enforces daily frequency limits (e.g. 2-3 posts/day) and minimum spacing between posts.
"""
import json, os, time
from datetime import datetime, timedelta
from pathlib import Path

QUEUE_FILE = Path("jobs/queue.json")

# Algorithmic peak engagement hours for short-form gaming content (Local 24h format)
# Peak 1: Lunch window (12:30)
# Peak 2: Commute / Late afternoon (17:45)
# Peak 3: Prime evening (21:15)
DEFAULT_PEAK_SLOTS = ["12:30", "17:45", "21:15"]
MIN_HOURS_BETWEEN_POSTS = 3.5
MAX_POSTS_PER_DAY = 3


def load_queue():
    if not QUEUE_FILE.exists():
        return {"items": []}
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}


def save_queue(q):
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(q, indent=2, ensure_ascii=False), encoding="utf-8")


def get_next_optimal_slot(peak_slots=None, min_gap_hours=MIN_HOURS_BETWEEN_POSTS, max_daily=MAX_POSTS_PER_DAY):
    """Calculates the next optimal timestamp (ISO format) for scheduling a post."""
    slots = peak_slots or DEFAULT_PEAK_SLOTS
    q = load_queue()
    scheduled = [
        datetime.fromisoformat(item["scheduled_time"])
        for item in q.get("items", [])
        if item.get("status") in ("approved", "pending") and "scheduled_time" in item
    ]
    scheduled.sort()

    now = datetime.now()
    # Check slots starting from today up to 14 days ahead
    for day_offset in range(14):
        candidate_day = (now + timedelta(days=day_offset)).date()
        daily_count = sum(1 for dt in scheduled if dt.date() == candidate_day)
        if daily_count >= max_daily:
            continue

        for slot_str in slots:
            hour, minute = map(int, slot_str.split(":"))
            candidate_dt = datetime.combine(candidate_day, datetime.min.time()).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )

            # Must be at least 20 minutes in the future
            if candidate_dt <= now + timedelta(minutes=20):
                continue

            # Must respect minimum spacing from all other scheduled posts
            too_close = False
            for s_dt in scheduled:
                gap = abs((candidate_dt - s_dt).total_seconds()) / 3600.0
                if gap < min_gap_hours:
                    too_close = True
                    break

            if not too_close:
                return candidate_dt.isoformat(timespec="minutes")

    # Fallback: tomorrow evening
    return (now + timedelta(days=1)).replace(hour=18, minute=0, second=0).isoformat(timespec="minutes")


def add_to_queue(job_id, clip_file, hook, caption, credit, source_url):
    q = load_queue()
    # Check if already in queue
    for item in q["items"]:
        if item.get("clip_file") == str(clip_file):
            return item

    scheduled_time = get_next_optimal_slot()
    item_id = f"clip_{len(q['items']) + 1}_{int(time.time())}"
    item = {
        "id": item_id,
        "job_id": job_id,
        "clip_file": str(clip_file),
        "hook": hook,
        "caption": caption,
        "credit": credit,
        "source_url": source_url,
        "scheduled_time": scheduled_time,
        "status": "pending",  # pending -> approved -> published | declined
        "created_at": datetime.now().isoformat(),
        "telegram_message_id": None
    }
    q["items"].append(item)
    save_queue(q)
    return item


def update_status(item_id, new_status, extra=None):
    q = load_queue()
    for item in q["items"]:
        if item["id"] == item_id:
            item["status"] = new_status
            if extra:
                item.update(extra)
            save_queue(q)
            return item
    return None
