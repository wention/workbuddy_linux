"""Version state, change detection and release grouping.

``data/versions.json`` is the single source of truth. It is committed back to the
repository so a scheduled run can tell "new version" from "already published".
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import CHANNELS
from .models import Change, Release, Snapshot, safe_tag

SCHEMA = 1
_KIND_RANK = {"bootstrap": 0, "upgrade": 1, "downgrade": 2, "rebuild": 3, "force": 4}


def empty_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "baseline": True,
        "updated_at": None,
        "latest_version": None,
        "latest_tag": None,
        "channels": {},
    }


def load(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    merged = empty_state()
    merged.update(data)
    merged["schema"] = SCHEMA
    merged["channels"] = data.get("channels") if isinstance(data.get("channels"), dict) else {}
    return merged


def save(path: Path | str, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def parse_version(value: str | None) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value or "")
    return tuple(int(p) for p in parts) if parts else (0,)


def compare_versions(left: str | None, right: str | None) -> int:
    """Return 1/0/-1. Numeric dotted versions sort correctly (5.10 > 5.9)."""

    a, b = parse_version(left), parse_version(right)
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return (a > b) - (a < b)


def detect_changes(
    state: dict[str, Any],
    snapshots: dict[str, Snapshot],
    *,
    force: bool = False,
) -> list[Change]:
    channels = state.get("channels") or {}
    baseline = bool(state.get("baseline", False))
    changes: list[Change] = []
    for channel in CHANNELS:
        snapshot = snapshots.get(channel.platform)
        if snapshot is None:
            continue
        previous = channels.get(channel.platform) or {}
        if force:
            kind = "force"
        elif baseline or not previous:
            kind = "bootstrap"
        elif previous.get("version") != snapshot.version:
            kind = (
                "upgrade"
                if compare_versions(snapshot.version, previous.get("version")) > 0
                else "downgrade"
            )
        elif previous.get("url") != snapshot.url:
            # Same version, different build hash: a silent rebuild.
            kind = "rebuild"
        else:
            continue
        changes.append(
            Change(
                channel=channel,
                snapshot=snapshot,
                kind=kind,
                previous_version=previous.get("version"),
                previous_url=previous.get("url"),
            )
        )
    return changes


def _pick(current: Any | None, previous: Any | None, same_artifact: bool) -> Any | None:
    return current if current is not None else (previous if same_artifact else None)


def apply_state(
    state: dict[str, Any],
    snapshots: dict[str, Snapshot],
    changes: list[Change],
    probes: dict[str, dict[str, Any]],
    verifications: dict[str, dict[str, Any]],
    now: str,
) -> dict[str, Any]:
    """Merge fresh upstream data into the persisted state."""

    old_channels = state.get("channels") or {}
    new_channels: dict[str, Any] = {}
    changed_platforms = {change.snapshot.platform for change in changes}

    for channel in CHANNELS:
        snapshot = snapshots.get(channel.platform)
        if snapshot is None:
            if channel.platform in old_channels:
                new_channels[channel.platform] = old_channels[channel.platform]
            continue
        previous = old_channels.get(channel.platform) or {}
        same_artifact = (
            previous.get("url") == snapshot.url
            and previous.get("version") == snapshot.version
        )
        probe = probes.get(channel.platform) or {}
        verification = verifications.get(channel.platform) or {}
        verified_sha = verification.get("sha256") or (
            previous.get("verified_sha256") if same_artifact else None
        )
        verified_at = (
            now
            if verification.get("sha256")
            else (previous.get("verified_at") if same_artifact else None)
        )
        record = snapshot.to_dict()
        record.update(
            {
                "arch": channel.arch,
                "pkg": channel.pkg,
                "distro": channel.distro,
                "size_bytes": _pick(probe.get("size_bytes"), previous.get("size_bytes"), same_artifact) if probe.get("ok") else (previous.get("size_bytes") if same_artifact else None),
                "etag": _pick(probe.get("etag"), previous.get("etag"), same_artifact),
                "last_modified": _pick(probe.get("last_modified"), previous.get("last_modified"), same_artifact),
                "probe_ok": bool(probe.get("ok")),
                "verified_sha256": verified_sha,
                "verified_at": verified_at,
                "verified_size_bytes": verification.get("size_bytes")
                or (previous.get("verified_size_bytes") if same_artifact else None),
                "first_seen_at": previous.get("first_seen_at") if same_artifact else now,
                "last_checked_at": now,
            }
        )
        new_channels[channel.platform] = record

    latest = max(
        new_channels.values(),
        key=lambda item: parse_version(item.get("version")),
        default=None,
    )
    latest_version = latest["version"] if latest else None
    result = dict(state)
    result.update(
        {
            "schema": SCHEMA,
            # baseline stays true until a run actually publishes something, so a
            # failed release step is retried on the next run instead of being lost.
            "baseline": False if changed_platforms else bool(state.get("baseline", False)),
            "updated_at": now,
            "latest_version": latest_version,
            "latest_tag": f"v{safe_tag(latest_version)}" if latest_version else None,
            "channels": new_channels,
        }
    )
    return result


def build_releases(changes: list[Change]) -> list[Release]:
    """Group channels that belong to the same upstream build into one release."""

    groups: dict[tuple[str, str], list[Change]] = {}
    for change in changes:
        key = (change.snapshot.version, change.snapshot.build)
        groups.setdefault(key, []).append(change)

    releases: list[Release] = []
    used_tags: set[str] = set()
    ordered = sorted(
        groups.items(),
        key=lambda pair: (parse_version(pair[0][0]), pair[0][1]),
    )
    for (version, build), group in ordered:
        kinds = {change.kind for change in group}
        only_rebuild = kinds == {"rebuild"}
        tag = f"v{safe_tag(version)}"
        if only_rebuild and build:
            tag = f"{tag}-{safe_tag(build)}"
        base = tag
        counter = 2
        while tag in used_tags:
            tag = f"{base}-{counter}"
            counter += 1
        used_tags.add(tag)
        kind = min(kinds, key=lambda k: _KIND_RANK.get(k, 99))
        releases.append(
            Release(
                tag=tag,
                version=version,
                build=build,
                kind=kind,
                changes=tuple(sorted(group, key=lambda c: c.channel.order)),
            )
        )
    return releases


def append_history(path: Path | str, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_history(path: Path | str) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records
