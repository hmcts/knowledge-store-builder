"""Re-apply the store's declared exclusions to every clone's `.graphifyignore`.

    knowledgestore apply-ignores

Run it after `sync` and before anything that reads a clone through graphify's
ignore rules: the detect scan, `check-grammars` and `extract-ast`.

## Why a stage

`sync` ends in `git clean -fd -e graphify-out`, which deletes every untracked
file in a clone except that directory - every clone's `.graphifyignore`
included. An exclusion a store relies on is therefore gone after each sync,
and nothing failed when nobody put it back: the next extraction read the
vendored tree or the symlinked one as source, succeeded, and reported healthy
counts. Stores re-applied their exclusions with a script of their own, and a
step that lives in a script is a step a refresh can skip.

So the exclusions are declared once, in a file the store commits, and this
stage writes them back. `extract-ast` reads the same declaration and refuses
when a clone's `.graphifyignore` lacks a declared line, so skipping this stage
is a named failure rather than a quieter layer.

## The declaration: `config/graphify-ignore.txt`

One exclusion a line, in `.graphifyignore` syntax. A bare line applies to every
declared repository; `name | pattern` applies to one, with the same `|` the
repository list uses. `#` starts a comment line.

    # every clone
    node_modules/
    # one repository
    service-a | src/main/resources/static/vendor/

It is optional. A store that declares nothing gets no change from this stage
or from `extract-ast`.

## What it writes

Each missing line is appended to the clone's `.graphifyignore`, under a comment
naming this stage, and every line already there is kept - a repository may
commit an ignore file of its own, which `sync` restores and this must not
replace. A line already present is not written again, so running the stage
twice leaves the bytes the first run wrote.

It does not claim the declaration is right. Which paths a store excludes is a
decision about what the store is for; this makes the decision survive a sync.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from . import config, export_git_history, io, store_paths

HEADER = "# re-applied by `knowledgestore apply-ignores` from config/graphify-ignore.txt"
IGNORE_FILE = ".graphifyignore"


@dataclass(frozen=True)
class Exclusion:
    """One declared line: the repository it is for ("" for every one), and the pattern."""

    repository: str
    pattern: str


def read_declarations(path: Path) -> "list[Exclusion]":
    """The declared exclusions, in file order. Absent means nothing is declared."""
    if not path.is_file():
        return []
    declared: list[Exclusion] = []
    lines = path.read_text(encoding="utf-8").splitlines()  # NOSONAR(S8707)
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        repository, separator, pattern = line.partition("|")
        if not separator:
            repository, pattern = "", line
        repository, pattern = repository.strip(), pattern.strip()
        if separator and not (repository and pattern):
            raise ValueError(f"Invalid exclusion at {path}:{number}: {raw}")
        declared.append(Exclusion(repository, pattern))
    return declared


def patterns_for(declared: "list[Exclusion]", repository: str) -> "list[str]":
    """The patterns one clone must carry: every global line, then its own, once each."""
    ordered = [e.pattern for e in declared if not e.repository]
    ordered += [e.pattern for e in declared if e.repository == repository]
    return list(dict.fromkeys(ordered))


def _present(clone: Path) -> "list[str]":
    # Sonar S8707 on these reads, on the grounds recorded in
    # `build_community_summaries.merge`: a maintainer CLI reading the store's own
    # declaration and its own clones, with no privilege boundary to cross.
    ignore = clone / IGNORE_FILE
    if not ignore.is_file():
        return []
    return [
        line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()
    ]  # NOSONAR(S8707)


def absent(clone: Path, patterns: "list[str]") -> "list[str]":
    """The declared patterns the clone's `.graphifyignore` does not hold as a line."""
    present = set(_present(clone))
    return [pattern for pattern in patterns if pattern not in present]


def apply(clone: Path, patterns: "list[str]") -> "list[str]":
    """Append the missing patterns, keeping every line already there. Returns what it added."""
    missing = absent(clone, patterns)
    if not missing:
        return []
    ignore = clone / IGNORE_FILE
    text = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""  # NOSONAR(S8707)
    if text and not text.endswith("\n"):
        text += "\n"
    if HEADER not in _present(clone):
        text += HEADER + "\n"
    text += "".join(f"{pattern}\n" for pattern in missing)
    io.checked_write_target(ignore)
    ignore.write_text(text, encoding="utf-8")  # NOSONAR(S2083) - checked above
    return missing


def main() -> int:
    where = store_paths.relative(config.GRAPHIFY_IGNORE_PATH)
    try:
        declared = read_declarations(config.GRAPHIFY_IGNORE_PATH)
    except (OSError, ValueError) as error:
        print(f"Cannot read the declared exclusions: {error}", file=sys.stderr)
        return 1
    if not declared:
        print(f"nothing declared in {where}; no {IGNORE_FILE} was changed.")
        return 0
    if not config.REPOSITORIES_CONFIG.is_file():
        print(f"Repository configuration not found: {config.REPOSITORIES_CONFIG}", file=sys.stderr)
        return 1
    try:
        repositories = [
            r.name for r in export_git_history.read_repository_config(config.REPOSITORIES_CONFIG)
        ]
    except (OSError, ValueError) as error:
        print(f"Cannot read the declared repositories: {error}", file=sys.stderr)
        return 1

    # A per-repository line naming a repository the store does not declare applies to
    # nothing. Named, because a mistyped name is otherwise an exclusion nobody has.
    for name in sorted({e.repository for e in declared if e.repository} - set(repositories)):
        print(
            f"warning: {where} names {name}, which config/repositories.txt does not "
            "declare; its exclusions apply to nothing.",
            file=sys.stderr,
        )

    width = max((len(name) for name in repositories), default=1)
    changed = 0
    for name in repositories:
        clone = config.REPOSITORIES_DIR / name
        if not clone.is_dir():
            print(f"  {name:<{width}}  no clone, skipped (run `knowledgestore sync`)")
            continue
        added = apply(clone, patterns_for(declared, name))
        changed += bool(added)
        state = f"added {len(added)}" if added else "all present"
        print(f"  {name:<{width}}  {state}")
    print(f"\n{changed} of {len(repositories)} clone(s) changed, from {where}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
