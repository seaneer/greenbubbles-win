"""Index regressions: BUG-001 (dropped tail batch) and BUG-002 (sort_seq collision)."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from tests import support


def setUpModule():
    support.require_data()


class IndexBuildCompletenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = support.get_store()
        cls.tmp = tempfile.TemporaryDirectory()
        from greenbubbles_win.index import SearchIndex

        cls.index = SearchIndex(Path(cls.tmp.name) / "index.sqlite3")
        cls.result = cls.index.build(cls.store, conversations=[support.GROUP])
        cls.truth = support.shard_truth(support.GROUP)

    @classmethod
    def tearDownClass(cls):
        cls.index.close()
        cls.tmp.cleanup()

    def test_every_shard_row_is_indexed(self):
        """BUG-001: the last partial batch used to be discarded before the watermark moved."""

        rows = self.index.conn.execute(
            "select count(*) from msg_text where conversation = ?", (support.GROUP,)
        ).fetchone()[0]
        self.assertEqual(rows, self.truth["messages"])
        self.assertEqual(self.result["rows"], self.truth["messages"])

    def test_watermark_matches_newest_message(self):
        newest = max(seq for seq, _ in self.truth["keys"])
        self.assertEqual(self.index.watermark(support.GROUP), newest)

    def test_incremental_build_adds_nothing_when_unchanged(self):
        again = self.index.build(self.store, conversations=[support.GROUP])
        self.assertEqual(again["addedMessages"], 0)

    def test_partial_build_is_not_marked_complete(self):
        self.assertFalse(self.index.stats()["complete"])

    def test_last_message_is_searchable(self):
        latest = self.store.locate(support.GROUP)
        self.assertIsNotNone(latest)
        rel, table = latest
        row = self.store.open(rel).execute(
            f"select message_content, WCDB_CT_message_content from {table} "
            "order by sort_seq desc limit 1"
        ).fetchone()
        self.assertIsNotNone(row)


class SortSeqCollisionTests(unittest.TestCase):
    """BUG-002: PK on (conversation, sort_seq) overwrote one of two messages sharing a sort key."""

    @classmethod
    def setUpClass(cls):
        cls.store = support.get_store()
        cls.tmp = tempfile.TemporaryDirectory()
        from greenbubbles_win.index import SearchIndex

        # Find a conversation that really has a colliding sort_seq, so the test is data-driven.
        import hashlib

        digest_map = {hashlib.md5(name.encode("utf-8")).hexdigest(): name for name in cls.store.conversation_ids()}
        cls.conversation = None
        for rel in cls.store.shards():
            conn = cls.store.open(rel)
            for (table,) in conn.execute(
                "select name from sqlite_master where type='table' and name like 'Msg_%'"
            ):
                row = conn.execute(
                    f"select sort_seq, count(*) as n from {table} "
                    "group by sort_seq having n > 1 order by n desc limit 1"
                ).fetchone()
                if row and table[4:] in digest_map:
                    cls.conversation = digest_map[table[4:]]
                    break
            if cls.conversation:
                break

        if not cls.conversation:
            raise unittest.SkipTest("no colliding sort_seq in this data set")

        cls.index = SearchIndex(Path(cls.tmp.name) / "index.sqlite3")
        cls.index.build(cls.store, conversations=[cls.conversation])

    @classmethod
    def tearDownClass(cls):
        cls.index.close()
        cls.tmp.cleanup()

    def test_all_colliding_rows_survive(self):
        conn = self.index.conn
        candidates = []
        for rel, table in self.store.locate_all(self.conversation):
            rows = self.store.open(rel).execute(
                f"select sort_seq, count(distinct local_id) from {table} "
                "group by sort_seq having count(distinct local_id) > 1"
            ).fetchall()
            candidates.extend(rows)
        self.assertTrue(candidates, "fixture conversation no longer has duplicates")

        seq, expected_count = candidates[0]
        indexed = conn.execute(
            "select count(distinct message_local_id) from msg_text where conversation = ? and sort_seq = ?",
            (self.conversation, seq),
        ).fetchone()[0]
        self.assertEqual(indexed, expected_count)


class IndexParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = support.get_store()
        cls.tmp = tempfile.TemporaryDirectory()
        from greenbubbles_win.index import SearchIndex

        cls.index = SearchIndex(Path(cls.tmp.name) / "index.sqlite3")
        cls.index.build(cls.store, conversations=[support.GROUP])

    @classmethod
    def tearDownClass(cls):
        cls.index.close()
        cls.tmp.cleanup()

    def test_index_matches_live_scan(self):
        from greenbubbles_win import query

        needle = "溢"
        indexed, _ = self.index.search(needle, conversation=support.GROUP, limit=500)
        live = query.search(
            self.store, needle, conversation=support.GROUP, limit=500, use_index=False
        )["items"]
        self.assertEqual(
            {(item["conversationId"], item["sortSequence"]) for item in indexed},
            {(item["conversationId"], item["sortSequence"]) for item in live},
        )


class GlobalIndexPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = support.ROOT / "state" / "search-index.sqlite3"
        if not path.exists():
            raise unittest.SkipTest("state index not built")
        from greenbubbles_win import index as indexmod

        cls.index = indexmod.SearchIndex()
        if cls.index.stats()["complete"] is not True:
            cls.index.close()
            raise unittest.SkipTest("state index is partial")

    @classmethod
    def tearDownClass(cls):
        cls.index.close()

    def test_unscoped_search_is_fast_and_warns(self):
        """BUG-009: unscoped search used to take 48–65s; guarded budget is 8s including IO."""

        support.require_query()
        started = time.time()
        items, _ = self.index.search(support.QUERY, limit=100)
        elapsed = time.time() - started
        self.assertLess(elapsed, 8.0, f"indexed search took {elapsed:.2f}s")
        self.assertGreater(len(items), 0)

        from greenbubbles_win import query

        envelope = query.search(support.get_store(), support.QUERY, limit=100)
        self.assertIn("searchIndexUsed", [warning["code"] for warning in envelope["warnings"]])


if __name__ == "__main__":
    unittest.main()
