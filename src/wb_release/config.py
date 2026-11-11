"""Static configuration for the WorkBuddy Linux release tracker.

The upstream update endpoint is undocumented and may change at any time; every
runtime-facing value is collected here so a change only touches one file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: The undocumented update endpoint described in the reference article.
API_URL_TEMPLATE = "https://copilot.tencent.com/v2/update?platform={platform}"

#: Only these hosts are accepted for the package download URL returned by the API.
DEFAULT_ALLOWED_DOWNLOAD_HOSTS = ("download.codebuddy.cn",)

#: Source article, kept for attribution.
ARTICLE_URL = "https://xingwangzhe.fun/posts/workbuddy-linux-deb-rpm/"
ARTICLE_TITLE = "WorkBuddy 没给 Ubuntu/Fedora 留下载入口？官方的 deb 和 rpm 都找到了"
ARTICLE_LICENSE = "CC BY-NC-SA 4.0"

#: Hard cap for a single package download (bytes). ~1.2 GiB.
DEFAULT_MAX_DOWNLOAD_BYTES = 1_200_000_000


@dataclass(frozen=True)
class Channel:
    """One of the four Linux platform channels served by the update endpoint."""

    platform: str
    arch: str
    pkg: str
    distro: str
    order: int

    @property
    def label(self) -> str:
        return f"{self.arch} / {self.pkg}"

    @property
    def api_url(self) -> str:
        return API_URL_TEMPLATE.format(platform=self.platform)


CHANNELS: tuple[Channel, ...] = (
    Channel(
        platform="workbuddy-linux-x64-deb",
        arch="x86_64 (amd64)",
        pkg="deb",
        distro="Ubuntu / Debian / UOS / deepin / openKylin",
        order=1,
    ),
    Channel(
        platform="workbuddy-linux-x64-rpm",
        arch="x86_64 (amd64)",
        pkg="rpm",
        distro="Fedora / RHEL / openSUSE",
        order=2,
    ),
    Channel(
        platform="workbuddy-linux-arm64-deb",
        arch="ARM64 (aarch64)",
        pkg="deb",
        distro="Ubuntu / Debian / UOS / deepin / openKylin",
        order=3,
    ),
    Channel(
        platform="workbuddy-linux-arm64-rpm",
        arch="ARM64 (aarch64)",
        pkg="rpm",
        distro="Fedora / RHEL / openSUSE",
        order=4,
    ),
)

CHANNELS_BY_PLATFORM = {c.platform: c for c in CHANNELS}


def allowed_download_hosts() -> frozenset[str]:
    """Return the allowlist, optionally extended with ``WB_ALLOWED_DOWNLOAD_HOSTS``.

    If Tencent ever moves the packages to a new CDN, set that repository
    variable (comma separated) instead of disabling URL validation.
    """

    raw = os.environ.get("WB_ALLOWED_DOWNLOAD_HOSTS", "")
    extra = tuple(host.strip().lower() for host in raw.split(",") if host.strip())
    return frozenset(host.lower() for host in (*DEFAULT_ALLOWED_DOWNLOAD_HOSTS, *extra))
