"""The server's security invariants (SECURITY.md, "The MCP server"), held as tests.

Three claims, each named here with what breaks it:

- The server cannot write, spawn or reach the network. A static read of every module
  under `knowledgestore/server/`: the runtime sweeps in `test_security.py` show that
  one run did not, this shows the code has no way to.
- No tool output names a host path, on success or on refusal.
- Store text is returned only under explicit text fields.

`test_security.py` already holds containment, a read-only sweep, and a sweep with
sockets and processes made to raise; this module adds what those cannot see.
"""

import ast
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import knowledgestore.server as server_package
from mcp import Client
from mcp_types import TextContent

from fixture_store.build import HOSTILE, build
from fixture_store.sweep import CALLS
from knowledgestore.server.server import KnowledgeStoreServer

PACKAGE = Path(server_package.__file__).resolve().parent

# The one module that writes: the query-index cache, outside the store, written to a
# temp file and renamed. A write in any other module fails the static check below.
# What it writes *to* is held at runtime: `test_security.py` sweeps a read-only store
# in every index mode and `test_index_cache.py` pins the cache location.
CACHE_WRITER = "index.py"

# Importing any of these gives a module a way to start a process, reach a network or
# remove files. Prefix matches: `urllib.request` is `urllib`.
FORBIDDEN_IMPORTS = frozenset(
    {
        "subprocess",
        "socket",
        "ssl",
        "urllib",
        "http",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "shutil",
        "multiprocessing",
        "pty",
    }
)

# `os.<name>` calls that start a process.
FORBIDDEN_OS = frozenset(
    {"system", "popen", "fork", "forkpty", "execv", "execve", "execl", "execvp", "execlp"}
    | {"spawnl", "spawnv", "spawnve", "spawnvp", "posix_spawn", "posix_spawnp"}
)

# Calls that change the filesystem. Allowed only in CACHE_WRITER.
WRITE_METHODS = frozenset(
    {"write_text", "write_bytes", "unlink", "mkdir", "rmdir", "touch", "symlink_to", "hardlink_to"}
)
WRITE_OS = frozenset(
    {"replace", "rename", "remove", "unlink", "rmdir", "mkdir", "makedirs", "removedirs"}
    | {"symlink", "link", "truncate", "chmod", "chown", "utime"}
)


def _modes(call: ast.Call) -> list[str]:
    """Every string a call passes where an `open` mode could be."""
    strings = [
        a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
    ]
    strings += [
        k.value.value
        for k in call.keywords
        if k.arg == "mode" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)
    ]
    return strings


def _opens_for_writing(call: ast.Call) -> bool:
    """`open`, `Path.open`, `os.fdopen`, `os.open` given a write, append, create or
    update mode, or an `O_*` write flag. The builtin `open` and `os.fdopen` take the
    mode second, so a mode there that is not a literal is refused: a mode that cannot
    be read cannot be shown read-only. A method called `open` on something else
    (the server's own `relocate.open(clone)` opens a store) is judged only by a
    literal mode."""
    func = call.func
    if isinstance(func, ast.Name) and func.id == "open":
        computed = len(call.args) > 1 or any(k.arg == "mode" for k in call.keywords)
    elif isinstance(func, ast.Attribute) and func.attr in ("open", "fdopen"):
        owner = func.value.id if isinstance(func.value, ast.Name) else ""
        if owner == "os" and func.attr == "open":
            flags = {n.attr for n in ast.walk(call) if isinstance(n, ast.Attribute)}
            return bool(flags & {"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"})
        computed = func.attr == "fdopen" and (
            len(call.args) > 1 or any(k.arg == "mode" for k in call.keywords)
        )
    else:
        return False
    modes = _modes(call)
    if any(set(mode) & set("wax+") for mode in modes):
        return True
    return computed and not modes


def _import_problems(node: ast.AST, where: str) -> list[str]:
    """Imports that give a module a process, a network, a remover, or the library."""
    found = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in FORBIDDEN_IMPORTS:
                found.append(f"{where} imports {alias.name}")
            if top == "knowledgestore" and not alias.name.startswith("knowledgestore.server"):
                found.append(f"{where} imports the library: {alias.name}")
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if node.level == 0 and module.split(".")[0] in FORBIDDEN_IMPORTS:
            found.append(f"{where} imports from {module}")
        if node.level == 0 and module.split(".")[0] == "knowledgestore":
            if not module.startswith("knowledgestore.server"):
                found.append(f"{where} imports the library: {module}")
        # `from .. import io` reaches the library's writers from inside the package.
        if node.level >= 2:
            found.append(f"{where} imports from outside the server package")
    return found


