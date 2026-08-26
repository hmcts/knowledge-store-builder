"""Peer-package symbols a shipped code block names must exist in that package.

`graphify` is a peer CLI, not a dependency, so nothing in this repository's build
or type checking looks at the Python API its skills tell an operator to drive.
`test_documented_stages.py` reads the same documents and deliberately ignores
import lines: it checks `knowledgestore <stage>` invocations against this
library's own stage table.

That left the peer API unchecked, and it drifted. The build skill's re-clustering
block named functions the installed package did not have, so an operator following it got an `ImportError` at the first line -- in the
middle of a procedure whose whole purpose is to carry authored prose through a
re-cluster without stranding it. The symbols were plausible and the block read as
authoritative.

This gate resolves what the documents actually say against what is installed. Two
properties in tension, and both are needed:

- **The symbols are discovered from the documents.** A hand-written list here
  would be a second model of the code blocks -- correct the day it is written and
  silently wrong afterwards, which is the failure mode `CLAUDE.md` describes for
  exclusion lists. The import statements are parsed out of the fenced blocks, so
  documenting a new symbol puts it under this gate with no edit here.
- **The discovery asserts it found something.** A parse that silently matches
  nothing is indistinguishable from a document with no defects, and that is the
  state this gate would go vacuous in. The floor and the parser fixtures run
  whether or not the peer is installed.

Resolution is skipped when graphify is absent, because it is a peer: a test that
fails on a machine that reasonably does not have it gets deleted rather than
fixed. The discovery half still runs there.
"""

from __future__ import annotations

import ast
import importlib
import re
import textwrap
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The peer whose API the skills document. graphify ships and versions
# independently of this library, which is why its symbols are the ones that rot.
PEER = "graphify"

try:
    importlib.import_module(PEER)
    HAS_PEER = True
except ImportError:  # pragma: no cover - depends on the environment
    HAS_PEER = False

needs_peer = unittest.skipUnless(HAS_PEER, f"needs {PEER} (a peer CLI, not a dependency)")

# One import statement naming the peer. The parenthesised form is matched first
# and spans lines, because that is how one of the shipped blocks writes it and a
# line-at-a-time reader would silently see only its first half.
IMPORT_STATEMENT = re.compile(
    rf"^[ \t]*(?:from[ \t]+{PEER}[\w.]*[ \t]+import[ \t]+(?:\([^)]*\)|[^\n]*)"
    rf"|import[ \t]+{PEER}[\w.]*)",
    re.MULTILINE,
)

# A floor on the parse rather than a census: well below what is documented today,
# so it fires when the reader stops reading and not when a block is edited. The
# per-file check below is what notices a single block going unread.
MINIMUM_REFERENCES = 3


@dataclass(frozen=True)
class Reference:
    """A symbol a document tells the reader to import, and where it says so."""

    path: str
    module: str
    name: str  # "" for `import graphify.x`, which names no attribute


def shipped_documentation() -> list[Path]:
    """Every file that tells a reader to run something, historical plans excluded.

    Plans record what was planned at a date rather than what to run today, which
    is the same exclusion `test_documented_stages.py` makes and for the same
    reason: freezing a record of a decision is the point of it.
    """
    skills = sorted(ROOT.joinpath("skills").rglob("SKILL.md"))
    docs = [
        path
        for path in sorted(ROOT.joinpath("docs").rglob("*.md"))
        if "superpowers" not in path.relative_to(ROOT).parts
    ]
    return skills + docs


def python_blocks(text: str) -> list[str]:
    """The fenced Python blocks, which are the parts a reader runs verbatim.

    Prose is excluded deliberately: a sentence may name a symbol in the past
    tense or as a counter-example, while a fenced block is an instruction.
    """
    blocks: list[str] = []
    body: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("```"):
            if body is None:
                body = [] if line[3:].strip() in {"python", "py"} else None
                continue
            blocks.append("\n".join(body))
            body = None
        elif body is not None:
            body.append(line)
    return blocks


