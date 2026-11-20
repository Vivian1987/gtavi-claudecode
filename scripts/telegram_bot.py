"""Telegram Approval & Notification Bot.

Sends rendered GTA 6 clips directly to your Telegram chat with inline Approve / Decline buttons.
Tracks approvals, updates queue state, and alerts you when it's time to post.
"""
import json, os, sys, time
from datetime import datetime
from pathlib import Path
try:
    import urllib3.util.connection as urllib_conn
    urllib_conn.HAS_IPV6 = False
except Exception:
    pass
import requests

sys.path.insert(0, str(Path(__file__).parent))
from smart_queue import load_queue, save_queue, update_status

CONFIG_FILE = Path("bot_config.json")


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
    CONFIG_FILE.write_text(json.dumps({
        "bot_token": token,
        "chat_id": str(chat_id)
    }, indent=2), encoding="utf-8")


class TelegramDesk:
    def __init__(self, token=None, chat_id=None):
        t, c = load_config()
        self.token = token or t
        self.chat_id = chat_id or c
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN not configured. Set it in bot_config.json or env variable.")
        self.api_base = f"https://api.telegram.org/bot{self.token}"

    def send_video_for_approval(self, queue_item):
        """Sends the MP4 file with inline Approve and Decline buttons."""
        clip_path = Path(queue_item["clip_file"])
        if not clip_path.exists():
            print(f"Error: clip file not found: {clip_path}")
            return None

        scheduled_dt = datetime.fromisoformat(queue_item["scheduled_time"])
        formatted_time = scheduled_dt.strftime("%a %b %d, %H:%M")

        caption = (
            f"🎬 *NEW GTA 6 CLIP READY*\n\n"
            f"🎯 *Hook:* {queue_item.get('hook', 'N/A')}\n"
            f"📝 *Caption:*\n{queue_item.get('caption', '')}\n\n"
            f"🎥 *Credit:* @{queue_item.get('credit', 'creator')}\n"
            f"⏰ *Optimal Scheduled Slot:* `{formatted_time}`\n"
            f"📊 *Source:* {queue_item.get('source_url', 'N/A')}\n"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"app_{queue_item['id']}"},
                    {"text": "❌ Decline", "callback_data": f"dec_{queue_item['id']}"}
                ]
            ]
        }

        url = f"{self.api_base}/sendVideo"
        with open(clip_path, "rb") as f:
            files = {"video": (clip_path.name, f, "video/mp4")}
            data = {
                "chat_id": self.chat_id,
                "caption": caption[:1024],  # Telegram caption max 1024 chars
                "parse_mode": "Markdown",
                "reply_markup": json.dumps(keyboard),
                "supports_streaming": "true"
            }
            res = requests.post(url, data=data, files=files, timeout=300)

        rjson = res.json()
        if rjson.get("ok"):
            msg_id = rjson["result"]["message_id"]
            update_status(queue_item["id"], "pending", {"telegram_message_id": msg_id})
            print(f"Sent clip to Telegram (message_id: {msg_id})")
            return msg_id
        else:
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

    def edit_caption(self, chat_id, message_id, new_caption, remove_keyboard=True):
        url = f"{self.api_base}/editMessageCaption"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": new_caption[:1024],
            "parse_mode": "Markdown"
        }
        if remove_keyboard:
            payload["reply_markup"] = json.dumps({"inline_keyboard": []})
        requests.post(url, json=payload, timeout=10)

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
            chat = msg.get("chat", {}).get("id")

            if data.startswith("app_"):
                item_id = data[4:]
                item = update_status(item_id, "approved")
                if item:
                    scheduled_dt = datetime.fromisoformat(item["scheduled_time"])
                    formatted_time = scheduled_dt.strftime("%a %b %d, %H:%M")
                    self.answer_callback(cb_id, f"✅ Approved for {formatted_time}!")
                    new_caption = (
                        f"✅ *APPROVED & SCHEDULED*\n\n"
                        f"⏰ *Publish Time:* `{formatted_time}`\n"
                        f"🎯 *Hook:* {item.get('hook')}\n"
                        f"🎥 *Credit:* @{item.get('credit')}\n"
                        f"📁 *File:* `{item.get('clip_file')}`"
                    )
                    self.edit_caption(chat, msg_id, new_caption)
                    print(f"Item {item_id} APPROVED.")
                else:
                    self.answer_callback(cb_id, "Item not found.")

            elif data.startswith("dec_"):
                item_id = data[4:]
                item = update_status(item_id, "declined")
                self.answer_callback(cb_id, "❌ Clip declined and archived.")
                new_caption = f"❌ *DECLINED & DISCARDED*\n(Item ID: {item_id})"
                self.edit_caption(chat, msg_id, new_caption)
                print(f"Item {item_id} DECLINED.")

        return next_offset

    def notify_scheduled_posts(self):
        """Checks if any approved post is due right now and alerts the user."""
        q = load_queue()
        now = datetime.now()
        for item in q.get("items", []):
            if item.get("status") == "approved":
                s_dt = datetime.fromisoformat(item["scheduled_time"])
                if s_dt <= now:
                    url = f"{self.api_base}/sendMessage"
                    text = (
                        f"🚨 *SCHEDULED POST TIME REACHED!* 🚨\n\n"
                        f"Your scheduled clip is ready to post:\n"
                        f"🎯 *Hook:* {item.get('hook')}\n"
                        f"📝 *Caption (tap to copy):*\n`{item.get('caption')}`\n\n"
                        f"🎥 *Credit:* @{item.get('credit')}\n"
                        f"📁 *Local File:* `{item.get('clip_file')}`"
                    )
                    requests.post(url, json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "Markdown"
                    }, timeout=10)
                    update_status(item["id"], "posted_alerted")
                    print(f"Post time reached for {item['id']}, alert sent!")


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
