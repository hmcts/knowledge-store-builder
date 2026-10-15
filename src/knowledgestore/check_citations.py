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

**A source link resolves against the build it claims to cite.** Answers cite a
node's `url`, `https://github.com/<org>/<repo>/blob/<sha>/<path>#L<n>`, written
anywhere in the prose - a markdown link, a bare link or a backticked one. Such a
link is read with `source_urls.parse_blob_url`, the inverse of the function that
wrote it, and holds two claims, each checked against what the store read:

- the file: its path must be one of the graph's `source_file`s *for that
  repository* (repo-relative on either build route). A path held only by another
  repository does not resolve. A path no node names - a corpus-search match - is
  asked of the repository's clone under `repositories/<name>/`, at the commit
  provenance records, with `git rev-parse --verify --quiet <sha>:<path>` (the
  existence test `git cat-file -e` makes, without a `fatal:` line on every miss
  interleaved with the report). Reported as `unknown file`, with the nearest file
  the graph holds for that repository. With no clone on this machine the file
  claim cannot be decided, so the link is listed as unchecked rather than failed -
  unless its sha is wrong, which needs no clone to say.
- the commit: its sha must be the one `knowledge/provenance.json` records for that
  repository. A branch or an abbreviated sha is not, and a link to it drifts off
  the line it cites. Reported as `different sha`, naming the sha the store read.

A link wrong both ways says both. The repository is found from the link's
`<org>/<repo>` through `config/repositories.txt`, the same map that wrote the link,
because the name on the host can differ from the store's. A link to a repository
the store does not configure - a third-party library, another organisation - is a
fourth result like a database column: counted, named, never failing. Any other URL
is skipped as before. The graph is the one already streamed for identifiers, so
checking links adds no second read of it; the line anchor is not checked.

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
is unresolved, 2 when it cannot run (no graph, no answers, or a repository list
that cannot be parsed), 0 otherwise. Database columns, links outside the estate and
skipped spans never fail it. The result is a statement about the graph's code
identifiers and files: a pass does not show a cited claim is *true*, only that the
thing it names exists.

Run: knowledgestore check-citations [--graph PATH] [path ...]
(run from the store root: the repository list and provenance are read from it)
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config, graph_files, graph_stream
from .build_community_summaries import _normalise, prose_identifiers
from .sync_repositories import run_git
from .source_urls import (
    BlobUrl,
    SourceUrls,
    parse_blob_url,
    repo_relative,
    single_root_repositories,
)

# The supported spelling of the private function. An alias rather than a copy.
normalise = _normalise

# Public: `answers` reads answers by the same rule, and a rename here must not
# silently become a break there. `SPAN` stays backticks only, because `answers`
# masks code spans with it.
FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.DOTALL | re.MULTILINE)
SPAN = re.compile(r"`([^`\n]+)`")
# A backticked span, or a github.com link written outside one. The link stops at
# the characters that end it in markdown - `)` closing a link, `>` an autolink -
# which `source_urls` always percent-encodes inside a path.
_CITED = re.compile(r"`([^`\n]+)`|(https://github\.com/[^\s<>()\[\]`'\"]+)")
_SENTENCE_END = ".,;:!?"
UNKNOWN_FILE = "unknown file"
DIFFERENT_SHA = "different sha"
OUTSIDE_STORE = "a repository this store does not build"
NO_CLONE = "a file no node names, and no clone under repositories/ to look in"
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
    # repository -> the `source_file`s its nodes carry, as the graph writes them
    sources: dict[str, set[str]] = field(default_factory=dict)
    # the store's repository list and provenance; None when they were not read
    urls: SourceUrls | None = None
    # the store's `repositories/`, whose clones answer for files no node names
    clones: Path | None = None
    run: Callable[[list[str]], str] = run_git
    _names: list[str] | None = None
    _basenames: list[str] | None = None
    _files: dict[str, set[str]] | None = None

    def add(self, label: str, source_file: str, repo: str = "") -> None:
        if repo and source_file:
            self.sources.setdefault(repo, set()).add(source_file)
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

    def repository_files(self, repo: str) -> set[str]:
        """The repo-relative files the graph holds for `repo`, on either build route."""
        if self._files is None:
            single_root = single_root_repositories(
                {"repo": name, "source_file": path}
                for name, paths in self.sources.items()
                for path in paths
            )
            self._files = {}
            for name, paths in self.sources.items():
                inside = (repo_relative(name, p, name in single_root) for p in paths)
                self._files[name] = {p for p in inside if p}
        return self._files.get(repo, set())

    def clone_holds(self, repo: str, sha: str, path: str) -> bool | None:
        """Whether `repo`'s clone holds `path` at `sha`; None when there is no clone."""
        clone = self.clones / repo if self.clones else None
        if clone is None or not (clone / ".git").exists():
            return None
        try:
            self.run(["-C", str(clone), "rev-parse", "--verify", "--quiet", f"{sha}:{path}"])
        except subprocess.CalledProcessError:
            return False
        return True


