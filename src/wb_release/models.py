"""Data models shared by the fetcher, state machine and renderer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .config import Channel

_BUILD_RE = re.compile(r"-([0-9a-fA-F]{6,40})\.(deb|rpm)$", re.IGNORECASE)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_timestamp(value: int | float | None) -> str | None:
    if value is None:
        return None
    try:
        return iso(datetime.fromtimestamp(float(value), tz=timezone.utc))
    except (ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class Snapshot:
    """The current upstream state of one platform channel."""

    platform: str
    version: str
    product_version: str
    url: str
    filename: str
    build: str
    upstream_sha256: str
    timestamp: int | None
    supports_fast_update: bool

    @property
    def released_at(self) -> str | None:
        return from_timestamp(self.timestamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "version": self.version,
            "product_version": self.product_version,
            "url": self.url,
            "filename": self.filename,
            "build": self.build,
            "upstream_sha256": self.upstream_sha256,
            "release_timestamp": self.timestamp,
            "released_at": self.released_at,
            "supports_fast_update": self.supports_fast_update,
        }


def build_from_filename(filename: str) -> str:
    """Extract the build hash from ``...-5.6.2.40046327-c7b525f3.deb``."""

    match = _BUILD_RE.search(filename)
    return match.group(1).lower() if match else ""


@dataclass(frozen=True)
class Change:
    """A detected difference between the saved state and upstream."""

    channel: Channel
    snapshot: Snapshot
    kind: str  # bootstrap | upgrade | downgrade | rebuild | force
    previous_version: str | None = None
    previous_url: str | None = None


@dataclass(frozen=True)
class Release:
    """One GitHub Release to be published."""

    tag: str
    version: str
    build: str
    kind: str
    changes: tuple[Change, ...] = field(default_factory=tuple)

    @property
    def title(self) -> str:
        return f"WorkBuddy Linux {self.tag}"


def safe_tag(value: str) -> str:
    """Make a string safe for use as a git tag / release tag."""

    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-.")
    return cleaned or "unknown"


def filename_from_url(url: str) -> str:
    return urlparse(url).path.rsplit("/", 1)[-1]
