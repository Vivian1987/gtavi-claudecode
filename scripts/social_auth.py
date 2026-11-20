"""Interactive 100% Free OAuth Setup & Token Health CLI.

Supports:
  - Instagram Reels (Meta Graph API Development Mode + 60-day token exchange)
  - YouTube Shorts (Google Desktop OAuth2 + RFC 7636 PKCE)
  - TikTok (Login Kit for Desktop OAuth2 + hex-encoded SHA-256 PKCE)

usage:
  python scripts/social_auth.py status [--verify]
  python scripts/social_auth.py setup instagram
  python scripts/social_auth.py setup youtube
  python scripts/social_auth.py setup tiktok [--mode inbox|direct]
  python scripts/social_auth.py refresh
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser
import requests

sys.path.insert(0, str(Path(__file__).parent))

from publishers import PublishError, get_publisher
from publishers.tiktok import generate_tiktok_desktop_pkce
from social_config import PLATFORMS, is_platform_configured, load_social_config, update_platform_config


def generate_rfc7636_pkce() -> tuple[str, str]:
    """Generate standard (code_verifier, base64url SHA-256 code_challenge)."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    verifier = "".join(secrets.choice(alphabet) for _ in range(64))
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def wait_for_oauth_callback(port: int = 8765, expected_path: str = "/callback", timeout: int = 180) -> dict:
    """Start a temporary localhost HTTP server and capture OAuth redirect query params."""
    captured: dict = {}

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.rstrip("/") != expected_path.rstrip("/"):
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(parsed.query)
            for k, v in qs.items():
                captured[k] = v[0] if v else ""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:sans-serif;padding:2rem;'>"
                b"<h2>Authorization received!</h2>"
                b"<p>You may close this tab and return to your terminal.</p>"
                b"</body></html>"
            )

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = timeout
    try:
        while not captured:
            server.handle_request()
            if not captured:
                break
    finally:
        server.server_close()
    return captured


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or default


def setup_instagram(args):
    cfg = load_social_config()
    ig = cfg.get("instagram", {})
    api_version = getattr(args, "api_version", None) or ig.get("api_version", "v25.0")

    print("\n=== Instagram Reels Free Setup (Meta Graph API - Development Mode) ===")
    print("Prerequisites (100% free):")
    print("  1. Switch your Instagram account to Business or Creator & link it to a Facebook Page.")
    print("  2. Create a free app at https://developers.facebook.com/ (leave in Development Mode).")
    print("  3. In Graph API Explorer, generate a User Token with permissions:")
    print("     instagram_business_basic, instagram_business_content_publish, pages_show_list, pages_read_engagement\n")

    app_id = args.app_id or _prompt("Meta App ID (for 60-day token exchange/refresh)", ig.get("app_id", ""))
    app_secret = args.app_secret or _prompt("Meta App Secret", ig.get("app_secret", ""))
    raw_token = args.token or _prompt("Paste User Access Token")
    if not raw_token:
        sys.exit("Error: Access token is required.")

    token = raw_token
    expires_in = 5184000
    if app_id and app_secret:
        print("Exchanging short-lived token for 60-day long-lived token...")
        resp = requests.get(
            f"https://graph.facebook.com/{api_version}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": raw_token,
            },
            timeout=30,
        )
        if resp.status_code == 200 and resp.json().get("access_token"):
            data = resp.json()
            token = data["access_token"]
            expires_in = int(data.get("expires_in", 5184000))
            print(f"Long-lived token obtained (valid ~{expires_in // 86400} days).")
        else:
            print(f"Warning: token exchange returned {resp.status_code} ({resp.text[:200]}). Using pasted token directly.")

    ig_user_id = args.ig_user_id or ig.get("ig_user_id", "")
    if not ig_user_id:
        print("Discovering linked Instagram Business/Creator accounts...")
        acct_resp = requests.get(
            f"https://graph.facebook.com/{api_version}/me/accounts",
            params={"fields": "id,name,instagram_business_account{id,username}", "access_token": token},
            timeout=30,
        )
        candidates = []
        if acct_resp.status_code == 200:
            for page in acct_resp.json().get("data", []):
                iba = page.get("instagram_business_account")
                if iba and iba.get("id"):
                    candidates.append((iba["id"], iba.get("username", "unknown"), page.get("name", "Page")))

        if len(candidates) == 1:
            ig_user_id = candidates[0][0]
            print(f"Found Instagram account @{candidates[0][1]} (id={ig_user_id}) linked to '{candidates[0][2]}'.")
        elif len(candidates) > 1:
            for idx, (uid, uname, pname) in enumerate(candidates, 1):
                print(f"  {idx}. @{uname} (id={uid}) via {pname}")
            choice = int(_prompt("Select account number", "1"))
            ig_user_id = candidates[choice - 1][0]
        else:
            ig_user_id = _prompt("Enter Instagram User ID manually")

    if not ig_user_id:
        sys.exit("Error: Instagram User ID is required.")

    update_platform_config(
        "instagram",
        {
            "enabled": True,
            "api_version": api_version,
            "app_id": app_id,
            "app_secret": app_secret,
            "ig_user_id": ig_user_id,
            "access_token": token,
            "token_obtained_at": datetime.now(timezone.utc).isoformat(),
            "token_expires_in": expires_in,
        },
    )
    print("Saved Instagram configuration to social_config.json!")


