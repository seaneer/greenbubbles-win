"""HTTP connector and MCP regressions (BUG-004, BUG-008, BUG-013)."""

from __future__ import annotations

import json
import unittest
from urllib.parse import quote

from tests import support


def setUpModule():
    support.require_data()


class HttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = support.HttpServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_health_is_open(self):
        status, body = self.server.json("/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertIn("routes", body)

    def test_messages_include_media_summary_by_default(self):
        status, body = self.server.json(
            f"/api/v1/messages?conversation={support.GROUP}"
            "&since=2026-11-01%2000:00&until=2026-11-19%2023:59&limit=100"
        )
        self.assertEqual(status, 200)
        with_media = [item for item in body["items"] if item.get("media")]
        self.assertTrue(with_media, "expected at least one media summary")
        for item in with_media:
            self.assertIn("kind", item["media"])
            self.assertIn("available", item["media"])
        status, slim = self.server.json(
            f"/api/v1/messages?conversation={support.GROUP}"
            "&since=2026-11-01%2000:00&until=2026-11-19%2023:59&limit=100&media=0"
        )
        self.assertEqual(status, 200)
        self.assertFalse(any(item.get("media") for item in slim["items"]))

    def test_source_exposes_conversations_alias(self):
        status, body = self.server.json("/api/v1/source")
        self.assertEqual(status, 200)
        self.assertEqual(body["conversations"], body["readiness"]["conversations"])

    def test_sanitize_strips_internal_ids(self):
        status, body = self.server.json(
            f"/api/v1/messages?conversation={support.GROUP}&limit=3&sanitize=1"
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["items"])
        for item in body["items"]:
            self.assertNotIn("sender", item)
            self.assertNotIn("conversationId", item)
            self.assertNotIn("id", item)
        codes = [warning.get("code") for warning in body["warnings"]]
        self.assertIn("sanitizedOutput", codes)

    def test_guide_serves_the_agent_prompt(self):
        status, _ = self.server.json("/guide", token=None)
        self.assertEqual(status, 401)
        status, body, headers = self.server.raw("/guide")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers.get("Content-Type", ""))
        text = body.decode("utf-8")
        self.assertIn("Agent 接入说明", text)
        self.assertIn("/api/v1/messages", text)

    def test_missing_token_is_401(self):
        status, _ = self.server.json(f"/api/v1/messages?conversation={support.GROUP}&limit=5", token=None)
        self.assertEqual(status, 401)

    def test_wrong_token_is_401(self):
        status, _ = self.server.json(f"/api/v1/chats?limit=5", token="wrong-token")
        self.assertEqual(status, 401)

    def test_query_token_is_accepted(self):
        status, body = self.server.json(f"/api/v1/chats?limit=5&token={self.server.token}", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_unknown_route_is_404(self):
        status, body = self.server.json("/api/v1/nope")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_missing_conversation_is_400(self):
        status, body = self.server.json("/api/v1/messages?limit=5")
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_bad_time_is_400(self):
        status, body = self.server.json(f"/api/v1/messages?conversation={support.GROUP}&since=nope")
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_paging_matches_cli(self):
        seen = []
        cursor = ""
        while True:
            path = f"/api/v1/messages?conversation={support.GROUP}&limit=100"
            if cursor:
                path += f"&cursor={cursor}"
            status, body = self.server.json(path)
            self.assertEqual(status, 200)
            seen.extend((item["conversationId"], item["sortSequence"]) for item in body["items"])
            cursor = body["page"]["nextCursor"]
            if not body["page"]["hasMore"]:
                break
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), support.shard_truth(support.GROUP)["messages"])

    def test_media_returns_binary_with_type(self):
        status, body = self.server.json(
            f"/api/v1/messages?conversation={support.GROUP}&limit=200&media=1"
        )
        self.assertEqual(status, 200)
        target = None
        for item in body["items"]:
            media = item.get("media") or {}
            if media.get("kind") == "image" and media.get("available"):
                target = item
                break
        if not target:
            self.skipTest("no available image in the sampled window")

        status, payload, headers = self.server.raw(
            f"/api/v1/media?conversation={support.GROUP}&id={target['id']}"
        )
        self.assertEqual(status, 200)
        self.assertGreater(len(payload), 1000)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertTrue(payload.startswith(b"\x89PNG"))

    def test_index_toggle(self):
        support.require_query()
        term = quote(support.QUERY)
        indexed_status, indexed = self.server.json(f"/api/v1/search?q={term}&limit=500")
        self.assertEqual(indexed_status, 200)
        codes = [warning["code"] for warning in indexed["warnings"]]
        if (support.ROOT / "state" / "search-index.sqlite3").exists():
            self.assertIn("searchIndexUsed", codes)
        live_status, live = self.server.json(
            f"/api/v1/search?q={term}&conversation={support.GROUP}&limit=500&index=0"
        )
        self.assertEqual(live_status, 200)
        index_keys = {
            (item["conversationId"], item["sortSequence"])
            for item in indexed["items"]
            if item["conversationId"] == support.GROUP
        }
        live_keys = {(item["conversationId"], item["sortSequence"]) for item in live["items"]}
        self.assertTrue(index_keys <= live_keys, "index returned a message the live scan cannot see")
        self.assertGreater(len(index_keys), 0)


