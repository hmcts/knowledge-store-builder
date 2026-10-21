"""Refuse to extract HCL or SQL with the interpreter that cannot parse it.

    knowledgestore check-grammars [--interpreter PATH]

graphify reads `.tf`, `.tfvars`, `.hcl` and `.sql` files as nothing when its
grammar package is absent: no error, no node, and a layer that merges cleanly
and is short. A green extraction is therefore not evidence the files were read,
so this check looks for the grammar before any extraction runs. `extract-ast`
calls the same check; a store that drives graphify itself runs this stage first.

## Which interpreter is probed

The quantity that matters is whether the interpreter that RUNS graphify can
import the grammar - not whether this library's own can, and not whether some
`graphify` on `PATH` can, since a machine may hold several and a store running
`python -m graphify` from its virtualenv means exactly that interpreter. Probed
most-specific first, and the source is printed so the output is auditable:

1. `--interpreter`
2. the repository's own `graphify-out/.graphify_python`
3. the store-level `graphify-out/.graphify_python`
4. `sys.executable`, the interpreter running `knowledgestore`

graphify records its interpreter in `.graphify_python`; the pointer is read, not
guessed. `extract-ast` imports graphify in-process, so it passes `sys.executable`
explicitly rather than relying on the fallback.

## What it scans

The content set graphify's detect scan computed - the same set `extract-ast`
reads - so graphify's exclusions apply and there is no second list to drift.
Extensions that do not occur mean no probe for their grammar.

**`--clones`** scans the clones instead: `repositories/<name>` for every
repository `config/repositories.txt` declares. A per-repository `graphify update`
route has no current content set - the file on disk comes from the previous
extraction, so a resync that adds a repository or a file type is checked against
a stale list and misses exactly the new files. The file list comes from graphify
itself: `graphify.extract.collect_files`, run inside the probed interpreter with
the clone as working directory, so it applies that graphify's own noise-directory,
symlink and `.graphifyignore` rules and the list is what that graphify will read.
When the interpreter cannot run it (no graphify, or an older one without it) the
check falls back to a walk that prunes `.git` and the pipeline's own output
directory and applies the repository's `.graphifyignore` with gitignore
semantics. That list is approximate, and the output says which method produced
each repository's list so a fallback cannot read as the exact check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import config, export_git_history, extract_ast

POINTER = ".graphify_python"

# grammar name -> (extensions graphify reads with it, the module it imports)
GRAMMARS: "dict[str, tuple[tuple[str, ...], str]]" = {
    "HCL": ((".tf", ".tfvars", ".hcl"), "tree_sitter_hcl"),
    "SQL": ((".sql",), "tree_sitter_sql"),
}

FIX = (
    "install the library's `ast` extra in that environment: "
    "pip install 'hmcts-knowledge-store-builder[ast]'"
)

Run = Callable[..., "subprocess.CompletedProcess"]


def has_module(interpreter: str, module: str, run: "Run | None" = None) -> bool:
    """Whether `interpreter` can import `module`. The probe is the interpreter's own import."""
    try:
        done = (run or subprocess.run)(
            [interpreter, "-c", f"import {module}"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def pointer(path: Path) -> "str | None":
    """The interpreter a `.graphify_python` file names, or None when absent or empty."""
    # Sonar S8707, on the grounds recorded in `build_community_summaries.merge`: a
    # maintainer CLI reading a pointer file from the operator's own store, with no
    # privilege boundary to cross.
    try:
        text = path.read_text(encoding="utf-8").strip()  # NOSONAR(S8707)
    except OSError:
        return None
    return text.splitlines()[0].strip() if text else None


def resolve_interpreter(
    repository: str, explicit: "str | None", graph_directory: Path, repositories: Path
) -> "tuple[str, str]":
    """The interpreter to probe for one repository, and which rule chose it."""
    if explicit:
        return explicit, "--interpreter"
    if repository:
        own = pointer(repositories / repository / "graphify-out" / POINTER)
        if own:
            return own, f"{repository}/graphify-out/{POINTER}"
    store = pointer(graph_directory / POINTER)
    if store:
        return store, f"store-level graphify-out/{POINTER}"
    return sys.executable, "sys.executable"


EXACT = "graphify detection (exact)"
APPROXIMATE = "library exclusions + .graphifyignore parse (approximate)"

# Run inside the probed interpreter with the clone as working directory, exactly as
# a per-repository `graphify update` scans it. The absolute path matters: handed
# a relative target, graphify's `.graphifyignore` matching does not apply. Prints the paths as JSON. Anything
# that stops it printing - no graphify, an older one with no `collect_files` - is
# the caller's cue to fall back, which is why it prints nothing on failure.
_COLLECT = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "from graphify.extract import collect_files\n"
    "print(json.dumps([str(p) for p in collect_files(Path.cwd())]))\n"
)


def graphify_file_list(interpreter: str, clone: Path, run: "Run | None" = None):
    """The paths graphify in `interpreter` would extract from `clone`, or None."""
    try:
        done = (run or subprocess.run)(
            [interpreter, "-c", _COLLECT],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            cwd=clone,
        )
        if done.returncode != 0:
            return None
        listed = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(listed, list) or not all(isinstance(item, str) for item in listed):
        return None
    return [clone / item for item in listed]


def _glob_regex(pattern: str) -> str:
    """A gitignore glob as a regular expression over a `/`-separated relative path."""
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            close = pattern.find("]", i + 2)
            if close == -1:
                out.append(re.escape(char))
            else:
                body = pattern[i + 1 : close]
                out.append("[" + ("^" if body.startswith("!") else "") + body.lstrip("!") + "]")
                i = close
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def ignore_rules(clone: Path) -> "list[tuple[re.Pattern, bool, bool]]":
    """(matcher, negated, directories only) for each line of the clone's `.graphifyignore`."""
    text = pointer_text(clone / ".graphifyignore")
    rules = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        line = line[1:] if negated else line
        directory_only = line.endswith("/")
        line = line.rstrip("/").lstrip("/") if line.startswith("/") else line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line or raw.lstrip("!").startswith("/")
        prefix = "" if anchored else "(?:.*/)?"
        rules.append((re.compile(f"^{prefix}{_glob_regex(line)}$"), negated, directory_only))
    return rules


def is_ignored(relative: str, is_directory: bool, rules) -> bool:
    """gitignore semantics: the last matching rule wins."""
    ignored = False
    for matcher, negated, directory_only in rules:
        if directory_only and not is_directory:
            continue
        if matcher.match(relative):
            ignored = not negated
    return ignored


def walked_file_list(clone: Path, artefacts_name: str) -> "list[Path]":
    """The approximate list: prune VCS and pipeline output, apply `.graphifyignore`."""
    rules = ignore_rules(clone)
    found: list[Path] = []
    for directory, subdirectories, filenames in os.walk(clone):
        here = Path(directory)
        relative_here = here.relative_to(clone)
        kept = []
        for sub in sorted(subdirectories):
            if sub in (".git", artefacts_name):
                continue
            if is_ignored((relative_here / sub).as_posix(), True, rules):
                continue
            kept.append(sub)
        subdirectories[:] = kept
        for name in sorted(filenames):
            if not is_ignored((relative_here / name).as_posix(), False, rules):
                found.append(here / name)
    return found


def pointer_text(path: Path) -> str:
    # Sonar S8707, on the grounds recorded in `build_community_summaries.merge`: a
    # maintainer CLI reading a file inside the operator's own clone.
    try:
        return path.read_text(encoding="utf-8")  # NOSONAR(S8707)
    except OSError:
        return ""


def clone_groups(
    explicit: "str | None", graph_directory: Path, run: "Run | None" = None
) -> "tuple[dict[str, list[Path]], dict[str, str]]":
    """Each declared repository's files and the method that listed them.

    Declared means named in `config/repositories.txt`, read with the library's own
    parser; a directory under `repositories/` that is not declared is not scanned,
    because it is not a repository this store extracts.
    """
    declared = export_git_history.read_repository_config(config.REPOSITORIES_CONFIG)
    groups: dict[str, list[Path]] = {}
    methods: dict[str, str] = {}
    for repository in declared:
        clone = config.REPOSITORIES_DIR / repository.name
        if not clone.is_dir():
            continue
        interpreter, _source = resolve_interpreter(
            repository.name, explicit, graph_directory, config.REPOSITORIES_DIR
        )
        listed = graphify_file_list(interpreter, clone, run)
        if listed is None:
            groups[repository.name] = walked_file_list(clone, graph_directory.name)
            methods[repository.name] = APPROXIMATE
        else:
            groups[repository.name] = listed
            methods[repository.name] = EXACT
    return groups, methods


def needing(files: "list[Path]") -> "dict[str, list[Path]]":
    """The files each grammar is needed for, omitting grammars nothing needs."""
    found: dict[str, list[Path]] = {}
    for grammar, (extensions, _module) in GRAMMARS.items():
        wanted = [path for path in files if path.suffix.lower() in extensions]
        if wanted:
            found[grammar] = wanted
    return found


def check(
    groups: "dict[str, list[Path]]",
    interpreter_for: "Callable[[str], tuple[str, str]]",
    run: "Run | None" = None,
    methods: "dict[str, str] | None" = None,
) -> "tuple[list[str], list[str]]":
    """(report lines, refusal lines). Refusal lines are empty when nothing is missing.

    A probe is made once per interpreter and grammar, however many repositories
    share it, and only for grammars some file in the group needs.
    """
    report: list[str] = []
    refusals: list[str] = []
    probed: dict[tuple[str, str], bool] = {}
    counts: dict[tuple[str, str, str, str], int] = {}
    for name, files in sorted(groups.items()):
        interpreter, source = interpreter_for(name)
        method = (methods or {}).get(name, "")
        for grammar, wanted in needing(files).items():
            key = (interpreter, grammar)
            if key not in probed:
                module = GRAMMARS[grammar][1]
                probed[key] = has_module(interpreter, module, run)
            counts[(interpreter, source, grammar, method)] = counts.get(
                (interpreter, source, grammar, method), 0
            ) + len(wanted)
    if not counts:
        used = sorted(set((methods or {}).values()))
        suffix = f" (file list: {'; '.join(used)})" if used else ""
        report.append(
            f"no .tf, .tfvars, .hcl or .sql files in the content set; nothing to probe.{suffix}"
        )
    for (interpreter, source, grammar, method), total in sorted(counts.items()):
        module = GRAMMARS[grammar][1]
        listed = f"; file list: {method}" if method else ""
        if probed[(interpreter, grammar)]:
            report.append(
                f"{grammar} grammar ({module}) present for {total:,} file(s); "
                f"probed {interpreter} ({source}){listed}"
            )
        else:
            refusals.append(
                f"{grammar} grammar ({module}) is missing for {total:,} file(s); probed "
                f"{interpreter} ({source}){listed}. graphify would read them as nothing, with no "
                f"error. Fix: {FIX}"
            )
    return report, refusals


def parse_args(argv: "list[str] | None") -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore check-grammars",
        description="Refuse when the interpreter that runs graphify lacks the HCL or SQL grammar.",
    )
    parser.add_argument(
        "--interpreter", help="the python that will run graphify (overrides the pointers)"
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--detect",
        type=Path,
        help="the extractor's content set (default: graphify-out/.graphify_detect.json)",
    )
    source.add_argument(
        "--files",
        type=Path,
        help="a newline-delimited path list to check instead of the content set",
    )
    source.add_argument(
        "--clones",
        action="store_true",
        help="scan the declared clones instead of a content set, which may be stale",
    )
    return parser.parse_args(argv)


def main(argv: "list[str] | None" = None, run: "Run | None" = None) -> int:
    arguments = parse_args(argv)
    graph_directory = config.GRAPH_PATH.parent
    methods: "dict[str, str] | None" = None
    if arguments.clones:
        try:
            groups, methods = clone_groups(arguments.interpreter, graph_directory, run)
        except (OSError, ValueError) as error:
            print(f"Cannot read the declared repositories: {error}", file=sys.stderr)
            return 1
    else:
        try:
            files, _source = extract_ast.resolve_input(arguments, graph_directory)
        except extract_ast.InputRefused as refusal:
            print(str(refusal), file=sys.stderr)
            return 1
        groups = extract_ast.by_repository(files, config.REPOSITORIES_DIR)
    report, refusals = check(
        groups,
        lambda name: resolve_interpreter(
            name, arguments.interpreter, graph_directory, config.REPOSITORIES_DIR
        ),
        run,
        methods,
    )
    for line in report:
        print(line)
    for line in refusals:
        print(line, file=sys.stderr)
    return 1 if refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
