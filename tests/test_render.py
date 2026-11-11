import json
import unittest
from pathlib import Path

from wb_release.net import fetch_all
from wb_release.render import (
    format_size,
    render_changelog,
    render_checksum_table,
    render_download_table,
    render_latest_badge,
    render_release_notes,
    render_summary,
    update_block,
    update_inline_block,
)
from wb_release.state import apply_state, build_releases, detect_changes, empty_state, read_history

CURRENT = json.loads(
    (Path(__file__).parent / "fixtures" / "upstream-current.json").read_text(encoding="utf-8")
)


class RenderTests(unittest.TestCase):
    def setUp(self):
        snaps, errors = fetch_all(fixtures=CURRENT, retries=0, sleeper=lambda _: None)
        assert not errors
        changes = detect_changes(empty_state(), snaps)
        self.state = apply_state(
            empty_state(), snaps, changes,
            {p: {"ok": True, "size_bytes": 429829872} for p in snaps},
            {"workbuddy-linux-x64-deb": {"sha256": "a" * 64, "size_bytes": 429829872}},
            "2026-11-11T00:00:00Z",
        )
        self.release = build_releases(changes)[0]

    def test_download_table(self):
        table = render_download_table(self.state)
        self.assertIn("linux-x64-deb", table)
        self.assertIn("https://download.codebuddy.cn/", table)
        self.assertEqual(table.count("| [下载"), 4)
        self.assertEqual(format_size(429829872), "409.9 MiB")

    def test_checksum_table_separates_verified_and_upstream(self):
        table = render_checksum_table(self.state)
        self.assertIn("a" * 64, table)
        self.assertIn("12149ad8c0cd748", table)
        self.assertIn("未实测", table)

    def test_release_notes_contract(self):
        notes = render_release_notes(self.release, self.state, "2026-11-11T00:00:00Z", verified=True)
        self.assertIn("# WorkBuddy Linux v5.6.2.40046327", notes)
        self.assertIn("sudo apt install", notes)
        self.assertIn("sudo dnf install", notes)
        self.assertIn("c7b525f3.deb", notes)
        self.assertIn("不要拿接口字段当校验标准", notes)
        self.assertIn("xingwangzhe.fun", notes)
        self.assertIn("supportsFastUpdate", notes)

    def test_update_block_is_idempotent(self):
        original = "# Title\n\n<!-- AUTOGEN:DOWNLOADS:START -->\nold\n<!-- AUTOGEN:DOWNLOADS:END -->\n\nfooter\n"
        once = update_block(original, "DOWNLOADS", "new table")
        twice = update_block(once, "DOWNLOADS", "new table")
        self.assertEqual(once, twice)
        self.assertIn("new table", once)
        self.assertIn("footer", once)
        appended = update_block("# Title\n", "DOWNLOADS", "new table")
        self.assertIn("AUTOGEN:DOWNLOADS:START", appended)

    def test_inline_snapshot_block(self):
        source = 'var FALLBACK = /* AUTOGEN:DATA:START */ null /* AUTOGEN:DATA:END */;'
        once = update_inline_block(source, "DATA", '{"a":1}')
        self.assertIn('{"a":1}', once)
        twice = update_inline_block(once, "DATA", '{"a":1}')
        self.assertEqual(once, twice)
        self.assertEqual(update_inline_block("plain", "DATA", "x"), "plain")

    def test_badge_and_summary(self):
        badge = render_latest_badge(self.state)
        self.assertEqual(badge["message"], "v5.6.2.40046327")
        summary = render_summary([self.release], self.state, {}, "2026-11-11T00:00:00Z")
        self.assertIn("v5.6.2.40046327", summary)

    def test_changelog(self):
        self.assertEqual("尚无发布记录" in render_changelog([]), True)
        history = [{"tag": "v5.6.2.40046327", "detected_at": "2026-11-11T00:00:00Z", "kind": "bootstrap", "channels": [{"platform": "workbuddy-linux-x64-deb", "version": "5.6.2", "url": "https://download.codebuddy.cn/a.deb", "size_bytes": 429829872}]}]
        text = render_changelog(history)
        self.assertIn("v5.6.2.40046327", text)
        self.assertIn("首次收录", text)


if __name__ == "__main__":
    unittest.main()