class McpProtocolTests(unittest.TestCase):
    def test_handshake_and_tool_list(self):
        responses, stderr = support.mcp_session(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        )
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], "0.2.1")
        self.assertEqual(len(responses[1]["result"]["tools"]), 6)
        self.assertEqual(len(responses), 2, "notifications must not produce a response")
        self.assertIn("mcp server ready", stderr)

    def test_get_messages_roundtrip(self):
        body = support.unpack_tool(
            support.mcp_tool("get_messages", {"conversation": support.GROUP, "limit": 5})
        )
        self.assertEqual(len(body["items"]), 5)
        self.assertEqual(body["items"][0]["conversationId"], support.GROUP)

    def test_limit_zero_returns_one(self):
        """BUG-004: `limit: 0` used to mean "use the default"."""

        support.require_query()
        body = support.unpack_tool(
            support.mcp_tool("search_messages", {"query": support.QUERY, "limit": 0})
        )
        self.assertEqual(len(body["items"]), 1)

    def test_search_recall_is_not_capped_by_default(self):
        support.require_query()
        body = support.unpack_tool(
            support.mcp_tool("search_messages", {"query": support.QUERY, "limit": 100})
        )
        self.assertGreater(len(body["items"]), 30)
        self.assertLessEqual(len(body["items"]), 100)

    def test_unknown_method_returns_minus_32601(self):
        """BUG-008: negative-path checks are deliberate, not a failure mode."""

        responses, _ = support.mcp_session(
            [{"jsonrpc": "2.0", "id": 7, "method": "does/not/exist"}]
        )
        self.assertEqual(responses[0]["error"]["code"], -32601)

    def test_unknown_tool_sets_is_error(self):
        responses, _ = support.mcp_session(
            [{"jsonrpc": "2.0", "id": 8, "method": "tools/call",
              "params": {"name": "no_such_tool", "arguments": {}}}]
        )
        self.assertTrue(responses[0]["result"]["isError"])

    def test_parse_error_returns_minus_32700(self):
        import subprocess

        process = subprocess.Popen(
            [str(support.PY), str(support.ROOT / "greenbubbles.py"), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=str(support.ROOT),
        )
        out, _ = process.communicate("{ this is not json }\n", timeout=60)
        response = json.loads(out.splitlines()[0])
        self.assertEqual(response["error"]["code"], -32700)

    def test_unicode_payload_survives(self):
        body = support.unpack_tool(
            support.mcp_tool("get_messages", {"conversation": support.GROUP, "limit": 500})
        )
        text = json.dumps(body, ensure_ascii=False)
        if not any(ord(character) > 127 for character in text):
            self.skipTest("the sampled window has no non-ASCII text")


if __name__ == "__main__":
    unittest.main()