def _call_problems(call: ast.Call, where: str, *, may_write: bool) -> list[str]:
    """Calls that start a process or, outside the cache writer, change the filesystem."""
    found = []
    func = call.func
    owner = (
        func.value.id
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
        else ""
    )
    attr = func.attr if isinstance(func, ast.Attribute) else ""
    if owner == "os" and attr in FORBIDDEN_OS:
        found.append(f"{where} calls os.{attr}")
    if may_write:
        return found
    if _opens_for_writing(call):
        found.append(f"{where} opens a file for writing")
    if attr in WRITE_METHODS:
        found.append(f"{where} calls .{attr}()")
    if owner == "os" and attr in WRITE_OS:
        found.append(f"{where} calls os.{attr}")
    if owner == "tempfile":
        found.append(f"{where} calls tempfile.{attr}")
    return found


def violations(source: str, filename: str, *, may_write: bool) -> list[str]:
    """What in this module could write, spawn or reach a network. Empty when nothing."""
    found = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        where = f"{filename}:{getattr(node, 'lineno', 0)}"
        found += _import_problems(node, where)
        if isinstance(node, ast.Call):
            found += _call_problems(node, where, may_write=may_write)
    return found


def package_violations(directory: Path) -> list[str]:
    found = []
    for path in sorted(directory.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        found += violations(source, path.name, may_write=path.name == CACHE_WRITER)
    return found


class ReadOnlyByConstructionTest(unittest.TestCase):
    def test_no_module_of_the_server_can_write_spawn_or_reach_a_network(self):
        """Catches: a server module importing `subprocess`, a socket or HTTP client,
        `shutil`, or a library module that writes (`knowledgestore.io`); calling
        `os.system`; opening a file for writing; or removing, renaming or creating a
        path - anywhere but the cache writer. The server being read-only is a
        property of its source, so it is read from its source."""
        self.assertEqual(package_violations(PACKAGE), [])

    def test_the_scan_reads_the_modules_the_package_holds(self):
        """Catches: the check above going vacuous - a moved package, or a glob that
        matches nothing, which would read as a clean pass over no code."""
        names = {path.name for path in PACKAGE.glob("*.py")}
        self.assertGreaterEqual(len(names), 15, names)
        self.assertTrue({"server.py", "index.py", "store.py", "cli.py"} <= names, names)
        self.assertIn(CACHE_WRITER, names)

    def test_the_cache_writer_is_the_only_module_that_writes_at_all(self):
        """Catches: the allowance for the cache writer widening into a way to write
        everywhere. With the allowance removed, the cache writer is the only module
        reported - so every write the package has is in that one file."""
        found = []
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            found += [
                line
                for line in violations(source, path.name, may_write=False)
                if "imports" not in line
            ]
        self.assertTrue(found, "the cache writer was expected to be flagged without its allowance")
        self.assertEqual({line.split(":")[0] for line in found}, {CACHE_WRITER})

    def test_the_scan_notices_each_kind_of_write_path(self):
        """Catches: a scan that has stopped recognising a way to write - each source
        here is one the package must never contain, and each must be reported."""
        bad = {
            "import subprocess": "imports subprocess",
            "import socket": "imports socket",
            "from urllib.request import urlopen": "imports from urllib.request",
            "import http.client": "imports http.client",
            "import shutil": "imports shutil",
            "from knowledgestore import io": "imports the library",
            "from knowledgestore.io import write_json": "imports the library",
            "from .. import io": "outside the server package",
            "import os\nos.system('x')": "os.system",
            "open('f', 'w')": "opens a file for writing",
            "open('f', mode='a')": "opens a file for writing",
            "from pathlib import Path\nPath('f').open('wb')": "opens a file for writing",
            "from pathlib import Path\nPath('f').write_text('x')": ".write_text()",
            "import os\nos.remove('f')": "os.remove",
            "import tempfile\ntempfile.mkstemp()": "tempfile.mkstemp",
        }
        for source, expected in bad.items():
            with self.subTest(source=source):
                found = violations(source, "x.py", may_write=False)
                self.assertTrue(any(expected in line for line in found), (source, found))

    def test_the_scan_leaves_reads_alone(self):
        """Catches: a scan so broad it refuses the server's own reads, which would be
        silenced by loosening it - so the reads it must allow are pinned."""
        reads = [
            "open('f')",
            "open('f', 'rb')",
            "from pathlib import Path\nPath('f').open('rb')",
            "from pathlib import Path\nPath('f').read_text()",
            "import sqlite3\nsqlite3.connect('file:x?mode=ro', uri=True)",
            "import os\nos.fspath('x')",
        ]
        for source in reads:
            with self.subTest(source=source):
                self.assertEqual(violations(source, "x.py", may_write=False), [])


class SweepTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.cache = self.tmp / "cache"
        self.root = build(self.tmp / "store", index="gz")

    async def answers(self, calls: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, Any]]:
        served = KnowledgeStoreServer(
            self.root,
            self.cache,
            max_response_bytes=512 * 1024,
            clock=lambda: datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),
        )
        self.addCleanup(served.close)
        out = []
        async with Client(served.server, mode="legacy") as client:
            for tool, arguments in calls:
                result = await client.call_tool(tool, arguments)
                block = result.content[0]
                assert isinstance(block, TextContent)
                out.append((tool, json.loads(block.text)))
        return out


