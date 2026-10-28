"""Which copy of the store the server answers from, and the switch to the clone the
asker registered (issue #28).

`store_info` says what chose the store (`store_source`) and which copy it is
(`served`), with no host path. A store chosen through `CLAUDE_PLUGIN_ROOT` gives
way, on the next call, to a clone the locations file lists for the same
`store.id`; no other source ever switches.

Each store is built by the synthetic fixture, then given a manifest `digest` of
our own, so every expected digest is written here by hand. Every home and cache
is a temporary directory: nothing here reads the real home.
"""

import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import TextContent

from fixture_store.build import MANIFEST, build
from knowledgestore.server import locate, results, server, store
from knowledgestore.server.server import KnowledgeStoreServer

SCRIPT = Path(sys.executable).parent / "knowledgestore"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

# The fixture's store.id, and its newest provenance commit as an instant
# (reporting-worker, 01:30 UTC on 16 October), kept as written (DESIGN.md 1).
STORE_ID = "example-store"
AS_OF = "2026-10-15T23:30:00-02:00"

# Two copies of one store, told apart only by the manifest digest they carry.
PLUGIN_DIGEST = "b1a6b1a6b1a6b1a6"
CLONE_DIGEST = "c10ec10ec10ec10e"
SWITCHED = "reloaded: now serving the registered clone"
AMBIGUOUS = "reload deferred: more than one registered clone carries this store id"

# Small enough that the store's repositories take more than one page.
PAGED_CAP = 2048


def stamp(root: Path, **fields) -> Path:
    """Set top-level manifest fields; a value of None removes the field."""
    path = root / MANIFEST
    document = json.loads(path.read_text(encoding="utf-8"))
    for name, value in fields.items():
        if value is None:
            document.pop(name, None)
        else:
            document[name] = value
    path.write_text(json.dumps(document), encoding="utf-8")
    return root


def set_store_id(root: Path, store_id: str) -> None:
    path = root / MANIFEST
    document = json.loads(path.read_text(encoding="utf-8"))
    document["store"]["id"] = store_id
    path.write_text(json.dumps(document), encoding="utf-8")


class ServedCopyTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cache = self.tmp / "cache"
        self.plugin = stamp(build(self.tmp / "plugin", index="none"), digest=PLUGIN_DIGEST)
        self.clone = stamp(build(self.tmp / "clone", index="none"), digest=CLONE_DIGEST)

    def register(self, *clones: Path) -> None:
        listing = self.home / ".config" / "knowledge-store" / "locations"
        listing.parent.mkdir(parents=True, exist_ok=True)
        listing.write_text("".join(f"{clone}\n" for clone in clones), encoding="utf-8")

    def served(
        self, root: Path, source: str, cap: int = results.DEFAULT_MAX_RESPONSE_BYTES
    ) -> KnowledgeStoreServer:
        """A server as the command line builds one: the source that chose `root`,
        and the locations file under this test's home."""
        served = KnowledgeStoreServer(
            root,
            self.cache,
            max_response_bytes=cap,
            clock=lambda: NOW,
            source=source,
            registered=locate.Registered(self.home),
        )
        self.addCleanup(served.close)
        return served

    def call(self, served: KnowledgeStoreServer, arguments: dict | None = None):
        """`store_info`: the decoded answer, whether it is an error, and its text."""

        async def main():
            client = Client(served.server, mode="legacy")
            async with client:
                result = await client.call_tool("store_info", arguments or {})
            block = result.content[0]
            assert isinstance(block, TextContent)
            return json.loads(block.text), result.is_error, block.text

        return asyncio.run(main())

    def assertNoHostPath(self, text: str) -> None:
        for path in (str(self.tmp), "/private/", "/tmp/", "/var/"):
            self.assertNotIn(path, text)


class SourceAndIdentityTest(ServedCopyTestCase):
    def test_a_manifest_without_a_digest_is_identified_by_its_bytes(self):
        """Catches: `served.digest` empty or null for a manifest that carries no
        `digest`, so the skill has nothing to compare with its clone; the bytes'
        sha256 is the identity the reload check already uses."""
        stamp(self.plugin, digest=None)
        expected = hashlib.sha256((self.plugin / MANIFEST).read_bytes()).hexdigest()
        answer, is_error, text = self.call(self.served(self.plugin, "--store"))
        self.assertFalse(is_error, answer)
        self.assertEqual(answer["served"]["digest"], expected)
        self.assertNoHostPath(text)


