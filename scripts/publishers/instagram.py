"""Instagram Reels publisher using Meta Graph API + direct rupload.facebook.com."""
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Optional
import requests

from .base import BasePublisher, PublishError, PublishResult, is_retryable_status, validate_video_file

try:
    from social_config import update_platform_config
except ImportError:
    from scripts.social_config import update_platform_config


def _safe_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}


def _extract_graph_error(resp: requests.Response) -> tuple[str, bool]:
    data = _safe_json(resp)
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or resp.text
        transient = bool(err.get("is_transient")) or is_retryable_status(resp.status_code)
        code = err.get("code")
        return f"Meta API error ({resp.status_code}, code={code}): {msg}", transient
    return f"Meta HTTP {resp.status_code}: {resp.text[:300]}", is_retryable_status(resp.status_code)


class InstagramPublisher(BasePublisher):
    """Publishes local MP4 clips directly to Instagram Reels via resumable upload."""

    platform_name = "instagram"

    def ensure_valid_token(self) -> str:
        token = self.p_cfg.get("access_token", "").strip()
        if not token:
            raise PublishError("Instagram access_token missing", retryable=False)

        obtained_str = (self.p_cfg.get("token_obtained_at") or "").strip()
        app_id = (self.p_cfg.get("app_id") or "").strip()
        app_secret = (self.p_cfg.get("app_secret") or "").strip()
        api_version = self.p_cfg.get("api_version", "v25.0")

        if obtained_str and app_id and app_secret:
            try:
                obtained_dt = datetime.fromisoformat(obtained_str)
                if obtained_dt.tzinfo is None:
                    obtained_dt = obtained_dt.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - obtained_dt).total_seconds() / 86400.0
                if age_days >= 30.0:
                    resp = requests.get(
                        f"https://graph.facebook.com/{api_version}/oauth/access_token",
                        params={
                            "grant_type": "fb_exchange_token",
                            "client_id": app_id,
                            "client_secret": app_secret,
                            "fb_exchange_token": token,
                        },
                        timeout=30,
                    )
                    if resp.status_code == 200:
                        data = _safe_json(resp)
                        new_token = data.get("access_token")
                        if new_token:
                            now_iso = datetime.now(timezone.utc).isoformat()
                            expires_in = int(data.get("expires_in", 5184000))
                            updated = update_platform_config(
                                "instagram",
                                {
                                    "access_token": new_token,
                                    "token_obtained_at": now_iso,
                                    "token_expires_in": expires_in,
                                },
                                self.config_path,
                            )
                            self.config["instagram"] = updated["instagram"]
                            return new_token
            except ValueError:
                pass
            except requests.RequestException:
                # Token may still be valid up to 60 days; fall back to existing token
                pass
        return token

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
        ig_user_id = (self.p_cfg.get("ig_user_id") or "").strip()
        if not ig_user_id:
            raise PublishError("Instagram ig_user_id missing", retryable=False)

        api_version = self.p_cfg.get("api_version", "v25.0")
        full_caption = (caption or hook or "").strip()[:2200]
        share_to_feed = "true" if self.p_cfg.get("share_to_feed", True) else "false"

        # Phase 1: Create resumable upload container
        init_url = f"https://graph.facebook.com/{api_version}/{ig_user_id}/media"
        try:
            init_resp = requests.post(
                init_url,
                data={
                    "media_type": "REELS",
                    "upload_type": "resumable",
                    "caption": full_caption,
                    "share_to_feed": share_to_feed,
                    "access_token": token,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise PublishError(f"Instagram container init failed: {exc}", retryable=True) from exc

        if init_resp.status_code not in (200, 201):
            msg, retryable = _extract_graph_error(init_resp)
            raise PublishError(msg, retryable=retryable, status_code=init_resp.status_code)

        init_data = _safe_json(init_resp)
        container_id = init_data.get("id")
        if not container_id:
            raise PublishError(f"Instagram container ID missing in response: {init_data}", retryable=False)
        upload_uri = init_data.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{api_version}/{container_id}"

        # Phase 2: Stream local MP4 binary directly to rupload.facebook.com
        headers = {
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(file_size),
            "Content-Type": "application/octet-stream",
        }
        try:
            with open(path, "rb") as fh:
                up_resp = requests.post(upload_uri, headers=headers, data=fh, timeout=300)
        except requests.RequestException as exc:
            raise PublishError(f"Instagram rupload failed: {exc}", retryable=True) from exc

        if up_resp.status_code not in (200, 201):
            msg, retryable = _extract_graph_error(up_resp)
            raise PublishError(msg, retryable=retryable, status_code=up_resp.status_code)

        up_data = _safe_json(up_resp)
        if up_data and up_data.get("success") is False:
            raise PublishError(f"Instagram binary upload rejected: {up_data}", retryable=False)

        # Phase 3: Poll container processing status
        poll_attempts = int(self.p_cfg.get("poll_attempts", 60))
        poll_interval = float(self.p_cfg.get("poll_interval_sec", 5))
        status_url = f"https://graph.facebook.com/{api_version}/{container_id}"
        finished = False
        for attempt in range(poll_attempts):
            try:
                st_resp = requests.get(
                    status_url,
                    params={"fields": "status_code,status", "access_token": token},
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise PublishError(f"Instagram status check failed: {exc}", retryable=True) from exc

            if st_resp.status_code != 200:
                msg, retryable = _extract_graph_error(st_resp)
                raise PublishError(msg, retryable=retryable, status_code=st_resp.status_code)

            st_data = _safe_json(st_resp)
            code = st_data.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                finished = True
                break
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(
                    f"Instagram container processing {code}: {st_data.get('status') or st_data}",
                    retryable=False,
                )
            if attempt + 1 < poll_attempts and poll_interval > 0:
                time.sleep(poll_interval)

        if not finished:
            raise PublishError("Timed out waiting for Instagram container processing", retryable=True)

        # Phase 4: Publish container and fetch live permalink
        pub_url = f"https://graph.facebook.com/{api_version}/{ig_user_id}/media_publish"
        try:
            pub_resp = requests.post(
                pub_url,
                data={"creation_id": container_id, "access_token": token},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise PublishError(f"Instagram media_publish failed: {exc}", retryable=True) from exc

        if pub_resp.status_code not in (200, 201):
            msg, retryable = _extract_graph_error(pub_resp)
            raise PublishError(msg, retryable=retryable, status_code=pub_resp.status_code)

        media_id = str(_safe_json(pub_resp).get("id") or "")
        if not media_id:
            raise PublishError("Instagram media_publish returned no media ID", retryable=False)

        permalink = "https://www.instagram.com/"
        try:
            perm_resp = requests.get(
                f"https://graph.facebook.com/{api_version}/{media_id}",
                params={"fields": "permalink", "access_token": token},
                timeout=20,
            )
            if perm_resp.status_code == 200:
                permalink = _safe_json(perm_resp).get("permalink") or permalink
        except requests.RequestException:
            pass

        return PublishResult(
            platform="instagram",
            ok=True,
            status="published",
            post_id=media_id,
            url=permalink,
            message="Published to Instagram Reels",
        )

    def verify_credentials(self) -> tuple[bool, str]:
        try:
            token = self.ensure_valid_token()
        except PublishError as exc:
            return False, exc.message

        ig_user_id = (self.p_cfg.get("ig_user_id") or "").strip()
        if not ig_user_id:
            return False, "Instagram ig_user_id missing"
        api_version = self.p_cfg.get("api_version", "v25.0")
        try:
            resp = requests.get(
                f"https://graph.facebook.com/{api_version}/{ig_user_id}",
                params={"fields": "id,username", "access_token": token},
                timeout=20,
            )
        except requests.RequestException as exc:
            return False, f"Network error: {exc}"
        if resp.status_code != 200:
            msg, _ = _extract_graph_error(resp)
            return False, msg
        data = _safe_json(resp)
        username = data.get("username") or ig_user_id
        return True, f"Connected as @{username} (id={data.get('id', ig_user_id)})"
