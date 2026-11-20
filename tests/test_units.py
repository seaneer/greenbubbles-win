"""Pure-unit regressions: no WeChat process or live database required."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests import support


class LimitClampTests(unittest.TestCase):
    """BUG-010: limit must clamp instead of leaking unbounded page sizes."""

    def test_default_for_zero_and_negative(self):
        from greenbubbles_win import query

        self.assertEqual(query.clamp_limit(0), query.DEFAULT_PAGE_LIMIT)
        self.assertEqual(query.clamp_limit(-7), query.DEFAULT_PAGE_LIMIT)

    def test_hard_ceiling(self):
        from greenbubbles_win import query

        self.assertEqual(query.clamp_limit(2000), query.MAX_PAGE_LIMIT)
        self.assertEqual(query.clamp_limit(500), 500)
        self.assertEqual(query.clamp_limit(1), 1)


class McpLimitTests(unittest.TestCase):
    """BUG-004: MCP used `or default`, so 0 silently became 50/30."""

    def test_missing_uses_default(self):
        from greenbubbles_win import mcp

        self.assertEqual(mcp._limit({}, 50), 50)

    def test_zero_and_negative_floor_to_one(self):
        from greenbubbles_win import mcp

        self.assertEqual(mcp._limit({"limit": 0}, 50), 1)
        self.assertEqual(mcp._limit({"limit": -3}, 50), 1)

    def test_strings_are_parsed_and_capped(self):
        from greenbubbles_win import mcp

        self.assertEqual(mcp._limit({"limit": "7"}, 50), 7)
        self.assertEqual(mcp._limit({"limit": "9999"}, 50), 500)
        self.assertEqual(mcp._limit({"limit": "abc"}, 50), 50)


class IndexDedupeTests(unittest.TestCase):
    """BUG-002: two messages can share sort_seq; dedupe must keep the first, not drop both."""

    @staticmethod
    def _row(conversation: str, seq: int, local_id: int):
        return (conversation, seq, local_id, 0, 0, "s", "s", 0, 1, 0, "text", "text", "id", "body")

    def test_keeps_first_of_same_sort_seq(self):
        from greenbubbles_win.index import SearchIndex

        rows = [self._row("c", 100, 12), self._row("c", 100, 11), self._row("c", 90, 9)]
        kept = SearchIndex._dedupe(rows, limit=10)
        self.assertEqual([(row[0], row[1], row[2]) for row in kept], [("c", 100, 12), ("c", 90, 9)])

    def test_respects_limit_plus_one_probe(self):
        from greenbubbles_win.index import SearchIndex

        rows = [self._row("c", seq, seq) for seq in (5, 4, 3, 2)]
        self.assertEqual(len(SearchIndex._dedupe(rows, limit=2)), 3)


class MediaDetectTests(unittest.TestCase):
    """BUG-003: `.dat` decryption used to lose the real extension and size."""

    def test_signatures(self):
        from greenbubbles_win import media

        self.assertEqual(media.detect_kind(b"\x89PNG\r\n\x1a\n" + b"0" * 20), ("png", "png"))
        self.assertEqual(media.detect_kind(b"\xff\xd8\xff\xe0" + b"0" * 20), ("jpg", "jpg"))
        self.assertEqual(media.detect_kind(b"GIF89a" + b"0" * 20), ("gif", "gif"))
        self.assertEqual(media.detect_kind(b"RIFF\x00\x00\x00\x00WEBP"), ("webp", "webp"))
        self.assertEqual(media.detect_kind(b"wxgf" + b"0" * 20), ("wxgf", "wxgf"))
        self.assertEqual(media.detect_kind(b"random"), ("bin", "unknown"))


class TimeParsingTests(unittest.TestCase):
    def test_date_and_seconds(self):
        from greenbubbles_win import cli

        self.assertEqual(cli.parse_time("2026-08-10"), 1_786_291_200)
        self.assertEqual(cli.parse_time("1795000000"), 1_795_000_000)
        self.assertIsNone(cli.parse_time(None))

    def test_garbage_raises_system_exit(self):
        from greenbubbles_win import cli

        with self.assertRaises(SystemExit):
            cli.parse_time("not-a-time")


class SnapshotTests(unittest.TestCase):
    """BUG-012: vault restore must reject a wrong phrase and overwrite=False."""

    def test_roundtrip_and_wrong_phrase(self):
        from greenbubbles_win import snapshot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            root.mkdir()
            (root / "a.txt").write_text("hello", encoding="utf-8")
            vault = Path(tmp) / "s.gbvault"
            info = snapshot.create(root, vault)
            phrase = info["mnemonic"]

            out = Path(tmp) / "restored"
            result = snapshot.restore(vault, phrase, out)
            restored = list(out.rglob("a.txt"))
            self.assertEqual(len(restored), 1, f"restored tree: {result['extracted']}")
            self.assertEqual(restored[0].read_text(encoding="utf-8"), "hello")
            self.assertTrue(result["extracted"])

            wrong = snapshot.new_mnemonic(24)
            self.assertNotEqual(wrong, phrase)
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.restore(vault, wrong, Path(tmp) / "bad")

            with self.assertRaises(snapshot.SnapshotError):
                snapshot.restore(vault, phrase, out)


class SnapshotTraversalTests(unittest.TestCase):
    """A crafted vault must not write outside the chosen destination."""

    def test_rejects_parent_traversal(self):
        import io
        import tarfile

        from greenbubbles_win import snapshot

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            info = tarfile.TarInfo("../evil.txt")
            payload = b"x"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(fileobj=io.BytesIO(buffer.getvalue()), mode="r:gz") as tar:
                with self.assertRaises(snapshot.SnapshotError):
                    snapshot._extract_safely(tar, Path(tmp))
            self.assertFalse((Path(tmp).parent / "evil.txt").exists())


class TruthyTests(unittest.TestCase):
    """BUG-001: hand-rolled truthiness caused a wrong branch in the first audit script."""

    def test_http_truthy_values(self):
        from greenbubbles_win import service

        for value in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(service._truthy(value))
        for value in ("0", "false", "", None, "no"):
            self.assertFalse(service._truthy(value))

    def test_access_log_redacts_query_token(self):
        from greenbubbles_win import service

        message = 'GET /api/v1/chats?limit=5&token=secret-value HTTP/1.1'
        redacted = service.redact_token(message)
        self.assertNotIn("secret-value", redacted)
        self.assertIn("token=<redacted>", redacted)


if __name__ == "__main__":
    unittest.main()