class StdioSourcesTest(ServedCopyTestCase):
    """The console script, started each of the four ways, as a client starts it."""

    def run_script(self, args: list[str], env: dict[str, str]) -> tuple[dict, bool | None, str]:
        async def main():
            params = StdioServerParameters(
                command=str(SCRIPT),
                args=["serve", *args],
                env={"HOME": str(self.home), "XDG_CACHE_HOME": str(self.cache), **env},
            )
            async with Client(params) as client:
                result = await client.call_tool("store_info", {})
            block = result.content[0]
            assert isinstance(block, TextContent)
            return json.loads(block.text), result.is_error, block.text

        return asyncio.run(main())

    def test_store_info_says_which_rule_chose_the_store_and_which_copy_it_is(self):
        """Catches: `store_info` silent on how the store was found or which copy
        it is, so a skill cannot tell the plugin's copy from its own clone; a
        `plugin_root` flag set for a store the plugin root did not supply; or a
        host path in the answer."""
        self.register(self.clone)
        starts = {
            "--store": (["--store", str(self.clone)], {}),
            "KNOWLEDGE_STORE_PATH": ([], {"KNOWLEDGE_STORE_PATH": str(self.clone)}),
            "--store-id": (["--store-id", STORE_ID], {}),
            "CLAUDE_PLUGIN_ROOT": ([], {"CLAUDE_PLUGIN_ROOT": str(self.plugin)}),
        }
        expected = {
            "--store": (CLONE_DIGEST, False),
            "KNOWLEDGE_STORE_PATH": (CLONE_DIGEST, False),
            "--store-id": (CLONE_DIGEST, False),
            "CLAUDE_PLUGIN_ROOT": (PLUGIN_DIGEST, True),
        }
        for source, (args, env) in starts.items():
            with self.subTest(source=source):
                if source == "CLAUDE_PLUGIN_ROOT":
                    # With the clone registered, the plugin copy would give way on
                    # the first call, so it is started with an empty listing.
                    self.register()
                answer, is_error, text = self.run_script(args, env)
                self.assertFalse(is_error, answer)
                digest, plugin_root = expected[source]
                self.assertEqual(answer["store_source"], source)
                self.assertEqual(
                    answer["served"],
                    {
                        "store_id": STORE_ID,
                        "digest": digest,
                        "sources_as_of": AS_OF,
                        "plugin_root": plugin_root,
                    },
                )
                self.assertIsNone(answer["store"]["notice"])
                self.assertNoHostPath(text)

    def test_a_clone_registered_after_start_replaces_the_plugin_copy(self):
        """Catches the issue's case: the locations file is empty at start, the
        plugin root is a store, and the asker's setup registers its clone
        afterwards. A server that resolves only at start serves the plugin copy
        for the whole session while the skill reports its clone as current; one
        that switches without saying so leaves the asker unaware the copy
        changed; one that switches back when the entry goes serves the stale
        copy again."""

        async def main():
            params = StdioServerParameters(
                command=str(SCRIPT),
                args=["serve"],
                env={
                    "HOME": str(self.home),
                    "XDG_CACHE_HOME": str(self.cache),
                    "CLAUDE_PLUGIN_ROOT": str(self.plugin),
                },
            )
            answers = []
            async with Client(params) as client:
                for step in ("before", "registered", "after", "unregistered"):
                    if step == "registered":
                        self.register(self.clone)
                    if step == "unregistered":
                        self.register()
                    result = await client.call_tool("store_info", {})
                    block = result.content[0]
                    assert isinstance(block, TextContent)
                    answers.append((json.loads(block.text), result.is_error, block.text))
            return answers

        self.register()
        before, registered, after, unregistered = asyncio.run(main())
        for answer, is_error, text in (before, registered, after, unregistered):
            self.assertFalse(is_error, answer)
            self.assertNoHostPath(text)

        self.assertEqual(before[0]["store_source"], "CLAUDE_PLUGIN_ROOT")
        self.assertEqual(before[0]["served"]["digest"], PLUGIN_DIGEST)
        self.assertTrue(before[0]["served"]["plugin_root"])
        self.assertIsNone(before[0]["store"]["notice"])

        # The call after the entry is written serves the clone, and says so.
        self.assertEqual(registered[0]["store_source"], "--store-id")
        self.assertEqual(registered[0]["served"]["digest"], CLONE_DIGEST)
        self.assertFalse(registered[0]["served"]["plugin_root"])
        self.assertEqual(registered[0]["store"]["notice"], SWITCHED)

        # Said once: the next call is an ordinary answer from the clone.
        self.assertEqual(after[0]["served"]["digest"], CLONE_DIGEST)
        self.assertIsNone(after[0]["store"]["notice"])

        # Never back to the plugin copy, even when the entry goes.
        self.assertEqual(unregistered[0]["served"]["digest"], CLONE_DIGEST)
        self.assertEqual(unregistered[0]["store_source"], "--store-id")


