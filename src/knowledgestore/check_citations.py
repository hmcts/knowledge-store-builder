"""Gate published answers on whether the code they cite exists in the graph.

A store that publishes answers about code makes claims a reader will act on, and
an invented identifier is the claim most worth catching: the first run of one
store's own version of this gate found a genuinely invented one. Stores were
writing it themselves (#357) and the copy was wrong where the library is right.
It normalised a citation with `cite.lower().rstrip("()")`, which strips trailing
brackets and then stops - so `setValue(null)` was looked up as `setvalue(null` and
a method that exists failed. Changing it to `split("(")[0]` flipped a different
verdict the wrong way: `ofNullable(...).ifPresent(...)` had passed only because
the old rule left an empty member, and the empty string is a substring of every
label. A failure that looks like a pass is not found by reproducing the first bug.

**One rule, supported.** Matching is the normalisation the summaries stage already
applies to its own grounding check - `build_community_summaries._normalise`, which
reduces to letters and digits so `setValue`, `.setValue()` and `set_value` are one
name. That function is underscore-prefixed, which a store pinning a release cannot
depend on, so it is exposed here as `normalise` (the same function, not a copy, so
the two checks cannot disagree). Store gates should call this stage, or `resolve`
and `normalise` from this module, and never reach for the private name.

**What counts as a citation.** An inline backticked span in a published answer. Call
arguments are dropped before matching, because citing the argument is not sloppy
prose - where the finding is about the value being passed, removing it deletes the
point - and the argument is not what resolves. A chain such as
`ofNullable(...).ifPresent(...)` is split into its members and every member must
resolve; a member that normalises to nothing never matches, which is the empty
string defect above. A span with no code shape (`null`, `retry`) is counted as
skipped, not checked: English words in backticks are not claims about code.

A path (`a/b/Foo.java`, or a bare `Foo.java`) resolves when its segments are the
trailing segments of a node's source file. Segments, not characters: an unanchored
suffix test also matches `oo/Foo.java` against `src/Foo.java`.

**Database columns are a third result, not a pass or a failure.** The graph holds
AST nodes, so a `table.column` citation will never resolve and never should.
Rejecting them hands a store that cites its schema a permanently red gate;
accepting them silently drops real misses. A span with no call, exactly two
lower-case snake_case members and no resolution is therefore reported on its own,
counted and named, and never changes the exit code. A human decides. If both
members do resolve it is an ordinary pass.

**Nearest real identifier.** An unresolved citation names the closest identifier
the graph holds, because "does not resolve" is a fraction of the help of "you meant
this one". It is a suggestion by spelling similarity, not a verdict that the two are
the same thing.

**Scope.** The published answers are `docs/topics` and `docs/deep-dives`, or the
files and directories named on the command line. Generated evidence - the
`*-input.json` extracts - is never read: evidence has to stay faithful even where it
is wrong, while an answer states what is true. Fenced code blocks are skipped.

**A gate, as `check-evidence` and `check-answers` are.** Exit 1 when any citation
is unresolved, 2 when it cannot run (no graph, no answers), 0 otherwise. Database
columns and skipped spans never fail it. The result is a statement about the
graph's code identifiers: a pass does not show a cited claim is *true*, only that
the thing it names exists.

Run: knowledgestore check-citations [--graph PATH] [path ...]
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config, graph_files, graph_stream
from .build_community_summaries import _normalise, prose_identifiers

# The supported spelling of the private function. An alias rather than a copy.
normalise = _normalise

_FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.DOTALL | re.MULTILINE)
_SPAN = re.compile(r"`([^`\n]+)`")
_INNER_CALL = re.compile(r"\([^()]*\)")
_NAME_CHARS = re.compile(r"[.\w$@]+")
_FILE_EXTENSION = re.compile(
    r"\.(?:java|ts|js|py|json|yaml|yml|xml|raml|csv|feature|sql|html|tsx)$", re.I
)
_COLUMN_PART = re.compile(r"[a-z][a-z0-9_]*")
NEAREST_CUTOFF = 0.7


@dataclass
class Vocabulary:
    """What the graph holds, in the forms a citation can be compared against."""

    # normalised label -> the label to show (shortest, then alphabetical)
    labels: dict[str, str] = field(default_factory=dict)
    # lower-cased last path segment -> source files (as lower-cased segment tuples)
    paths: dict[str, set[tuple[str, ...]]] = field(default_factory=dict)
    # lower-cased basename -> a real source file to show
    files: dict[str, str] = field(default_factory=dict)
    _names: list[str] | None = None
    _basenames: list[str] | None = None

    def add(self, label: str, source_file: str) -> None:
        key = _normalise(label)
        if key:
            shown = self.labels.get(key)
            if shown is None or (len(label), label) < (len(shown), shown):
                self.labels[key] = label
        if source_file:
            segments = tuple(s for s in source_file.lower().split("/") if s)
            if segments:
                self.paths.setdefault(segments[-1], set()).add(segments)
                base = self.files.get(segments[-1])
                if base is None or source_file < base:
                    self.files[segments[-1]] = source_file

    def names(self) -> list[str]:
        if self._names is None:
            self._names = sorted(self.labels)
        return self._names

    def basenames(self) -> list[str]:
        if self._basenames is None:
            self._basenames = sorted(self.paths)
        return self._basenames


def load_vocabulary(graph: Path) -> Vocabulary:
    vocabulary = Vocabulary()
    for node in graph_stream.iter_array(graph, "nodes"):
        vocabulary.add(str(node.get("label") or ""), str(node.get("source_file") or ""))
    return vocabulary


@dataclass(frozen=True)
class Verdict:
    """`kind` is resolved, unresolved, column or skipped."""

    kind: str
    nearest: str = ""


def _members(span: str) -> tuple[str, bool] | None:
    """The call-free text of a span and whether it carried a call; None if unusable."""
    text = span.strip()
    called = "(" in text
    previous = None
    while previous != text:
        previous = text
        text = _INNER_CALL.sub("", text)
    if "(" in text or ")" in text or not _NAME_CHARS.fullmatch(text):
        return None
    return text, called


def _is_path(span: str) -> bool:
    text = span.strip()
    if re.search(r"\s|://", text):
        return False
    return "/" in text or bool(_FILE_EXTENSION.search(text))


def _nearest(target: str, pool: list[str]) -> str:
    close = difflib.get_close_matches(target, pool, n=1, cutoff=NEAREST_CUTOFF)
    return close[0] if close else ""


def _resolve_path(span: str, vocabulary: Vocabulary) -> Verdict:
    text = span.strip().removeprefix("./").lstrip("/")
    if _normalise(text) in vocabulary.labels:
        return Verdict("resolved")
    parts = tuple(p for p in text.lower().split("/") if p)
    if not parts or any(not _normalise(p) for p in parts):
        return Verdict("skipped")
    for full in vocabulary.paths.get(parts[-1], ()):
        if full[-len(parts) :] == parts:
            return Verdict("resolved")
    close = _nearest(parts[-1], vocabulary.basenames())
    return Verdict("unresolved", vocabulary.files[close] if close else "")


def resolve(span: str, vocabulary: Vocabulary) -> Verdict:
    """Decide one backticked span against the graph."""
    if _is_path(span):
        return _resolve_path(span, vocabulary)
    split = _members(span)
    if split is None:
        return Verdict("skipped")
    text, called = split
    names = [m for m in text.split(".") if _normalise(m)]
    if not names:
        return Verdict("skipped")
    code_shaped = called or len(names) > 1 or bool(prose_identifiers(text))
    if not code_shaped:
        return Verdict("skipped")
    if _normalise(text) in vocabulary.labels and _normalise(text):
        return Verdict("resolved")
    missing = [m for m in names if _normalise(m) not in vocabulary.labels]
    if not missing:
        return Verdict("resolved")
    if not called and len(names) == 2 and all(_COLUMN_PART.fullmatch(m) for m in names):
        return Verdict("column")
    key = _normalise(missing[0])
    close = _nearest(key, vocabulary.names())
    return Verdict("unresolved", vocabulary.labels[close] if close else "")


def citations(text: str) -> list[str]:
    """Inline backticked spans outside fenced code blocks, in order, de-duplicated."""
    seen: dict[str, None] = {}
    for match in _SPAN.finditer(_FENCE.sub("", text)):
        seen.setdefault(match.group(1).strip(), None)
    return list(seen)


def answer_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.md")))
        elif path.is_file():
            files.append(path)
    return files


@dataclass
class Report:
    resolved: int = 0
    skipped: int = 0
    unresolved: list[tuple[str, str, str]] = field(default_factory=list)
    columns: list[tuple[str, str]] = field(default_factory=list)


def check(files: list[Path], vocabulary: Vocabulary) -> Report:
    report = Report()
    for path in files:
        for span in citations(path.read_text(encoding="utf-8")):
            verdict = resolve(span, vocabulary)
            if verdict.kind == "resolved":
                report.resolved += 1
            elif verdict.kind == "skipped":
                report.skipped += 1
            elif verdict.kind == "column":
                report.columns.append((str(path), span))
            else:
                report.unresolved.append((str(path), span, verdict.nearest))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledgestore check-citations",
        description="Fail if a published answer cites code the graph does not hold.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="answer files or directories (default: docs/topics and docs/deep-dives)",
    )
    parser.add_argument("--graph", type=Path, help="default: this store's graph")
    arguments = parser.parse_args(argv)

    graph = arguments.graph if arguments.graph else graph_files.graph_to_read(config.GRAPH_PATH)
    if graph is None or not graph.is_file():
        print(
            f"No graph at {arguments.graph or config.GRAPH_PATH} (or its .gz) - "
            "run `knowledgestore merge-layers` first, or pass --graph.",
            file=sys.stderr,
        )
        return 2
    roots = arguments.paths or [config.TOPICS_DOCS_DIR, config.DEEPDIVES_DOCS_DIR]
    files = answer_files(roots)
    if not files:
        print(
            "No published answers to check under "
            + ", ".join(str(r) for r in roots)
            + " - nothing was asserted.",
            file=sys.stderr,
        )
        return 2

    report = check(files, load_vocabulary(graph))
    checked = report.resolved + len(report.unresolved)
    print(
        f"{len(files):,} answer files against {graph.name}: {checked:,} citations checked, "
        f"{report.resolved:,} resolve, {len(report.unresolved):,} do not; "
        f"{len(report.columns):,} database-column citations reported separately; "
        f"{report.skipped:,} spans skipped (no code shape)"
    )
    if report.columns:
        print(
            "\nDatabase columns, neither passed nor failed (the graph holds code, not "
            "schemas) - check these by hand:"
        )
        for path, span in report.columns:
            print(f"  {path}  `{span}`")
    if not report.unresolved:
        return 0
    print("\nCited but not in the graph:", file=sys.stderr)
    for path, span, nearest in report.unresolved:
        hint = f"  nearest: {nearest}" if nearest else "  no near match"
        print(f"  {path}  `{span}`{hint}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