def load_vocabulary(
    graph: Path,
    urls: SourceUrls | None = None,
    clones: Path | None = None,
    run: Callable[[list[str]], str] = run_git,
) -> Vocabulary:
    """Stream the graph's nodes once. Without `urls`, no source link is checked;
    without `clones`, a link to a file no node names is unchecked."""
    vocabulary = Vocabulary(urls=urls, clones=clones, run=run)
    for node in graph_stream.iter_array(graph, "nodes"):
        vocabulary.add(
            str(node.get("label") or ""),
            str(node.get("source_file") or ""),
            str(node.get("repo") or ""),
        )
    return vocabulary


@dataclass(frozen=True)
class Verdict:
    """`kind` is resolved, unresolved, column, unchecked or skipped.

    `reason` says why a source link is unresolved or unchecked; else it is empty.
    """

    kind: str
    nearest: str = ""
    reason: str = ""


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
    """The closest real identifier, because "does not resolve" is a frustrating
    verdict on its own.

    Cost is linear in the pool and paid once per miss. Measured on this
    interpreter with randomly generated labels: 0.016s per miss over 10,000
    labels, 0.164s over 100,000, 0.655s over 400,000 - so an estate-sized graph
    with fifty misses spends roughly half a minute here. That is tolerable for a
    publish-time gate and is recorded so the next reader need not re-measure:

        python3 -c "import difflib,random,string,time; random.seed(7); \
        pool=[''.join(random.choices(string.ascii_lowercase,k=12)) for _ in range(400000)]; \
        t=time.perf_counter(); difflib.get_close_matches('setvaluenull',pool,n=1,cutoff=0.7); \
        print(time.perf_counter()-t)"

    If it ever stops being tolerable, cap the misses that get a suggestion
    rather than lowering the cutoff: a lower cutoff returns worse suggestions at
    the same cost.
    """
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


def _resolve_link(link: BlobUrl, vocabulary: Vocabulary) -> Verdict:
    urls = vocabulary.urls
    repos = urls.repositories_at(link.org, link.repo) if urls else ()
    if urls is None or not repos:
        return Verdict("unchecked", reason=OUTSIDE_STORE)
    holding = {r for r in repos if link.path in vocabulary.repository_files(r)}
    built = {r for r in repos if urls.build_sha(r) == link.sha}
    undecided = False
    if not holding:
        # A file no node names: the clone answers, at the commit the store read.
        for r in repos:
            held = vocabulary.clone_holds(r, urls.build_sha(r) or link.sha, link.path)
            undecided = undecided or held is None
            if held:
                holding.add(r)
    if holding & built:
        return Verdict("resolved")
    if not holding and undecided and built:
        return Verdict("unchecked", reason=NO_CLONE)
    reasons, nearest = [], ""
    if not holding and not undecided:
        reasons.append(f"{UNKNOWN_FILE} in {', '.join(repos)}")
        pool = sorted(set().union(*(vocabulary.repository_files(r) for r in repos)))
        nearest = _nearest(link.path, pool)
    if not built:
        read = sorted({urls.build_sha(r) for r in repos} - {""})
        reasons.append(
            f"{DIFFERENT_SHA}: the store read {', '.join(read)}"
            if read
            else f"{DIFFERENT_SHA}: the store records no commit for {', '.join(repos)}"
        )
    if not reasons:
        # Duplicate entries for one origin: one holds the file, another the commit.
        reasons.append(f"{DIFFERENT_SHA}: no single repository holds the file at it")
    return Verdict("unresolved", nearest, "; ".join(reasons))


