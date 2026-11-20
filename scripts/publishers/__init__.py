"""Native 100% free social media publisher implementations."""
from typing import Optional

from .base import BasePublisher, PublishError, PublishResult
from .instagram import InstagramPublisher
from .tiktok import TikTokPublisher
from .youtube import YouTubePublisher

PUBLISHER_CLASSES = {
    "instagram": InstagramPublisher,
    "youtube": YouTubePublisher,
    "tiktok": TikTokPublisher,
}


def get_publisher(platform: str, config: Optional[dict] = None, config_path=None) -> BasePublisher:
    """Instantiate a platform publisher by name."""
    if config is None:
        try:
            from social_config import load_social_config
        except ImportError:
            from scripts.social_config import load_social_config
        config = load_social_config(config_path)

    cls = PUBLISHER_CLASSES.get(platform)
    if cls is None:
        raise ValueError(f"Unsupported platform: {platform}")
    return cls(config=config, config_path=config_path)


__all__ = [
    "BasePublisher",
    "InstagramPublisher",
    "PUBLISHER_CLASSES",
    "PublishError",
    "PublishResult",
    "TikTokPublisher",
    "YouTubePublisher",
    "get_publisher",
]
