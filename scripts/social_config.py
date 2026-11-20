"""Thread-safe and process-safe configuration management for social media publishers."""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import tempfile

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

PLATFORMS = ("instagram", "youtube", "tiktok")
_DEFAULT_FILENAME = "social_config.json"
CONFIG_FILE = Path(os.environ.get("SOCIAL_CONFIG_FILE", _DEFAULT_FILENAME))

DEFAULT_CONFIG = {
    "default_platforms": ["instagram", "youtube", "tiktok"],
    "max_retries": 3,
    "retry_backoff_minutes": [5, 15, 45],
    "instagram": {
        "enabled": False,
        "api_version": "v25.0",
        "app_id": "",
        "app_secret": "",
        "ig_user_id": "",
        "access_token": "",
        "token_obtained_at": "",
        "token_expires_in": 5184000,
        "share_to_feed": True,
    },
    "youtube": {
        "enabled": False,
        "client_id": "",
        "client_secret": "",
        "access_token": "",
        "refresh_token": "",
        "token_expires_at": "",
        "channel_id": "",
        "category_id": "20",
        "privacy_status": "private",
        "use_native_publish_at": False,
        "made_for_kids": False,
    },
    "tiktok": {
        "enabled": False,
        "client_key": "",
        "client_secret": "",
        "access_token": "",
        "refresh_token": "",
        "token_expires_at": "",
        "open_id": "",
        "mode": "inbox",
        "privacy_level": "SELF_ONLY",
        "disable_duet": False,
        "disable_comment": False,
        "disable_stitch": False,
        "redirect_uri": "http://127.0.0.1:8765/callback/",
    },
}


def resolve_config_path(path=None):
    """Resolve the config file path from explicit arg, env var, or module default."""
    if path is not None:
        return Path(path)
    env_path = os.environ.get("SOCIAL_CONFIG_FILE")
    if env_path and str(CONFIG_FILE) == _DEFAULT_FILENAME:
        return Path(env_path)
    return Path(CONFIG_FILE)


@contextmanager
def _config_lock(config_path: Path):
    """Acquire an exclusive advisory lock around config reads/writes."""
    lock_path = config_path.parent / f".{config_path.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield
        return
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def deep_merge(base, override):
    """Recursively merge override into a deep copy of base."""
    result = copy.deepcopy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _parse_bool(val):
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def apply_env_overrides(cfg):
    """Apply optional environment variable overrides to a config copy."""
    out = copy.deepcopy(cfg)
    env_map = {
        "instagram": {
            "IG_ACCESS_TOKEN": ("access_token", str),
            "IG_USER_ID": ("ig_user_id", str),
            "IG_APP_ID": ("app_id", str),
            "IG_APP_SECRET": ("app_secret", str),
            "IG_API_VERSION": ("api_version", str),
            "IG_ENABLED": ("enabled", _parse_bool),
        },
        "youtube": {
            "YT_CLIENT_ID": ("client_id", str),
            "YT_CLIENT_SECRET": ("client_secret", str),
            "YT_ACCESS_TOKEN": ("access_token", str),
            "YT_REFRESH_TOKEN": ("refresh_token", str),
            "YT_PRIVACY_STATUS": ("privacy_status", str),
            "YT_ENABLED": ("enabled", _parse_bool),
        },
        "tiktok": {
            "TIKTOK_CLIENT_KEY": ("client_key", str),
            "TIKTOK_CLIENT_SECRET": ("client_secret", str),
            "TIKTOK_ACCESS_TOKEN": ("access_token", str),
            "TIKTOK_REFRESH_TOKEN": ("refresh_token", str),
            "TIKTOK_MODE": ("mode", str),
            "TIKTOK_PRIVACY_LEVEL": ("privacy_level", str),
            "TIKTOK_ENABLED": ("enabled", _parse_bool),
        },
    }
    for platform, mapping in env_map.items():
        section = out.setdefault(platform, {})
        for env_name, (key, caster) in mapping.items():
            val = os.environ.get(env_name)
            if val is not None and val != "":
                section[key] = caster(val)
    return out


def _load_raw_config(target: Path):
    if not target.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return copy.deepcopy(DEFAULT_CONFIG)
    return deep_merge(DEFAULT_CONFIG, raw)


def _write_atomic(target: Path, cfg: dict):
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(payload)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def load_social_config(path=None):
    """Load social configuration merged with defaults and environment overrides."""
    target = resolve_config_path(path)
    raw = _load_raw_config(target)
    return apply_env_overrides(raw)


def save_social_config(cfg, path=None):
    """Persist social configuration atomically with restrictive permissions."""
    target = resolve_config_path(path)
    merged = deep_merge(DEFAULT_CONFIG, cfg)
    with _config_lock(target):
        _write_atomic(target, merged)
    return apply_env_overrides(merged)


def update_platform_config(platform, patch, path=None):
    """Atomically read-modify-write a single platform configuration section."""
    target = resolve_config_path(path)
    with _config_lock(target):
        current = _load_raw_config(target)
        section = current.setdefault(platform, {})
        section.update(patch)
        _write_atomic(target, current)
    return apply_env_overrides(current)


def is_platform_configured(platform, cfg=None):
    """Return True if a platform is enabled and has usable credentials."""
    cfg = cfg if cfg is not None else load_social_config()
    p = cfg.get(platform, {})
    if not p.get("enabled"):
        return False
    if platform == "instagram":
        return bool(p.get("ig_user_id") and p.get("access_token"))
    if platform == "youtube":
        return bool((p.get("client_id") and p.get("client_secret") and p.get("refresh_token")) or p.get("access_token"))
    if platform == "tiktok":
        return bool((p.get("client_key") and p.get("client_secret") and p.get("refresh_token")) or p.get("access_token"))
    return False


def configured_platforms(cfg=None):
    """Return list of configured and enabled platforms."""
    cfg = cfg if cfg is not None else load_social_config()
    return [p for p in PLATFORMS if is_platform_configured(p, cfg)]
