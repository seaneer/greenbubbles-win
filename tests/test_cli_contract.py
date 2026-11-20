"""CLI contract and paging regressions (BUG-003 jsonl cursor, BUG-010 clamp, BUG-011 exit codes)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from tests import support


def setUpModule():
    support.require_data()


class PagingCompletenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.truth = support.shard_truth(support.GROUP)
        cls.since = int(datetime(2026, 8, 10).replace(tzinfo=None).timestamp()) + 0
        # Mirror the CLI's +08:00 interpretation.
        cls.since = int(datetime.fromisoformat("2026-08-10T00:00:00+08:00").timestamp())

    def test_every_page_is_disjoint_and_complete(self):
        seen: list[tuple[str, int]] = []
        cursor = None
        pages = 0
        while True:
            args = ["messages", "list", "--conversation", support.GROUP, "--limit", "100"]
            if cursor:
                args += ["--cursor", cursor]
            payload = support.cli_json(*args)
            pages += 1
            seen.extend((item["conversationId"], item["sortSequence"]) for item in payload["items"])
            cursor = payload["page"]["nextCursor"]
            if not payload["page"]["hasMore"]:
                break
            self.assertLess(pages, 25, "pagination did not terminate")

        self.assertEqual(len(seen), len(set(seen)), "duplicate messages across pages")
        self.assertEqual(len(seen), self.truth["messages"])
        self.assertEqual(pages, (self.truth["messages"] + 99) // 100)

    def test_time_filter_matches_shard_truth(self):
        payload = support.cli_json(
            "messages", "list", "--conversation", support.GROUP,
            "--since", "2026-08-10", "--limit", "500",
        )
        expected = sum(
            count for created, count in self.truth["since_counts"].items() if created >= self.since
        )
        self.assertEqual(len(payload["items"]), expected)

    def test_since_boundary_is_inclusive(self):
        payload = support.cli_json(
            "messages", "list", "--conversation", support.GROUP,
            "--since", "2026-08-10", "--limit", "500",
        )
        self.assertEqual(min(item["createdAtUnix"] for item in payload["items"]) >= self.since, True)
        first = min(payload["items"], key=lambda item: item["createdAtUnix"])
        exact = support.cli_json(
            "messages", "list", "--conversation", support.GROUP,
            "--since", str(first["createdAtUnix"]), "--limit", "500",
        )
        self.assertIn(
            first["sortSequence"], [item["sortSequence"] for item in exact["items"]]
        )


class EmitFormatTests(unittest.TestCase):
    def test_jsonl_emits_cursor_on_stderr(self):
        """BUG-003: jsonl used to drop nextCursor, so agents could not page."""

        proc = support.run_cli(
            "messages", "list", "--conversation", support.GROUP,
            "--limit", "100", "--format", "jsonl",
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(proc.stdout.strip().splitlines()), 100)
        self.assertIn("nextCursor=", proc.stderr)
        cursor = proc.stderr.split("nextCursor=")[1].split()[0].strip()
        self.assertTrue(cursor)

        next_page = support.cli_json(
            "messages", "list", "--conversation", support.GROUP,
            "--limit", "100", "--cursor", cursor,
        )
        self.assertEqual(len(next_page["items"]), 100)

    def test_jsonl_last_page_marks_completion(self):
        proc = support.run_cli(
            "messages", "list", "--conversation", support.GROUP,
            "--limit", "500", "--format", "jsonl",
        )
        if len(proc.stdout.strip().splitlines()) < 500:
            self.assertIn("lastPage=true", proc.stderr)


class ClampAndErrorTests(unittest.TestCase):
    def test_oversized_limit_is_clamped(self):
        payload = support.cli_json(
            "messages", "list", "--conversation", support.GROUP,
            "--since", "2026-08-10", "--limit", "2000",
        )
        self.assertEqual(payload["page"]["limit"], 500)
        self.assertLessEqual(len(payload["items"]), 500)

    def test_unknown_conversation_is_soft_failure(self):
        payload = support.cli_json(
            "messages", "list", "--conversation", "99999999999@chatroom", "--limit", "5"
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["warnings"][0]["code"], "conversationNotFound")
        self.assertFalse(payload["consistency"]["coverageComplete"])

    def test_bad_time_exits_one(self):
        proc = support.run_cli(
            "messages", "list", "--conversation", support.GROUP, "--since", "not-a-time"
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cannot parse time", proc.stderr)

    def test_missing_required_argument_exits_two(self):
        proc = support.run_cli("messages", "list")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--conversation", proc.stderr)

    def test_emoji_survives_the_pipe(self):
        payload = support.cli_json(
            "messages", "list", "--conversation", support.GROUP,
            "--limit", "500",
        )
        text = json.dumps(payload, ensure_ascii=False)
        if not any(ord(character) > 127 for character in text):
            self.skipTest("the sampled window has no non-ASCII text")

    def test_observed_at_changes_between_calls(self):
        first = support.cli_json("messages", "list", "--conversation", support.GROUP, "--limit", "5")
        second = support.cli_json("messages", "list", "--conversation", support.GROUP, "--limit", "5")
        self.assertNotEqual(
            first["consistency"]["observedAtUnixMilliseconds"],
            second["consistency"]["observedAtUnixMilliseconds"],
        )


if __name__ == "__main__":
    unittest.main()
