"""Two documentation failure modes this repository has already had, as one gate.

Both are silent. Nothing failed when either happened, and both are recorded in
`CLAUDE.md` as human obligations - which is to say as things somebody has to
remember.

**A renamed heading breaks an inbound deep link.** Another repository's README
links into these docs. Install detail used to live in this repository's README,
a consumer linked to it, and those sections were removed while that link still
pointed at them; the only reason it was caught is that somebody thought to look.
The check cannot be written the obvious way round, because the repositories that
link in here are private and are deliberately not named in this one, so CI has
nothing to grep. So this repository declares the anchors instead - a committed
list, `docs/load-bearing-anchors.txt` - and the gate fails when a declared
anchor no longer has a heading behind it. That inverts the obligation into one a
maintainer can discharge inside their own repository: leave a line alone.

**A link that no longer resolves.** Relative links and in-page anchors across
`README.md`, `docs/` and `skills/` are the routing between one persona's
document and the next, and a heading can be renamed for good reasons by someone
who has no idea what points at it.

Each check is a plain named function taking the repository root and returning a
`Report`, and `CHECKS` lists them explicitly. Neither half of that is
incidental. A registry built by import-time decoration cannot be driven one
check at a time, so the checks cannot be tested individually - and an untestable
gate is what this repository refuses.

`Report.read` is the other half. A gate over prose cannot be mutation-tested
from `src/`, so it has to say in its own run that it looked at something: a
check that read no link and a check that read every link both report no
problems, and the second is a pass while the first is a broken extractor. Zero
is therefore a failure, not a pass, and the runner says so in those words.

Run: python3 tests/docs_integrity.py
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Where the declared anchors live. Beside the documents it guards, because a
# maintainer renaming a heading is already working in that directory.
DECLARATION = Path("docs/load-bearing-anchors.txt")

# What the link check reads. The README plus the two directories a persona is
# routed through; `CLAUDE.md` and `CHEATSHEET.md` are reached as link targets
# rather than scanned, which is enough to resolve an anchor into either.
DOC_ROOTS = ("README.md", "docs", "skills")

# Schemes that leave this repository. Nothing here can say whether they resolve.
EXTERNAL = ("http://", "https://", "mailto:")

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# Inline links only: `[text](target)`, with an optional `"title"` after the
# target. A target containing whitespace is not one this repository writes.
_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+\"[^\"]*\")?\s*\)")
# What a GitHub heading slug drops: everything that is not a word character, a
# hyphen or a space. `Use \`explorer.html\`` becomes `use-explorerhtml`.
_NOT_IN_SLUG = re.compile(r"[^\w\- ]", re.UNICODE)


@dataclass(frozen=True)
class Report:
    """What one check looked at, and what was wrong with it.

    `read` is not decoration and not a statistic. It is how the check reports
    that it is still reading the artefact it claims to read: an empty
    `problems` means nothing when `read` is zero, so the runner treats zero as
    a failure rather than a pass.
    """

    subject: str
    read: int
    problems: list[str]


@dataclass(frozen=True)
class Gate:
    """One check, its name in the output, and what to do when it fails."""

    name: str
    run: Callable[[Path], Report]
    remedy: str


def unfenced_lines(text: str) -> Iterator[tuple[int, str]]:
    """Numbered lines outside fenced code blocks.

    Fences are excluded from both extractors for the same reason: what is
    inside one is an example a reader copies, not a statement this repository
    makes. For headings it is load-bearing today - these documents are full of
    shell blocks whose first line is a `#` comment, and reading those as
    headings invents anchors and truncates sections. For links it is a
    precaution: an example naming a path inside somebody else's store would
    fail a check that cannot see their store.
    """
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield number, line


def unclosed_fence(text: str) -> int | None:
    """The line a fence opens on and never closes, or None when they balance.

    Skipping fences is what makes an odd one dangerous: everything after it is
    read as being inside a code block, so its links and headings are never
    looked at and the check reports clean over the part of the document it
    stopped reading. That is a partial vacuity the problem count cannot show,
    so it is reported as a problem of its own.
    """
    opened: int | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            opened = None if opened else number
    return opened


def anchor_slug(heading: str) -> str:
    """A heading's GitHub anchor: lowercased, punctuation dropped, spaces hyphens."""
    return _NOT_IN_SLUG.sub("", heading.strip().lower()).replace(" ", "-")


def heading_anchors(text: str) -> list[str]:
    """Every anchor the document offers, in order.

    Repeated headings get GitHub's `-1`, `-2` suffixes, so a document with two
    `## Troubleshooting` sections resolves both rather than only the first.
    """
    anchors: list[str] = []
    seen: Counter[str] = Counter()
    for _, line in unfenced_lines(text):
        found = _HEADING.match(line)
        if not found:
            continue
        slug = anchor_slug(found.group(2))
        seen[slug] += 1
        anchors.append(slug if seen[slug] == 1 else f"{slug}-{seen[slug] - 1}")
    return anchors


