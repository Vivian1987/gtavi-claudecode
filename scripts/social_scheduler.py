"""Native Social Media Scheduler & CLI Dispatcher.

Publishes approved GTA 6 vertical clips at scheduled times across Instagram Reels,
YouTube Shorts, and TikTok using official free APIs, with cross-process locking,
per-platform isolation, retry backoff, and Telegram manual fallback when unconfigured.

usage:
  python scripts/social_scheduler.py --status
  python scripts/social_scheduler.py --once [--dry-run]
  python scripts/social_scheduler.py --daemon [--interval 30]
  python scripts/social_scheduler.py --publish-now <item_id> [--platforms ig,yt,tt]
  python scripts/social_scheduler.py --retry <item_id>
  python scripts/social_scheduler.py --reschedule <item_id> "2026-11-21T17:45:00+01:00"
  python scripts/social_scheduler.py --print-systemd
  python scripts/social_scheduler.py --print-cron
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

sys.path.insert(0, str(Path(__file__).parent))

import smart_queue
from smart_queue import (
    SUPPORTED_PLATFORMS,
    TERMINAL_SUCCESS_STATUSES,
    get_due_items,
    get_item,
    load_queue,
    parse_scheduled_time,
    record_platform_result,
    reschedule_item,
    reset_failed_platforms,
    update_status,
)
from social_config import configured_platforms, is_platform_configured, load_social_config
from publishers import PublishError, PublishResult, get_publisher

PLATFORM_ALIASES = {
    "ig": "instagram",
    "instagram": "instagram",
    "yt": "youtube",
    "youtube": "youtube",
    "tt": "tiktok",
    "tiktok": "tiktok",
}


@contextmanager
def scheduler_lock():
    """Non-blocking cross-process lock to prevent overlapping scheduler runs."""
    lock_env = os.environ.get("SOCIAL_SCHEDULER_LOCK")
    lock_path = Path(lock_env) if lock_env else (Path(smart_queue.QUEUE_FILE).parent / ".scheduler.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield True
        return
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _default_notifier():
    try:
        from telegram_bot import TelegramDesk, load_config

        token, chat_id = load_config()
        if token and chat_id:
            return TelegramDesk(token, chat_id)
    except Exception:
        pass
    return None


def _parse_platforms_arg(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    out = []
    for part in raw.split(","):
        key = PLATFORM_ALIASES.get(part.strip().lower())
        if not key:
            raise ValueError(f"Unknown platform '{part.strip()}'. Expected instagram/ig, youtube/yt, or tiktok/tt.")
        if key not in out:
            out.append(key)
    return out


def pre_schedule_native(item_id: str, config=None, config_path=None, notifier=None) -> PublishResult | None:
    """Optionally pre-upload to YouTube using native publishAt right after Telegram approval."""
    item = get_item(item_id)
    if not item or item.get("status") != "approved":
        return None

    cfg = config if config is not None else load_social_config(config_path)
    yt_cfg = cfg.get("youtube", {})
    yt_state = item.get("platforms", {}).get("youtube", {})

    if not yt_state.get("enabled"):
        return None
    if yt_state.get("status") in TERMINAL_SUCCESS_STATUSES:
        return None
    if not is_platform_configured("youtube", cfg):
        return None
    if not yt_cfg.get("use_native_publish_at", False):
        return None

    sched_dt = parse_scheduled_time(item["scheduled_time"])
    if sched_dt <= datetime.now().astimezone() + timedelta(minutes=15):
        return None

    pub = get_publisher("youtube", cfg, config_path=config_path)
    try:
        res = pub.publish(
            clip_path=item["clip_file"],
            hook=item.get("hook", ""),
            caption=item.get("caption", ""),
            credit=item.get("credit", ""),
            scheduled_time=item.get("scheduled_time"),
            force_native_schedule=True,
        )
    except PublishError as exc:
        res = PublishResult(
            platform="youtube",
            ok=False,
            status="failed",
            message=exc.message,
            retryable=exc.retryable,
        )
    except Exception as exc:
        res = PublishResult(
            platform="youtube",
            ok=False,
            status="failed",
            message=str(exc),
            retryable=True,
        )

    updated = record_platform_result(
        item_id,
        "youtube",
        res.to_dict(),
        max_retries=int(cfg.get("max_retries", 3)),
        backoff_minutes=cfg.get("retry_backoff_minutes", [5, 15, 45]),
    )
    if notifier and hasattr(notifier, "send_publish_report"):
        notifier.send_publish_report(updated, {"youtube": res})
    return res


def publish_item(
    item_id: str,
    platforms: list[str] | None = None,
    dry_run: bool = False,
    config=None,
    config_path=None,
    notifier=None,
    force: bool = False,
    ignore_backoff: bool = False,
) -> dict[str, PublishResult]:
    """Publish a queue item to its enabled (or specified) platforms."""
    item = get_item(item_id)
    if not item:
        raise ValueError(f"Queue item not found: {item_id}")

    cfg = config if config is not None else load_social_config(config_path)
    if notifier is None and not dry_run:
        notifier = _default_notifier()

    active_configured = configured_platforms(cfg)
    if not active_configured and not dry_run:
        if notifier and hasattr(notifier, "send_manual_post_alert"):
            notifier.send_manual_post_alert(item)
        update_status(item_id, "posted_alerted")
        return {}

    target_list = platforms if platforms is not None else list(SUPPORTED_PLATFORMS)
    max_retries = int(cfg.get("max_retries", 3))
    backoff = cfg.get("retry_backoff_minutes", [5, 15, 45])
    now_dt = datetime.now().astimezone()
    results: dict[str, PublishResult] = {}

    for plat in target_list:
        p_state = item.get("platforms", {}).get(plat, {})
        if platforms is None and not p_state.get("enabled", True):
            continue

        cur_plat_status = p_state.get("status", "pending")
        if cur_plat_status in TERMINAL_SUCCESS_STATUSES and not force:
            continue

        if cur_plat_status == "failed" and not ignore_backoff and not force and platforms is None:
            if int(p_state.get("attempts", 0)) >= max_retries:
                continue
            retry_at = p_state.get("next_retry_at")
            if retry_at and parse_scheduled_time(retry_at) > now_dt:
                continue

        if dry_run:
            configured_flag = is_platform_configured(plat, cfg)
            note = "configured" if configured_flag else "NOT configured (would skip)"
            results[plat] = PublishResult(
                platform=plat,
                ok=True,
                status="dry_run",
                message=f"[DRY-RUN] Would publish {item.get('clip_file')} to {plat} ({note})",
            )
            continue

        if not is_platform_configured(plat, cfg):
            res = PublishResult(
                platform=plat,
                ok=False,
                status="skipped",
                message=f"{plat} is not configured in social_config.json",
                retryable=False,
            )
            results[plat] = res
            record_platform_result(item_id, plat, res.to_dict(), max_retries=max_retries, backoff_minutes=backoff)
            continue

        pub = get_publisher(plat, cfg, config_path=config_path)
        try:
            res = pub.publish(
                clip_path=item["clip_file"],
                hook=item.get("hook", ""),
                caption=item.get("caption", ""),
                credit=item.get("credit", ""),
                scheduled_time=item.get("scheduled_time"),
            )
        except PublishError as exc:
            res = PublishResult(
                platform=plat,
                ok=False,
                status="failed",
                message=exc.message,
                retryable=exc.retryable,
            )
        except Exception as exc:
            res = PublishResult(
                platform=plat,
                ok=False,
                status="failed",
                message=str(exc),
                retryable=True,
            )

        results[plat] = res
        record_platform_result(item_id, plat, res.to_dict(), max_retries=max_retries, backoff_minutes=backoff)

    if results and not dry_run and notifier and hasattr(notifier, "send_publish_report"):
        updated = get_item(item_id)
        notifier.send_publish_report(updated, results)

    return results


def process_due_items(dry_run: bool = False, config=None, config_path=None, notifier=None) -> list[tuple[str, dict]]:
    """Process all due approved items and retry-eligible failed items under cross-process lock."""
    with scheduler_lock() as acquired:
        if not acquired:
            return []

        cfg = config if config is not None else load_social_config(config_path)
        max_retries = int(cfg.get("max_retries", 3))
        due = get_due_items(max_retries=max_retries)
        if not due:
            return []

        if notifier is None and not dry_run:
            notifier = _default_notifier()

        processed = []
        for item in due:
            res = publish_item(
                item["id"],
                dry_run=dry_run,
                config=cfg,
                config_path=config_path,
                notifier=notifier,
            )
            processed.append((item["id"], res))
        return processed


def format_status_table() -> str:
    """Return a readable table of all queued items and per-platform states."""
    q = load_queue()
    items = q.get("items", [])
    if not items:
        return "Queue is empty (no items in jobs/queue.json)."

    lines = [
        f"{'ID':<22} {'Scheduled Time':<24} {'Status':<15} {'Platforms'}",
        "-" * 92,
    ]
    short_labels = {"instagram": "IG", "youtube": "YT", "tiktok": "TT"}
    for it in items:
        plats = it.get("platforms", {})
        badges = []
        for p in SUPPORTED_PLATFORMS:
            pst = plats.get(p, {})
            lbl = short_labels[p]
            if not pst.get("enabled", True):
                badges.append(f"{lbl}:off")
            else:
                badges.append(f"{lbl}:{pst.get('status', 'pending')}")
        lines.append(
            f"{it.get('id', '?'):<22} {str(it.get('scheduled_time', '-')):<24} "
            f"{it.get('status', '?'):<15} {' | '.join(badges)}"
        )
    return "\n".join(lines)


def generate_systemd_units() -> str:
    """Return ready-to-install user systemd service & timer unit files."""
    repo_dir = Path.cwd().resolve()
    python_exe = Path(sys.executable).absolute()
    script_path = (Path(__file__).resolve()).relative_to(repo_dir) if Path(__file__).resolve().is_relative_to(repo_dir) else Path(__file__).resolve()

    service = f"""# ~/.config/systemd/user/gtavi-social-scheduler.service
