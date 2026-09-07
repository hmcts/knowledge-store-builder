"""Every documented install command must be runnable, and give what the store says.

Two invariants, both about installing from a store's committed files, and both
silent when broken.

A store that pins its dependencies compiles a lock file. `uv pip compile` only
writes the index into that lock when asked (`--emit-index-url`), so a lock
compiled without it names no index - and installing from it fails with
"No matching distribution found", which reads as a missing release rather than a
missing feed. The store that this library was built for lost real time to that,
twice: its README carried `pip install -r requirements.lock` as the rebuild step
while a comment ten lines away explained that the command cannot work.

The invariant this enforces holds whichever way a store resolves it:

  - compile the lock with `--emit-index-url --emit-build-options` and it carries
    the index itself; every documented command is then valid and this check
    passes with nothing to say; or
  - leave the lock as it is, and every documented command that installs from it
    must pass the index on the command line.

Only shell inside fenced code blocks is read, because that is what a reader
copies. Prose about the failure, and `#` comments explaining it, are not
instructions - a store is expected to document the trap without tripping it.

**The second invariant: the lock must resolve the versions the requirements input
pins.** A lock is documented as the hash-pinned resolution of that input, and
nothing checked the relationship. A store that moves a pin to a version its index
has not published cannot recompile the lock, so the two files disagree - and
`pip install --require-hashes -r <lock>` then installs the version the LOCK
names, silently, because the lock is what that command reads. The store says it
uses one version and every build uses another.

That is the same class as an environment drifting from its lock, from the
direction nothing was watching: an environment can be reinstalled, but a
committed pin no lock satisfies is a claim the repository is making and getting
wrong. It was found on a store where a pin was moved to a release that was tagged
before its artefact reached the index - three checks passed, none of them having
opened the requirements input.

Every `name==version` requirement is compared, rather than a named package, so a
store pinning more than one thing is covered without this knowing what it pins.

**Neither half claims anything about a set it found empty.** A comparison that read
no pin at all is refused rather than reported, because a fully resolved store and a
parse that has stopped matching produce the same empty result and the same
sentence - and the second is the realistic one: a pin moved behind a `-r` include,
an input renamed, a spelling this module does not read. The documented-command half
cannot refuse - a store need not document installing from its lock - so it names
how many commands it read instead of asserting that all of them pass.

**A lock that is not compiled from anything is a third state, and it has to declare
itself.** The refusal above offered two remedies - commit the input, or delete the
lock - and a hand-authored lock can take neither: there is no input, and writing a
synthetic one to satisfy a check is inventing the artefact whose absence is the
honest state. A store reported a lock it maintains by hand, carrying commentary a
recompile would destroy and with a determinism test asserting the lock names the
library, so deleting it was not available either. That store had no green path,
which is the shape of a check people learn to skip.

So a lock may carry `AUTHORED_MARKER`, and an empty comparison is then skipped
rather than refused. Three properties keep that from being the switch that
`_report_unresolved` still declines to offer:

  - the store states it. Authorship is never inferred from an input being absent,
    because that is the check guessing, and guessing is what the refusal closed;
  - only the empty comparison is skipped. A marked lock beside an input that does
    state pins is compared exactly as any other lock, because the harm - a build
    installing the lock's version while the input claims another - does not depend
    on how the lock was written; and
  - the skip is reported every run. A pass that compared nothing says so, so no
    green result can be read as a comparison having happened.

What it does cost is stated in the message it prints: while the marker stands, an
input renamed out from under this half is no longer detected.

Run: knowledgestore check-install-docs
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from . import config

# Both directives name an index. `--index-url` is pip's primary index and
# `--extra-index-url` an additional feed, but either one in a lock means the lock
# carries an index, and either one on a command line supplies it.
#
# Matching only the additional form reported a lock whose own line reads
# `--index-url https://pypi.org/simple` as naming no index. That is a permanent
# failure on a correctly specified lock, and a check that cannot be satisfied is
# one people learn to skip - so it cost more than the vacuous pass it replaced.
# The bug is the usual shape here: the code was correct about
# `--extra-index-url` and the sentence it was answering was "does this lock name
# an index".
INDEX_FLAGS = ("--index-url", "--extra-index-url")
# The one the fix messages suggest adding to a command, which leaves whatever the
# environment already treats as primary in place.
INDEX_FLAG = "--extra-index-url"
DOC_SUFFIXES = {".md", ".yml", ".yaml", ".sh"}
SKIP_DIRS = {".git", ".venv", "node_modules", "repositories", "knowledge", "graphify-out"}

# How a lock declares that it is written by hand rather than compiled from an
# input. Three properties were wanted of the syntax and each one chose a part of
# it:
#
#   - pip must not be able to read it as anything. `#` begins a comment, which pip
#     and `uv` discard before parsing, so the declaration cannot alter what an
#     install resolves however the file is fed to them. Every alternative - a
#     requirement, an option, a `--config-settings` key - is a line some resolver
#     has an opinion about.
#   - a lock full of prose must not declare itself by accident. The reporting
#     store's lock carries dozens of commentary lines, so the marker is matched as
#     the whole stripped line and the namespace makes it deliberate. It reuses the
#     `knowledgestore: <thing>` form this library already writes into other
#     people's files (`extract_gherkin.REPORT_NOTE_MARKER`), inside the host
#     format's comment syntax.
#   - losing it must be visible. `uv pip compile` writes its own header and keeps
#     no comment it did not write, so a lock regenerated by mistake drops the line:
#     the skip stops, this half starts refusing again, and the cause is one removed
#     line at the top of the lock's diff rather than a behaviour change with no
#     artefact behind it.
AUTHORED_MARKER = "# knowledgestore: hand-authored lock"


# `name[extra,extra]==version ; marker` - extras and markers are stripped, because
# neither changes which version is asked for. A requirement pinned any other way
# (`>=`, `~=`, a URL, a `-r` include) is not compared: only `==` states a single
# version a lock can be checked against, and inventing a comparison for the others
# would report a disagreement that is not one.
_PINNED = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)(?:\[[^\]]*\])?==(?P<version>[^\s;#]+)")


def _normalise(name: str) -> str:
    """PEP 503 name comparison: `-`, `_` and `.` are equivalent, case is not."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pinned_versions(requirements: Path) -> dict[str, str]:
    """Normalised name -> version, for every `==` requirement the input states.

    A comment is skipped by the pattern being anchored, not by stripping `#` from
    the line first. That strip used to be here and was provably inert: `match`
    anchors at the start, so a leading `#` cannot begin a name whatever precedes
    the comparison, and the version class already excludes `#`, so a trailing
    comment on a live pin never reached the version either. Removing it leaves one
    mechanism holding the behaviour instead of two, one of which was doing nothing
    - and the test named for commented pins could not fail while the inert strip
    stood in front of the real guard.
    """
    found: dict[str, str] = {}
    if not requirements.is_file():
        return found
    for line in requirements.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _PINNED.match(line.strip())
        if match:
            found[_normalise(match.group("name"))] = match.group("version")
    return found


