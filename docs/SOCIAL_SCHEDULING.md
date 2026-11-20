# Native 100% Free Social Media Scheduling

This repository supports **100% free, native scheduling and publishing** for **Instagram Reels**, **YouTube Shorts**, and **TikTok** using each platform's official free API—with **no Buffer/Zapier fees**, **no paid cloud storage**, and **no extra pip dependencies**.

---

## How It Works

1. `scripts/auto_pipeline.py` cuts 1080×1920 vertical clips, checks rules via `scripts/check.py`, and queues them in `jobs/queue.json` at optimal peak slots (`12:30`, `17:45`, `21:15`, max 3/day, ≥3.5h spacing).
2. `scripts/telegram_bot.py` sends the clip preview with **per-platform toggles** (`✅ IG`, `✅ YT`, `✅ TT`) plus `✅ Approve`, `⚡ Post Now`, and `❌ Decline`.
3. `scripts/social_scheduler.py` dispatches due clips directly from disk to each enabled platform:
   - **Instagram Reels:** Streams MP4 bytes directly to Meta's `rupload.facebook.com` and publishes publicly.
   - **YouTube Shorts:** Streams 256 KB-aligned resumable chunks to YouTube Data API v3.
   - **TikTok:** Streams chunked `FILE_UPLOAD` bytes to TikTok Content Posting API v2.
4. If no platform credentials are configured yet in `social_config.json`, the scheduler gracefully falls back to sending the original Telegram manual-post alert.

---

## Free Platform Setup

Copy the template (optional—`social_auth.py setup` creates `social_config.json` automatically):

```bash
cp social_config.json.example social_config.json
```

> `social_config.json` is listed in `.gitignore` and saved with `0600` file permissions so tokens stay private.

### 1. Instagram Reels (Publishes Publicly Immediately — Free)

Meta allows apps in **Development Mode** to publish public Reels to accounts owned by App Admins, Developers, or Instagram Testers **without submitting for Meta App Review**.

1. Switch your Instagram account to **Business** or **Creator** and link it to a Facebook Page.
2. Create a free app at [Meta for Developers](https://developers.facebook.com/) (keep it in **Development Mode**).
3. Open **Graph API Explorer**, select your app, generate a User Access Token with:
   - `instagram_business_basic`
   - `instagram_business_content_publish`
   - `pages_show_list`
   - `pages_read_engagement`
4. Run the setup wizard (automatically exchanges for a 60-day token and finds your `ig_user_id`):
   ```bash
   .venv/bin/python scripts/social_auth.py setup instagram
   ```
   *(Tokens older than 30 days are auto-refreshed whenever a job runs or via `social_auth.py refresh`.)*

### 2. YouTube Shorts (Free Daily Upload Quota)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a free project and enable **YouTube Data API v3**.
2. Configure OAuth Consent Screen (External, add your Google account under **Test Users**) and create a **Desktop App** OAuth 2.0 Client ID.
3. Run:
   ```bash
   .venv/bin/python scripts/social_auth.py setup youtube
   ```
4. **Private vs Public / `publishAt`:**
   - Google locks `videos.insert` uploads from unverified API projects to `private` until you pass Google's free **YouTube API Services Compliance Audit** (`"privacy_status": "private"` and `"use_native_publish_at": false` by default).
   - Once audited, update `"privacy_status": "public"` (and optionally `"use_native_publish_at": true`) in `social_config.json`.

### 3. TikTok (Free — Inbox Mode Requires No Audit)

1. In [TikTok for Developers](https://developers.tiktok.com/), create a free **Desktop App** (`Login Kit for Desktop` + `Content Posting API`).
2. Add Redirect URI: `http://127.0.0.1:8765/callback/` (or `http://127.0.0.1:*/callback/`).
3. Run:
   ```bash
   .venv/bin/python scripts/social_auth.py setup tiktok --mode inbox
   ```
4. **Inbox Mode vs Direct Mode:**
   - **`inbox` (default, zero audit needed):** Uploads the rendered MP4 straight into your TikTok mobile app inbox at the exact scheduled time (`SEND_TO_USER_INBOX`). Just tap the TikTok notification and hit **Post**.
   - **`direct` (after passing TikTok's free audit):** Set `"mode": "direct"` and `"privacy_level": "PUBLIC_TO_EVERYONE"` in `social_config.json` for hands-free public posting.

---

## Verify Credentials & Manage the Queue

```bash
# Check configured platforms & run read-only live checks
.venv/bin/python scripts/social_auth.py status --verify

# View queue slots and per-platform status
.venv/bin/python scripts/social_scheduler.py --status

# Dry-run due items (safe preview)
.venv/bin/python scripts/social_scheduler.py --once --dry-run

# Publish a specific item immediately to selected platforms
.venv/bin/python scripts/social_scheduler.py --publish-now clip_1_1795195996 --platforms ig,yt

# Retry failed platforms on an item
.venv/bin/python scripts/social_scheduler.py --retry clip_1_1795195996
```

---

## Keep Scheduled Slots Firing Automatically (100% Free)

Choose any option:

1. **Alongside the full pipeline:** `auto_pipeline.py --loop` runs Telegram polling and `social_scheduler` automatically.
2. **Linux `systemd --user` timer (recommended when pipeline isn't running):**
   ```bash
   .venv/bin/python scripts/social_scheduler.py --print-systemd
   ```
3. **Standard `crontab`:**
   ```bash
   .venv/bin/python scripts/social_scheduler.py --print-cron
   ```
