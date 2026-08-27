"""An operator must be told that local extractors run before the archive is rewritten.

Three stages rewrite the committed `graphify-out/graph.json.gz` from the
mid-pipeline `graphify-out/graph.json`. A store whose own extractors add nodes
after one of them leaves the two files describing different graphs, and the next
of those stages refuses — in a message that counts communities and clustered
nodes, because destroying a clustering is the worst thing the overwrite can do.
The reporter of #244 met that refusal twice and only the second occasion was
about clustering at all: the symptom points somewhere the cause is not, and the
remedy the refusal prints (decompress the archive over `graph.json`, or remove
`graph.json`) discards the local layer when the local layer is what wrote it.

The break this catches has two halves, and the second is the one worth having:

1. **The guidance is stated.** The ordering constraint, the symptom to recognise
   it by, and the warning about the printed remedy.
2. **The stage set is derived from the code, not maintained by hand.** A list of
   three stage names in a document is correct the day it is written. `CLAUDE.md`
   names this failure mode directly, and it is the reason the constraint is worth
   documenting at all: which stages rewrite the archive is not visible from
   outside. So the set the documents name is compared against the set of modules
   that write `config.GRAPH_PATH.with_suffix(".json.gz")`, read out of the source
   by AST rather than by a grep for the writing helper — `io.gzip_text` also
   writes the summaries snapshot, the intent index and the ticket descriptions,
   and a gate that counted those would name the wrong stages.

Both directions of that comparison fail. A fourth module starting to write the
archive is drift the documents have not caught up with; one of the three ceasing
to write it leaves a warning that costs an operator time for nothing.

The code side asserts its own discriminating power in the same run, over forged
module sources rather than the tree: a writer, a writer that reaches the archive
through a local name, a module that only reads it, and one that touches neither.
Without the third and fourth the detector could be answering "does this module
mention the graph", which is a neighbouring question with the same answer today.

The extractor and the prose forge are `tests/doc_sections`, shared with the other
gates over prose; what stays here is these sections' headings, their fragments
and their assertions.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from doc_sections import (
    Copy,
    body_of,
    commands,
    missing_elements,
    section_after_rename,
    section_lines,
    sensitivity,
)

from knowledgestore import cli

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "knowledgestore"

# The committed archive, as it is spelled at every site that writes it:
# `config.GRAPH_PATH` names the uncompressed file, and the archive is derived
# from it. Matching the expression rather than the string `.json.gz` keeps the
# other gzipped artefacts out of the answer.
ARCHIVE_SUFFIX = ".json.gz"
GRAPH_PATH = "GRAPH_PATH"

# Where a path goes to be written. `open` is in it without inspecting the mode:
# this gate's job is to notice a module that has started writing the archive, so
# a read through `open` counted as a write would fail loudly and be corrected,
# while a write that went unrecognised would pass silently.
WRITE_SINKS = frozenset(
    {"gzip_text", "write_gzip_json", "write_json", "write_text", "write_bytes", "open"}
)

# Modules that name the archive without writing it. `status` compares its commit
# date with the graph report's. Listed so a fifth module touching the archive
# through a sink this file does not know about still has to be classified by
# hand rather than passing as neither a reader nor a writer.
ARCHIVE_READERS = frozenset({"status"})

# `knowledgestore <stage>` inside the section's fenced blocks, which is where the
# stage set is derived from: a block is what an operator reads as the list, and
# prose elsewhere in the section mentions stages it is not claiming write the
# archive. The negative lookbehind is `test_documented_stages`': `from
# knowledgestore import x` is an import, not an invocation.
STAGE_INVOCATION = re.compile(r"(?<!from )\bknowledgestore\s+([a-z][a-z0-9-]*)")

# Each fragment is the shortest one that cannot survive its element being
# dropped, so the surrounding prose stays free to change while the guidance does
# not. None is a substring of another: the forge removes every occurrence of one
# fragment and requires exactly that fragment to be reported missing.
REQUIRED = (
    # the mechanism the constraint follows from
    "rewrite the committed archive",
    # the constraint itself, in both of its permitted orders
    "before those three, or remove the archive",
    # who it applies to
    "extractors of its own",
    # the symptom, and that it is not what it appears to be
    "communities and clustered nodes",
    "the cause is the ordering",
    # the trap: the printed remedy assumes the plain file is the stale one
    "Do not follow the remedy it prints",
    "gunzip -kf graphify-out/graph.json.gz",
    "a local extractor has just written",
)

GUARDED = (
    Copy(
        "docs/refreshing-a-store.md",
        "## Run local extractors before the stages that rewrite the archive",
        REQUIRED,
    ),
    Copy(
        "skills/knowledge-store-build/SKILL.md",
        "### Run local extractors before the stages that rewrite the archive",
        REQUIRED,
    ),
)

# Forged module sources for the code side's own sensitivity. Each is the shape a
# real module would take, and the four answers differ, which is what makes the
# detector's answer about writing rather than about mentioning.
FORGED_WRITER = """
from . import config, io

