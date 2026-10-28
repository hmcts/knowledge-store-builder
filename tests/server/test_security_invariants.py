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
from knowledgestore.server import index
from knowledgestore.server.server import KnowledgeStoreServer

PACKAGE = Path(server_package.__file__).resolve().parent

# The functions that write the query-index cache, outside the store: a temp file
# renamed into place, a digest beside it, and the removal of a stale entry or of
# another version's. Only these, only in `index.py`, may change the filesystem; a
# write anywhere else in that module, or in any other, fails the static check below.
# What they write *to* is held at runtime by `CacheWritesStayInTheCacheTest`.
CACHE_WRITER = "index.py"
CACHE_WRITER_FUNCTIONS = frozenset({"_discard", "_write", "_write_digest", "_remove_others"})

# Importing any of these gives a module a way to start a process, reach a network,
# remove files, or load code the scan cannot read. Prefix matches: `urllib.request`
# is `urllib`. `importlib.metadata` is the one allowed corner of `importlib`: the
# package reads its own version with it.
FORBIDDEN_IMPORTS = frozenset(
    {"subprocess", "socket", "ssl", "urllib", "http", "requests", "httpx", "aiohttp"}
    | {"ftplib", "smtplib", "telnetlib", "xmlrpc", "shutil", "multiprocessing", "pty"}
    | {"importlib", "ctypes"}
)
ALLOWED_IMPORTS = ("importlib.metadata",)

# Builtins that run or load code the scan cannot see.
FORBIDDEN_BUILTINS = frozenset({"exec", "eval", "__import__"})

# `os.<name>` calls that start a process.
FORBIDDEN_OS = frozenset(
    {"system", "popen", "fork", "forkpty", "execv", "execve", "execl", "execvp", "execlp"}
    | {"spawnl", "spawnv", "spawnve", "spawnvp", "posix_spawn", "posix_spawnp"}
)

# Methods that change the filesystem, whatever they are called on. `replace` is
# judged by its argument count: `Path.replace(target)` takes one, `str.replace`
# takes two or more.
WRITE_METHODS = frozenset(
    {"write_text", "write_bytes", "unlink", "mkdir", "rmdir", "touch", "symlink_to"}
    | {"hardlink_to", "rename", "chmod", "truncate"}
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
    """Imports that give a module a process, a network, a remover, dynamic code, or
    the library. A name imported out of `os` that writes is refused at the import, so
    the bare call that follows it needs no resolving."""
    found = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in FORBIDDEN_IMPORTS and not alias.name.startswith(ALLOWED_IMPORTS):
                found.append(f"{where} imports {alias.name}")
            if top == "knowledgestore" and not alias.name.startswith("knowledgestore.server"):
                found.append(f"{where} imports the library: {alias.name}")
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        top = module.split(".")[0]
        if node.level == 0 and top in FORBIDDEN_IMPORTS and not module.startswith(ALLOWED_IMPORTS):
            found.append(f"{where} imports from {module}")
        if node.level == 0 and top == "knowledgestore":
            if not module.startswith("knowledgestore.server"):
                found.append(f"{where} imports the library: {module}")
        # `from .. import io` reaches the library's writers from inside the package.
        if node.level >= 2:
            found.append(f"{where} imports from outside the server package")
        if node.level == 0 and module == "os":
            for alias in node.names:
                if alias.name == "*" or alias.name in WRITE_OS | FORBIDDEN_OS:
                    found.append(f"{where} imports os.{alias.name}")
        if node.level == 0 and module == "tempfile":
            found.append(f"{where} imports from tempfile")
    return found


def _call_problems(call: ast.Call, where: str, *, may_write: bool) -> list[str]:
    """Calls that run dynamic code or start a process or, outside the cache writer,
    change the filesystem."""
    found = []
    func = call.func
    if isinstance(func, ast.Name) and func.id in FORBIDDEN_BUILTINS:
        found.append(f"{where} calls {func.id}")
    is_attribute = isinstance(func, ast.Attribute)
    owner = func.value.id if is_attribute and isinstance(func.value, ast.Name) else ""
    attr = func.attr if is_attribute else ""
    if owner == "os" and attr in FORBIDDEN_OS:
        found.append(f"{where} calls os.{attr}")
    if may_write:
        return found
    if _opens_for_writing(call):
        found.append(f"{where} opens a file for writing")
    if attr in WRITE_METHODS:
        found.append(f"{where} calls .{attr}()")
    if attr == "replace" and owner != "os" and len(call.args) + len(call.keywords) == 1:
        found.append(f"{where} calls .replace() with one argument (Path.replace)")
    if owner == "os" and attr in WRITE_OS:
        found.append(f"{where} calls os.{attr}")
    if owner == "tempfile":
        found.append(f"{where} calls tempfile.{attr}")
    return found


def violations(source: str, filename: str, *, writers: frozenset[str] = frozenset()) -> list[str]:
    """What in this module could write, spawn or reach a network. Empty when nothing.

    `writers` names the top-level functions allowed to change the filesystem; the
    rest of the module is held to the same rule as every other. Each finding reads
    `file:line [function] what`."""
    found: list[str] = []

    def visit(node: ast.AST, function: str) -> None:
        if not function and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        where = f"{filename}:{getattr(node, 'lineno', 0)} [{function}]"
        found.extend(_import_problems(node, where))
        if isinstance(node, ast.Call):
            found.extend(_call_problems(node, where, may_write=function in writers))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(source, filename=filename), "")
    return found