class SwitchRuleTest(ServedCopyTestCase):
    def test_an_entry_for_another_store_id_does_not_switch(self):
        """Catches: a switch to any registered clone, so the plugin's store is
        replaced by an unrelated store the asker happens to have cloned."""
        set_store_id(self.clone, "other-store")
        served = self.served(self.plugin, "CLAUDE_PLUGIN_ROOT")
        self.call(served)
        self.register(self.clone)
        answer, is_error, _ = self.call(served)
        self.assertFalse(is_error, answer)
        self.assertEqual(answer["served"]["digest"], PLUGIN_DIGEST)
        self.assertEqual(answer["store_source"], "CLAUDE_PLUGIN_ROOT")
        self.assertIsNone(answer["store"]["notice"])

    def test_an_entry_naming_the_plugin_root_itself_is_not_a_switch(self):
        """Catches: the plugin root, listed in the locations file, taken for a
        different clone, so every call reopens the same copy and says it
        switched."""
        served = self.served(self.plugin, "CLAUDE_PLUGIN_ROOT")
        self.register(self.plugin)
        answers = [self.call(served) for _ in range(2)]
        for answer, is_error, _ in answers:
            self.assertFalse(is_error, answer)
            self.assertIsNone(answer["store"]["notice"])
            self.assertEqual(answer["store_source"], "CLAUDE_PLUGIN_ROOT")
            self.assertTrue(answer["served"]["plugin_root"])

    def test_a_store_chosen_by_any_other_rule_never_switches(self):
        """Catches: a re-resolve that runs whatever chose the store, so an
        explicit --store, the environment or --store-id is overridden by a
        locations entry written later."""
        for source in ("--store", "KNOWLEDGE_STORE_PATH", "--store-id"):
            with self.subTest(source=source):
                self.register()
                served = self.served(self.plugin, source)
                self.call(served)
                self.register(self.clone)
                answer, is_error, _ = self.call(served)
                self.assertFalse(is_error, answer)
                self.assertEqual(answer["served"]["digest"], PLUGIN_DIGEST)
                self.assertEqual(answer["store_source"], source)
                self.assertIsNone(answer["store"]["notice"])