def main():
    with io.gzip_text(config.GRAPH_PATH.with_suffix(".json.gz")) as out:
        out.write("{}")
"""

FORGED_ALIASED_WRITER = """
from . import config, io

def main():
    archive = config.GRAPH_PATH.with_suffix(".json.gz")
    with io.gzip_text(archive) as out:
        out.write("{}")
"""

FORGED_READER = """
from . import config

def main():
    archive = config.GRAPH_PATH.with_suffix(".json.gz")
    return archive.is_file()
"""

FORGED_UNRELATED = """
from . import config

def main():
    return config.GRAPH_PATH.read_text(encoding="utf-8")
"""


def _names_graph_path(node: ast.AST) -> bool:
    """Any `.GRAPH_PATH` attribute, or a bare `GRAPH_PATH`.

    The receiver is deliberately not pinned to `config`: a module importing the
    constant directly, or under another name for the module, still names the same
    path, and a gate that only recognised `config.GRAPH_PATH` would go blind on
    the day somebody wrote it the other way.
    """
    if isinstance(node, ast.Attribute):
        return node.attr == GRAPH_PATH
    return isinstance(node, ast.Name) and node.id == GRAPH_PATH


def _is_archive_expression(node: ast.AST) -> bool:
    """`GRAPH_PATH.with_suffix(".json.gz")`: the committed archive, as an expression.

    Matched as the expression rather than as the string `.json.gz`, because every
    other gzipped artefact in the package would match the string — the summaries
    snapshot, the intent index, the ticket descriptions — and naming their stages
    as archive writers is the wrong answer this gate exists to avoid.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_suffix"
        and _names_graph_path(node.func.value)
        and any(isinstance(arg, ast.Constant) and arg.value == ARCHIVE_SUFFIX for arg in node.args)
    )


def _archive_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to the archive path.

    Without these the detector reads the expression only where it is written
    inline. Moving it onto a variable first is the ordinary way to write the same
    code, and it would take a new writer out of this gate's sight — the same
    blindness `test_refusal_remedies_are_runnable` closes for wording moved into
    a constant.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign) and _is_archive_expression(node.value):
            targets = list(node.targets)
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_archive_expression(node.value)
        ):
            targets = [node.target]
        aliases.update(target.id for target in targets if isinstance(target, ast.Name))
    return aliases


def _is_archive_reference(node: ast.AST, aliases: set[str]) -> bool:
    """The archive, written out or reached through one of this module's names."""
    if _is_archive_expression(node):
        return True
    return isinstance(node, ast.Name) and node.id in aliases


def _sink_name(func: ast.expr) -> str | None:
    """What a call is calling, by its last name: `io.gzip_text` -> `gzip_text`."""
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def archive_use(source: str) -> tuple[bool, bool]:
    """(writes the archive, mentions the archive) for one module's source."""
    tree = ast.parse(source)
    aliases = _archive_aliases(tree)
    mentions = any(_is_archive_reference(node, aliases) for node in ast.walk(tree))
    writes = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _sink_name(node.func) not in WRITE_SINKS:
            continue
        reached = [*node.args, *(keyword.value for keyword in node.keywords)]
        if isinstance(node.func, ast.Attribute):
            # The receiver too: `archive.write_text(...)` writes through the path
            # itself rather than passing it to a helper.
            reached.append(node.func.value)
        writes = writes or any(_is_archive_reference(target, aliases) for target in reached)
    return writes, mentions


def _stage_of_module() -> dict[str, str]:
    """Module stem -> the stage name an operator types, from `cli.STAGES`."""
    return {module: stage for stage, (module, _help) in cli.STAGES.items()}


def _classified() -> tuple[set[str], set[str]]:
    """(modules writing the archive, modules mentioning it), by module stem."""
    writers: set[str] = set()
    mentioners: set[str] = set()
    for path in sorted(SRC.glob("*.py")):
        writes, mentions = archive_use(path.read_text(encoding="utf-8"))
        if mentions:
            mentioners.add(path.stem)
        if writes:
            writers.add(path.stem)
    return writers, mentioners


def writing_stages() -> set[str]:
    """The archive writers as the stage names an operator types.

    A writing module with no stage resolves to a name no document can match, so
    the comparison fails and reports which module it was rather than raising out
    of the gate on a `KeyError`.
    """
    stages = _stage_of_module()
    writers, _ = _classified()
    return {stages.get(module, f"{module} (no stage in cli.STAGES)") for module in writers}


def documented_writers(copy: Copy) -> set[str]:
    """The stage names one copy lists in its command block as archive writers."""
    return set(STAGE_INVOCATION.findall(commands(body_of(copy))))


