"""Smart Scheduling Engine for GTA 6 vertical clips.

Calculates optimal posting times based on algorithmic engagement peaks across
Instagram Reels, YouTube Shorts, TikTok, and X.
Enforces daily frequency limits, minimum spacing between posts, process-safe
queue persistence, per-platform status tracking, and partial-failure retries.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

QUEUE_FILE = Path("jobs/queue.json")
SUPPORTED_PLATFORMS = ("instagram", "youtube", "tiktok")
TERMINAL_SUCCESS_STATUSES = ("published", "inbox_sent", "scheduled_native")

# Algorithmic peak engagement hours for short-form gaming content (Local 24h format)
# Peak 1: Lunch window (12:30)
# Peak 2: Commute / Late afternoon (17:45)
# Peak 3: Prime evening (21:15)
DEFAULT_PEAK_SLOTS = ["12:30", "17:45", "21:15"]
MIN_HOURS_BETWEEN_POSTS = 3.5
MAX_POSTS_PER_DAY = 3


def parse_scheduled_time(ts_str: str) -> datetime:
    """Parse an ISO timestamp string into a timezone-aware local datetime."""
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo
        dt = dt.replace(tzinfo=local_tz)
    return dt


def default_platforms_map(enabled_platforms=None) -> dict:
    """Create initial per-platform state dictionary for a queue item."""
    if enabled_platforms is None:
        enabled_set = set(SUPPORTED_PLATFORMS)
    else:
        enabled_set = set(enabled_platforms)

    return {
        p: {
            "enabled": p in enabled_set,
            "status": "pending",
            "post_id": None,
            "url": None,
            "error": None,
            "attempts": 0,
            "next_retry_at": None,
            "published_at": None,
        }
        for p in SUPPORTED_PLATFORMS
    }


def normalize_item(item: dict) -> dict:
    """Ensure legacy queue items contain normalized per-platform metadata."""
    defaults = default_platforms_map()
    existing = item.get("platforms")
    if not isinstance(existing, dict):
        item["platforms"] = defaults
        return item

    normalized = {}
    for p in SUPPORTED_PLATFORMS:
        base = defaults[p].copy()
        if isinstance(existing.get(p), dict):
            base.update(existing[p])
        normalized[p] = base
    item["platforms"] = normalized
    return item


@contextmanager
def _queue_lock():
    """Acquire an exclusive advisory file lock around queue operations."""
    qfile = Path(QUEUE_FILE)
    qfile.parent.mkdir(parents=True, exist_ok=True)
    lock_path = qfile.parent / f".{qfile.name}.lock"
    if fcntl is None:
        yield
        return
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_queue_unlocked() -> dict:
    qfile = Path(QUEUE_FILE)
    if not qfile.exists():
        return {"items": []}
    try:
        data = json.loads(qfile.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"items": []}
        items = [normalize_item(dict(it)) for it in data.get("items", []) if isinstance(it, dict)]
        data["items"] = items
        return data
    except Exception:
        return {"items": []}


def _save_queue_unlocked(q: dict):
    qfile = Path(QUEUE_FILE)
    qfile.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(q, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{qfile.name}.", suffix=".tmp", dir=str(qfile.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(payload)
        os.replace(tmp_path, qfile)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def load_queue():
    with _queue_lock():
        return _load_queue_unlocked()


def save_queue(q):
    with _queue_lock():
        _save_queue_unlocked(q)


def get_next_optimal_slot(peak_slots=None, min_gap_hours=MIN_HOURS_BETWEEN_POSTS, max_daily=MAX_POSTS_PER_DAY):
    """Calculates the next optimal timezone-aware ISO timestamp for scheduling a post."""
    slots = peak_slots or DEFAULT_PEAK_SLOTS
    q = load_queue()
    scheduled = [
        parse_scheduled_time(item["scheduled_time"])
        for item in q.get("items", [])
        if item.get("status") in ("approved", "pending", "publishing", "partial_failed")
        and item.get("scheduled_time")
    ]
    scheduled.sort()

    now = datetime.now().astimezone()
    local_tz = now.tzinfo
    # Check slots starting from today up to 14 days ahead
    for day_offset in range(14):
        candidate_day = (now + timedelta(days=day_offset)).date()
        daily_count = sum(1 for dt in scheduled if dt.astimezone(local_tz).date() == candidate_day)
        if daily_count >= max_daily:
            continue

        for slot_str in slots:
            hour, minute = map(int, slot_str.split(":"))
            candidate_dt = datetime.combine(candidate_day, datetime.min.time(), tzinfo=local_tz).replace(
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
    return (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0).isoformat(timespec="minutes")


def add_to_queue(job_id, clip_file, hook, caption, credit, source_url, platforms=None):
    """Add a rendered clip to the queue at the next optimal posting slot."""
    with _queue_lock():
        q = _load_queue_unlocked()
        for item in q["items"]:
            if item.get("clip_file") == str(clip_file):
                return item

    scheduled_time = get_next_optimal_slot()
    with _queue_lock():
        q = _load_queue_unlocked()
        for item in q["items"]:
            if item.get("clip_file") == str(clip_file):
                return item

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
            "status": "pending",  # pending -> approved -> published | partial_failed | failed | declined
            "created_at": datetime.now().astimezone().isoformat(),
            "telegram_message_id": None,
            "platforms": default_platforms_map(platforms),
        }
        q["items"].append(item)
        _save_queue_unlocked(q)
        return item


def update_status(item_id, new_status, extra=None):
    with _queue_lock():
        q = _load_queue_unlocked()
        for item in q["items"]:
            if item["id"] == item_id:
                item["status"] = new_status
                if extra:
                    item.update(extra)
                normalize_item(item)
                _save_queue_unlocked(q)
                return item
    return None


def get_item(item_id):
    q = load_queue()
    for item in q.get("items", []):
        if item.get("id") == item_id:
            return item
    return None


def toggle_platform(item_id, platform):
    """Flip the enabled flag for a single platform on a queue item."""
    if platform not in SUPPORTED_PLATFORMS:
        return None
    with _queue_lock():
        q = _load_queue_unlocked()
        for item in q["items"]:
            if item["id"] == item_id:
                normalize_item(item)
                cur = bool(item["platforms"][platform].get("enabled", True))
                item["platforms"][platform]["enabled"] = not cur
                _save_queue_unlocked(q)
                return item
    return None


def reschedule_item(item_id, new_scheduled_time: str, approve: bool = False):
    """Update an item's scheduled_time (and optionally approve it)."""
    parse_scheduled_time(new_scheduled_time)  # validate ISO timestamp
    with _queue_lock():
        q = _load_queue_unlocked()
        for item in q["items"]:
            if item["id"] == item_id:
                normalize_item(item)
                item["scheduled_time"] = new_scheduled_time
                if approve:
                    item["status"] = "approved"
                _save_queue_unlocked(q)
                return item
    return None