def package_violations(directory: Path) -> list[str]:
    """Every module under the package, subpackages included."""
    found = []
    for path in sorted(directory.rglob("*.py")):
        name = path.relative_to(directory).as_posix()
        writers = CACHE_WRITER_FUNCTIONS if name == CACHE_WRITER else frozenset()
        found += violations(path.read_text(encoding="utf-8"), name, writers=writers)
    return found


class ReadOnlyByConstructionTest(unittest.TestCase):
    def test_no_module_of_the_server_can_write_spawn_or_reach_a_network(self):
        """Catches: a server module importing `subprocess`, a socket or HTTP client,
        `shutil`, `importlib`, `ctypes` or a library module that writes
        (`knowledgestore.io`); calling `os.system`, `exec`, `eval` or `__import__`;
        opening a file for writing; or removing, renaming or creating a path - by
        `os.`, by `Path` method, or by a name imported from `os` - anywhere but the
        functions that write the cache. The server being read-only is a property of
        its source, so it is read from its source."""
        self.assertEqual(package_violations(PACKAGE), [])

    def test_the_scan_reads_the_modules_the_package_holds(self):
        """Catches: the check above going vacuous - a moved package, or a glob that
        matches nothing, which would read as a clean pass over no code."""
        names = {path.name for path in PACKAGE.rglob("*.py")}
        self.assertGreaterEqual(len(names), 15, names)
        self.assertLessEqual({"server.py", "index.py", "store.py", "cli.py"}, names)
        self.assertIn(CACHE_WRITER, names)

    def test_the_scan_descends_into_subpackages(self):
        """Catches: a scan that reads only the package's top directory, so a write
        in a subpackage is never looked at."""
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "inner"
            nested.mkdir()
            (nested / "deep.py").write_text("import subprocess\n", encoding="utf-8")
            self.assertEqual(
                package_violations(Path(tmp)), ["inner/deep.py:1 [] imports subprocess"]
            )

    def test_the_cache_functions_are_the_only_code_that_writes_at_all(self):
        """Catches: the allowance for the cache writer widening into a way to write
        everywhere. With the allowance removed, the only code reported is the named
        functions of `index.py` - so every write the package has is in them, and the
        named functions still write (an allowance for code that no longer writes
        would be a hole waiting for a tenant)."""
        found = []
        for path in sorted(PACKAGE.rglob("*.py")):
            name = path.relative_to(PACKAGE).as_posix()
            source = path.read_text(encoding="utf-8")
            found += [
                line for line in violations(source, name) if "imports" not in line and "[" in line
            ]
        placed = {(line.split(":")[0], line.split("[")[1].split("]")[0]) for line in found}
        self.assertEqual(placed, {(CACHE_WRITER, name) for name in CACHE_WRITER_FUNCTIONS}, found)

    def test_the_scan_notices_each_kind_of_write_path(self):
        """Catches: a scan that has stopped recognising a way to write, spawn or load
        code - each source here is one the package must never contain, and each must
        be reported."""
        bad = {
            "import subprocess": "imports subprocess",
            "import socket": "imports socket",
            "from urllib.request import urlopen": "imports from urllib.request",
            "import http.client": "imports http.client",
            "import shutil": "imports shutil",
            "from shutil import rmtree": "imports from shutil",
            "from subprocess import run": "imports from subprocess",
            "from socket import socket": "imports from socket",
            "import importlib": "imports importlib",
            "import importlib.util": "imports importlib.util",
            "from importlib import import_module": "imports from importlib",
            "import ctypes": "imports ctypes",
            "__import__('os')": "calls __import__",
            "exec('x = 1')": "calls exec",
            "eval('1')": "calls eval",
            "from knowledgestore import io": "imports the library",
            "from knowledgestore.io import write_json": "imports the library",
            "from .. import io": "outside the server package",
            "import os\nos.system('x')": "os.system",
            "from os import remove\nremove('f')": "imports os.remove",
            "from os import rename": "imports os.rename",
            "from os import *": "imports os.*",
            "from tempfile import mkstemp": "imports from tempfile",
            "open('f', 'w')": "opens a file for writing",
            "open('f', mode='a')": "opens a file for writing",
            "from pathlib import Path\nPath('f').open('wb')": "opens a file for writing",
            "from pathlib import Path\nPath('f').write_text('x')": ".write_text()",
            "from pathlib import Path\nPath('f').rename('g')": ".rename()",
            "from pathlib import Path\nPath('f').replace('g')": "Path.replace",
            "from pathlib import Path\nPath('f').symlink_to('g')": ".symlink_to()",
            "from pathlib import Path\nPath('f').hardlink_to('g')": ".hardlink_to()",
            "from pathlib import Path\nPath('f').touch()": ".touch()",
            "from pathlib import Path\nPath('f').unlink()": ".unlink()",
            "from pathlib import Path\nPath('f').rmdir()": ".rmdir()",
            "import os\nos.remove('f')": "os.remove",
            "import os\nos.rename('f', 'g')": "os.rename",
            "import os\nos.replace('f', 'g')": "os.replace",
            "import os\nos.unlink('f')": "os.unlink",
            "import os\nos.rmdir('f')": "os.rmdir",
            "import os\nos.makedirs('f')": "os.makedirs",
            "import tempfile\ntempfile.mkstemp()": "tempfile.mkstemp",
        }
        for source, expected in bad.items():
            with self.subTest(source=source):
                found = violations(source, "x.py")
                self.assertTrue(any(expected in line for line in found), (source, found))

    def test_the_exemption_is_the_named_functions_only(self):
        """Catches: the cache allowance covering a whole module - a write in another
        function of `index.py` must be reported, and one inside a named function not."""
        write = "import os\ndef {name}(p):\n    os.remove(p)\n"
        self.assertEqual(
            violations(write.format(name="_write"), "index.py", writers=CACHE_WRITER_FUNCTIONS),
            [],
        )
        self.assertTrue(
            violations(write.format(name="_other"), "index.py", writers=CACHE_WRITER_FUNCTIONS)
        )

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
            "from os import fspath, getpid",
            "'a-b'.replace('-', '_')",
            "from importlib.metadata import version",
        ]
        for source in reads:
            with self.subTest(source=source):
                self.assertEqual(violations(source, "x.py"), [])


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


