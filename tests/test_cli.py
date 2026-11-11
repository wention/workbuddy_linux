import json
import tempfile
import unittest
from pathlib import Path

from wb_release import cli
from wb_release.state import load

HERE = Path(__file__).parent
CURRENT = str(HERE / "fixtures" / "upstream-current.json")
NEXT = str(HERE / "fixtures" / "upstream-next.json")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "README.md").write_text("# Test project\n", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def _seed(self):
        return cli.main(["seed", "--root", str(self.root), "--fixtures", CURRENT, "--no-probe"])

    def test_seed_check_publish_then_idempotent(self):
        self.assertEqual(self._seed(), 0)
        state = load(self.root / "data" / "versions.json")
        self.assertTrue(state["baseline"])
        self.assertEqual(state["latest_version"], "5.6.2.40046327")
        self.assertIn("[下载 deb]", (self.root / "README.md").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "docs" / "versions.json").exists())
        self.assertTrue((self.root / "docs" / "latest.json").exists())

        output = self.root / "outputs.txt"
        rc = cli.main(
            [
                "check", "--root", str(self.root), "--fixtures", NEXT,
                "--no-probe", "--no-verify", "--github-output", str(output),
            ]
        )
        self.assertEqual(rc, 0)
        body = output.read_text(encoding="utf-8")
        self.assertIn("has_update=true", body)
        self.assertIn("release_count=1", body)
        self.assertIn("v5.6.3.40099999", body)

        notes = self.root / "dist" / "releases" / "v5.6.3.40099999.md"
        self.assertTrue(notes.exists())
        self.assertIn("sudo apt install", notes.read_text(encoding="utf-8"))
        self.assertTrue((self.root / "dist" / "releases" / "v5.6.3.40099999.json").exists())
        self.assertEqual(
            len(json.loads((self.root / "dist" / "releases.json").read_text(encoding="utf-8"))), 1
        )
        self.assertIn("v5.6.3.40099999", (self.root / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertFalse(load(self.root / "data" / "versions.json")["baseline"])

        output2 = self.root / "outputs2.txt"
        self.assertEqual(
            cli.main(
                [
                    "check", "--root", str(self.root), "--fixtures", NEXT,
                    "--no-probe", "--no-verify", "--github-output", str(output2),
                ]
            ),
            0,
        )
        self.assertIn("has_update=false", output2.read_text(encoding="utf-8"))

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self._seed(), 0)
        before = (self.root / "data" / "versions.json").read_bytes()
        rc = cli.main(
            ["check", "--root", str(self.root), "--fixtures", NEXT, "--no-verify", "--no-probe", "--dry-run"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(before, (self.root / "data" / "versions.json").read_bytes())
        self.assertFalse((self.root / "dist" / "releases").exists())

    def test_force_regenerates_notes(self):
        self.assertEqual(self._seed(), 0)
        base = ["check", "--root", str(self.root), "--fixtures", CURRENT, "--no-probe", "--no-verify"]
        self.assertEqual(cli.main(base), 0)
        self.assertEqual(cli.main(base + ["--force"]), 0)
        self.assertTrue((self.root / "dist" / "releases" / "v5.6.2.40046327.md").exists())


if __name__ == "__main__":
    unittest.main()