def locked_versions(lock: Path) -> dict[str, str]:
    """Normalised name -> version, for every requirement the lock resolves.

    A lock line carries its hashes on continuations (`pkg==1.2.3 \\`), so the
    version is the first whitespace-delimited token after `==`.
    """
    found: dict[str, str] = {}
    if not lock.is_file():
        return found
    for line in lock.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _PINNED.match(line.strip())
        if match:
            found[_normalise(match.group("name"))] = match.group("version")
    return found


def unresolved_pins(requirements: Path, lock: Path) -> list[tuple[str, str, str | None]]:
    """(name, pinned, locked) for every pin the lock does not deliver.

    `locked` is None when the lock resolves the requirement not at all, which is a
    different failure from resolving it at another version: the first installs no
    such package, the second installs a different one.
    """
    locked = locked_versions(lock)
    return [
        (name, version, locked.get(name))
        for name, version in sorted(pinned_versions(requirements).items())
        if locked.get(name) != version
    ]


def index_directive(line: str) -> tuple[str, str | None] | None:
    """`(flag, url)` when a requirements or lock line names an index, else None.

    Both spellings pip accepts are handled - `--flag URL` and `--flag=URL` - and
    the flag is matched as a whole token rather than a prefix, so a longer option
    that merely begins the same way is not mistaken for one.
    """
    token, _, rest = line.strip().partition(" ")
    flag, equals, joined = token.partition("=")
    if flag not in INDEX_FLAGS:
        return None
    return flag, ((joined if equals else rest).strip() or None)


