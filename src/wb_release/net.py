"""Upstream HTTP access: update API, HEAD probing and optional checksum downloads.

Everything talks through an injectable ``opener`` object so the whole pipeline can
be exercised offline in CI (``--fixtures``) and in unit tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .config import CHANNELS, Channel, allowed_download_hosts
from .models import Snapshot, build_from_filename, filename_from_url

LOG = logging.getLogger("wb_release.net")

USER_AGENT = "workbuddy-linux-releases/1.0 (+github-actions; release tracker)"
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,63}$")


class FetchError(Exception):
    """Raised when the upstream response is unusable."""


def default_opener() -> Any:
    return urllib.request.build_opener()


def _retry_open(
    opener: Any,
    request: urllib.request.Request,
    *,
    timeout: float,
    retries: int,
    sleeper: Callable[[float], None],
) -> Any:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return opener.open(request, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - surface as FetchError below
            last_error = exc
            if attempt < retries:
                sleeper(min(2 ** attempt, 5))
    raise FetchError(f"request failed after {retries + 1} attempts: {last_error}")


def http_get_json(
    url: str,
    *,
    opener: Any | None = None,
    timeout: float = 20,
    retries: int = 2,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    opener = opener or default_opener()
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    response = _retry_open(opener, request, timeout=timeout, retries=retries, sleeper=sleeper)
    try:
        raw = response.read(2_000_000)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError(f"upstream returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise FetchError("upstream JSON is not an object")
    return payload


def parse_payload(
    channel: Channel,
    payload: dict[str, Any],
    *,
    hosts: frozenset[str] | None = None,
) -> Snapshot:
    """Validate one API response and turn it into a :class:`Snapshot`.

    The download URL is checked against an allowlist: the release body links
    straight to whatever upstream returns, so a hijacked/odd URL must not be
    published without an explicit configuration change.
    """

    hosts = hosts or allowed_download_hosts()
    if "error" in payload and payload["error"]:
        raise FetchError(f"upstream error for {channel.platform}: {payload['error']}")

    version = payload.get("version") or payload.get("productVersion")
    url = payload.get("url")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise FetchError(f"{channel.platform}: missing/invalid version: {version!r}")
    if not isinstance(url, str) or not url.strip():
        raise FetchError(f"{channel.platform}: missing download url")

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FetchError(f"{channel.platform}: download url is not https: {url!r}")
    if parsed.hostname.lower() not in hosts:
        raise FetchError(
            f"{channel.platform}: download host {parsed.hostname!r} is not allowlisted "
            f"({', '.join(sorted(hosts))}); set WB_ALLOWED_DOWNLOAD_HOSTS if the CDN moved"
        )
    if not parsed.path.lower().endswith(f".{channel.pkg}"):
        raise FetchError(
            f"{channel.platform}: expected a .{channel.pkg} url, got {parsed.path!r}"
        )

    timestamp_raw = payload.get("timestamp")
    try:
        timestamp = int(timestamp_raw) if timestamp_raw is not None else None
    except (TypeError, ValueError):
        timestamp = None

    raw_hash = payload.get("sha256hash") or ""
    filename = filename_from_url(url)
    return Snapshot(
        platform=channel.platform,
        version=version,
        product_version=str(payload.get("productVersion") or version),
        url=url,
        filename=filename,
        build=build_from_filename(filename),
        upstream_sha256=raw_hash if isinstance(raw_hash, str) else "",
        timestamp=timestamp,
        supports_fast_update=bool(payload.get("supportsFastUpdate", False)),
    )


def fetch_all(
    *,
    opener: Any | None = None,
    fixtures: dict[str, Any] | None = None,
    channels: tuple[Channel, ...] = CHANNELS,
    timeout: float = 20,
    retries: int = 2,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Snapshot], dict[str, str]]:
    """Return ``(snapshots, errors)``. One broken channel never blocks the rest."""

    opener = opener or default_opener()
    snapshots: dict[str, Snapshot] = {}
    errors: dict[str, str] = {}
    for channel in channels:
        try:
            if fixtures is not None:
                if channel.platform not in fixtures:
                    raise FetchError(f"missing fixture for {channel.platform}")
                payload = fixtures[channel.platform]
                if not isinstance(payload, dict):
                    raise FetchError("fixture is not a JSON object")
            else:
                payload = http_get_json(
                    channel.api_url,
                    opener=opener,
                    timeout=timeout,
                    retries=retries,
                    sleeper=sleeper,
                )
            snapshots[channel.platform] = parse_payload(channel, payload)
        except Exception as exc:  # noqa: BLE001 - isolate per-channel failures
            errors[channel.platform] = str(exc)
            LOG.warning("channel %s failed: %s", channel.platform, exc)
    return snapshots, errors


def probe_url(
    url: str,
    *,
    opener: Any | None = None,
    timeout: float = 20,
    retries: int = 1,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Cheap HEAD request for size / etag / last-modified; never fatal."""

    opener = opener or default_opener()
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT}, method="HEAD"
    )
    try:
        response = _retry_open(
            opener, request, timeout=timeout, retries=retries, sleeper=sleeper
        )
        headers = {
            str(k).lower(): str(v)
            for k, v in getattr(response, "headers", {}).items()
        }
        close = getattr(response, "close", None)
        if callable(close):
            close()
        size_raw = headers.get("content-length")
        size = int(size_raw) if size_raw and size_raw.isdigit() else None
        return {
            "ok": True,
            "size_bytes": size,
            "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified"),
            "content_type": headers.get("content-type"),
        }
    except Exception as exc:  # noqa: BLE001 - probing is best effort
        LOG.warning("HEAD %s failed: %s", url, exc)
        return {"ok": False, "error": str(exc)}


def download_and_hash(
    url: str,
    dest: Path,
    *,
    opener: Any | None = None,
    timeout: float = 120,
    retries: int = 2,
    max_bytes: int = 1_200_000_000,
    sleeper: Callable[[float], None] = time.sleep,
    chunk_size: int = 1 << 20,
) -> dict[str, Any]:
    """Stream a package to ``dest`` while computing its real SHA256.

    The article found that upstream's ``sha256hash`` does NOT match the file,
    so a verified checksum has to be computed from the bytes themselves.
    """

    opener = opener or default_opener()
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    response = _retry_open(
        opener, request, timeout=timeout, retries=retries, sleeper=sleeper
    )
    declared = getattr(response, "headers", {}).get("Content-Length")
    if declared and str(declared).isdigit() and int(declared) > max_bytes:
        raise FetchError(f"declared size {declared} exceeds cap {max_bytes}")

    digest = hashlib.sha256()
    total = 0
    try:
        with dest.open("wb") as handle:
            while True:
                block = response.read(chunk_size)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    raise FetchError(f"download exceeded cap {max_bytes} bytes")
                digest.update(block)
                handle.write(block)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    return {"sha256": digest.hexdigest(), "size_bytes": total, "path": str(dest)}