def setup_youtube(args):
    cfg = load_social_config()
    yt = cfg.get("youtube", {})

    print("\n=== YouTube Shorts Free Setup (YouTube Data API v3) ===")
    print("Prerequisites (100% free):")
    print("  1. Create a free Google Cloud project & enable YouTube Data API v3.")
    print("  2. Create a Desktop App OAuth Client ID.\n")

    client_id = args.client_id or _prompt("Google OAuth Client ID", yt.get("client_id", ""))
    client_secret = args.client_secret or _prompt("Google OAuth Client Secret", yt.get("client_secret", ""))
    if not client_id or not client_secret:
        sys.exit("Error: client_id and client_secret are required.")

    port = args.port
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    verifier, challenge = generate_rfc7636_pkce()
    state = secrets.token_urlsafe(16)

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    print(f"\nOpen this URL in your browser to authorize YouTube:\n{auth_url}\n")
    if not args.no_browser:
        webbrowser.open(auth_url)

    code = None
    if args.manual:
        pasted = _prompt("Paste the full redirect URL or authorization code")
        if pasted.startswith("http"):
            code = parse_qs(urlparse(pasted).query).get("code", [""])[0]
        else:
            code = pasted
    else:
        print(f"Waiting for OAuth callback on {redirect_uri} ...")
        cb = wait_for_oauth_callback(port=port, expected_path="/callback")
        if cb.get("error"):
            sys.exit(f"OAuth error: {cb['error']}")
        if cb.get("state") and cb.get("state") != state:
            sys.exit("OAuth state mismatch.")
        code = cb.get("code")

    if not code:
        sys.exit("Error: No authorization code received.")

    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if token_resp.status_code != 200:
        sys.exit(f"Token exchange failed ({token_resp.status_code}): {token_resp.text}")

    tdata = token_resp.json()
    access_token = tdata["access_token"]
    refresh_token = tdata.get("refresh_token") or yt.get("refresh_token", "")
    expires_at = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + int(tdata.get("expires_in", 3600)),
        tz=timezone.utc,
    ).isoformat()

    channel_id = yt.get("channel_id", "")
    ch_resp = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if ch_resp.status_code == 200 and ch_resp.json().get("items"):
        ch = ch_resp.json()["items"][0]
        channel_id = ch["id"]
        print(f"Authorized YouTube channel: {ch['snippet']['title']} ({channel_id})")

    update_platform_config(
        "youtube",
        {
            "enabled": True,
            "client_id": client_id,
            "client_secret": client_secret,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_expires_at": expires_at,
            "channel_id": channel_id,
        },
    )
    print("Saved YouTube configuration to social_config.json!")


def setup_tiktok(args):
    cfg = load_social_config()
    tt = cfg.get("tiktok", {})

    print("\n=== TikTok Free Setup (Login Kit for Desktop + Content Posting API v2) ===")
    print("Prerequisites (100% free):")
    print("  1. Create a Desktop App in TikTok Developer Portal with Login Kit + Content Posting API.")
    print(f"  2. Register Redirect URI: http://127.0.0.1:{args.port}/callback/ (or wildcard port).\n")

    client_key = args.client_key or _prompt("TikTok Client Key", tt.get("client_key", ""))
    client_secret = args.client_secret or _prompt("TikTok Client Secret", tt.get("client_secret", ""))
    mode = (args.mode or _prompt("Posting mode (inbox=no audit needed, direct=audited app)", tt.get("mode", "inbox"))).lower()
    if mode not in ("inbox", "direct"):
        sys.exit("Error: mode must be 'inbox' or 'direct'.")
    if not client_key or not client_secret:
        sys.exit("Error: client_key and client_secret are required.")

    port = args.port
    redirect_uri = args.redirect_uri or f"http://127.0.0.1:{port}/callback/"
    verifier, challenge = generate_tiktok_desktop_pkce()
    state = secrets.token_urlsafe(16)
    scope = "user.info.basic,video.upload" if mode == "inbox" else "user.info.basic,video.upload,video.publish"

    params = {
        "client_key": client_key,
        "response_type": "code",
        "scope": scope,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"https://www.tiktok.com/v2/auth/authorize/?{urlencode(params)}"
    print(f"\nOpen this URL in your browser to authorize TikTok:\n{auth_url}\n")
    if not args.no_browser:
        webbrowser.open(auth_url)

    code = None
    if args.manual:
        pasted = _prompt("Paste the full redirect URL or authorization code")
        if pasted.startswith("http"):
            code = parse_qs(urlparse(pasted).query).get("code", [""])[0]
        else:
            code = pasted
    else:
        parsed_redir = urlparse(redirect_uri)
        print(f"Waiting for OAuth callback on {redirect_uri} ...")
        cb = wait_for_oauth_callback(port=parsed_redir.port or port, expected_path=parsed_redir.path or "/callback/")
        if cb.get("error"):
            sys.exit(f"TikTok OAuth error: {cb.get('error_description') or cb['error']}")
        if cb.get("state") and cb.get("state") != state:
            sys.exit("TikTok OAuth state mismatch.")
        code = cb.get("code")

    if not code:
        sys.exit("Error: No authorization code received.")

    token_resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    if token_resp.status_code != 200:
        sys.exit(f"TikTok token exchange failed ({token_resp.status_code}): {token_resp.text}")

    tdata = token_resp.json()
    if tdata.get("error") or not tdata.get("access_token"):
        sys.exit(f"TikTok token error: {tdata}")

    expires_at = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + int(tdata.get("expires_in", 86400)),
        tz=timezone.utc,
    ).isoformat()

    update_platform_config(
        "tiktok",
        {
            "enabled": True,
            "client_key": client_key,
            "client_secret": client_secret,
            "access_token": tdata["access_token"],
            "refresh_token": tdata.get("refresh_token", ""),
            "open_id": tdata.get("open_id", ""),
            "token_expires_at": expires_at,
            "mode": mode,
            "redirect_uri": redirect_uri,
        },
    )
    print(f"Saved TikTok configuration (mode={mode}) to social_config.json!")