def resolve(span: str, vocabulary: Vocabulary) -> Verdict:
    """Decide one cited span or source link against the graph."""
    link = parse_blob_url(span)
    if link is not None:
        return _resolve_link(link, vocabulary)
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


def spans(text: str) -> list[str]:
    """Inline backticked spans outside fenced code blocks, in order, de-duplicated.

    `answers` lists these first and its own URL and corpus-path finds after them;
    a record's citation order is part of what it writes, so it reads spans alone.
    """
    seen: dict[str, None] = {}
    for match in SPAN.finditer(FENCE.sub("", text)):
        seen.setdefault(match.group(1).strip(), None)
    return list(seen)


def citations(text: str) -> list[str]:
    """Inline backticked spans, and source links written anywhere, outside fenced
    code blocks - in order, de-duplicated."""
    seen: dict[str, None] = {}
    for match in _CITED.finditer(FENCE.sub("", text)):
        if match.group(1) is not None:
            seen.setdefault(match.group(1).strip(), None)
            continue
        link = match.group(2).rstrip(_SENTENCE_END)
        if parse_blob_url(link) is not None:
            seen.setdefault(link, None)
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
    # source links the store cannot decide, either way: (file, link)
    unchecked: list[tuple[str, str]] = field(default_factory=list)
    # why each source link failed or went unchecked, by span (one span, one reason)
    reasons: dict[str, str] = field(default_factory=dict)


def check(files: list[Path], vocabulary: Vocabulary) -> Report:
    report = Report()
    for path in files:
        # NOSONAR(S8707) - reading a path the operator named is the purpose of the
        # flag. Grounds are stated once in `build_community_summaries.merge`; this
        # site cites them rather than restating them, so the two cannot drift.
        for span in citations(path.read_text(encoding="utf-8")):  # NOSONAR(S8707)
            verdict = resolve(span, vocabulary)
            if verdict.kind == "resolved":
                report.resolved += 1
            elif verdict.kind == "skipped":
                report.skipped += 1
            elif verdict.kind == "column":
                report.columns.append((str(path), span))
            elif verdict.kind == "unchecked":
                report.unchecked.append((str(path), span))
                report.reasons[span] = verdict.reason
            else:
                report.unresolved.append((str(path), span, verdict.nearest))
                if verdict.reason:
                    report.reasons[span] = verdict.reason
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

    try:
        urls = SourceUrls.from_store()
    except ValueError as error:
        # Read as an empty estate, it would turn every source link into an unchecked one.
        print(f"Cannot read {config.REPOSITORIES_CONFIG}: {error}", file=sys.stderr)
        return 2

    report = check(files, load_vocabulary(graph, urls, config.REPOSITORIES_DIR))
    checked = report.resolved + len(report.unresolved)
    print(
        f"{len(files):,} answer files against {graph.name}: {checked:,} citations checked, "
        f"{report.resolved:,} resolve, {len(report.unresolved):,} do not; "
        f"{len(report.columns):,} database-column citations reported separately; "
        f"{report.skipped:,} spans skipped (no code shape)"
        + (
            f"; {len(report.unchecked):,} source links this store cannot decide, "
            "reported separately"
            if report.unchecked
            else ""
        )
    )
    if report.columns:
        print(
            "\nDatabase columns, neither passed nor failed (the graph holds code, not "
            "schemas) - check these by hand:"
        )
        for path, span in report.columns:
            print(f"  {path}  `{span}`")
    if report.unchecked:
        print(
            "\nSource links this store cannot decide, neither passed nor failed - check "
            "these by hand:"
        )
        for path, span in report.unchecked:
            print(f"  {path}  `{span}`  {report.reasons.get(span, '')}".rstrip())
    if not report.unresolved:
        return 0
    print("\nCited but not in the graph:", file=sys.stderr)
    for path, span, nearest in report.unresolved:
        hint = f"  nearest: {nearest}" if nearest else "  no near match"
        reason = report.reasons.get(span)
        if reason:
            hint = f"  {reason}" + (f"  nearest: {nearest}" if nearest else "")
        print(f"  {path}  `{span}`{hint}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