def relative_links(text: str) -> list[tuple[int, str]]:
    """Inline link targets that stay inside the repository, as (line, target)."""
    return [
        (number, target)
        for number, line in unfenced_lines(text)
        for target in _LINK.findall(line)
        if not target.startswith(EXTERNAL)
    ]


def documents(root: Path) -> list[Path]:
    """Every Markdown file the link check reads, sorted so output is stable."""
    found: list[Path] = []
    for name in DOC_ROOTS:
        entry = root / name
        found.extend([entry] if entry.is_file() else sorted(entry.rglob("*.md")))
    return found


def declarations(root: Path) -> list[str]:
    """The declared anchors, comments and blank lines dropped."""
    path = root / DECLARATION
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def shown(root: Path, target: Path) -> str:
    """A path as it appears in output: repository-relative where it can be."""
    return str(target.relative_to(root)) if target.is_relative_to(root) else str(target)


def unresolved(root: Path, target: Path, anchor: str) -> str | None:
    """Why `target#anchor` does not resolve, or None when it does.

    An anchor is only checkable in Markdown, and saying so is the point: a
    silent skip would let the gate report green over a link it never read. A
    non-Markdown target with an anchor is named as unverified rather than
    passed.
    """
    if not target.is_file():
        return f"no such file: {shown(root, target)}"
    if not anchor:
        return None
    if target.suffix != ".md":
        return f"anchor on a non-Markdown target cannot be checked: #{anchor}"
    if anchor not in heading_anchors(target.read_text(encoding="utf-8")):
        return f"no heading in {shown(root, target)} makes #{anchor}"
    return None


def declared_anchors_resolve(root: Path) -> Report:
    """Every declared load-bearing anchor still has a heading behind it.

    Breaks when a heading another repository deep-links to is renamed or
    removed, which is the failure this whole file exists for.
    """
    problems: list[str] = []
    entries = declarations(root)
    for entry in entries:
        path, _, anchor = entry.partition("#")
        problem = unresolved(root, (root / path).resolve(), anchor)
        if problem:
            problems.append(f"{DECLARATION}: {entry}\n    {problem}")
    return Report("declared anchor(s)", len(entries), problems)


def internal_links_resolve(root: Path) -> Report:
    """Every relative link and in-page anchor points at something that exists.

    Breaks when a document is renamed, moved or deleted while something still
    links to it, and when a heading one of these documents targets is renamed.
    """
    problems: list[str] = []
    read = 0
    for document in documents(root):
        text = document.read_text(encoding="utf-8")
        opened = unclosed_fence(text)
        if opened is not None:
            problems.append(
                f"{document.relative_to(root)}:{opened}: unclosed code fence"
                f"\n    nothing after this line was read, so the check cannot report on it"
            )
        for number, target in relative_links(text):
            read += 1
            path, _, anchor = target.partition("#")
            resolved = (document.parent / path).resolve() if path else document
            problem = unresolved(root, resolved, anchor)
            if problem:
                problems.append(f"{document.relative_to(root)}:{number}: {target}\n    {problem}")
    return Report("internal link(s)", read, problems)


# Listed, not decorated. Import-time registration would make every check
# unreachable on its own, and each of these has to be callable by name.
CHECKS: tuple[Gate, ...] = (
    Gate(
        "declared-anchors",
        declared_anchors_resolve,
        f"Restore the heading, or change the consumer and its line in {DECLARATION} together.",
    ),
    Gate(
        "internal-links",
        internal_links_resolve,
        "Point the link at what the document is called now, restore the heading, or"
        "\nclose the fence so the rest of the document is read.",
    ),
)


def main(root: Path = ROOT) -> int:
    """Run every check and report as one gate. 0 when all pass, 1 when any fails.

    `root` is a parameter so the runner itself is testable against a forged
    repository - including the empty one, which is the only way to drive the
    read-nothing refusal.
    """
    failed = 0
    for gate in CHECKS:
        report = gate.run(root)
        if report.read == 0:
            print(
                f"{gate.name}: read no {report.subject} at all, so it has nothing to report on"
                f"\n  - a check that looked at nothing is not a check that passed.",
                file=sys.stderr,
            )
            failed = 1
            continue
        if report.problems:
            print(
                f"{gate.name}: {len(report.problems)} of {report.read}"
                f" {report.subject} do not resolve:\n",
                file=sys.stderr,
            )
            for problem in report.problems:
                print(f"  {problem}", file=sys.stderr)
            print(f"\n{gate.remedy}", file=sys.stderr)
            failed = 1
            continue
        print(f"{gate.name}: all {report.read} {report.subject} resolve")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
