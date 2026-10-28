"""The server's boundary: what reaches the wire, how arguments are converted, how
every failure becomes a tool error result, and which thread answers.

Each test drives the real `mcp` server through the SDK's own client, in-process
over its JSON-RPC framing (`mode="legacy"`: the initialize handshake and memory
streams), against the synthetic store built for real. The only stand-ins are the
clock and the cache directory.
"""

import asyncio
import gzip
import os
import sqlite3
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import TextContent

from fixture_store.build import BY_REPO, EXTRA_LAYERS_DECLARED, GRAPH, MANIFEST, build
from fixture_store.sweep import CALLS, checks
from knowledgestore.server import results, server
from knowledgestore.server.server import KnowledgeStoreServer

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

# DESIGN.md 5.3: the test cap, under which by-repo/infra-modules.json pages.
TEST_CAP = 4096

REGISTRY = "knowledge/relationships/repository-dependencies.json"
KINDS = "knowledge/relationships/kinds.json"
FAQ = EXTRA_LAYERS_DECLARED["faq"]["path"]


OUTSIDE_STORE = {
    "error": {
        "type": "outside_store",
        "message": "a path in the store resolves outside it, so it was not read",
    }
}


class ServerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.cache = self.tmp / "cache"
        # A file beside the store that no answer may ever name.
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def store(self, index: str = "gz", **options) -> Path:
        return build(
            self.tmp / f"s-{index}-{len(list(self.tmp.iterdir()))}", index=index, **options
        )

    def served(self, root: Path, cap: int = results.DEFAULT_MAX_RESPONSE_BYTES, cache=None):
        served = KnowledgeStoreServer(
            root, cache or self.cache, max_response_bytes=cap, clock=lambda: NOW
        )
        self.addCleanup(served.close)
        return served

    def client(self, served: KnowledgeStoreServer) -> Client:
        return Client(served.server, mode="legacy")

    async def call(self, client: Client, tool: str, arguments: dict) -> tuple[dict, bool, str]:
        """The decoded answer, whether it is an error result, and its text."""
        result = await client.call_tool(tool, arguments)
        self.assertEqual(len(result.content), 1, "one content block per answer")
        block = result.content[0]
        assert isinstance(block, TextContent)
        return json.loads(block.text), result.is_error, block.text

    def assertNoHostPath(self, text: str) -> None:
        for path in (str(self.tmp), str(self.outside), "/private/", "/tmp/", "/var/"):
            self.assertNotIn(path, text)

    def escape(self, root: Path, rel: str) -> None:
        """Move a store file outside the store and leave a symlink in its place."""
        target = self.outside / Path(rel).name
        (root / rel).rename(target)
        (root / rel).symlink_to(target)


class WireBytesTest(ServerTestCase):
    async def test_the_text_sent_is_the_capped_encoding_and_nothing_else(self):
        """Catches: the SDK's default `json.dumps` (spaced, ASCII-escaped) on the wire
        in place of `results.encode`, which the cap measures, so a page the pager fit
        under the cap arrives over it; or a second copy of the answer sent as
        structured content beside the text (DESIGN.md 5.3: infra-modules pages at
        4096 bytes)."""
        served = self.served(self.store(), cap=TEST_CAP)
        async with self.client(served) as client:
            cursor = None
            pages = 0
            while True:
                arguments: dict[str, Any] = {"repo": "infra-modules"}
                if cursor:
                    arguments["cursor"] = cursor
                result = await client.call_tool("repo_answer", arguments)
                self.assertFalse(result.is_error)
                self.assertIsNone(result.structured_content)
                self.assertEqual(len(result.content), 1)
                block = result.content[0]
                assert isinstance(block, TextContent)
                sent = block.text.encode("utf-8")
                self.assertLessEqual(len(sent), TEST_CAP)
                self.assertEqual(sent, results.encode(json.loads(sent)))
                pages += 1
                cursor = json.loads(sent)["next_page"]
                if cursor is None:
                    break
        self.assertGreater(pages, 1, "the oversize file must page under the test cap")