def declares_itself_authored(lock: Path) -> int | None:
    """The 1-based line number of the authored marker in the lock, else None.

    The line number rather than a bool, because the skip notice has to cite where
    the declaration is. A check's silence only licenses a claim about the artefact
    it read, and the same holds for a check's speech: "this lock says it is
    hand-authored" is a claim about one line, and naming it is what lets a reader
    disagree with the run.
    """
    if not lock.is_file():
        return None
    for number, line in enumerate(
        lock.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        if line.strip() == AUTHORED_MARKER:
            return number
    return None


def declares_an_index(lock: Path) -> bool:
    """True when the lock names an index, making the flag unnecessary."""
    if not lock.is_file():
        return False
    return any(
        index_directive(line)
        for line in lock.read_text(encoding="utf-8", errors="replace").splitlines()
    )


def index_url(requirements: Path) -> str | None:
    """The index a store's requirements input names, for the fix message.

    `--extra-index-url` wins where an input names both, and the preference is
    load-bearing rather than tidy. A store publishing to a private feed alongside
    PyPI writes both lines, PyPI first. The package the failing command cannot
    find is on the private feed - PyPI is already the default and suggesting it
    is advice that changes nothing. Accepting either directive here without
    ranking them made the hint name PyPI, which is a worse message than the one
    this function replaced.
    """
    if not requirements.is_file():
        return None
    found = [
        directive
        for directive in map(
            index_directive, requirements.read_text(encoding="utf-8", errors="replace").splitlines()
        )
        if directive and directive[1]
    ]
    for flag in reversed(INDEX_FLAGS):
        for directive in found:
            if directive[0] == flag:
                return directive[1]
    return None


def shell_commands(text: str, is_markdown: bool) -> list[tuple[int, str]]:
    """Logical shell commands as (line number, command).

    Continuations are joined so a flag on a later line still counts, `#`
    comments are dropped, and in Markdown only fenced blocks are read.
    """
    commands: list[tuple[int, str]] = []
    in_fence = not is_markdown
    pending: list[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        if is_markdown and raw.lstrip().startswith("```"):
            in_fence = not in_fence
            pending = []
            continue
        if not in_fence:
            continue
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not pending:
            start = number
        if line.endswith("\\"):
            pending.append(line[:-1])
            continue
        pending.append(line)
        commands.append((start, " ".join(pending)))
        pending = []
    if pending:
        commands.append((start, " ".join(pending)))
    return commands


def lock_installs(root: Path, lock_name: str) -> list[tuple[str, int, str]]:
    """Every documented command that installs from the lock, offending or not.

    Separate from `offending_commands` so the passing message can say how many
    commands it read. "Every documented install from it passes the flag" over an
    empty list is an affirmative claim about nothing, and the ways this list
    empties are all invisible from the message: a documentation suffix absent from
    `DOC_SUFFIXES`, a `SKIP_DIRS` entry that grew to cover where the docs live, the
    lock renamed, or a spelling of the requirement flag `target` does not match.
    """
    target = re.compile(rf"-r\s+\S*{re.escape(lock_name)}\b")
    found: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix not in DOC_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if lock_name not in text:
            continue
        for number, command in shell_commands(text, path.suffix == ".md"):
            if target.search(command):
                found.append((str(path.relative_to(root)), number, command.strip()))
    return found


def offending_commands(installs: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
    """Of the documented installs from the lock, those that name no index."""
    return [entry for entry in installs if not any(flag in entry[2] for flag in INDEX_FLAGS)]


def _installs_line(lock_name: str, installs: int) -> str:
    """What the documented-command half can honestly claim about what it read."""
    if not installs:
        return (
            f"{lock_name} names no index, and no documented command installs from it -"
            f"\nnothing to check. This half read 0 such commands, so it says nothing about"
            f"\nwhether an install from {lock_name} would work as documented."
        )
    return (
        f"{lock_name} names no index; all {installs} documented install(s) "
        f"from it pass {INDEX_FLAG}"
    )


def _comparison_skipped(lock: Path, line: int) -> str:
    """What a skipped comparison has to say so a pass cannot read as a comparison.

    Every sentence here is load-bearing against one misreading. It cites the line,
    so the declaration can be checked rather than taken. It says 0 pins were
    compared, so the count in a green run is the skip's and not a resolution's. And
    it names what the marker costs, because a store that added it for one lock is
    the store that will inherit it on a lock that is compiled.
    """
    return (
        f"{lock.name} declares itself hand-authored at line {line}: `{AUTHORED_MARKER}`.\n"
        f"The pin comparison was SKIPPED, not passed - it compared 0 pins.\n\n"
        f"A lock written by hand is not the resolution of a requirements input, so there is"
        f"\nno input for this half to compare it against and nothing here says the versions"
        f"\n{lock.name} names are the ones the store intends. Only the documented-command"
        f"\nhalf below ran; its result stands on its own and covers no part of this one.\n\n"
        f"Remove the marker line as soon as {lock.name} starts being compiled from an input."
        f"\nWhile it stands, an input renamed out from under this half is not detected."
    )


def _nothing_to_compare(requirements: Path, lock: Path) -> str:
    """Why an empty comparison is refused rather than reported as a clean one."""
    if requirements.is_file():
        cause = f"{requirements.name} states 0 `==` pins"
        remedy = (
            f"Either the pin is not in {requirements.name} - a `-r` include is not followed, and"
            f"\nonly `name[extras]==version` is read - or it is spelled in a way this check does"
            f"\nnot match, in which case the parse is the defect and not the store. The"
            f"\ndocumented store layout keeps the exact `==` pin in {requirements.name}."
        )
    else:
        cause = f"there is no {requirements.name} beside it to compare against"
        remedy = (
            f"Commit the input {lock.name} was compiled from as {requirements.name}, or delete"
            f"\n{lock.name}: a lock is documented as the resolution of an input, and a store"
            f"\nthat installs the library directly keeps neither file."
        )
    return (
        f"{lock.name} was compared against nothing: {cause}.\n\n"
        f"An empty comparison and a clean one reach this line by the same path, so a pass"
        f"\nhere would be a true sentence about nothing and nothing downstream could tell"
        f"\nthe two apart. {lock.name} may well resolve everything it names; this half"
        f"\ncannot say so.\n\n{remedy}\n\n"
        f"If neither fits because {lock.name} is not compiled from anything - written and"
        f"\nmaintained by hand - it can declare that instead. Add a line reading exactly:\n\n"
        f"    {AUTHORED_MARKER}\n\n"
        f"and this half is skipped and reports that it compared nothing. Do not add it to a"
        f"\nlock that is compiled: it would hide an input renamed out from under this check."
    )


def _report_unresolved(requirements: Path, lock: Path) -> int:
    """Print any pin the lock does not deliver. Returns the exit code for that half.

    An empty result has two causes this cannot distinguish - every pin resolved, or
    no pin ever parsed - so nothing is claimed until at least one pin has been read.

    There is deliberately no setting that turns the refusal back into a pass. The
    routes to an empty parse are a pin moved behind a `-r` include, a spelling this
    module stops matching, and an input renamed; all three look identical from here,
    none of them is something a store would think to declare in advance, and a
    switch that silences the refusal silences those too.

    What the lock may declare is not that, and the difference is the whole of #312.
    A hand-authored lock is a state of the artefact, in the artefact, that the store
    knows in advance and writes down - not a configured tolerance for whatever this
    parse happens to return. The three routes above stay refused for every lock that
    has not said it is written by hand, and the declaration is read from the lock
    rather than from the environment for the same reason: a setting travels with a
    run and this travels with the file it describes.

    The check is still the one being run: the marker is consulted only once the
    comparison has already come back empty, so a marked lock with pins to compare is
    compared, and a store cannot mark its way out of a disagreement.
    """
    pinned = pinned_versions(requirements)
    if not pinned:
        authored = declares_itself_authored(lock)
        if authored is not None:
            print(_comparison_skipped(lock, authored))
            return 0
        print(_nothing_to_compare(requirements, lock), file=sys.stderr)
        return 1
    unresolved = unresolved_pins(requirements, lock)
    if not unresolved:
        print(
            f"{lock.name} resolves every one of the {len(pinned)} pin(s) {requirements.name} states"
        )
        return 0
    print(
        f"{lock.name} does not deliver what {requirements.name} pins, and the lock is what"
        f"\n`pip install --require-hashes` reads - so a build would use the lock's version:\n",
        file=sys.stderr,
    )
    for name, pinned, locked in unresolved:
        got = locked if locked is not None else "not resolved at all"
        print(f"  {name}: pinned {pinned}, lock has {got}", file=sys.stderr)
    print(
        f"\nRecompile the lock from {requirements.name}. If it cannot resolve a pinned"
        f"\nversion, that version is not published and the pin should not have moved.",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    lock = config.LOCK_PATH
    if not lock.is_file():
        print(f"No lock file at {lock} - nothing to check")
        return 0

    # Both halves always run, and the exit code carries either. Stopping at the
    # first would hide the second from whoever fixes the first.
    resolution = _report_unresolved(config.REQUIREMENTS_PATH, lock)

    # One exit for the resolution result rather than one per passing branch. With a
    # `return resolution` in each, either could be severed on its own - and every
    # stage test reached only the first, because all of them use a lock that names
    # its index. The uncovered branch was the one a store takes when the lock carries
    # no index, which is the shape the older half of this check was written for.
    # Structure rather than coverage: one site to carry it, one site to break.
    if declares_an_index(lock):
        print(f"{lock.name} names its index - no documented command needs {INDEX_FLAG}")
    else:
        installs = lock_installs(config.ROOT, lock.name)
        offenders = offending_commands(installs)
        if offenders:
            print(
                f"{lock.name} names no index, so these documented commands fail as written:\n",
                file=sys.stderr,
            )
            for path, number, command in offenders:
                print(f"  {path}:{number}\n    {command}", file=sys.stderr)
            url = index_url(config.REQUIREMENTS_PATH)
            hint = f"{INDEX_FLAG} {url}" if url else f"{INDEX_FLAG} <your index>"
            print(
                f"\nAdd {hint} to each, or recompile the lock with"
                f"\n--emit-index-url --emit-build-options so it carries the index itself.",
                file=sys.stderr,
            )
            return 1
        print(_installs_line(lock.name, len(installs)))
    return resolution


if __name__ == "__main__":
    raise SystemExit(main())
