"""Every documented install command must be runnable exactly as written.

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

Run: knowledgestore check-install-docs
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from . import config

INDEX_FLAG = "--extra-index-url"
DOC_SUFFIXES = {".md", ".yml", ".yaml", ".sh"}
SKIP_DIRS = {".git", ".venv", "node_modules", "repositories", "knowledge", "graphify-out"}


def declares_an_index(lock: Path) -> bool:
    """True when the lock names an index, making the flag unnecessary."""
    if not lock.is_file():
        return False
    return any(
        line.strip().startswith(INDEX_FLAG)
        for line in lock.read_text(encoding="utf-8", errors="replace").splitlines()
    )


def index_url(requirements: Path) -> str | None:
    """The index a store's requirements input names, for the fix message."""
    if not requirements.is_file():
        return None
    for line in requirements.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith(INDEX_FLAG):
            return line.split(maxsplit=1)[1].strip() if len(line.split()) > 1 else None
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


def offending_commands(root: Path, lock_name: str) -> list[tuple[str, int, str]]:
    """Documented commands installing from the lock without naming an index."""
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
            if target.search(command) and INDEX_FLAG not in command:
                found.append((str(path.relative_to(root)), number, command.strip()))
    return found


def main() -> int:
    lock = config.LOCK_PATH
    if not lock.is_file():
        print(f"No lock file at {lock} - nothing to check")
        return 0

    if declares_an_index(lock):
        print(f"{lock.name} names its index - no documented command needs {INDEX_FLAG}")
        return 0

    offenders = offending_commands(config.ROOT, lock.name)
    if not offenders:
        print(f"{lock.name} names no index; every documented install from it passes {INDEX_FLAG}")
        return 0

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


if __name__ == "__main__":
    raise SystemExit(main())