class WireTypesTest(ServerTestCase):
    async def asyncSetUp(self):
        self.root = self.store()

    async def refused(self, tool: str, arguments: dict) -> str:
        async with self.client(self.served(self.root)) as client:
            answer, is_error, _ = await self.call(client, tool, arguments)
        self.assertTrue(is_error, answer)
        self.assertEqual(set(answer), {"error"})
        self.assertEqual(answer["error"]["type"], "argument_refused")
        return answer["error"]["message"]

    async def test_a_community_id_given_as_decimal_text_is_converted(self):
        """Catches: a community id sent as text (as some clients send numbers)
        refused, or looked up as the text `"1"` and missed (DESIGN.md 3.3)."""
        async with self.client(self.served(self.root)) as client:
            answer, is_error, _ = await self.call(client, "community", {"id": "1"})
        self.assertFalse(is_error, answer)
        self.assertEqual(answer["community"]["label"], "Platform and delivery")
        self.assertEqual(answer["total"], 24)

    async def test_a_community_id_outside_int64_is_refused_not_raised(self):
        """Catches: an id SQLite cannot bind escaping as an exception, or wrapped
        into the SDK's own error text instead of the server's error shape."""
        for value in (2**63, -(2**63) - 1, str(2**63)):
            with self.subTest(value=value):
                self.assertEqual(
                    await self.refused("community", {"id": value}),
                    "id must be a whole number from -2**63 to 2**63 - 1",
                )

    async def test_a_community_id_that_is_not_a_whole_number_is_refused(self):
        """Catches: `True` read as 1, `1.5` truncated, or text that is not a number
        passed on to the tool."""
        for value in (True, 1.5, "1.0", "one", " 1", [1]):
            with self.subTest(value=value):
                self.assertEqual(
                    await self.refused("community", {"id": value}),
                    "id must be a whole number from -2**63 to 2**63 - 1",
                )

    async def test_a_whole_number_in_other_digits_is_refused(self):
        """Catches: decimal text matched with Unicode digits, which `int` reads
        (`"١"` is 1), so a number spelt in Arabic-Indic or full-width digits is
        accepted as one."""
        for value in ("١", "１", "-٣"):
            with self.subTest(value=value):
                self.assertEqual(
                    await self.refused("community", {"id": value}),
                    "id must be a whole number from -2**63 to 2**63 - 1",
                )

    async def test_repos_given_as_a_list_is_answered_as_the_same_text(self):
        """Catches: a list sent over the wire refused as not text, or converted to
        something that answers differently from the comma-separated names."""
        served = self.served(self.store(minimum_standard=True))
        async with self.client(served) as client:
            listed, listed_error, _ = await self.call(
                client, "minimum_standard", {"repos": ["payments-api", "ledger-service"]}
            )
            text, text_error, _ = await self.call(
                client, "minimum_standard", {"repos": "payments-api,ledger-service"}
            )
        self.assertFalse(listed_error, listed)
        self.assertFalse(text_error, text)
        self.assertEqual(listed, text)

    async def test_repos_that_is_neither_text_nor_a_list_of_text_is_refused(self):
        """Catches: a non-string element or another type reaching the lookup, or a
        refusal that does not name the accepted forms."""
        for value in (["payments-api", 5], 5, True, {"a": "b"}):
            with self.subTest(value=value):
                self.assertEqual(
                    await self.refused("minimum_standard", {"repos": value}),
                    "repos must be a list of repository names (a comma-separated string is also accepted)",
                )

    def test_repos_schema_lists_the_array_first_and_the_string_as_compatibility(self):
        """Catches: the string offered ahead of the array, so a client choosing the
        first branch sends the form that is only kept for v0.2.1 callers."""
        forms = server.BY_NAME["minimum_standard"].schema()["properties"]["repos"]["anyOf"]
        self.assertEqual([form["type"] for form in forms], ["array", "string"])
        self.assertEqual(forms[0]["description"], "a list of repository names")
        self.assertEqual(
            forms[1]["description"], "a comma-separated string, accepted for compatibility"
        )

    def test_repos_declares_both_forms_in_its_schema(self):
        """Catches: `repos` declared as text only, so a client validating against the
        schema never sends the list, or as a list only, which refuses the text."""
        properties = server.BY_NAME["minimum_standard"].schema()["properties"]
        self.assertEqual(
            [
                {k: v for k, v in form.items() if k != "description"}
                for form in properties["repos"]["anyOf"]
            ],
            [{"type": "array", "items": {"type": "string"}}, {"type": "string"}],
        )
        self.assertNotIn("type", properties["repos"])

    async def test_a_cursor_that_is_not_text_is_refused(self):
        """Catches: a numeric cursor passed to the pager, whose parser expects text."""
        self.assertEqual(
            await self.refused("dependents", {"target": "ledger-service", "cursor": 5}),
            "cursor must be text",
        )

    async def test_depth_and_limit_are_whole_numbers(self):
        """Catches: `depth` or `limit` given as a float or text accepted, or an
        out-of-range depth reaching the walk."""
        self.assertEqual(
            await self.refused("neighbours", {"id": "payments-api::PaymentService", "depth": 1.5}),
            "depth must be a whole number from -2**63 to 2**63 - 1",
        )
        self.assertEqual(
            await self.refused("neighbours", {"id": "payments-api::PaymentService", "depth": 3}),
            "depth must be 1 or 2: neighbours walks at most 2 hops",
        )
        self.assertEqual(
            await self.refused("search", {"text": "refund", "limit": "many"}),
            "limit must be a whole number from -2**63 to 2**63 - 1",
        )

    async def test_depth_as_decimal_text_is_converted(self):
        """Catches: a depth sent as text refused, or compared as text."""
        async with self.client(self.served(self.root)) as client:
            answer, is_error, _ = await self.call(
                client, "neighbours", {"id": "payments-api::PaymentService", "depth": "1"}
            )
        self.assertFalse(is_error, answer)
        self.assertEqual(answer["total"], 6)

    async def test_a_missing_or_unknown_argument_is_refused(self):
        """Catches: a missing required argument reaching the tool as a Python
        TypeError, or an argument the tool does not take silently ignored (a
        misspelt `kind` would read as no filter). An unknown name is counted, not
        quoted."""
        self.assertEqual(await self.refused("node", {}), "id is required")
        self.assertEqual(
            await self.refused("node", {"id": "x", "/etc/passwd": 1}),
            "1 argument this tool does not take; it takes: id",
        )

    async def test_a_repository_name_is_bounded_at_1024_bytes(self):
        """Catches: a repository name of any length compared against every estate
        name for near misses, which is unbounded work, in any tool that takes one.
        1024 bytes is accepted (512 two-byte characters); one byte more is not."""
        at_bound, over = "é" * 512, "é" * 512 + "x"
        cases = (
            ("find_repo", "name", {}),
            ("search", "repo", {"text": "refund"}),
            ("dependents", "target", {}),
            ("dependencies", "repo", {}),
            ("repo_answer", "repo", {}),
            ("tickets", "repo", {}),
        )
        for tool, name, others in cases:
            with self.subTest(tool=tool):
                described = "repository name" if tool == "find_repo" else name
                self.assertEqual(
                    await self.refused(tool, {**others, name: over}),
                    f"{described} must be at most 1024 bytes",
                )
                async with self.client(self.served(self.root)) as client:
                    answer, is_error, _ = await self.call(client, tool, {**others, name: at_bound})
                self.assertFalse(is_error, answer)

    async def test_a_boolean_must_be_true_or_false(self):
        """Catches: any truthy value turning on a transitive walk."""
        self.assertEqual(
            await self.refused("dependents", {"target": "ledger-service", "transitive": 1}),
            "transitive must be true or false",
        )

    async def test_null_is_the_same_as_leaving_an_optional_argument_out(self):
        """Catches: a JSON null for an optional argument passed on as None to a tool
        that then refuses it, or as the text "None"."""
        async with self.client(self.served(self.root)) as client:
            answer, is_error, _ = await self.call(
                client, "dependents", {"target": "ledger-service", "kind": None, "cursor": None}
            )
        self.assertFalse(is_error, answer)
        self.assertEqual(answer["total"], 2)

    async def test_an_unknown_tool_is_a_protocol_error(self):
        """Catches: a call to no tool answered as a tool result (the MCP spec makes
        it a protocol error, invalid params), or one that crashes the call."""
        async with self.client(self.served(self.root)) as client:
            with self.assertRaises(MCPError) as caught:
                await client.call_tool("delete_everything", {})
            # The connection still answers afterwards.
            answer, is_error, _ = await self.call(client, "node", {"id": "x"})
        self.assertEqual(caught.exception.error.code, -32602)
        self.assertNotIn("delete_everything", caught.exception.error.message)
        self.assertFalse(is_error, answer)


