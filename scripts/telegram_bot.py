"""Telegram Approval, Per-Platform Toggle & Native Dispatch Bot.

Sends rendered GTA 6 clips directly to your Telegram chat with inline
per-platform toggles (IG / YT / TT) and Approve / Post Now / Decline buttons.
Tracks approvals, delegates native scheduled uploads to social_scheduler, and
falls back to manual post alerts if no platforms are configured yet.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import threading
import time

try:
    import urllib3.util.connection as urllib_conn
    urllib_conn.HAS_IPV6 = False
except Exception:
    pass
import requests

sys.path.insert(0, str(Path(__file__).parent))
from smart_queue import (
    SUPPORTED_PLATFORMS,
    get_item,
    parse_scheduled_time,
    reschedule_item,
    reset_failed_platforms,
    toggle_platform,
    update_status,
)

CONFIG_FILE = Path("bot_config.json")

_TOGGLE_PREFIXES = {
    "ig": "instagram",
    "yt": "youtube",
    "tt": "tiktok",
}
_PLATFORM_DISPLAY = {
    "instagram": "Instagram Reels",
    "youtube": "YouTube Shorts",
    "tiktok": "TikTok",
}
_PLATFORM_SHORT = {
    "instagram": ("ig", "IG"),
    "youtube": ("yt", "YT"),
    "tiktok": ("tt", "TT"),
}


def _md_safe(text: str) -> str:
    """Escape characters that break Telegram legacy Markdown parsing."""
    s = str(text or "")
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s


def load_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            token = token or cfg.get("bot_token")
            chat_id = chat_id or cfg.get("chat_id")
        except Exception:
            pass
    return token, chat_id


def save_config(token, chat_id):
    CONFIG_FILE.write_text(
        json.dumps({"bot_token": token, "chat_id": str(chat_id)}, indent=2),
        encoding="utf-8",
    )


def format_enabled_platforms(queue_item: dict) -> str:
    plats = queue_item.get("platforms", {})
    names = [_PLATFORM_DISPLAY[p] for p in SUPPORTED_PLATFORMS if plats.get(p, {}).get("enabled", True)]
    return ", ".join(names) if names else "None"


def build_approval_keyboard(queue_item: dict) -> dict:
    """Build inline keyboard with per-platform toggles (Row 1) and actions (Row 2)."""
    plats = queue_item.get("platforms", {})
    item_id = queue_item["id"]
    toggle_row = []
    for plat in SUPPORTED_PLATFORMS:
        short_code, label = _PLATFORM_SHORT[plat]
        enabled = bool(plats.get(plat, {}).get("enabled", True))
        icon = "✅" if enabled else "⬜"
        toggle_row.append({"text": f"{icon} {label}", "callback_data": f"tog_{short_code}_{item_id}"})

    action_row = [
        {"text": "✅ Approve", "callback_data": f"app_{item_id}"},
        {"text": "⚡ Post Now", "callback_data": f"now_{item_id}"},
        {"text": "❌ Decline", "callback_data": f"dec_{item_id}"},
    ]
    return {"inline_keyboard": [toggle_row, action_row]}


class TelegramDesk:
    def __init__(self, token=None, chat_id=None):
        t, c = load_config()
        self.token = token or t
        self.chat_id = chat_id or c
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN not configured. Set it in bot_config.json or env variable.")
        self.api_base = f"https://api.telegram.org/bot{self.token}"
        self._scheduler_thread = None
        self._sync_scheduler = False

    def _run_background(self, fn, *args, **kwargs):
        if self._sync_scheduler:
            return fn(*args, **kwargs)
        t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
        t.start()
        return t

    def send_video_for_approval(self, queue_item):
        """Send the MP4 file with inline platform toggles and Approve/Post Now/Decline buttons."""
        clip_path = Path(queue_item["clip_file"])
        if not clip_path.exists():
            print(f"Error: clip file not found: {clip_path}")
            return None

        scheduled_dt = parse_scheduled_time(queue_item["scheduled_time"])
        formatted_time = scheduled_dt.strftime("%a %b %d, %H:%M")
        target_plats = format_enabled_platforms(queue_item)

        caption = (
            f"🎬 *NEW GTA 6 CLIP READY*\n\n"
            f"🎯 *Hook:* {_md_safe(queue_item.get('hook', 'N/A'))}\n"
            f"📝 *Caption:*\n{_md_safe(queue_item.get('caption', ''))}\n\n"
            f"🎥 *Credit:* @{_md_safe(queue_item.get('credit', 'creator'))}\n"
            f"⏰ *Optimal Scheduled Slot:* `{formatted_time}`\n"
            f"🌐 *Platforms:* {target_plats}\n"
            f"📊 *Source:* {queue_item.get('source_url', 'N/A')}\n"
        )

        keyboard = build_approval_keyboard(queue_item)
        url = f"{self.api_base}/sendVideo"
        with open(clip_path, "rb") as f:
            files = {"video": (clip_path.name, f, "video/mp4")}
            data = {
                "chat_id": self.chat_id,
                "caption": caption[:1024],
                "parse_mode": "Markdown",
                "reply_markup": json.dumps(keyboard),
                "supports_streaming": "true",
            }
            res = requests.post(url, data=data, files=files, timeout=300)

        rjson = res.json()
        if rjson.get("ok"):
            msg_id = rjson["result"]["message_id"]
            update_status(queue_item["id"], "pending", {"telegram_message_id": msg_id})
            print(f"Sent clip to Telegram (message_id: {msg_id})")
            return msg_id
        print(f"Failed to send to Telegram: {rjson}")
        return None

    def poll_updates(self, offset=None):
        url = f"{self.api_base}/getUpdates"
        params = {"timeout": 15}
        if offset:
            params["offset"] = offset
        try:
            r = requests.get(url, params=params, timeout=20)
            return r.json().get("result", [])
        except Exception as e:
            print(f"Polling error: {e}")
            return []

    def answer_callback(self, callback_id, text):
        url = f"{self.api_base}/answerCallbackQuery"
        requests.post(url, json={"callback_query_id": callback_id, "text": text}, timeout=10)

    def edit_caption(self, chat_id, message_id, new_caption, remove_keyboard=True, reply_markup=None):
        url = f"{self.api_base}/editMessageCaption"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": new_caption[:1024],
            "parse_mode": "Markdown",
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        elif remove_keyboard:
            payload["reply_markup"] = json.dumps({"inline_keyboard": []})
        requests.post(url, json=payload, timeout=10)

    def edit_reply_markup(self, chat_id, message_id, keyboard):
        url = f"{self.api_base}/editMessageReplyMarkup"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": json.dumps(keyboard),
        }
        requests.post(url, json=payload, timeout=10)

    def _safe_pre_schedule(self, item_id):
        try:
            import social_scheduler
            social_scheduler.pre_schedule_native(item_id, notifier=self)
        except Exception as exc:
            print(f"[Pre-schedule error for {item_id}]: {exc}")

    def _safe_publish_now(self, item_id):
        try:
            import social_scheduler
            social_scheduler.publish_item(item_id, notifier=self, ignore_backoff=True)
        except Exception as exc:
            print(f"[Publish-now error for {item_id}]: {exc}")

    def process_updates(self, offset=None):
        updates = self.poll_updates(offset)
        next_offset = offset
        for u in updates:
            next_offset = u["update_id"] + 1
            cb = u.get("callback_query")
            if not cb:
                continue

            data = cb.get("data", "")
            cb_id = cb.get("id")
            msg = cb.get("message", {})
            msg_id = msg.get("message_id")
            chat = msg.get("chat", {}).get("id") or self.chat_id

            if data.startswith("tog_"):
                parts = data.split("_", 2)
                if len(parts) == 3 and parts[1] in _TOGGLE_PREFIXES:
                    plat_key = _TOGGLE_PREFIXES[parts[1]]
                    item_id = parts[2]
                    updated = toggle_platform(item_id, plat_key)
                    if updated:
                        is_on = updated["platforms"][plat_key]["enabled"]
                        self.answer_callback(cb_id, f"{_PLATFORM_DISPLAY[plat_key]}: {'ON' if is_on else 'OFF'}")
                        self.edit_reply_markup(chat, msg_id, build_approval_keyboard(updated))
                    else:
                        self.answer_callback(cb_id, "Item not found.")

            elif data.startswith("app_"):
                item_id = data[4:]
                item = update_status(item_id, "approved")
                if item:
                    scheduled_dt = parse_scheduled_time(item["scheduled_time"])
                    formatted_time = scheduled_dt.strftime("%a %b %d, %H:%M")
                    target_plats = format_enabled_platforms(item)
                    self.answer_callback(cb_id, f"✅ Approved for {formatted_time}!")
                    new_caption = (
                        f"✅ *APPROVED & SCHEDULED*\n\n"
                        f"⏰ *Publish Time:* `{formatted_time}`\n"
                        f"🌐 *Platforms:* {target_plats}\n"
                        f"🎯 *Hook:* {_md_safe(item.get('hook'))}\n"
                        f"🎥 *Credit:* @{_md_safe(item.get('credit'))}\n"
                        f"📁 *File:* `{item.get('clip_file')}`"
                    )
                    self.edit_caption(chat, msg_id, new_caption)
                    self._run_background(self._safe_pre_schedule, item_id)
                    print(f"Item {item_id} APPROVED ({target_plats}).")
                else:
                    self.answer_callback(cb_id, "Item not found.")

            elif data.startswith("now_"):
                item_id = data[4:]
                now_iso = datetime.now().astimezone().isoformat(timespec="minutes")
                item = reschedule_item(item_id, now_iso, approve=True)
                if item:
                    target_plats = format_enabled_platforms(item)
                    self.answer_callback(cb_id, "⚡ Publishing now...")
                    new_caption = (
                        f"⏳ *PUBLISHING NOW...*\n\n"
                        f"🌐 *Platforms:* {target_plats}\n"
                        f"🎯 *Hook:* {_md_safe(item.get('hook'))}\n"
                        f"📁 *File:* `{item.get('clip_file')}`"
                    )
                    self.edit_caption(chat, msg_id, new_caption)
                    self._run_background(self._safe_publish_now, item_id)
                    print(f"Item {item_id} triggered for immediate publish.")
                else:
                    self.answer_callback(cb_id, "Item not found.")

            elif data.startswith("ret_"):
                item_id = data[4:]
                item = reset_failed_platforms(item_id)
                if item:
                    self.answer_callback(cb_id, "🔄 Retrying failed platforms...")
                    self._run_background(self._safe_publish_now, item_id)
                    print(f"Item {item_id} queued for retry.")
                else:
                    self.answer_callback(cb_id, "Item not found.")

            elif data.startswith("dec_"):
                item_id = data[4:]
                update_status(item_id, "declined")
                self.answer_callback(cb_id, "❌ Clip declined and archived.")
                new_caption = f"❌ *DECLINED & DISCARDED*\n(Item ID: {item_id})"
                self.edit_caption(chat, msg_id, new_caption)
                print(f"Item {item_id} DECLINED.")

        return next_offset

    def send_manual_post_alert(self, item):
        """Fallback reminder when no social platform APIs are configured yet."""
        url = f"{self.api_base}/sendMessage"
        text = (
            f"🚨 *SCHEDULED POST TIME REACHED!* 🚨\n\n"
            f"Your scheduled clip is ready to post:\n"
            f"🎯 *Hook:* {_md_safe(item.get('hook'))}\n"
            f"📝 *Caption (tap to copy):*\n`{item.get('caption')}`\n\n"
            f"🎥 *Credit:* @{_md_safe(item.get('credit'))}\n"
            f"📁 *Local File:* `{item.get('clip_file')}`"
        )
        requests.post(
            url,
            json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        print(f"Post time reached for {item['id']}, manual alert sent!")

    def send_publish_report(self, item, results: dict):
        """Send a concise multi-platform publish outcome message to Telegram."""
        if not item:
            return
        lines = [
            f"📣 *PUBLISH REPORT* (`{item.get('id')}`)",
            f"🎯 *Hook:* {_md_safe(item.get('hook'))}",
            "",
        ]
        icons = {
            "published": "✅",
            "scheduled_native": "🗓️",
            "inbox_sent": "📥",
            "skipped": "⏭️",
            "failed": "❌",
        }
        for plat, res in results.items():
            label = _PLATFORM_DISPLAY.get(plat, plat.title())
            icon = icons.get(res.status, "ℹ️")
            if res.ok:
                link_part = f" — {res.url}" if res.url else ""
                lines.append(f"{icon} *{label}:* {_md_safe(res.message)}{link_part}")
            else:
                lines.append(f"{icon} *{label}:* {_md_safe(res.message)}")

        payload = {
            "chat_id": self.chat_id,
            "text": "\n".join(lines)[:4096],
            "parse_mode": "Markdown",
        }
        if item.get("status") in ("partial_failed", "failed"):
            payload["reply_markup"] = json.dumps(
                {"inline_keyboard": [[{"text": "🔄 Retry Failed", "callback_data": f"ret_{item['id']}"}]]}
            )
        requests.post(f"{self.api_base}/sendMessage", json=payload, timeout=10)

    def _run_due_scheduler(self):
        try:
            import social_scheduler
            social_scheduler.process_due_items(notifier=self)
        except Exception as exc:
            print(f"[Scheduler Worker Error]: {exc}")

    def notify_scheduled_posts(self):
        """Trigger due scheduled posts via social_scheduler without blocking Telegram polling."""
        if self._sync_scheduler:
            self._run_due_scheduler()
            return
        if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
            return
        self._scheduler_thread = threading.Thread(target=self._run_due_scheduler, daemon=True)
        self._scheduler_thread.start()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", action="store_true", help="Start polling for user button clicks")
    a = ap.parse_args()

    desk = TelegramDesk()
    print("Telegram Desk listening for button clicks... (Press Ctrl+C to stop)")
    offset = None
    while True:
        offset = desk.process_updates(offset)
        desk.notify_scheduled_posts()
        time.sleep(2)