def references(text: str, path: str) -> list[Reference]:
    """Every peer symbol the text's code blocks import.

    Each statement is parsed with `ast` rather than picked apart by the regex, so
    the aliases come from Python's own reading of the line and the parenthesised,
    multi-line and `as`-renamed forms need no separate cases.
    """
    found: list[Reference] = []
    for block in python_blocks(text):
        for match in IMPORT_STATEMENT.finditer(block):
            statement = textwrap.dedent(match.group(0))
            try:
                tree = ast.parse(statement)
            except SyntaxError as error:
                # Loudly, rather than dropping the statement: a reference this
                # cannot read is a reference nothing resolves, which is the shape
                # a silent skip would hide.
                raise AssertionError(
                    f"{path} documents a {PEER} import that is not valid Python "
                    f"({statement!r}): {error}"
                ) from error
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and _is_peer(node.module):
                    module = node.module or ""
                    found += [Reference(path, module, alias.name) for alias in node.names]
                elif isinstance(node, ast.Import):
                    found += [
                        Reference(path, alias.name, "")
                        for alias in node.names
                        if _is_peer(alias.name)
                    ]
    return found


def _is_peer(module: str | None) -> bool:
    return module == PEER or bool(module and module.startswith(f"{PEER}."))


def unresolved(found: list[Reference]) -> list[str]:
    """Which references do not resolve against the installed peer.

    Separate from the assertions so the sensitivity check can drive it with a
    fabricated document.
    """
    problems: list[str] = []
    for reference in found:
        try:
            module = importlib.import_module(reference.module)
        except ImportError as error:
            problems.append(f"{reference.path} imports {reference.module}: {error}")
            continue
        if not reference.name or hasattr(module, reference.name):
            continue
        try:
            # `from graphify import cluster` names a submodule rather than an
            # attribute until something has imported it.
            importlib.import_module(f"{reference.module}.{reference.name}")
        except ImportError:
            problems.append(f"{reference.path} imports {reference.module}.{reference.name}")
    return problems


