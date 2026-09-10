"""Four documentation failure modes this repository has already had, as one gate.

All four are silent. Nothing failed when any of them happened, and each is
recorded in `CLAUDE.md` as a human obligation - which is to say as something
somebody has to remember.

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

**A mirrored rule that has drifted from its master.**
`docs/grounding-and-verification.md` states a contract the skills restate at the
point each one needs it, and the obligation runs one way: an agent reads the
skill it was invoked with and may never open the master, so a skill carrying a
superseded rule is the rule that gets applied. `docs/mirrored-contract.txt`
declares the statements the two ends share, and both ends are checked - the
master's own prose as well as the copy - because a list of sentences held only
against the copies keeps passing after the master is reworded. The other
direction is the same defect pointing outward: a file that carries a copy the
master's mirror list does not name will not be updated when the master changes,
so a document restating the contract without being declared is reported too.

**A retired instruction that has come back.** The README kept `graphify .` at
the store root long after the build skill documented why that cannot work. A
check for "the docs and the skills disagree" in general is not expressible; a
check for one named instruction reappearing is, and it is what happened.
`docs/retired-instructions.txt` is that list, and only fenced blocks are read -
prose about a retired instruction is legitimate and common, while a block is
what an operator copies.

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

# The mirrored contract: its master, the statements the copies share with it,
# and the separator those entries use.
MASTER = Path("docs/grounding-and-verification.md")
MIRRORS = Path("docs/mirrored-contract.txt")
SEPARATOR = " :: "

# The retired instructions that must not reappear in a command block.
RETIRED = Path("docs/retired-instructions.txt")

# How many of the master's statements a document has to carry before it is a
# copy of the contract rather than a document that shares its vocabulary. One
# is a phrase this repository uses everywhere - `summaries verify` is a command
# name, and two guides run it without restating anything. Two independent
# statements of the rule is a restatement, and the check names which two it
# found so a maintainer can declare the mirror or reword the document.
COPY_THRESHOLD = 2

# What every check here reads: the README, the cheatsheet, and the two
# directories a persona is routed through. `CLAUDE.md` is reached as a link
# target rather than scanned, which is enough to resolve an anchor into it.
#
# `CHEATSHEET.md` was a link target too until #346, and being one is not enough
# for the checks that read command blocks: it is a route into a store, an
# operator copies from it, and it instructed `--no-cluster` while the build
# skill forbade it. Every gate here reported "nothing to report" over a file
# none of them opened. `test_docs_integrity.py` pins its presence, because
# dropping it from this tuple restores that silence without failing anything
# else - every other assertion in that module is a floor the remaining
# documents clear on their own.
DOC_ROOTS = ("README.md", "CHEATSHEET.md", "docs", "skills")

# Schemes that leave this repository. Nothing here can say whether they resolve.
EXTERNAL = ("http://", "https://", "mailto:")

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# Inline links only: `[text](target)`, with an optional `"title"` after the
# target. A target containing whitespace is not one this repository writes.
_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+\"[^\"]*\")?\s*\)")
# What a GitHub heading slug drops: everything that is not a word character, a
# hyphen or a space. `Use \`explorer.html\`` becomes `use-explorerhtml`.
_NOT_IN_SLUG = re.compile(r"[^\w\- ]", re.UNICODE)
# A path the master's mirror list names, backticked, in a blockquoted line. The
# master declares its mirrors twice - a table for the contract, a bullet list
# for the section on estate content - and both are blockquotes, so this reads
# either without knowing which.
_MIRROR_PATH = re.compile(r"`([^`]+\.md)`")
# What a command token may sit against without being that command. A retired
# instruction has to be bounded at both ends or `graphify .` matches
# `graphify ...`, which is a different command and a legitimate one.
_IN_TOKEN = re.compile(r"[\w\-./]")


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


def fence_state(text: str) -> Iterator[tuple[int, str, bool]]:
    """Numbered lines, each with whether it is inside a fenced code block.

    One fence rule for every extractor here, rather than one per extractor.
    Two of them read outside the fences and one reads inside, and a second copy
    of the tracking is a second place for it to be subtly different - which is
    the failure `tests/doc_sections.py` records having had. The fence lines
    themselves belong to neither side and are not yielded.
    """
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        yield number, line, fenced


def unfenced_lines(text: str) -> Iterator[tuple[int, str]]:
    """Numbered lines outside fenced code blocks: what the document asserts.

    Fences are excluded from both extractors for the same reason: what is
    inside one is an example a reader copies, not a statement this repository
    makes. For headings it is load-bearing today - these documents are full of
    shell blocks whose first line is a `#` comment, and reading those as
    headings invents anchors and truncates sections. For links it is a
    precaution: an example naming a path inside somebody else's store would
    fail a check that cannot see their store.
    """
    return ((number, line) for number, line, fenced in fence_state(text) if not fenced)


def fenced_lines(text: str) -> Iterator[tuple[int, str]]:
    """Numbered lines inside fenced code blocks: what a reader copies and runs.

    The polarity is the opposite of `unfenced_lines` and deliberately so. A
    sentence about an instruction is a statement this repository makes about it,
    often that it is wrong; a line in a block is the instruction. Retiring an
    instruction has to leave the discussion of it legal, so the retired-
    instruction check reads only from here.
    """
    return ((number, line) for number, line, fenced in fence_state(text) if fenced)


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


def declared_entries(root: Path, name: Path) -> list[str]:
    """The entries in one declaration file, comments and blank lines dropped.

    Every declaration this gate reads is a committed list a maintainer edits,
    so each one explains itself at length in `#` comments and is parsed the
    same way. A missing file is an empty list rather than an error: the runner
    reports reading nothing, which is the more useful failure.
    """
    path = root / name
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def declarations(root: Path) -> list[str]:
    """The declared anchors, comments and blank lines dropped."""
    return declared_entries(root, DECLARATION)


def paired(entries: list[str]) -> list[tuple[str, str]]:
    """Declaration entries split on the separator, unsplittable ones dropped.

    A line with no separator is not silently taken as a one-sided entry: the
    checks count what they read, so an entry that parsed to nothing lowers the
    count rather than passing as something. Both files that use this shape say
    what the two halves are.
    """
    return [
        (left.strip(), right.strip())
        for left, _, right in (entry.partition(SEPARATOR) for entry in entries)
        if left.strip() and right.strip()
    ]


def collapsed(text: str) -> str:
    """One line, so a statement matches across a wrapped line break.

    The master and the skills are hard-wrapped prose, so almost every sentence
    worth pinning is broken by a newline somewhere. Matching raw would make a
    declared statement unwritable rather than merely awkward.
    """
    return " ".join(text.split())


def master_prose(root: Path) -> str:
    """The master's own statements, collapsed, with its mirror lists removed.

    Blockquoted lines are dropped because a blockquote in the master is where it
    says where it is mirrored, not where it states a rule - and those lists
    paraphrase the rules closely enough to satisfy a declared statement on
    their own. Left in, the check would compare the master's table with itself
    and report agreement for a rule the prose no longer holds.
    """
    path = root / MASTER
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    return collapsed(
        "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    )


def master_mirror_list(root: Path) -> list[str]:
    """The paths the master declares it is mirrored into, in the order given.

    Read from the master rather than restated here, so the list is load-bearing
    the moment a row is added to it. Both of the master's declarations are
    blockquotes - a table for the contract, bullets for the section on estate
    content - so both are read the same way.
    """
    path = root / MASTER
    if not path.is_file():
        return []
    found: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith(">"):
            continue
        found.extend(name for name in _MIRROR_PATH.findall(line) if name not in found)
    return found


def instructed(line: str, instruction: str) -> bool:
    """Whether one line runs a retired instruction, rather than containing it.

    Bounded at both ends against the characters a command token is made of, so
    `graphify .` is not found in `graphify ...` - a different command, and one
    the guides use. Written as a scan rather than a regex over the instruction
    because the instruction is committed text a maintainer writes, and text
    that is compiled as a pattern is text that can fail to compile.
    """
    start = line.find(instruction)
    while start != -1:
        end = start + len(instruction)
        before = line[start - 1] if start else " "
        after = line[end] if end < len(line) else " "
        if not _IN_TOKEN.match(before) and not _IN_TOKEN.match(after):
            return True
        start = line.find(instruction, start + 1)
    return False


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


def mirrored_contract_agrees(root: Path) -> Report:
    """Every declared statement is still in the master and still in its mirror.

    Breaks when the master is reworded and a copy is left behind, which is the
    failure `CLAUDE.md` calls the dangerous one: the skill an agent reads still
    states the superseded rule, and it reads as authoritative. Breaks the other
    way too - a copy edited away from a master that has not moved - because
    which end changed is not knowable from here and both need the same edit.

    Two more failures are the master's list rather than its prose: a declared
    mirror the list does not name, and a listed mirror with no declared
    statement. Either leaves the pair uncheckable while looking checked.
    """
    problems: list[str] = []
    prose = master_prose(root)
    listed = master_mirror_list(root)
    declared = paired(declared_entries(root, MIRRORS))
    for path, statement in declared:
        if path not in listed:
            problems.append(
                f"{MIRRORS}: {path}\n    the master's mirror list does not name it, so an"
                f" edit to {MASTER} would not be pointed at this copy"
            )
        target = root / path
        if not target.is_file():
            problems.append(f"{MIRRORS}: {path}\n    no such file: {shown(root, target)}")
            continue
        if statement not in prose:
            problems.append(
                f"{MIRRORS}: {path} :: {statement}\n    {MASTER} no longer states it in its"
                f" own prose, so the copies now agree with each other and not with a master"
            )
        if statement not in collapsed(target.read_text(encoding="utf-8")):
            problems.append(
                f"{MIRRORS}: {path} :: {statement}\n    the mirror no longer states it;"
                f" update the copy in the same change as {MASTER}"
            )
    for path in listed:
        if path not in {declared_path for declared_path, _ in declared}:
            problems.append(
                f"{MASTER}: {path}\n    listed as carrying the contract, but {MIRRORS}"
                f" declares no statement for it, so nothing about it is checked"
            )
    return Report("mirrored contract statement(s)", len(declared), problems)


def mirrors_are_declared(root: Path) -> Report:
    """No document restates the contract without the master naming it as a copy.

    Breaks when a fourth skill or guide starts carrying the contract and nobody
    adds it to the master's mirror list - the same defect as a drifted copy,
    pointing the other way: the next edit to the master will not reach a copy
    nothing knows about. Detection is by how many of the master's own statements
    a document carries, because a copy of a rule is written in the words of the
    rule.
    """
    statements = {statement for _, statement in paired(declared_entries(root, MIRRORS))}
    listed = set(master_mirror_list(root))
    problems: list[str] = []
    read = 0
    if not statements:
        problems.append(
            f"{MIRRORS} declares no statements, so nothing here can recognise a copy"
            f"\n    every document below would read as carrying none"
        )
    for document in documents(root):
        name = str(document.relative_to(root))
        if name == str(MASTER) or name in listed:
            continue
        read += 1
        text = collapsed(document.read_text(encoding="utf-8"))
        carried = sorted(statement for statement in statements if statement in text)
        if len(carried) >= COPY_THRESHOLD:
            problems.append(
                f"{name}: states {len(carried)} of the contract's rules and is not in"
                f" {MASTER}'s mirror list\n    {carried}\n    declare it as a mirror, or"
                f" say it in words the master does not use"
            )
    return Report("undeclared document(s)", read, problems)


def retired_instructions_stay_retired(root: Path) -> Report:
    """No command block runs an instruction this repository has retired.

    Breaks when a retired instruction comes back in a block a reader copies,
    which is what happened with `graphify .` at the store root: the README kept
    it long after the build skill documented why it cannot work, and nothing
    compared the two documents.

    Prose is not read, and that is the whole of how the check stays usable.
    Every document that retires an instruction has to be able to name it, so a
    check over prose would fire on the explanation and be turned off. What it
    cannot catch is therefore an instruction written as a prose imperative;
    `docs/retired-instructions.txt` says so rather than implying otherwise.
    """
    retired = paired(declared_entries(root, RETIRED))
    problems: list[str] = []
    read = 0
    for document in documents(root):
        text = document.read_text(encoding="utf-8")
        for number, line in fenced_lines(text):
            for instruction, remedy in retired:
                read += 1
                if instructed(line, instruction):
                    problems.append(
                        f"{document.relative_to(root)}:{number}: {line.strip()}\n    `"
                        f"{instruction}` was retired: {remedy}"
                    )
    return Report("command line(s) against a retired instruction", read, problems)


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
    Gate(
        "mirrored-contract",
        mirrored_contract_agrees,
        f"Move the statement at both ends in one change. {MASTER} is the master, so a"
        f"\nreworded rule there is not done until every copy carries it - and a copy is"
        f"\nnot fixed by deleting its line from {MIRRORS}.",
    ),
    Gate(
        "declared-mirrors",
        mirrors_are_declared,
        f"Add the document to {MASTER}'s mirror list and declare its statements in"
        f"\n{MIRRORS}, or point at the master instead of restating it.",
    ),
    Gate(
        "retired-instructions",
        retired_instructions_stay_retired,
        f"Show the route that replaced it. {RETIRED} names one per entry, and the"
        f"\nprose explaining why the retired one fails is what a block must not undo.",
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
                f"{gate.name}: {len(report.problems)} problem(s) in {report.read}"
                f" {report.subject}:\n",
                file=sys.stderr,
            )
            for problem in report.problems:
                print(f"  {problem}", file=sys.stderr)
            print(f"\n{gate.remedy}", file=sys.stderr)
            failed = 1
            continue
        print(f"{gate.name}: checked {report.read} {report.subject}, nothing to report")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