class SwitchSafetyTest(ServedCopyTestCase):
    def test_two_clones_carrying_the_store_id_are_said_once_until_the_listing_changes(self):
        """Catches: two registered clones with the plugin store's id leaving the
        plugin copy served with no signal, so the asker never learns why their
        clone is not answering; or the notice repeated on every answer."""
        twin = stamp(build(self.tmp / "twin", index="none"), digest="7a1a7a1a7a1a7a1a")
        served = self.served(self.plugin, "CLAUDE_PLUGIN_ROOT")
        self.register(self.clone, twin)
        said, again = self.call(served), self.call(served)
        for answer, is_error, _ in (said, again):
            self.assertFalse(is_error, answer)
            self.assertEqual(answer["served"]["digest"], PLUGIN_DIGEST)
            self.assertEqual(answer["store_source"], "CLAUDE_PLUGIN_ROOT")
        self.assertEqual(said[0]["store"]["notice"], AMBIGUOUS)
        self.assertIsNone(again[0]["store"]["notice"])

        # Put right in the locations file: the next call switches.
        self.register(self.clone)
        answer, is_error, _ = self.call(served)
        self.assertFalse(is_error, answer)
        self.assertEqual(answer["served"]["digest"], CLONE_DIGEST)
        self.assertEqual(answer["store"]["notice"], SWITCHED)

    def test_the_listing_is_not_read_again_while_nothing_it_names_has_changed(self):
        """Catches: the locations file, and every listed clone's manifest, read on
        every call while the plugin root is served, though nothing has changed
        since the last read found no clone to switch to; or a change to the file
        not read."""
        set_store_id(self.clone, "other-store")
        self.register(self.clone)
        served = self.served(self.plugin, "CLAUDE_PLUGIN_ROOT")
        with mock.patch.object(locate, "_listed_clones", wraps=locate._listed_clones) as listed:
            for _ in range(3):
                answer, is_error, _ = self.call(served)
                self.assertFalse(is_error, answer)
            self.assertEqual(listed.call_count, 1)
            # Another line in the file, so its size changes: read once more.
            self.register(self.clone, self.tmp / "not-a-clone")
            self.call(served)
            self.call(served)
            self.assertEqual(listed.call_count, 2)

    def test_a_listed_clone_pulled_later_is_switched_to_without_a_locations_write(self):
        """Catches: a clone listed before it held a manifest never looked at again,
        because the locations file itself did not change after it was pulled."""
        held = self.tmp / "manifest-aside.json"
        (self.clone / MANIFEST).rename(held)
        self.register(self.clone)
        served = self.served(self.plugin, "CLAUDE_PLUGIN_ROOT")
        answer, _, _ = self.call(served)
        self.assertEqual(answer["served"]["digest"], PLUGIN_DIGEST)

        held.rename(self.clone / MANIFEST)
        answer, is_error, _ = self.call(served)
        self.assertFalse(is_error, answer)
        self.assertEqual(answer["served"]["digest"], CLONE_DIGEST)
        self.assertEqual(answer["store"]["notice"], SWITCHED)

    def test_a_clone_that_fails_to_open_leaves_the_plugin_copy_answering(self):
        """Catches: a registered clone whose opening fails in a way other than a
        refusal (here, any exception) turning every answer into an error, where
        the plugin copy answered before the clone was registered."""
        clone, real = self.clone, store.open_store

        def opening(root, cache_home):
            if root == clone:
                raise RuntimeError("the clone could not be opened")
            return real(root, cache_home)

        with mock.patch.object(server, "open_store", opening):
            served = self.served(self.plugin, "CLAUDE_PLUGIN_ROOT")
            self.register(self.clone)
            with self.assertLogs("knowledgestore.server.store", "WARNING"):
                answer, is_error, text = self.call(served)
        self.assertFalse(is_error, answer)
        self.assertEqual(answer["served"]["digest"], PLUGIN_DIGEST)
        self.assertEqual(answer["store_source"], "CLAUDE_PLUGIN_ROOT")
        self.assertIsNone(answer["store"]["notice"])
        self.assertNoHostPath(text)


class CursorAcrossSwitchTest(ServedCopyTestCase):
    def test_a_cursor_from_the_plugin_copy_is_refused_after_the_switch(self):
        """Catches: a page cursor made for the plugin copy applied to the clone
        after the switch, so a listing silently continues in another copy of the
        store. The two copies list the same repositories, so only the store
        digest the cursor is bound to tells them apart."""
        served = self.served(self.plugin, "CLAUDE_PLUGIN_ROOT", cap=PAGED_CAP)
        first, is_error, _ = self.call(served)
        self.assertFalse(is_error, first)
        cursor = first["next_page"]
        self.assertIsNotNone(cursor, "the test cap must page the store's repositories")

        self.register(self.clone)
        answer, is_error, text = self.call(served, {"cursor": cursor})
        self.assertTrue(is_error, answer)
        self.assertEqual(answer["error"]["type"], "cursor_refused")
        self.assertNoHostPath(text)

        # The switch happened: a fresh listing comes from the clone.
        fresh, is_error, _ = self.call(served)
        self.assertFalse(is_error, fresh)
        self.assertEqual(fresh["served"]["digest"], CLONE_DIGEST)


if __name__ == "__main__":
    unittest.main()