def documented_references() -> list[Reference]:
    return [
        reference
        for path in shipped_documentation()
        for reference in references(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
    ]


class DocumentedPeerImportsAreDiscoverable(unittest.TestCase):
    """The guard on the instrument, which runs whether or not the peer is here.

    Everything below depends on the parse finding the import statements. If a
    document is reformatted, a fence loses its language tag, or the pattern is
    narrowed, the resolution test would pass over an empty list and report green
    while checking nothing.
    """

    def test_the_scan_finds_documented_peer_imports(self):
        found = documented_references()
        self.assertGreaterEqual(
            len(found),
            MINIMUM_REFERENCES,
            f"parsed only {len(found)} {PEER} imports from the shipped documentation; the "
            "documents or the pattern changed and this gate is no longer reading them",
        )
        self.assertTrue(
            all(_is_peer(reference.module) for reference in found),
            f"a reference outside {PEER} was collected, so the pattern is over-reaching",
        )

    def test_every_file_naming_a_peer_import_yields_references(self):
        """Breaks when one block stops being read while the others still are.

        The floor above only notices total collapse. A fence that loses its
        `python` tag, or a block converted to an indented one, would drop that
        file's symbols out of the gate with every other file still reporting -- so
        each file whose text contains a line-initial peer import must produce at
        least one reference from a block.
        """
        for path in shipped_documentation():
            text = path.read_text(encoding="utf-8")
            if not IMPORT_STATEMENT.search(text):
                continue
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertTrue(
                    references(text, str(path.relative_to(ROOT))),
                    f"{path.relative_to(ROOT)} contains a {PEER} import statement that no "
                    "fenced Python block yielded: either the fence lost its language tag "
                    "or the statement is loose in the prose",
                )

    def test_it_reads_a_multi_line_parenthesised_import(self):
        """The form one shipped block uses. A line-at-a-time reader would collect
        only the names on the first line and silently ignore the rest."""
        text = "```python\nfrom graphify.cluster import (cluster,\n    label_by_hub)\n```\n"
        self.assertEqual(
            [(r.module, r.name) for r in references(text, "fixture.md")],
            [("graphify.cluster", "cluster"), ("graphify.cluster", "label_by_hub")],
        )

    def test_it_reads_an_indented_import_and_a_renamed_one(self):
        text = "```py\n    from graphify.export import to_json as write\n    import graphify.paths\n```\n"
        self.assertEqual(
            [(r.module, r.name) for r in references(text, "fixture.md")],
            [("graphify.export", "to_json"), ("graphify.paths", "")],
        )

    def test_it_ignores_imports_of_anything_but_the_peer(self):
        """The library's own imports are `test_documented_stages.py`'s business,
        and this gate over-reaching into them would make it fail on a module that
        is checked by the build."""
        text = "```python\nfrom knowledgestore import cli\nimport json\n```\n"
        self.assertEqual(references(text, "fixture.md"), [])

    def test_an_unreadable_import_fails_rather_than_being_skipped(self):
        """Breaks if a statement this cannot parse is dropped instead.

        A dropped statement is a documented symbol nothing resolves, reported as a
        clean pass -- the state the floor above exists to prevent, arriving one
        reference at a time.
        """
        text = "```python\nfrom graphify.cluster import cluster,\n```\n"
        with self.assertRaises(AssertionError) as raised:
            references(text, "fixture.md")
        self.assertIn("not valid Python", str(raised.exception))

    def test_it_ignores_an_import_written_in_prose(self):
        """A sentence may name a symbol as history or as a counter-example. Only a
        fenced block is an instruction a reader runs, so only a block is read."""
        text = "Earlier releases used from graphify.cluster import label_communities.\n"
        self.assertEqual(references(text, "fixture.md"), [])


@needs_peer
class DocumentedPeerSymbolsResolve(unittest.TestCase):
    def test_every_documented_symbol_exists_in_the_installed_peer(self):
        """Breaks when a shipped code block names a symbol the peer does not have.

        When it does, the finding is not "fix the code" -- this repository has none
        here -- it is that the peer moved and the document has to be corrected.
        Until then, an operator following the block gets an `ImportError` on its
        first line.
        """
        problems = unresolved(documented_references())
        self.assertEqual(
            problems,
            [],
            f"{problems} -- the documented {PEER} API no longer resolves against the "
            f"installed one ({importlib.import_module(PEER).__file__}), so following the "
            "block raises ImportError",
        )

    def test_this_gate_notices_a_symbol_that_does_not_exist(self):
        """The sensitivity check, in the same run.

        The assertion above can only pass or fail; it cannot report that it has
        stopped resolving anything. So the resolver is driven against a fabricated
        block that must fail, and one that must pass, rather than trusted because
        the shipped documents happen to be correct.
        """
        forged = "```python\nfrom graphify.cluster import cluster, reticulate\nimport graphify.absent\n```\n"
        found = references(forged, "fixture.md")
        self.assertEqual(len(found), 3, "precondition: the fixture names three references")
        problems = unresolved(found)
        self.assertEqual(
            len(problems),
            2,
            f"a missing attribute and a missing module went unreported ({problems}), so "
            "this gate is vacuous",
        )
        self.assertTrue(any("graphify.cluster.reticulate" in problem for problem in problems))
        self.assertTrue(any("graphify.absent" in problem for problem in problems))

    def test_it_resolves_a_symbol_the_peer_really_has(self):
        """The control for the check above: proves a clean result means resolved
        rather than not looked at."""
        text = "```python\nfrom graphify.cluster import cluster\n```\n"
        self.assertEqual(unresolved(references(text, "fixture.md")), [])


if __name__ == "__main__":
    unittest.main()