def cmd_status(verify: bool = False):
    cfg = load_social_config()
    print("=== Social Publisher Configuration Status ===")
    for plat in PLATFORMS:
        pcfg = cfg.get(plat, {})
        configured = is_platform_configured(plat, cfg)
        enabled = bool(pcfg.get("enabled"))
        extra = ""
        if plat == "instagram":
            extra = f"api={pcfg.get('api_version')} user={pcfg.get('ig_user_id') or '-'}"
        elif plat == "youtube":
            extra = f"privacy={pcfg.get('privacy_status')} native_publish_at={pcfg.get('use_native_publish_at')}"
        elif plat == "tiktok":
            extra = f"mode={pcfg.get('mode')} privacy={pcfg.get('privacy_level')}"

        state_str = "READY" if configured else ("ENABLED (incomplete creds)" if enabled else "DISABLED")
        print(f"  - {plat:<10} : {state_str:<26} | {extra}")
        if verify and configured:
            ok, msg = get_publisher(plat, cfg).verify_credentials()
            print(f"      verify -> {'OK' if ok else 'FAIL'}: {msg}")


def cmd_refresh():
    cfg = load_social_config()
    for plat in PLATFORMS:
        if not is_platform_configured(plat, cfg):
            continue
        pub = get_publisher(plat, cfg)
        try:
            pub.ensure_valid_token()
            print(f"  - {plat}: token valid / refreshed")
        except PublishError as exc:
            print(f"  - {plat}: FAIL ({exc.message})")


def main():
    ap = argparse.ArgumentParser(description="Free OAuth Setup & Token Manager for Social Publishers")
    sub = ap.add_subparsers(dest="cmd")

    st = sub.add_parser("status", help="Show configuration status for IG, YT, and TikTok")
    st.add_argument("--verify", action="store_true", help="Run live read-only credential checks")

    sub.add_parser("refresh", help="Refresh access tokens for configured platforms")

    setup_p = sub.add_parser("setup", help="Configure free OAuth credentials for a platform")
    setup_sub = setup_p.add_subparsers(dest="platform")

    ig_p = setup_sub.add_parser("instagram", help="Configure Instagram Reels (Meta Graph API)")
    ig_p.add_argument("--app-id", default="")
    ig_p.add_argument("--app-secret", default="")
    ig_p.add_argument("--token", default="")
    ig_p.add_argument("--ig-user-id", default="")
    ig_p.add_argument("--api-version", default="v25.0")

    yt_p = setup_sub.add_parser("youtube", help="Configure YouTube Shorts (Data API v3)")
    yt_p.add_argument("--client-id", default="")
    yt_p.add_argument("--client-secret", default="")
    yt_p.add_argument("--port", type=int, default=8765)
    yt_p.add_argument("--no-browser", action="store_true")
    yt_p.add_argument("--manual", action="store_true")

    tt_p = setup_sub.add_parser("tiktok", help="Configure TikTok (Content Posting API v2)")
    tt_p.add_argument("--client-key", default="")
    tt_p.add_argument("--client-secret", default="")
    tt_p.add_argument("--mode", choices=["inbox", "direct"], default="")
    tt_p.add_argument("--port", type=int, default=8765)
    tt_p.add_argument("--redirect-uri", default="")
    tt_p.add_argument("--no-browser", action="store_true")
    tt_p.add_argument("--manual", action="store_true")

    a = ap.parse_args()
    if a.cmd == "status":
        cmd_status(verify=a.verify)
    elif a.cmd == "refresh":
        cmd_refresh()
    elif a.cmd == "setup":
        if a.platform == "instagram":
            setup_instagram(a)
        elif a.platform == "youtube":
            setup_youtube(a)
        elif a.platform == "tiktok":
            setup_tiktok(a)
        else:
            setup_p.print_help()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
