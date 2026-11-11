import json
import tempfile
import unittest
from pathlib import Path

from wb_release.config import CHANNELS
from wb_release.net import fetch_all
from wb_release.state import (
    append_history,
    apply_state,
    build_releases,
    compare_versions,
    detect_changes,
    empty_state,
    load,
    read_history,
    save,
)

HERE = Path(__file__).parent
CURRENT = json.loads((HERE / "fixtures" / "upstream-current.json").read_text(encoding="utf-8"))
NEXT = json.loads((HERE / "fixtures" / "upstream-next.json").read_text(encoding="utf-8"))


def snapshots_from(payloads):
    found, errors = fetch_all(fixtures=payloads, retries=0, sleeper=lambda _: None)
    assert not errors, errors
    return found


class StateTests(unittest.TestCase):
    def test_version_compare_is_numeric(self):
        self.assertGreater(compare_versions("5.10.0", "5.9.9"), 0)
        self.assertLess(compare_versions("5.6.2.40046327", "5.6.3.40099999"), 0)
        self.assertEqual(compare_versions("5.6.2", "5.6.2.0"), 0)

    def test_baseline_produces_four_changes_and_one_release(self):
        state = empty_state()
        snaps = snapshots_from(CURRENT)
        changes = detect_changes(state, snaps)
        self.assertEqual(len(changes), 4)
        self.assertTrue(all(c.kind == "bootstrap" for c in changes))
        releases = build_releases(changes)
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0].tag, "v5.6.2.40046327")

        updated = apply_state(state, snaps, changes, {}, {}, "2026-11-11T00:00:00Z")
        self.assertFalse(updated["baseline"])
        self.assertEqual(updated["latest_version"], "5.6.2.40046327")
        self.assertEqual(len(updated["channels"]), 4)
        self.assertEqual(detect_changes(updated, snaps), [])

    def test_upgrade_detected_once(self):
        state = apply_state(empty_state(), snapshots_from(CURRENT), detect_changes(empty_state(), snapshots_from(CURRENT)), {}, {}, "2026-11-11T00:00:00Z")
        snaps = snapshots_from(NEXT)
        changes = detect_changes(state, snaps)
        self.assertEqual(len(changes), 4)
        self.assertTrue(all(c.kind == "upgrade" for c in changes))
        self.assertEqual(build_releases(changes)[0].tag, "v5.6.3.40099999")

    def test_rebuild_same_version_gets_distinct_tag(self):
        state = apply_state(empty_state(), snapshots_from(CURRENT), detect_changes(empty_state(), snapshots_from(CURRENT)), {}, {}, "2026-11-11T00:00:00Z")
        payloads = json.loads(json.dumps(CURRENT))
        payloads["workbuddy-linux-x64-deb"]["url"] = payloads["workbuddy-linux-x64-deb"]["url"].replace("c7b525f3", "deadbeef")
        changes = detect_changes(state, snapshots_from(payloads))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kind, "rebuild")
        self.assertEqual(build_releases(changes)[0].tag, "v5.6.2.40046327-deadbeef")

    def test_downgrade_is_reported(self):
        state = apply_state(empty_state(), snapshots_from(NEXT), detect_changes(empty_state(), snapshots_from(NEXT)), {}, {}, "2026-11-11T00:00:00Z")
        changes = detect_changes(state, snapshots_from(CURRENT))
        self.assertEqual(len(changes), 4)
        self.assertTrue(all(c.kind == "downgrade" for c in changes))

    def test_force_marks_all_channels(self):
        state = apply_state(empty_state(), snapshots_from(CURRENT), detect_changes(empty_state(), snapshots_from(CURRENT)), {}, {}, "2026-11-11T00:00:00Z")
        changes = detect_changes(state, snapshots_from(CURRENT), force=True)
        self.assertEqual(len(changes), 4)
        self.assertTrue(all(c.kind == "force" for c in changes))

    def test_probe_and_verification_are_persisted(self):
        state = empty_state()
        snaps = snapshots_from(CURRENT)
        changes = detect_changes(state, snaps)
        platform = CHANNELS[0].platform
        probes = {platform: {"ok": True, "size_bytes": 429829872, "etag": '"x"'}}
        verifications = {platform: {"sha256": "f" * 64, "size_bytes": 429829872}}
        updated = apply_state(state, snaps, changes, probes, verifications, "2026-11-11T00:00:00Z")
        record = updated["channels"][platform]
        self.assertEqual(record["size_bytes"], 429829872)
        self.assertEqual(record["verified_sha256"], "f" * 64)
        self.assertEqual(record["first_seen_at"], "2026-11-11T00:00:00Z")

    def test_state_and_history_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = empty_state()
            state["latest_version"] = "5.6.2.40046327"
            save(root / "data" / "versions.json", state)
            self.assertEqual(load(root / "data" / "versions.json")["latest_version"], "5.6.2.40046327")
            self.assertTrue(load(root / "missing.json")["baseline"])
            append_history(root / "data" / "history.jsonl", [{"tag": "v1"}, {"tag": "v2"}])
            self.assertEqual([r["tag"] for r in read_history(root / "data" / "history.jsonl")], ["v1", "v2"])


if __name__ == "__main__":
    unittest.main()
