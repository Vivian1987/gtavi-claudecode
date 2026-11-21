"""TikTok Content Posting API v2 publisher (Inbox Drafts + Direct Post)."""
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import secrets
import time
from typing import Optional
import requests

from .base import BasePublisher, PublishError, PublishResult, is_retryable_status, validate_video_file

try:
    from social_config import update_platform_config
except ImportError:
    from scripts.social_config import update_platform_config

MIB = 1024 * 1024
SINGLE_CHUNK_MAX = 64 * MIB
DEFAULT_MULTI_CHUNK_SIZE = 10 * MIB


def _safe_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}


def _extract_tiktok_error(resp: requests.Response) -> tuple[str, bool]:
    data = _safe_json(resp)
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict) and err.get("code") not in (None, "", "ok"):
        code = err.get("code")
        msg = err.get("message") or resp.text
        retryable = is_retryable_status(resp.status_code) or code in ("rate_limit_exceeded", "internal_error")
        return f"TikTok API error ({resp.status_code}, code={code}): {msg}", retryable
    if isinstance(data, dict) and data.get("error"):
        return f"TikTok OAuth error ({resp.status_code}): {data.get('error_description') or data.get('error')}", is_retryable_status(resp.status_code)
    return f"TikTok HTTP {resp.status_code}: {resp.text[:300]}", is_retryable_status(resp.status_code)


