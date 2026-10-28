"""The security controls (spec, Security), held against hostile stores and callers.

- Path containment: a layer symlinked out of the store is refused at open, and an
  id is looked up, never joined into a path.
- Read-only: a full tool sweep over a read-only store changes no byte under it.
- No outbound network and no subprocesses: with both made to raise inside the
  server process, a full sweep still answers.

Every sweep goes through the real `mcp` server with the SDK's own client,
in-process, against the synthetic store built for real.
"""

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from mcp import Client
from mcp_types import TextContent

from fixture_store.build import EXTRA_LAYERS_DECLARED, GRAPH, INDEX_MODES, MANIFEST, build
from fixture_store.sweep import CALLS, checks
from knowledgestore.server import store
from knowledgestore.server.manifest import Refused
from knowledgestore.server.server import KnowledgeStoreServer

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

SCRIPT = Path(sys.executable).parent / "knowledgestore"

FAQ = EXTRA_LAYERS_DECLARED["faq"]["path"]

PLANTED = "planted outside the store: never an answer"


def tree(root: Path) -> dict[str, tuple[str, str]]:
    """Every entry under `root`, by store-relative path: its type and mode, and a
    file's sha256. A new, removed, changed or re-permissioned entry differs."""
    found = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        kind = "dir" if stat.S_ISDIR(info.st_mode) else "file"
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if kind == "file" else ""
        found[path.relative_to(root).as_posix()] = (f"{kind} {oct(info.st_mode)}", digest)
    return found


class SecurityTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.cache = self.tmp / "cache"
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def store(self, index: str = "gz") -> Path:
        return build(self.tmp / f"s-{index}", index=index)

    def served(self, root: Path) -> KnowledgeStoreServer:
        served = KnowledgeStoreServer(
            root, self.cache, max_response_bytes=512 * 1024, clock=lambda: NOW
        )
        self.addCleanup(served.close)
        return served

    async def sweep(self, root: Path, index: str = "gz") -> dict[str, dict]:
        """One call per tool, each answer checked against its DESIGN.md literals."""
        answers = {}
        async with Client(self.served(root).server, mode="legacy") as client:
            for tool, arguments in CALLS:
                result = await client.call_tool(tool, arguments)
                block = result.content[0]
                assert isinstance(block, TextContent)
                answer = json.loads(block.text)
                self.assertFalse(result.is_error, answer)
                for what, read, literal in checks(tool, index):
                    self.assertEqual(read(answer), literal, f"{tool}: {what}")
                answers[tool] = answer
        return answers

    def escape(self, root: Path, rel: str) -> Path:
        target = self.outside / Path(rel).name
        (root / rel).rename(target)
        (root / rel).symlink_to(target)
        return target


