"""The coverage map, `library_store/COVERAGE.md`, kept honest.

The map names, for every tool, every argument of every tool and every layer
field the server reads, the test that fails if that read breaks. These tests
hold it to `server.TOOLS` and to the test suite as it is: a tool or argument
with no row, a row with no test, or a row citing a test that no longer exists
fails here, so the map cannot claim coverage the suite does not have.

The map cites each library contract in its `WithIndex` form; `both_modes`
makes a `WithoutIndex` twin of every one. The older-store and partial-store
contracts are cited in their `Indexed` form, with an `Unindexed` twin over the
same build's store without the index. The guard checks that the twin exists
and that each form reads the store its name claims.
"""

import re
import unittest
from pathlib import Path

from knowledgestore.server import server
from library_store.generate import NO_INDEX, OUT, PARTIAL, Build, frozen

COVERAGE = Path(__file__).resolve().parent / "library_store" / "COVERAGE.md"

# `module.Class.test_name`, as `loadTestsFromName` resolves it from `tests/`.
TEST_ID = re.compile(r"`(test_\w+\.\w+\.test_\w+)`")
NAME = re.compile(r"^`(\w+)`$")
TOOL_ROW = "—"
# `test_library_older_stores.V0_18_3Indexed`: the frozen release it reads.
OLDER_FORM = re.compile(r"^test_library_older_stores\.V(\d+)_(\d+)_(\d+)Indexed\.")


def _section(heading: str) -> list[list[str]]:
    """The body rows of the table under `## <heading>`, each as its cells."""
    rows: list[list[str]] = []
    inside = False
    for line in COVERAGE.read_text("utf-8").splitlines():
        if line.startswith("## "):
            inside = line[3:].strip() == heading
            continue
        if inside and line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells[0] in ("Tool", "Layer field") or set(cells[0]) <= {"-", ":"}:
                continue
            rows.append(cells)
    return rows


def _arguments() -> dict[tuple[str, str], tuple[str, str]]:
    """(tool, argument or `—`) -> (library cell, fixture cell), from the map."""
    found: dict[tuple[str, str], tuple[str, str]] = {}
    for tool, argument, library, fixture in _section("Tools and arguments"):
        tool_name = NAME.match(tool)
        argument_name = NAME.match(argument)
        assert tool_name, f"not a tool name: {tool}"
        key = (tool_name[1], argument_name[1] if argument_name else argument)
        assert key not in found, f"two rows for {key}"
        found[key] = (library, fixture)
    return found


def _library_ids(cell: str) -> list[str]:
    return [found for found in TEST_ID.findall(cell) if found.startswith("test_library_")]


def _loads(name: str) -> unittest.TestCase | None:
    """The one test `name` resolves to, or None for the loader's stand-in for a
    name it could not find. (A `both_modes` class's own name differs from the
    module attribute the map cites, so the method name is what is compared.)"""
    tests = list(unittest.defaultTestLoader.loadTestsFromName(name))
    if (
        len(tests) == 1
        and isinstance(tests[0], unittest.TestCase)
        and type(tests[0]).__name__ != "_FailedTest"
        and tests[0]._testMethodName == name.rsplit(".", 1)[1]
    ):
        return tests[0]
    return None


class CoverageMapTest(unittest.TestCase):
    def test_every_tool_has_a_contract_test(self):
        """Catches: a tool added to `server.TOOLS` - a fourteenth - with no
        contract test against the library's store, or a tool's row left with
        only fixture tests."""
        rows = _arguments()
        for spec in server.TOOLS:
            with self.subTest(tool=spec.name):
                self.assertIn((spec.name, TOOL_ROW), rows, "no row for the tool")
                library, _ = rows[(spec.name, TOOL_ROW)]
                self.assertTrue(_library_ids(library), "no library contract test")
        tools = {spec.name for spec in server.TOOLS}
        self.assertEqual({tool for tool, _ in rows} - tools, set(), "rows for no tool")

    def test_every_argument_has_a_row(self):
        """Catches: an argument added to a tool with no test that exercises it, a
        row that names no test - or a fixture test with no reason the library's
        output cannot exercise the argument - or a row left for an argument the
        tool no longer takes."""
        rows = _arguments()
        expected = {(spec.name, param.name) for spec in server.TOOLS for param in spec.params}
        for tool, argument in sorted(expected):
            with self.subTest(tool=tool, argument=argument):
                self.assertIn((tool, argument), rows, "no row for the argument")
                library, fixture = rows[(tool, argument)]
                if _library_ids(library):
                    continue
                fixture_ids = TEST_ID.findall(fixture)
                self.assertTrue(fixture_ids, "neither a library nor a fixture test")
                reason = TEST_ID.sub("", fixture).strip(" ;,.:—-")
                self.assertTrue(reason, "a fixture test with no reason")
        stale = {key for key in rows if key[1] != TOOL_ROW} - expected
        self.assertEqual(stale, set(), "rows for arguments no tool takes")

    def test_every_cited_test_exists(self):
        """Catches: a test renamed or deleted while the map still cites it, so the
        map claims coverage the suite no longer has; or a contract cited in one
        read mode whose other mode was lost."""
        cited = sorted(set(TEST_ID.findall(COVERAGE.read_text("utf-8"))))
        self.assertTrue(cited)
        for name in cited:
            with self.subTest(test=name):
                self._assert_resolves(name)

    def test_every_layer_field_cites_a_test_that_exists(self):
        """Catches: a layer-field row added to the map with no test cited, or citing
        a test that was renamed, deleted or lost its WithoutIndex twin, so a read of
        a library layer is claimed covered when nothing fails if it breaks."""
        rows = _section("Layer fields")
        self.assertTrue(rows)
        for field, library, *_ in rows:
            with self.subTest(field=field):
                ids = _library_ids(library)
                self.assertTrue(ids, "no library contract test cited")
                for name in ids:
                    with self.subTest(test=name):
                        self._assert_resolves(name)

    def _assert_resolves(self, name: str) -> None:
        test = _loads(name)
        self.assertIsNotNone(test, "does not resolve to a test")
        if "WithIndex." in name:
            # Each form must read the store its name claims.
            self._assert_twin(name, "WithIndex.", "WithoutIndex.", (OUT, NO_INDEX))
        elif "Indexed." in name:
            self._assert_twin(name, "Indexed.", "Unindexed.", self._build_of(name))

    def _build_of(self, name: str) -> tuple[Path, Path]:
        """The (indexed, unindexed) stores an `…Indexed` form's module and name
        claim: the partial store, or the frozen release its name spells."""
        build: Build | None = None
        if name.startswith("test_library_partial."):
            build = PARTIAL
        elif older := OLDER_FORM.match(name):
            build = frozen(".".join(older.groups()))
        self.assertIsNotNone(build, "an `Indexed` form outside the partial and older stores")
        assert build is not None
        return build.store, build.no_index

    def _assert_twin(self, name: str, form: str, twin_form: str, stores: tuple[Path, Path]) -> None:
        test = _loads(name)
        self.assertEqual(
            (getattr(test, "INDEXED", None), getattr(test, "STORE", None)),
            (True, stores[0]),
            "the cited form does not read the indexed store",
        )
        twin = _loads(name.replace(form, twin_form))
        self.assertIsNotNone(twin, f"no {twin_form.rstrip('.')} twin")
        self.assertEqual(
            (getattr(twin, "INDEXED", None), getattr(twin, "STORE", None)),
            (False, stores[1]),
            "the twin does not read the store without the index",
        )


if __name__ == "__main__":
    unittest.main()