def everything_under(root: Path) -> dict[str, tuple[str, int]]:
    """Every entry under `root` with its type and, for a file, its size."""
    found = {}
    for path in sorted(root.rglob("*")):
        found[path.relative_to(root).as_posix()] = (
            "dir" if path.is_dir() else "file",
            0 if path.is_dir() else path.stat().st_size,
        )
    return found


class CacheWritesStayInTheCacheTest(SweepTestCase):
    async def test_a_sweep_writes_only_under_the_cache_directory(self):
        """Catches: a cache function writing anywhere but the cache directory - into
        the store beside the archive, into the home directory, or into the temp tree
        around them. The cache directory is resolved from `XDG_CACHE_HOME` by the
        server's own `cache_home`, the cache is made to be written (a gzip index must
        be decompressed into it), and everything else under the temp tree must be
        exactly as it was. This is the runtime half of the static check, which only
        says which functions may write, not where."""
        home = self.tmp / "home"
        home.mkdir()
        cache = index.cache_home({"XDG_CACHE_HOME": str(self.tmp / "xdg")}, home)
        self.assertEqual(cache, self.tmp / "xdg")
        before = everything_under(self.tmp)
        before_home = everything_under(home)
        self.cache = cache
        await self.answers(list(CALLS))
        after = everything_under(self.tmp)
        written = {name for name in after if after[name] != before.get(name)}
        self.assertTrue(written, "nothing was written, so the cache was never exercised")
        outside = {name for name in written if not name.split("/")[0] == "xdg"}
        self.assertEqual(outside, set(), "a write left the cache directory")
        self.assertTrue(any(name.endswith(".sqlite") for name in written), written)
        self.assertEqual(everything_under(home), before_home)
        self.assertEqual(
            everything_under(self.root),
            {k[len("store/") :]: v for k, v in before.items() if k.startswith("store/")},
        )


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