class ContainmentTest(SecurityTestCase):
    def test_a_layer_symlinked_out_of_the_store_is_refused_at_open(self):
        """Catches: a store whose graph layer is a symlink to a file outside it
        opened and served, so the first graph read follows the link."""
        root = self.store("none")
        self.escape(root, GRAPH)
        with self.assertRaises(Refused) as caught:
            store.open_store(root, self.cache)
        self.assertIn("'graph'", str(caught.exception))
        self.assertNotIn(str(self.outside), str(caught.exception))

    def test_an_extra_layer_symlinked_out_of_the_store_is_refused_at_open(self):
        """Catches: the open-time check reading only the library's layers, so an
        extra layer the manifest declares escapes it."""
        root = self.store()
        self.escape(root, FAQ)
        with self.assertRaises(Refused) as caught:
            store.open_store(root, self.cache)
        self.assertIn("'faq'", str(caught.exception))

    def test_the_console_script_refuses_an_escaping_store_on_stderr(self):
        """Catches: the open-time refusal written to stdout, the protocol channel,
        or ending in a traceback rather than exit status 2."""
        root = self.store("none")
        self.escape(root, GRAPH)
        result = subprocess.run(
            [str(SCRIPT), "serve", "--store", str(root)],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "HOME": str(self.tmp), "XDG_CACHE_HOME": str(self.cache)},
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("resolves outside the store", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_manifest_symlinked_out_names_no_target_on_stderr(self):
        """Catches: the start-up refusal of an escaping manifest quoting where the
        symlink resolves to, a host path the store chose."""
        root = self.store()
        self.escape(root, MANIFEST)
        result = subprocess.run(
            [str(SCRIPT), "serve", "--store", str(root)],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "HOME": str(self.tmp), "XDG_CACHE_HOME": str(self.cache)},
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("a manifest outside the store", result.stderr)
        self.assertNotIn(str(self.outside), result.stderr)

    async def test_a_reload_that_meets_an_escaping_layer_says_so(self):
        """Catches: a pull that makes a layer escape served from the new store, or
        refused with the notice for an unreadable manifest, which names the wrong
        cause; and a notice that names a host path."""
        root = self.store()
        served = self.served(root)
        async with Client(served.server, mode="legacy") as client:
            self.escape(root, FAQ)
            path = root / MANIFEST
            document = json.loads(path.read_text(encoding="utf-8"))
            document["digest"] = "changed-by-a-pull"
            path.write_text(json.dumps(document), encoding="utf-8")
            for tool, arguments in (("store_info", {}), ("find_repo", {"name": "payment-api"})):
                result = await client.call_tool(tool, arguments)
                block = result.content[0]
                assert isinstance(block, TextContent)
                answer = json.loads(block.text)
                with self.subTest(tool=tool):
                    self.assertFalse(result.is_error, answer)
                    self.assertEqual(
                        answer["store"]["notice"],
                        "reload refused: a layer path leaves the store; "
                        "still serving the previous store",
                    )
                    self.assertNotIn(str(self.tmp), block.text)
                    for what, read, literal in checks(tool):
                        self.assertEqual(read(answer), literal, what)

    async def test_a_node_id_that_reads_as_a_path_is_a_lookup_miss(self):
        """Catches: a tool argument joined to the store root and read, so an id
        like `../../etc/passwd` names a file instead of a node. A file planted at
        the place a join would reach must never appear in an answer."""
        root = self.store()
        (self.tmp / "planted").write_text(PLANTED, encoding="utf-8")
        async with Client(self.served(root).server, mode="legacy") as client:
            for node_id in ("../../etc/passwd", "../planted", str(self.tmp / "planted")):
                with self.subTest(id=node_id):
                    result = await client.call_tool("node", {"id": node_id})
                    block = result.content[0]
                    assert isinstance(block, TextContent)
                    answer = json.loads(block.text)
                    self.assertFalse(result.is_error, answer)
                    self.assertIsNone(answer["node"])
                    self.assertEqual(
                        answer["searched"], "searched 12 repositories in the index layer"
                    )
                    self.assertNotIn(PLANTED, block.text)
                    self.assertNotIn("root:", block.text)


class ReadOnlyTest(SecurityTestCase):
    def make_read_only(self, root: Path) -> None:
        paths = [root, *root.rglob("*")]
        modes = {path: path.lstat().st_mode for path in paths}

        def restore():
            for path in sorted(modes, key=lambda p: len(p.parts)):
                os.chmod(path, stat.S_IMODE(modes[path]))

        self.addCleanup(restore)
        for path in sorted(paths, key=lambda p: -len(p.parts)):
            os.chmod(path, 0o555 if path.is_dir() else 0o444)

    async def test_a_full_sweep_writes_nothing_under_the_store(self):
        """Catches: any write under the store - a decompressed index left beside the
        archive, a SQLite journal beside an index opened in place, a cache inside the
        store - in every index mode. Each mode is swept against a read-only store, so
        a write that fails surfaces as a failed answer, and one that succeeds as a
        changed tree."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignores file modes")
        for index in INDEX_MODES:
            with self.subTest(index=index):
                root = self.store(index)
                self.make_read_only(root)
                before = tree(root)
                await self.sweep(root, index)
                self.assertEqual(tree(root), before)


class NoNetworkNoSubprocessTest(SecurityTestCase):
    async def test_a_full_sweep_needs_neither_a_socket_nor_a_child_process(self):
        """Catches: any tool opening a socket (an outbound fetch) or starting a
        process (`git`, a shell). Both are made to raise inside this process, which
        is the server process here, before the store is opened; the sweep must
        still answer every tool with its DESIGN.md literals."""

        def refused(*args, **kwargs):
            raise AssertionError("the server opened a socket or started a process")

        root = self.store()
        with (
            mock.patch.object(socket.socket, "__init__", refused),
            mock.patch.object(subprocess.Popen, "__init__", refused),
        ):
            await self.sweep(root)


if __name__ == "__main__":
    unittest.main()
