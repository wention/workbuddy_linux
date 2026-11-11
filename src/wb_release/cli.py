"""Command line entry point.

``check``  : daily job — fetch, diff, render, emit GitHub Actions outputs.
``seed``   : one-shot local bootstrap — fill docs from current upstream data.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import CHANNELS, DEFAULT_MAX_DOWNLOAD_BYTES
from .models import Release, iso, utc_now
from .net import download_and_hash, fetch_all, probe_url
from .render import (
    render_changelog,
    render_download_table,
    render_latest_badge,
    render_manifest,
    render_release_notes,
    render_summary,
    update_block,
    update_inline_block,
)
from .state import (
    apply_state,
    append_history,
    build_releases,
    detect_changes,
    empty_state,
    load,
    read_history,
    save,
)

LOG = logging.getLogger("wb_release")


class Paths:
    def __init__(self, root: Path, dist: Path | None = None) -> None:
        self.root = root
        self.state = root / "data" / "versions.json"
        self.history = root / "data" / "history.jsonl"
        self.readme = root / "README.md"
        self.changelog = root / "CHANGELOG.md"
        self.docs_versions = root / "docs" / "versions.json"
        self.docs_badge = root / "docs" / "latest.json"
        self.docs_page = root / "docs" / "index.html"
        self.dist = dist or (root / "dist")
        self.releases = self.dist / "releases"
        self.assets = self.dist / "assets"
        self.tmp = self.dist / "tmp"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wb_release",
        description="Track WorkBuddy Linux deb/rpm releases and publish GitHub Releases.",
    )
    parser.add_argument("--version", action="version", version=f"wb_release {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default=".", help="repository root (default: .)")
        p.add_argument("--dist", default=None, help="generated artifacts directory")
        p.add_argument("--fixtures", default=None, help="offline JSON payloads, keyed by platform")
        p.add_argument("--no-probe", action="store_true", help="skip CDN HEAD probes")
        p.add_argument("--timeout", type=float, default=20, help="API/HEAD timeout (s)")
        p.add_argument("--retries", type=int, default=2, help="retries per request")
        p.add_argument("--dry-run", action="store_true", help="compute only, write nothing")

    check = sub.add_parser("check", help="check upstream and prepare releases")
    common(check)
    check.add_argument("--force", action="store_true", help="treat every channel as changed")
    check.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="download changed packages and compute the real SHA256 (default: on)",
    )
    check.add_argument(
        "--mirror",
        action="store_true",
        help="keep downloaded packages under dist/assets/<tag>/ for release upload",
    )
    check.add_argument(
        "--max-download-bytes",
        type=int,
        default=DEFAULT_MAX_DOWNLOAD_BYTES,
        help="safety cap for one package download",
    )
    check.add_argument("--download-timeout", type=float, default=300, help="download socket timeout (s)")
    check.add_argument("--github-output", default=None, help="path to $GITHUB_OUTPUT")

    seed = sub.add_parser("seed", help="bootstrap data/docs from current upstream state")
    common(seed)
    return parser


def _load_fixtures(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("fixtures file must be a JSON object keyed by platform")
    return data


def _collect(paths: Paths, args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, str]]:
    snapshots, errors = fetch_all(
        fixtures=_load_fixtures(args.fixtures),
        timeout=args.timeout,
        retries=args.retries,
    )
    probes: dict[str, dict[str, Any]] = {}
    if not args.no_probe and not args.fixtures:
        for platform, snapshot in snapshots.items():
            probes[platform] = probe_url(
                snapshot.url, timeout=args.timeout, retries=min(args.retries, 1)
            )
    return {"snapshots": snapshots, "probes": probes}, errors


def _write_outputs(path: str | None, values: dict[str, Any]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
    LOG.info("wrote GitHub outputs: %s", ", ".join(values))


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _update_page_snapshot(paths: Paths, state: dict[str, Any]) -> None:
    """Embed the current state into docs/index.html as an offline fallback."""

    if not paths.docs_page.exists():
        return
    html = paths.docs_page.read_text(encoding="utf-8")
    snapshot = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    _write_text(
        paths.docs_page,
        update_inline_block(html, "DATA", snapshot),
    )


def _verify_changed(
    args: argparse.Namespace,
    paths: Paths,
    changes: list[Any],
    tag_by_platform: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    verifications: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    for change in changes:
        platform = change.snapshot.platform
        if args.mirror:
            dest = paths.assets / tag_by_platform[platform] / change.snapshot.filename
        else:
            dest = paths.tmp / change.snapshot.filename
        LOG.info("verifying %s -> %s", platform, dest)
        try:
            result = download_and_hash(
                change.snapshot.url,
                dest,
                timeout=args.download_timeout,
                retries=args.retries,
                max_bytes=args.max_download_bytes,
            )
            verifications[platform] = result
            LOG.info("  sha256=%s (%s bytes)", result["sha256"], result["size_bytes"])
            if not args.mirror:
                dest.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - checksum verification is best effort
            failures[f"{platform}:verify"] = str(exc)
            LOG.warning("  verification failed: %s", exc)
    return verifications, failures


def _history_records(
    releases: list[Release], state: dict[str, Any], now: str
) -> list[dict[str, Any]]:
    channels = state.get("channels") or {}
    records = []
    for release in releases:
        entries = []
        for change in release.changes:
            record = channels.get(change.snapshot.platform) or change.snapshot.to_dict()
            entries.append(
                {
                    "platform": change.snapshot.platform,
                    "version": change.snapshot.version,
                    "url": change.snapshot.url,
                    "size_bytes": record.get("size_bytes"),
                }
            )
        records.append(
            {
                "detected_at": now,
                "tag": release.tag,
                "version": release.version,
                "kind": release.kind,
                "channels": entries,
            }
        )
    return records


def _write_generated(
    paths: Paths,
    state: dict[str, Any],
    releases: list[Release],
    records: list[dict[str, Any]],
    errors: dict[str, str],
    now: str,
    verified: bool,
) -> None:
    save(paths.state, state)
    save(paths.docs_versions, state)
    _write_json(paths.docs_badge, render_latest_badge(state))
    append_history(paths.history, records)
    _write_text(paths.changelog, render_changelog(read_history(paths.history)))

    readme = paths.readme.read_text(encoding="utf-8") if paths.readme.exists() else ""
    _write_text(paths.readme, update_block(readme, "DOWNLOADS", render_download_table(state)))
    _update_page_snapshot(paths, state)

    paths.releases.mkdir(parents=True, exist_ok=True)
    for stale in paths.releases.glob("*"):
        if stale.is_file() and stale.suffix in {".md", ".json"}:
            stale.unlink()
    manifest_index = []
    for release in releases:
        notes = render_release_notes(release, state, now, verified=verified)
        manifest = render_manifest(release, state, now)
        _write_text(paths.releases / f"{release.tag}.md", notes)
        _write_json(paths.releases / f"{release.tag}.json", manifest)
        manifest_index.append(
            {
                "tag": release.tag,
                "title": release.title,
                "notes": f"dist/releases/{release.tag}.md",
                "manifest": f"dist/releases/{release.tag}.json",
            }
        )
    _write_json(paths.dist / "releases.json", manifest_index)
    _write_text(paths.dist / "summary.md", render_summary(releases, state, errors, now))


def run_check(args: argparse.Namespace) -> int:
    paths = Paths(Path(args.root).resolve(), Path(args.dist).resolve() if args.dist else None)
    now = iso(utc_now())
    state = load(paths.state)

    collected, errors = _collect(paths, args)
    snapshots = collected["snapshots"]
    probes = collected["probes"]
    if not snapshots:
        LOG.error("all %s upstream channels failed: %s", len(CHANNELS), errors)
        _write_outputs(args.github_output, {"has_update": "false", "error": "upstream_unreachable"})
        return 1
    if len(snapshots) < len(CHANNELS):
        LOG.warning("only %s/%s channels responded; the rest keep their previous data", len(snapshots), len(CHANNELS))

    changes = detect_changes(state, snapshots, force=args.force)
    if not changes:
        LOG.info("no new version (upstream=%s)", max((s.version for s in snapshots.values()), default="?"))
        _write_outputs(
            args.github_output,
            {
                "has_update": "false",
                "release_count": "0",
                "latest_version": state.get("latest_version") or "",
                "changed_channels": "0",
            },
        )
        return 0

    releases = build_releases(changes)
    tag_by_platform = {
        change.snapshot.platform: release.tag
        for release in releases
        for change in release.changes
    }
    LOG.info("detected %s change(s) -> %s release(s): %s", len(changes), len(releases), ", ".join(r.tag for r in releases))

    verifications: dict[str, dict[str, Any]] = {}
    verify_errors: dict[str, str] = {}
    do_verify = args.verify and not args.dry_run
    if args.verify and args.dry_run:
        LOG.info("dry-run: skipping package downloads")
    if do_verify:
        shutil.rmtree(paths.tmp, ignore_errors=True)
        verifications, verify_errors = _verify_changed(args, paths, changes, tag_by_platform)
        errors.update(verify_errors)

    updated_state = apply_state(state, snapshots, changes, probes, verifications, now)
    records = _history_records(releases, updated_state, now)

    if args.dry_run:
        LOG.info("dry-run: would write state/docs and %s release notes", len(releases))
        for release in releases:
            LOG.info("  %s <- %s", release.tag, ", ".join(c.snapshot.platform for c in release.changes))
    else:
        _write_generated(
            paths, updated_state, releases, records, errors, now, verified=bool(verifications)
        )

    _write_outputs(
        args.github_output,
        {
            "has_update": "true",
            "release_count": str(len(releases)),
            "release_tags": ",".join(r.tag for r in releases),
            "latest_version": updated_state.get("latest_version") or "",
            "changed_channels": str(len(changes)),
            "dry_run": "true" if args.dry_run else "false",
        },
    )
    return 0


def run_seed(args: argparse.Namespace) -> int:
    paths = Paths(Path(args.root).resolve(), Path(args.dist).resolve() if args.dist else None)
    now = iso(utc_now())
    collected, errors = _collect(paths, args)
    snapshots = collected["snapshots"]
    if not snapshots:
        LOG.error("cannot seed: %s", errors)
        return 1
    state = apply_state(empty_state(), snapshots, [], collected["probes"], {}, now)
    state["baseline"] = True  # first scheduled run publishes the initial Release
    if args.dry_run:
        LOG.info("dry-run seed: %s channels", len(snapshots))
        return 0
    save(paths.state, state)
    save(paths.docs_versions, state)
    _write_json(paths.docs_badge, render_latest_badge(state))
    paths.history.parent.mkdir(parents=True, exist_ok=True)
    paths.history.touch(exist_ok=True)
    _write_text(paths.changelog, render_changelog(read_history(paths.history)))
    readme = paths.readme.read_text(encoding="utf-8") if paths.readme.exists() else ""
    _write_text(paths.readme, update_block(readme, "DOWNLOADS", render_download_table(state)))
    _update_page_snapshot(paths, state)
    LOG.info("seeded %s channels at version %s", len(snapshots), state.get("latest_version"))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            return run_check(args)
        if args.command == "seed":
            return run_seed(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOG.error("%s", exc)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