class ArchiveWriterOrderingGuidanceTest(unittest.TestCase):
    def test_every_guarded_section_is_present(self):
        """Breaks if either heading is renamed or removed, which would otherwise
        leave every assertion below passing over an empty string."""
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                self.assertGreater(
                    section_lines(body_of(copy)),
                    8,
                    f"{copy.path}'s {copy.heading!r} section is gone or too short to hold "
                    "the constraint, the symptom and the warning about the printed remedy",
                )

    def test_every_copy_states_the_constraint_the_symptom_and_the_trap(self):
        """Breaks if either copy loses the ordering, the stage set's consequence,
        the symptom that identifies it, or the warning about the printed remedy.

        The symptom is the half that cost the reporter of #244 two diagnoses: a
        copy stating the rule without it leaves an operator reading a message
        about clustering and looking at their clustering.
        """
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                absent = missing_elements(body_of(copy), copy.required)
                self.assertEqual(
                    absent,
                    [],
                    f"{copy.path}'s {copy.heading!r} section no longer states: {absent}",
                )

    def test_the_documented_stage_set_is_the_set_that_writes_the_archive(self):
        """The gate that matters. Breaks in both directions.

        A fourth module writing `config.GRAPH_PATH.with_suffix(".json.gz")` is a
        stage an operator has to order their extractors around and no document
        names. One of the three ceasing to write it leaves a warning that sends
        somebody looking for a constraint that is no longer there.
        """
        writers = writing_stages()
        for copy in GUARDED:
            documented = documented_writers(copy)
            with self.subTest(path=copy.path):
                self.assertEqual(
                    documented,
                    writers,
                    f"{copy.path}'s {copy.heading!r} section names {sorted(documented)} as "
                    f"the stages that rewrite the archive; the source says "
                    f"{sorted(writers)}. Undocumented: "
                    f"{sorted(writers - documented)}. Named but no longer writing: "
                    f"{sorted(documented - writers)}.",
                )

    def test_no_other_module_touches_the_archive_unclassified(self):
        """Breaks when a module starts naming the archive through a sink this file
        does not recognise, which is how the gate above would go blind.

        A write reaching the archive by a route `WRITE_SINKS` does not list would
        otherwise read as a module that merely mentions it, and the stage set
        would stay green while a fourth writer shipped.
        """
        writers, mentioners = _classified()
        unclassified = sorted(mentioners - writers - ARCHIVE_READERS)
        self.assertEqual(
            unclassified,
            [],
            f"{unclassified} name the committed archive and are neither a known reader "
            "nor recognised as writing it. Classify each: add it to the documents and to "
            "ARCHIVE_READERS, or teach WRITE_SINKS the route it writes by.",
        )

    def test_this_gate_tells_a_writer_from_a_reader(self):
        """The code side's sensitivity check, in the same run.

        Forges four module sources and requires four different answers. If the
        reader and the writer ever answered alike, the stage set above would be
        derived from "which modules mention the graph archive" — a neighbouring
        question with the same answer today and a different one tomorrow.
        """
        self.assertEqual(archive_use(FORGED_WRITER), (True, True), "an inline write")
        self.assertEqual(
            archive_use(FORGED_ALIASED_WRITER),
            (True, True),
            "a write that reaches the archive through a local name, which is the "
            "ordinary way to write the same code",
        )
        self.assertEqual(
            archive_use(FORGED_READER),
            (False, True),
            "a module that only reads the archive must not be named as a stage an "
            "operator has to order their extractors around",
        )
        self.assertEqual(archive_use(FORGED_UNRELATED), (False, False), "neither")

    def test_this_gate_notices_a_dropped_element(self):
        """The prose sensitivity check, in the same run.

        Forges each real section with one required element removed and asserts the
        checker reports exactly that element. If this ever passes trivially, the
        assertions above are measuring the presence of a document rather than the
        constraint, the symptom and the trap it has to state.
        """
        for copy in GUARDED:
            report = sensitivity(body_of(copy), copy.required)
            with self.subTest(path=copy.path):
                self.assertEqual(
                    report.already_missing,
                    [],
                    f"precondition: {copy.path} has already dropped "
                    f"{report.already_missing}, so the forges below remove nothing and "
                    "conclude nothing",
                )
                self.assertEqual(
                    report.undetected,
                    [],
                    f"{copy.path} states {report.undetected} only incidentally: removing "
                    "it is not reported as that element going missing, so the constraint "
                    "or its symptom could leave the document unnoticed",
                )

    def test_this_gate_notices_a_removed_heading(self):
        """The other way the gate could go vacuous: the extractor finding nothing
        after a rename, and every content assertion passing over ""."""
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                self.assertEqual(
                    section_after_rename(copy),
                    "",
                    f"renaming {copy.heading!r} away in {copy.path} still yielded a "
                    "section, so the assertions above may be reading a different one",
                )


if __name__ == "__main__":
    unittest.main()
