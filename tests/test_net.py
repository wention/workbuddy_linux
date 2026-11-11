import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from wb_release.config import CHANNELS_BY_PLATFORM
from wb_release.net import (
    FetchError,
    download_and_hash,
    fetch_all,
    http_get_json,
    parse_payload,
    probe_url,
)
from wb_release.models import build_from_filename

PAYLOAD = {
    "version": "5.6.2.40046327",
    "url": "https://download.codebuddy.cn/workbuddy/saas/linux-x64-deb/WorkBuddy-linux-x64-deb-5.6.2.40046327-c7b525f3.deb",
    "productVersion": "5.6.2.40046327",
    "sha256hash": "12149ad8c0cd748fd5062d8f62c7827d688d8095427ada99b1ae9e7019897523",
    "timestamp": 1790769527,
    "supportsFastUpdate": False,
}


class FakeResponse:
    def __init__(self, body: bytes = b"", headers: dict | None = None):
        self._body = body
        self.headers = headers or {}
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:size], self._body[size:]
        return data

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, responses: list[FakeResponse | Exception]):
        self.responses = list(responses)
        self.calls = 0

    def open(self, request, timeout=None):  # noqa: ANN001
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class NetTests(unittest.TestCase):
    def test_parse_payload(self):
        snap = parse_payload(CHANNELS_BY_PLATFORM["workbuddy-linux-x64-deb"], PAYLOAD)
        self.assertEqual(snap.version, "5.6.2.40046327")
        self.assertEqual(snap.build, "c7b525f3")
        self.assertEqual(snap.filename, "WorkBuddy-linux-x64-deb-5.6.2.40046327-c7b525f3.deb")
        self.assertEqual(snap.released_at, "2026-09-30T11:58:47Z")
        self.assertFalse(snap.supports_fast_update)

    def test_parse_payload_rejects_foreign_host(self):
        payload = dict(PAYLOAD, url="https://evil.example.com/pkg.deb")
        with self.assertRaises(FetchError):
            parse_payload(CHANNELS_BY_PLATFORM["workbuddy-linux-x64-deb"], payload)

    def test_parse_payload_rejects_http(self):
        payload = dict(PAYLOAD, url=PAYLOAD["url"].replace("https://", "http://"))
        with self.assertRaises(FetchError):
            parse_payload(CHANNELS_BY_PLATFORM["workbuddy-linux-x64-deb"], payload)

    def test_parse_payload_rejects_wrong_package_type(self):
        with self.assertRaises(FetchError):
            parse_payload(CHANNELS_BY_PLATFORM["workbuddy-linux-x64-rpm"], PAYLOAD)

    def test_parse_payload_rejects_bad_version(self):
        with self.assertRaises(FetchError):
            parse_payload(CHANNELS_BY_PLATFORM["workbuddy-linux-x64-deb"], dict(PAYLOAD, version="oops"))

    def test_parse_payload_accepts_custom_host(self):
        payload = dict(PAYLOAD, url="https://cdn.example.org/pkg.deb")
        snap = parse_payload(
            CHANNELS_BY_PLATFORM["workbuddy-linux-x64-deb"],
            payload,
            hosts=frozenset({"cdn.example.org"}),
        )
        self.assertEqual(snap.url, payload["url"])

    def test_http_get_json_retries(self):
        opener = FakeOpener(
            [OSError("temporary"), OSError("temporary"), FakeResponse(json.dumps(PAYLOAD).encode())]
        )
        result = http_get_json("https://example.invalid", opener=opener, retries=2, sleeper=lambda _: None)
        self.assertEqual(result["version"], PAYLOAD["version"])
        self.assertEqual(opener.calls, 3)

    def test_http_get_json_invalid_json(self):
        opener = FakeOpener([FakeResponse(b"not json")])
        with self.assertRaises(FetchError):
            http_get_json("https://example.invalid", opener=opener, retries=0, sleeper=lambda _: None)

    def test_fetch_all_with_fixtures_isolates_bad_channel(self):
        fixture_path = Path(__file__).parent / "fixtures" / "upstream-current.json"
        fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
        fixtures["workbuddy-linux-x64-deb"] = dict(
            fixtures["workbuddy-linux-x64-deb"], version="bad version!"
        )
        snapshots, errors = fetch_all(fixtures=fixtures, retries=0, sleeper=lambda _: None)
        self.assertEqual(len(snapshots), 3)
        self.assertIn("workbuddy-linux-x64-deb", errors)

    def test_probe_url(self):
        opener = FakeOpener(
            [FakeResponse(headers={"Content-Length": "429829872", "ETag": '"abc"', "Last-Modified": "Wed, 30 Sep 2026 04:48:28 GMT"})]
        )
        info = probe_url("https://download.codebuddy.cn/a.deb", opener=opener, retries=0, sleeper=lambda _: None)
        self.assertTrue(info["ok"])
        self.assertEqual(info["size_bytes"], 429829872)
        self.assertEqual(info["last_modified"], "Wed, 30 Sep 2026 04:48:28 GMT")

    def test_probe_url_failure_is_not_fatal(self):
        opener = FakeOpener([OSError("timeout")])
        info = probe_url("https://download.codebuddy.cn/a.deb", opener=opener, retries=0, sleeper=lambda _: None)
        self.assertFalse(info["ok"])

    def test_download_and_hash(self):
        blob = b"workbuddy" * 5000
        opener = FakeOpener([FakeResponse(blob, headers={"Content-Length": str(len(blob))})])
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "pkg.deb"
            result = download_and_hash(
                "https://download.codebuddy.cn/pkg.deb",
                dest,
                opener=opener,
                retries=0,
                chunk_size=1024,
                sleeper=lambda _: None,
            )
            self.assertEqual(result["sha256"], hashlib.sha256(blob).hexdigest())
            self.assertEqual(result["size_bytes"], len(blob))
            self.assertTrue(dest.exists())

    def test_build_from_filename(self):
        self.assertEqual(build_from_filename("WorkBuddy-linux-arm64-rpm-5.6.2.1-abcdef12.rpm"), "abcdef12")
        self.assertEqual(build_from_filename("WorkBuddy.deb"), "")


if __name__ == "__main__":
    unittest.main()
