"""Media and cache-path regressions (BUG-003, BUG-006)."""

from __future__ import annotations

import unittest

from pathlib import Path

from tests import support


class MediaEntityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from greenbubbles_win import query

        support.require_data()
        cls.store = support.get_store()
        envelope = query.messages(
            cls.store, support.GROUP, limit=200, since=None, with_media=True
        )
        cls.items = envelope["items"]

    def _first_image(self):
        for item in self.items:
            info = item.get("media") or {}
            if info.get("kind") == "image" and info.get("available"):
                md5 = ((item.get("content") or {}).get("Image") or {}).get("md5")
                if md5:
                    return item, info, md5
        self.skipTest("no decrypted image in the sampled window")

    def test_extension_and_size_come_from_plaintext(self):
        """BUG-003: exported images used to be named `.dat` because the extension was dropped."""

        item, info, md5 = self._first_image()
        self.assertTrue(info["extension"])
        self.assertNotEqual(info["extension"], "dat")
        self.assertIn(info["extension"], {"png", "jpg", "jpeg", "gif", "webp", "wxgf"})

        found = self.store.media.image(item["conversationId"], md5)
        self.assertIsNotNone(found)
        if found.data is not None:
            self.assertEqual(found.size, len(found.data))
            self.assertGreater(found.size, 0)

    def test_decrypted_bytes_match_the_extension(self):
        item, info, md5 = self._first_image()
        found = self.store.media.image(item["conversationId"], md5)
        if not found or found.data is None:
            self.skipTest("image present on disk but not decryptable in this environment")

        from greenbubbles_win import media

        extension, label = media.detect_kind(found.data)
        self.assertNotEqual(extension, "bin", f"undecodable bytes ({label})")
        if found.extension == "png":
            self.assertTrue(found.data.startswith(b"\x89PNG"))
            self.assertEqual(extension, "png")
        elif found.extension in {"jpg", "jpeg"}:
            self.assertTrue(found.data.startswith(b"\xff\xd8"))


class CachePathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.require_data()

    def test_shard_relative_path_resolves_under_cache_db(self):
        """BUG-006: a relative path bug made `message/message_0.db` resolve to db_storage."""

        store = support.get_store()
        rel = store.shards()[0]
        conn = store.open(rel)
        self.assertIsNotNone(conn.execute("select 1").fetchone())
        expected = Path(store.cache.root) / "db" / rel
        self.assertTrue(expected.exists(), f"missing decrypted shard: {expected}")

    def test_all_shards_open(self):
        store = support.get_store()
        for rel in store.shards():
            conn = store.open(rel)
            self.assertIsNotNone(conn.execute("select count(*) from sqlite_master").fetchone())


class AttachmentArtifactTests(unittest.TestCase):
    """BUG-007: the first report pointed `read.txt` at a README; indexes must be self-consistent."""

    @classmethod
    def setUpClass(cls):
        support.require_reports()

    def test_attachment_index_rows_match_disk(self):
        import csv

        report = support.REPORT_DIR
        with (report / "attachments-index.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertGreater(len(rows), 40)
        missing = 0
        for row in rows:
            if row["kind"] == "file-missing":
                missing += 1
                continue
            path = report / row["path"]
            self.assertTrue(path.exists(), f"indexed attachment is missing: {row['path']}")
            self.assertGreater(path.stat().st_size, 0)
        self.assertEqual(missing, 1, "exactly one attachment is expected to be absent on disk")

    def test_message_dump_is_parseable_and_unique(self):
        import json

        lines = (support.REPORT_DIR / "messages-all.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        records = [json.loads(line) for line in lines]
        keys = {(record["conversationId"], record["sortSequence"]) for record in records}
        self.assertEqual(len(keys), len(records))


if __name__ == "__main__":
    unittest.main()