def generate_tiktok_desktop_pkce() -> tuple[str, str]:
    """Generate (code_verifier, hex-encoded SHA-256 code_challenge) for TikTok Login Kit for Desktop."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    verifier = "".join(secrets.choice(alphabet) for _ in range(64))
    challenge = hashlib.sha256(verifier.encode("utf-8")).hexdigest()
    return verifier, challenge


def plan_tiktok_chunks(file_size: int) -> tuple[int, int, list[tuple[int, int]]]:
    """Compute TikTok-compliant (chunk_size, total_chunk_count, byte_ranges) for FILE_UPLOAD.

    TikTok rules:
      - <= 64 MiB: single chunk equal to file_size, total_chunk_count = 1.
      - > 64 MiB: chunk_size = 10 MiB, total_chunk_count = floor(file_size / chunk_size),
        with the final chunk absorbing trailing remainder bytes (<= 128 MiB).
    """
    if file_size <= 0:
        raise ValueError("file_size must be > 0")
    if file_size <= SINGLE_CHUNK_MAX:
        return file_size, 1, [(0, file_size - 1)]

    chunk_size = DEFAULT_MULTI_CHUNK_SIZE
    total_chunk_count = file_size // chunk_size
    ranges: list[tuple[int, int]] = []
    for idx in range(total_chunk_count):
        start = idx * chunk_size
        end = (file_size - 1) if idx == total_chunk_count - 1 else ((idx + 1) * chunk_size - 1)
        ranges.append((start, end))
    return chunk_size, total_chunk_count, ranges


def _parse_iso_utc(ts_str: str) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


class TikTokPublisher(BasePublisher):
    """Publishes local MP4 clips to TikTok Inbox Drafts or Direct Post."""

    platform_name = "tiktok"

    def ensure_valid_token(self) -> str:
        token = (self.p_cfg.get("access_token") or "").strip()
        expires_at = _parse_iso_utc((self.p_cfg.get("token_expires_at") or "").strip())

        if token and expires_at and expires_at > datetime.now(timezone.utc) + timedelta(seconds=120):
            return token

        refresh_token = (self.p_cfg.get("refresh_token") or "").strip()
        client_key = (self.p_cfg.get("client_key") or "").strip()
        client_secret = (self.p_cfg.get("client_secret") or "").strip()

        if refresh_token and client_key and client_secret:
            try:
                resp = requests.post(
                    "https://open.tiktokapis.com/v2/oauth/token/",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={
                        "client_key": client_key,
                        "client_secret": client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise PublishError(f"TikTok OAuth refresh network error: {exc}", retryable=True) from exc

            if resp.status_code != 200:
                msg, retryable = _extract_tiktok_error(resp)
                raise PublishError(f"TikTok token refresh failed: {msg}", retryable=retryable, status_code=resp.status_code)

            data = _safe_json(resp)
            if data.get("error") or not data.get("access_token"):
                msg, retryable = _extract_tiktok_error(resp)
                raise PublishError(msg, retryable=retryable, status_code=resp.status_code)

            new_access = data["access_token"]
            # TikTok rotates refresh_token on each refresh; persist immediately
            new_refresh = data.get("refresh_token") or refresh_token
            open_id = data.get("open_id") or self.p_cfg.get("open_id", "")
            new_exp = (datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 86400)))).isoformat()
            updated = update_platform_config(
                "tiktok",
                {
                    "access_token": new_access,
                    "refresh_token": new_refresh,
                    "open_id": open_id,
                    "token_expires_at": new_exp,
                },
                self.config_path,
            )
            self.config["tiktok"] = updated["tiktok"]
            return new_access

        if token:
            return token
        raise PublishError("TikTok OAuth credentials incomplete (need refresh_token or access_token)", retryable=False)

    def _query_creator_info(self, token: str) -> dict:
        resp = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={},
            timeout=20,
        )
        if resp.status_code != 200:
            msg, retryable = _extract_tiktok_error(resp)
            raise PublishError(msg, retryable=retryable, status_code=resp.status_code)
        data = _safe_json(resp)
        err = data.get("error", {})
        if err.get("code") not in (None, "", "ok"):
            msg, retryable = _extract_tiktok_error(resp)
            raise PublishError(msg, retryable=retryable)
        return data.get("data", {})

    def publish(
        self,
        clip_path,
        hook: str,
        caption: str,
        credit: str,
        scheduled_time: Optional[str] = None,
    ) -> PublishResult:
        path = Path(clip_path)
        file_size = validate_video_file(path)
        token = self.ensure_valid_token()
        mode = (self.p_cfg.get("mode") or "inbox").strip().lower()

        chunk_size, total_chunks, ranges = plan_tiktok_chunks(file_size)
        source_info = {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        }
        api_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

        # Step 1: Initialize upload session
        try:
            if mode == "direct":
                creator = self._query_creator_info(token)
                desired_privacy = (self.p_cfg.get("privacy_level") or "SELF_ONLY").strip()
                options = creator.get("privacy_level_options") or []
                if options and desired_privacy not in options:
                    desired_privacy = "SELF_ONLY" if "SELF_ONLY" in options else options[0]
                init_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
                payload = {
                    "post_info": {
                        "title": (caption or hook or "").strip()[:2200],
                        "privacy_level": desired_privacy,
                        "disable_duet": bool(self.p_cfg.get("disable_duet", False)),
                        "disable_comment": bool(self.p_cfg.get("disable_comment", False)),
                        "disable_stitch": bool(self.p_cfg.get("disable_stitch", False)),
                        "video_cover_timestamp_ms": 1000,
                    },
                    "source_info": source_info,
                }
            else:
                init_url = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
                payload = {"source_info": source_info}

            init_resp = requests.post(init_url, headers=api_headers, json=payload, timeout=30)
        except requests.RequestException as exc:
            raise PublishError(f"TikTok upload init network error: {exc}", retryable=True) from exc

        if init_resp.status_code not in (200, 201):
            msg, retryable = _extract_tiktok_error(init_resp)
            raise PublishError(msg, retryable=retryable, status_code=init_resp.status_code)

        init_json = _safe_json(init_resp)
        err_obj = init_json.get("error", {})
        if err_obj.get("code") not in (None, "", "ok"):
            msg, retryable = _extract_tiktok_error(init_resp)
            raise PublishError(msg, retryable=retryable)

        init_data = init_json.get("data", {})
        publish_id = init_data.get("publish_id")
        upload_url = init_data.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishError(f"TikTok init missing publish_id/upload_url: {init_json}", retryable=False)

        # Step 2: Upload sequential chunks via PUT
        with open(path, "rb") as fh:
            for start, end in ranges:
                fh.seek(start)
                length = end - start + 1
                chunk = fh.read(length)
                try:
                    put_resp = requests.put(
                        upload_url,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {start}-{end}/{file_size}",
                        },
                        data=chunk,
                        timeout=300,
                    )
                except requests.RequestException as exc:
                    raise PublishError(f"TikTok chunk upload failed at byte {start}: {exc}", retryable=True) from exc

                if put_resp.status_code not in (200, 201, 206):
                    raise PublishError(
                        f"TikTok chunk upload HTTP {put_resp.status_code}: {put_resp.text[:250]}",
                        retryable=is_retryable_status(put_resp.status_code),
                        status_code=put_resp.status_code,
                    )

        # Step 3: Poll publish status
        poll_attempts = int(self.p_cfg.get("poll_attempts", 30))
        poll_interval = float(self.p_cfg.get("poll_interval_sec", 4))
        last_status = "PROCESSING_UPLOAD"
        for attempt in range(poll_attempts):
            try:
                st_resp = requests.post(
                    "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
                    headers=api_headers,
                    json={"publish_id": publish_id},
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise PublishError(f"TikTok status fetch failed: {exc}", retryable=True) from exc

            if st_resp.status_code != 200:
                msg, retryable = _extract_tiktok_error(st_resp)
                raise PublishError(msg, retryable=retryable, status_code=st_resp.status_code)

            st_json = _safe_json(st_resp)
            st_data = st_json.get("data", {})
            last_status = st_data.get("status", last_status)

            if last_status == "SEND_TO_USER_INBOX":
                return PublishResult(
                    platform="tiktok",
                    ok=True,
                    status="inbox_sent",
                    post_id=publish_id,
                    url="https://www.tiktok.com/notifications",
                    message="Sent to TikTok app inbox (open TikTok mobile app to finish posting)",
                )
            if last_status == "PUBLISH_COMPLETE":
                pub_ids = st_data.get("publicaly_available_post_id") or []
                post_id = str(pub_ids[0]) if pub_ids else publish_id
                url = f"https://www.tiktok.com/@me/video/{post_id}" if pub_ids else "https://www.tiktok.com/"
                return PublishResult(
                    platform="tiktok",
                    ok=True,
                    status="published",
                    post_id=post_id,
                    url=url,
                    message="Published directly to TikTok",
                )
            if last_status == "FAILED":
                reason = st_data.get("fail_reason") or "unknown"
                raise PublishError(f"TikTok publish failed: {reason}", retryable=False)

            if attempt + 1 < poll_attempts and poll_interval > 0:
                time.sleep(poll_interval)

        fallback_status = "inbox_sent" if mode == "inbox" else "published"
        return PublishResult(
            platform="tiktok",
            ok=True,
            status=fallback_status,
            post_id=publish_id,
            url="https://www.tiktok.com/notifications" if mode == "inbox" else "https://www.tiktok.com/",
            message=f"TikTok upload completed (status={last_status})",
        )

    def verify_credentials(self) -> tuple[bool, str]:
        try:
            token = self.ensure_valid_token()
        except PublishError as exc:
            return False, exc.message

        mode = (self.p_cfg.get("mode") or "inbox").strip().lower()
        try:
            resp = requests.get(
                "https://open.tiktokapis.com/v2/user/info/",
                params={"fields": "open_id,display_name"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
            if resp.status_code == 200:
                data = _safe_json(resp).get("data", {}).get("user", {})
                if data:
                    name = data.get("display_name") or data.get("open_id")
                    return True, f"Connected as {name} (mode={mode})"
        except requests.RequestException as exc:
            return False, f"Network error: {exc}"

        if mode == "direct":
            try:
                creator = self._query_creator_info(token)
                nick = creator.get("creator_nickname") or creator.get("creator_username") or "creator"
                return True, f"Connected as @{nick} (mode=direct)"
            except PublishError as exc:
                return False, exc.message

        return True, f"Token valid (mode={mode})"
