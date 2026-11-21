"""Base classes, result structures, and helpers for native social publishers."""
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class PublishResult:
    """Normalized outcome from publishing a clip to a social platform."""

    platform: str
    ok: bool
    status: str  # "published" | "inbox_sent" | "scheduled_native" | "failed" | "skipped" | "dry_run"
    post_id: Optional[str] = None
    url: Optional[str] = None
    message: str = ""
    retryable: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class PublishError(Exception):
    """Raised when a platform API request fails."""

    def __init__(self, message: str, retryable: bool = False, status_code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


def is_retryable_status(status_code: Optional[int]) -> bool:
    """Return True if an HTTP status code represents a transient failure."""
    if status_code is None:
        return True
    return status_code in (408, 425, 429, 500, 502, 503, 504)


def validate_video_file(clip_path: Path) -> int:
    """Verify the local video file exists and is non-empty, returning byte size."""
    p = Path(clip_path)
    if not p.exists() or not p.is_file():
        raise PublishError(f"Video file not found: {p}", retryable=False)
    size = p.stat().st_size
    if size <= 0:
        raise PublishError(f"Video file is empty: {p}", retryable=False)
    return size


class BasePublisher(ABC):
    """Abstract publisher contract implemented by each platform client."""

    platform_name: str = ""

    def __init__(self, config: dict, config_path: Optional[Path] = None):
        self.config = config
        self.config_path = config_path

    @property
    def p_cfg(self) -> dict:
        return self.config.get(self.platform_name, {})

    def is_configured(self) -> bool:
        try:
            from social_config import is_platform_configured
        except ImportError:
            from scripts.social_config import is_platform_configured
        return is_platform_configured(self.platform_name, self.config)

    @abstractmethod
    def ensure_valid_token(self) -> str:
        """Return a valid access token, refreshing if needed."""

    @abstractmethod
    def publish(
        self,
        clip_path,
        hook: str,
        caption: str,
        credit: str,
        scheduled_time: Optional[str] = None,
    ) -> PublishResult:
        """Upload and publish (or schedule/inbox-deliver) the clip."""

    @abstractmethod
    def verify_credentials(self) -> tuple[bool, str]:
        """Run a lightweight read-only check against the platform API."""