def compute_aggregate_status(item: dict) -> str:
    """Derive aggregate queue item status from per-platform states."""
    cur_status = item.get("status", "pending")
    if cur_status == "declined":
        return "declined"

    platforms = item.get("platforms", {})
    enabled_states = [v for v in platforms.values() if v.get("enabled")]
    if not enabled_states:
        return cur_status

    statuses = [v.get("status", "pending") for v in enabled_states]
    any_success = any(s in TERMINAL_SUCCESS_STATUSES for s in statuses)
    any_failed = any(s == "failed" for s in statuses)
    any_pending = any(s == "pending" for s in statuses)
    all_skipped = all(s == "skipped" for s in statuses)

    if any_pending and any_failed:
        return "partial_failed"
    if any_pending:
        return "pending" if cur_status == "pending" else "approved"
    if any_failed and any_success:
        return "partial_failed"
    if any_failed and not any_success:
        return "failed"
    if any_success:
        return "published"
    if all_skipped:
        return "skipped"
    return cur_status


def record_platform_result(item_id, platform, result_dict, max_retries=3, backoff_minutes=(5, 15, 45)):
    """Persist the outcome of publishing to one platform and recompute aggregate status."""
    if platform not in SUPPORTED_PLATFORMS:
        return None
    with _queue_lock():
        q = _load_queue_unlocked()
        for item in q["items"]:
            if item["id"] == item_id:
                normalize_item(item)
                p_state = item["platforms"][platform]
                st = result_dict.get("status", "failed")
                p_state["status"] = st
                if result_dict.get("post_id") is not None:
                    p_state["post_id"] = result_dict["post_id"]
                if result_dict.get("url") is not None:
                    p_state["url"] = result_dict["url"]

                if st in TERMINAL_SUCCESS_STATUSES:
                    p_state["error"] = None
                    p_state["next_retry_at"] = None
                    p_state["published_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                elif st == "failed":
                    attempts = int(p_state.get("attempts", 0)) + 1
                    p_state["attempts"] = attempts
                    p_state["error"] = result_dict.get("message") or result_dict.get("error") or "Unknown error"
                    if result_dict.get("retryable", False) and attempts < max_retries and backoff_minutes:
                        delay_min = backoff_minutes[min(attempts - 1, len(backoff_minutes) - 1)]
                        p_state["next_retry_at"] = (
                            datetime.now().astimezone() + timedelta(minutes=delay_min)
                        ).isoformat(timespec="seconds")
                    else:
                        p_state["next_retry_at"] = None
                elif st == "skipped":
                    p_state["error"] = result_dict.get("message") or "Skipped"
                    p_state["next_retry_at"] = None

                item["status"] = compute_aggregate_status(item)
                _save_queue_unlocked(q)
                return item
    return None


def reset_failed_platforms(item_id):
    """Reset failed/skipped enabled platforms back to pending so a manual retry runs them."""
    with _queue_lock():
        q = _load_queue_unlocked()
        for item in q["items"]:
            if item["id"] == item_id:
                normalize_item(item)
                for p in SUPPORTED_PLATFORMS:
                    p_state = item["platforms"][p]
                    if p_state.get("enabled") and p_state.get("status") in ("failed", "skipped"):
                        p_state["status"] = "pending"
                        p_state["attempts"] = 0
                        p_state["next_retry_at"] = None
                        p_state["error"] = None
                item["status"] = "approved"
                _save_queue_unlocked(q)
                return item
    return None


def get_due_items(now=None, max_retries=3) -> list[dict]:
    """Return approved due items and items with retryable failed platforms whose backoff elapsed."""
    if isinstance(now, str):
        now_dt = parse_scheduled_time(now)
    elif now is None:
        now_dt = datetime.now().astimezone()
    else:
        now_dt = now if now.tzinfo is not None else now.replace(tzinfo=datetime.now().astimezone().tzinfo)

    due = []
    for item in load_queue().get("items", []):
        status = item.get("status")
        platforms = item.get("platforms", {})
        enabled = [v for v in platforms.values() if v.get("enabled")]
        if not enabled:
            continue

        if status == "approved":
            sched_str = item.get("scheduled_time")
            if not sched_str:
                continue
            if parse_scheduled_time(sched_str) <= now_dt and any(v.get("status") == "pending" for v in enabled):
                due.append(item)
        elif status in ("partial_failed", "failed"):
            has_due_retry = False
            for v in enabled:
                if v.get("status") == "failed" and int(v.get("attempts", 0)) < max_retries:
                    retry_at = v.get("next_retry_at")
                    if retry_at and parse_scheduled_time(retry_at) <= now_dt:
                        has_due_retry = True
                        break
            if has_due_retry:
                due.append(item)
    return due