# Calls the server refuses or misses, so the refusal text is swept as well as the
# answers. Each is a different message-building path.
REFUSED_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("node", {"id": "../../etc/passwd"}),
    ("node", {"id": "no-such::node"}),
    ("neighbours", {"id": "payments-api::PaymentService", "depth": 9}),
    ("search", {"text": "RefundHandler", "page": "not-a-cursor"}),
    ("search", {"text": "x" * 5000}),
    ("ticket", {"key": "NOPE-1"}),
    ("dependents", {"target": "no-such-repo"}),
    ("repo_answer", {"repo": "no-such-repo"}),
    ("minimum_standard", {"repos": "no-such-repo"}),
]


class NoHostPathTest(SweepTestCase):
    async def test_no_tool_answer_or_refusal_names_a_host_path(self):
        """Catches: a tool returning the store root, the cache directory, or the
        temp tree the store sits in - in `store_info`, a notice, a `searched`
        sentence, or the message of a refusal. Every tool is called once, and nine
        calls it must refuse or miss are added so refusal text is read too."""
        host = [str(self.tmp), str(self.root), str(self.cache), str(self.tmp.parent)]
        swept = await self.answers([*CALLS, *REFUSED_CALLS])
        self.assertEqual(len(swept), len(CALLS) + len(REFUSED_CALLS))
        refused = [answer for _, answer in swept[len(CALLS) :] if "error" in answer]
        self.assertGreaterEqual(len(refused), 3, "the refusal calls stopped refusing")
        for tool, answer in swept:
            text = json.dumps(answer)
            for path in host:
                with self.subTest(tool=tool, path=path):
                    self.assertNotIn(path, text)


TEXT_FIELDS = frozenset({"text", "summary", "description", "line", "body"})


def occurrences(value: Any, needle: str, key: str = "") -> list[str]:
    """The key each string containing `needle` sits under, anywhere in the answer."""
    if isinstance(value, dict):
        return [k for name, item in value.items() for k in occurrences(item, needle, name)]
    if isinstance(value, list):
        return [k for item in value for k in occurrences(item, needle, key)]
    return [key] if isinstance(value, str) and needle in value else []


class StoreTextFieldsTest(SweepTestCase):
    async def test_store_text_is_returned_only_under_explicit_text_fields(self):
        """Catches: instruction-shaped store text leaking into a sentence the server
        composes (`searched`, `reach_note`, `not_applied`, a `notice`, an error
        message) or into a key's name rather than a data field. The fixture store
        holds one hostile commit description; wherever it appears in any answer, the
        key above it must be an explicit text field."""
        calls = [
            *CALLS,
            ("tickets", {"repo": "ledger-service"}),
            ("answers", {"query": "ignore previous instructions landed"}),
            ("search", {"text": "ignore previous instructions"}),
        ]
        swept = await self.answers(calls)
        found = [(tool, key) for tool, answer in swept for key in occurrences(answer, HOSTILE)]
        self.assertTrue(found, "the hostile text never came back, so nothing was checked")
        for tool, key in found:
            with self.subTest(tool=tool, key=key):
                self.assertIn(key, TEXT_FIELDS)

    async def test_the_hostile_text_is_a_key_nowhere(self):
        """Catches: store text becoming structure - a key built from a ticket
        description or a commit subject - which no field-name check would see."""
        swept = await self.answers(list(CALLS))

        def keys(value: Any) -> list[str]:
            if isinstance(value, dict):
                return [str(k) for k in value] + [x for v in value.values() for x in keys(v)]
            if isinstance(value, list):
                return [x for v in value for x in keys(v)]
            return []

        for tool, answer in swept:
            with self.subTest(tool=tool):
                self.assertFalse([k for k in keys(answer) if "Ignore previous" in k])


if __name__ == "__main__":
    unittest.main()