class ErrorMappingTest(ServerTestCase):
    async def test_a_cursor_from_elsewhere_is_a_cursor_refusal(self):
        """Catches: `CursorRefused` escaping, or reported as some other failure."""
        async with self.client(self.served(self.store())) as client:
            answer, is_error, _ = await self.call(
                client, "dependents", {"target": "ledger-service", "cursor": "1.0.0000000000000000"}
            )
        self.assertTrue(is_error)
        self.assertEqual(answer["error"]["type"], "cursor_refused")

    async def test_a_refusal_quoting_an_absolute_path_does_not_carry_it(self):
        """Catches: a refusal that quotes caller input (the cursor) sent without the
        host-path scrub, the one error message that can echo a path."""
        async with self.client(self.served(self.store())) as client:
            answer, is_error, text = await self.call(
                client, "dependents", {"target": "ledger-service", "cursor": "/etc/passwd"}
            )
        self.assertTrue(is_error)
        self.assertEqual(
            answer,
            {
                "error": {
                    "type": "cursor_refused",
                    "message": "page cursor '<host path>' is not from this listing of the "
                    "current store (the store may have changed since): start again without "
                    "a page cursor",
                }
            },
        )

    async def test_a_graph_the_index_builder_would_refuse_is_a_graph_refusal(self):
        """Catches: `GraphRefused` escaping, losing the reason it names."""
        root = self.store("none")
        (root / GRAPH).write_bytes(
            gzip.compress(json.dumps({"nodes": [{"id": None, "label": "x"}], "links": []}).encode())
        )
        async with self.client(self.served(root)) as client:
            answer, is_error, _ = await self.call(client, "node", {"id": "x"})
        self.assertTrue(is_error)
        self.assertEqual(
            answer,
            {
                "error": {
                    "type": "graph_refused",
                    "message": "a graph node with a null id or kind, which the index refuses",
                }
            },
        )

    async def test_a_layer_file_that_is_not_json_is_unreadable(self):
        """Catches: a parse error escaping, or its text (which can quote the file)
        reaching the client."""
        root = self.store()
        (root / BY_REPO / "ledger-service.json").write_text("{not json", encoding="utf-8")
        async with self.client(self.served(root)) as client:
            answer, is_error, _ = await self.call(client, "repo_answer", {"repo": "ledger-service"})
        self.assertTrue(is_error)
        self.assertEqual(
            answer,
            {"error": {"type": "store_unreadable", "message": "a store file could not be parsed"}},
        )

    async def test_an_unrecognised_layer_is_a_refusal_with_its_reason(self):
        """Catches: `LayerUnrecognised` reported as an unreadable store file - the
        generic `ValueError` sentence, which drops the format found and the format
        read - or answered as a result, which a client reads as an answer (spec,
        Absence semantics). The reason names no host path."""
        root = self.store(relationships_format=None)
        async with self.client(self.served(root)) as client:
            answer, is_error, text = await self.call(
                client, "dependents", {"target": "ledger-service"}
            )
        self.assertTrue(is_error)
        self.assertEqual(
            answer,
            {
                "error": {
                    "type": "layer_unrecognised",
                    "message": "relationships layer present but format unrecognised "
                    "(found none, supports 1.x); not read",
                }
            },
        )
        self.assertNoHostPath(text)

    async def test_an_escaping_registry_names_no_host_path(self):
        """Catches: `OutsideStore`'s own message, which names the store root and the
        target, reaching the client through the registry lookup a name outside the
        estate makes (DESIGN.md 5.4)."""
        root = self.store()
        self.escape(root, REGISTRY)
        async with self.client(self.served(root)) as client:
            answer, is_error, text = await self.call(
                client, "find_repo", {"name": "legacy-network"}
            )
        self.assertTrue(is_error)
        self.assertEqual(answer, OUTSIDE_STORE)
        self.assertNoHostPath(text)

    async def test_an_escaping_kinds_file_names_no_host_path(self):
        """Catches: the same leak through `kinds.json`, read when a kind is given."""
        root = self.store()
        self.escape(root, KINDS)
        async with self.client(self.served(root)) as client:
            answer, is_error, text = await self.call(
                client, "dependents", {"target": "ledger-service", "kind": "helm_chart"}
            )
        self.assertTrue(is_error)
        self.assertEqual(answer, OUTSIDE_STORE)
        self.assertNoHostPath(text)

    async def test_an_extra_layer_escaping_mid_session_names_no_host_path(self):
        """Catches: the same leak through an extra answers layer whose file became a
        symlink out of the store after start-up (a pull), where no reload runs
        because the manifest did not change."""
        root = self.store()
        served = self.served(root)
        self.escape(root, FAQ)
        async with self.client(served) as client:
            answer, is_error, text = await self.call(client, "answers", {"query": "refund"})
        self.assertTrue(is_error)
        self.assertEqual(answer, OUTSIDE_STORE)
        self.assertNoHostPath(text)

    async def test_an_unwritable_cache_is_reported_without_its_path(self):
        """Catches: the index fallback's reason, which carries the OS error and so
        the cache path, returned in `store_info` with the host path in it."""
        blocked = self.tmp / "cache-is-a-file"
        blocked.write_text("", encoding="utf-8")
        async with self.client(self.served(self.store(), cache=blocked)) as client:
            answer, is_error, text = await self.call(client, "store_info", {})
        self.assertFalse(is_error)
        self.assertEqual(
            answer["index"]["fallback"], "the index cache could not be written: Not a directory"
        )
        self.assertNoHostPath(text)

    async def test_an_unwritable_cache_whose_path_has_spaces_names_none_of_it(self):
        """Catches: a fallback reason built from `str(OSError)`, whose quoted filename
        a scrub cut short at the first space, leaving `Application Support/<user>`
        in the answer."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignores file modes")
        cache = self.tmp / "with space" / "Application Support" / "secret-user-name"
        cache.mkdir(parents=True)
        os.chmod(cache, 0o555)
        self.addCleanup(os.chmod, cache, 0o755)
        async with self.client(self.served(self.store(), cache=cache)) as client:
            answer, is_error, text = await self.call(client, "store_info", {})
        self.assertFalse(is_error)
        self.assertEqual(
            answer["index"]["fallback"], "the index cache could not be written: Permission denied"
        )
        for fragment in ("with space", "Application Support", "secret-user-name"):
            self.assertNotIn(fragment, text)
        self.assertNoHostPath(text)

    def test_a_misused_connection_is_internal_not_unreadable(self):
        """Catches: `sqlite3.ProgrammingError` (a closed connection, the wrong
        thread) reported as a damaged store, so the server's own defect is never
        logged."""
        self.assertEqual(
            server.error(sqlite3.ProgrammingError("closed")), ("internal", server.INTERNAL)
        )
        self.assertEqual(
            server.error(sqlite3.DatabaseError("malformed")),
            ("store_unreadable", "the query index could not be read"),
        )


class ThreadsTest(ServerTestCase):
    async def sweep_concurrently(self, index: str) -> None:
        served = self.served(self.store(index))
        answers: dict[tuple[str, int], tuple[dict, bool]] = {}
        async with self.client(served) as client:

            async def one(tool: str, arguments: dict, round_: int) -> None:
                answer, is_error, _ = await self.call(client, tool, arguments)
                answers[(tool, round_)] = (answer, is_error)

            await asyncio.gather(
                *(one(tool, arguments, round_) for round_ in range(3) for tool, arguments in CALLS)
            )
        self.assertEqual(len(answers), 3 * len(CALLS))
        for (tool, round_), (answer, is_error) in sorted(answers.items()):
            with self.subTest(tool=tool, round=round_):
                self.assertFalse(is_error, answer)
                for what, read, literal in checks(tool, index):
                    self.assertEqual(read(answer), literal, what)

    async def test_concurrent_calls_share_the_index_connection_safely(self):
        """Catches: tool handlers run on the SDK's worker-thread pool, where the
        index connection, made with `check_same_thread`, refuses every thread but
        the one that opened it."""
        await self.sweep_concurrently("gz")

    async def test_concurrent_calls_share_the_fallback_label_index_safely(self):
        """Catches: the same for the graph fallback's memoised in-memory FTS5
        connection, built on whichever thread searched first."""
        await self.sweep_concurrently("none")


class StartUpTest(ServerTestCase):
    def test_an_unsupported_manifest_refuses_before_serving(self):
        """Catches: a store the server cannot read served anyway, or the refusal
        raised as something the console script cannot report (spec, Start-up 2)."""
        from knowledgestore.server.manifest import Refused

        root = self.store()
        path = root / MANIFEST
        document = json.loads(path.read_text(encoding="utf-8"))
        document["format"]["major"] = 2
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(Refused):
            KnowledgeStoreServer(root, self.cache, max_response_bytes=TEST_CAP, clock=lambda: NOW)


if __name__ == "__main__":
    unittest.main()
