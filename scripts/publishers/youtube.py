"""YouTube Shorts publisher using YouTube Data API v3 pure-HTTP resumable upload."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Optional
import requests

from .base import BasePublisher, PublishError, PublishResult, is_retryable_status, validate_video_file

try:
    from social_config import update_platform_config
except ImportError:
    from scripts.social_config import update_platform_config

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB (multiple of Google's required 256 KiB alignment)


def _safe_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}


def _extract_youtube_error(resp: requests.Response) -> tuple[str, bool]:
    data = _safe_json(resp)
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or resp.text
        return f"YouTube API error ({resp.status_code}): {msg}", is_retryable_status(resp.status_code)
    return f"YouTube HTTP {resp.status_code}: {resp.text[:300]}", is_retryable_status(resp.status_code)


def _parse_iso_utc(ts_str: str) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo
            dt = dt.replace(tzinfo=local_tz)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def format_youtube_short_metadata(hook: str, caption: str, credit: str, category_id: str = "20") -> dict:
    """Build YouTube Shorts snippet metadata respecting title (100 chars) and description limits."""
    hook_clean = " ".join((hook or "").split())
    if not hook_clean:
        for line in (caption or "").splitlines():
            stripped = line.strip()
            if stripped:
                hook_clean = stripped
                break
    if not hook_clean:
        hook_clean = "GTA 6 Clip"

    if "#shorts" not in hook_clean.lower():
        max_base = 100 - len(" #Shorts")
        if len(hook_clean) > max_base:
            hook_clean = hook_clean[: max_base - 1].rstrip() + "…"
        title = f"{hook_clean} #Shorts"
    else:
        title = hook_clean[:100]

    desc_parts = []
    cap_clean = (caption or "").strip()
    if cap_clean:
        desc_parts.append(cap_clean)
    else:
        desc_parts.append(hook_clean)
    if credit and f"@{credit}".lower() not in "\n".join(desc_parts).lower():
        desc_parts.append(f"Credit: @{credit}")
    description = "\n".join(desc_parts).strip()
    if "#shorts" not in description.lower():
        description = f"{description}\n#Shorts"
    description = description[:5000]

    raw_tags = re.findall(r"#(\w+)", description)
    tags = []
    seen = set()
    total_chars = 0
    for tag in raw_tags:
        low = tag.lower()
        if low in seen:
            continue
        if len(tags) >= 15 or total_chars + len(tag) > 400:
            break
        seen.add(low)
        tags.append(tag)
        total_chars += len(tag)
    if "Shorts" not in tags and len(tags) < 15:
        tags.append("Shorts")

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "categoryId": str(category_id or "20"),
    }


class YouTubePublisher(BasePublisher):
    """Publishes local MP4 clips as YouTube Shorts using Data API v3."""

    platform_name = "youtube"

    def ensure_valid_token(self) -> str:
        token = (self.p_cfg.get("access_token") or "").strip()
        expires_at = _parse_iso_utc((self.p_cfg.get("token_expires_at") or "").strip())

        if token and expires_at and expires_at > datetime.now(timezone.utc) + timedelta(seconds=120):
            return token

        refresh_token = (self.p_cfg.get("refresh_token") or "").strip()
        client_id = (self.p_cfg.get("client_id") or "").strip()
        client_secret = (self.p_cfg.get("client_secret") or "").strip()

        if refresh_token and client_id and client_secret:
            try:
                resp = requests.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise PublishError(f"YouTube OAuth refresh network error: {exc}", retryable=True) from exc

            if resp.status_code != 200:
                msg, retryable = _extract_youtube_error(resp)
                raise PublishError(f"YouTube OAuth refresh failed: {msg}", retryable=retryable, status_code=resp.status_code)

            data = _safe_json(resp)
            new_token = data.get("access_token")
            if not new_token:
                raise PublishError(f"YouTube OAuth refresh missing access_token: {data}", retryable=False)
            new_exp = (datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600)))).isoformat()
            updated = update_platform_config(
                "youtube",
                {"access_token": new_token, "token_expires_at": new_exp},
                self.config_path,
            )
            self.config["youtube"] = updated["youtube"]
            return new_token

        if token:
            return token
        raise PublishError("YouTube OAuth credentials incomplete (need refresh_token or access_token)", retryable=False)

    def publish(
        self,
        clip_path,
        hook: str,
        caption: str,
        credit: str,
        scheduled_time: Optional[str] = None,
        force_native_schedule: bool = False,
    ) -> PublishResult:
        path = Path(clip_path)
        file_size = validate_video_file(path)
        token = self.ensure_valid_token()

        snippet = format_youtube_short_metadata(hook, caption, credit, self.p_cfg.get("category_id", "20"))
        made_for_kids = bool(self.p_cfg.get("made_for_kids", False))

        sched_utc = _parse_iso_utc(scheduled_time or "")
        use_native = (bool(self.p_cfg.get("use_native_publish_at", False)) or force_native_schedule) and sched_utc is not None
        if use_native and sched_utc > datetime.now(timezone.utc) + timedelta(minutes=15):
            status_body = {
                "privacyStatus": "private",
                "publishAt": sched_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "selfDeclaredMadeForKids": made_for_kids,
            }
            result_status = "scheduled_native"
        else:
            status_body = {
                "privacyStatus": self.p_cfg.get("privacy_status", "private"),
                "selfDeclaredMadeForKids": made_for_kids,
            }
            result_status = "published"

        # Step 1: Initiate resumable upload session
        init_url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
        try:
            init_resp = requests.post(
                init_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": "video/mp4",
                    "X-Upload-Content-Length": str(file_size),
                },
                json={"snippet": snippet, "status": status_body},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise PublishError(f"YouTube resumable init network error: {exc}", retryable=True) from exc

        if init_resp.status_code not in (200, 201):
            msg, retryable = _extract_youtube_error(init_resp)
            raise PublishError(msg, retryable=retryable, status_code=init_resp.status_code)

        session_uri = init_resp.headers.get("Location") or init_resp.headers.get("location")
        if not session_uri:
            raise PublishError("YouTube resumable init response missing Location header", retryable=False)

        # Step 2: Upload binary in 256 KB-aligned chunks
        chunk_size = int(self.p_cfg.get("chunk_size", CHUNK_SIZE))
        vid_data = None
        with open(path, "rb") as fh:
            offset = 0
            while offset < file_size:
                fh.seek(offset)
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                end_byte = offset + len(chunk) - 1
                try:
                    put_resp = requests.put(
                        session_uri,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "video/mp4",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
                        },
                        data=chunk,
                        timeout=300,
                    )
                except requests.RequestException as exc:
                    raise PublishError(f"YouTube upload chunk failed at offset {offset}: {exc}", retryable=True) from exc

                if put_resp.status_code in (200, 201):
                    vid_data = _safe_json(put_resp)
                    break
                if put_resp.status_code == 308:
                    range_hdr = put_resp.headers.get("Range") or put_resp.headers.get("range")
                    if range_hdr and "-" in range_hdr:
                        offset = int(range_hdr.split("-")[-1]) + 1
                    else:
                        offset += len(chunk)
                    continue

                msg, retryable = _extract_youtube_error(put_resp)
                raise PublishError(msg, retryable=retryable, status_code=put_resp.status_code)

        if not vid_data or not vid_data.get("id"):
            raise PublishError("YouTube upload finished without returning a video ID", retryable=False)

        vid_id = str(vid_data["id"])
        url = f"https://youtube.com/shorts/{vid_id}"
        privacy = status_body.get("privacyStatus", "private")
        msg = (
            f"YouTube Short scheduled natively for {status_body['publishAt']}"
            if result_status == "scheduled_native"
            else f"Uploaded YouTube Short ({privacy})"
        )
        return PublishResult(
            platform="youtube",
            ok=True,
            status=result_status,
            post_id=vid_id,
            url=url,
            message=msg,
        )

    def verify_credentials(self) -> tuple[bool, str]:
        try:
            token = self.ensure_valid_token()
        except PublishError as exc:
            return False, exc.message

        try:
            resp = requests.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
        except requests.RequestException as exc:
            return False, f"Network error: {exc}"

        if resp.status_code != 200:
            msg, _ = _extract_youtube_error(resp)
            return False, msg

        data = _safe_json(resp)
        items = data.get("items") or []
        if not items:
            return True, "Authenticated (no YouTube channel found on account)"
        ch = items[0]
        title = ch.get("snippet", {}).get("title", "Unknown")
        return True, f"Connected to channel '{title}' (id={ch.get('id')})"