[Unit]
Description=GTA 6 Native Social Media Scheduler (One-Shot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={repo_dir}
ExecStart={python_exe} {script_path} --once
"""
    timer = """# ~/.config/systemd/user/gtavi-social-scheduler.timer
[Unit]
Description=Run GTA 6 Native Social Media Scheduler every minute

[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
Persistent=true

[Install]
WantedBy=timers.target
"""
    instructions = """# Install & enable (100% free local background scheduling):
mkdir -p ~/.config/systemd/user
# Save the two sections above into their respective paths, then run:
systemctl --user daemon-reload
systemctl --user enable --now gtavi-social-scheduler.timer
systemctl --user status gtavi-social-scheduler.timer
"""
    return f"{service}\n{timer}\n{instructions}"


def generate_cron_line() -> str:
    """Return a ready-to-paste crontab entry."""
    repo_dir = Path.cwd().resolve()
    python_exe = Path(sys.executable).absolute()
    return f"*/2 * * * * cd {repo_dir} && {python_exe} scripts/social_scheduler.py --once >> jobs/scheduler.log 2>&1"


def main():
    ap = argparse.ArgumentParser(description="Native 100% Free Social Media Scheduler")
    ap.add_argument("--status", action="store_true", help="Print queue and per-platform status table")
    ap.add_argument("--once", action="store_true", help="Process all currently due scheduled items once")
    ap.add_argument("--daemon", action="store_true", help="Run scheduler continuously in a polling loop")
    ap.add_argument("--interval", type=float, default=30.0, help="Polling interval in seconds for --daemon")
    ap.add_argument("--publish-now", metavar="ITEM_ID", help="Publish a specific queue item immediately")
    ap.add_argument("--platforms", help="Comma-separated platforms (e.g. ig,yt,tt) for --publish-now")
    ap.add_argument("--retry", metavar="ITEM_ID", help="Reset failed/skipped platforms and retry immediately")
    ap.add_argument("--reschedule", nargs=2, metavar=("ITEM_ID", "ISO_TIME"), help="Reschedule an item")
    ap.add_argument("--dry-run", action="store_true", help="Preview actions without uploading or modifying queue")
    ap.add_argument("--force", action="store_true", help="Force re-upload even if already published")
    ap.add_argument("--print-systemd", action="store_true", help="Print Linux systemd --user timer configuration")
    ap.add_argument("--print-cron", action="store_true", help="Print crontab line")
    a = ap.parse_args()

    if a.print_systemd:
        print(generate_systemd_units())
        return
    if a.print_cron:
        print(generate_cron_line())
        return
    if a.status:
        print(format_status_table())
        return

    if a.reschedule:
        item_id, new_time = a.reschedule
        updated = reschedule_item(item_id, new_time)
        if not updated:
            sys.exit(f"Queue item not found: {item_id}")
        print(f"Rescheduled {item_id} -> {updated['scheduled_time']}")
        return

    if a.retry:
        if not a.dry_run:
            reset_failed_platforms(a.retry)
        results = publish_item(a.retry, dry_run=a.dry_run, notifier=False if a.dry_run else None, ignore_backoff=True)
        for plat, res in results.items():
            print(f"[{plat}] {res.status}: {res.message} {res.url or ''}".strip())
        return

    if a.publish_now:
        plats = _parse_platforms_arg(a.platforms)
        results = publish_item(
            a.publish_now,
            platforms=plats,
            dry_run=a.dry_run,
            notifier=False if a.dry_run else None,
            force=a.force,
            ignore_backoff=True,
        )
        if not results:
            print("No platforms executed (either none configured -> manual fallback triggered, or all skipped).")
        for plat, res in results.items():
            print(f"[{plat}] {res.status}: {res.message} {res.url or ''}".strip())
        return

    if a.once:
        processed = process_due_items(dry_run=a.dry_run, notifier=False if a.dry_run else None)
        if not processed:
            print("No due scheduled posts at this time.")
        for item_id, res_map in processed:
            print(f"Processed {item_id}:")
            for plat, res in res_map.items():
                print(f"  [{plat}] {res.status}: {res.message} {res.url or ''}".strip())
        return

    if a.daemon:
        print(f"Social Scheduler daemon started (interval={a.interval}s). Press Ctrl+C to stop.")
        while True:
            try:
                processed = process_due_items(dry_run=a.dry_run, notifier=False if a.dry_run else None)
                for item_id, res_map in processed:
                    summary = ", ".join(f"{k}={v.status}" for k, v in res_map.items()) or "manual_alert"
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Processed {item_id}: {summary}")
                time.sleep(a.interval)
            except KeyboardInterrupt:
                print("\nStopping scheduler daemon.")
                break
        return

    ap.print_help()


if __name__ == "__main__":
    main()
