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
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import config, extract_ast

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
) -> "tuple[list[str], list[str]]":
    """(report lines, refusal lines). Refusal lines are empty when nothing is missing.

    A probe is made once per interpreter and grammar, however many repositories
    share it, and only for grammars some file in the group needs.
    """
    report: list[str] = []
    refusals: list[str] = []
    probed: dict[tuple[str, str], bool] = {}
    counts: dict[tuple[str, str, str], int] = {}
    for name, files in sorted(groups.items()):
        interpreter, source = interpreter_for(name)
        for grammar, wanted in needing(files).items():
            key = (interpreter, grammar)
            if key not in probed:
                module = GRAMMARS[grammar][1]
                probed[key] = has_module(interpreter, module, run)
            counts[(interpreter, source, grammar)] = counts.get(
                (interpreter, source, grammar), 0
            ) + len(wanted)
    if not counts:
        report.append("no .tf, .tfvars, .hcl or .sql files in the content set; nothing to probe.")
    for (interpreter, source, grammar), total in sorted(counts.items()):
        module = GRAMMARS[grammar][1]
        if probed[(interpreter, grammar)]:
            report.append(
                f"{grammar} grammar ({module}) present for {total:,} file(s); "
                f"probed {interpreter} ({source})"
            )
        else:
            refusals.append(
                f"{grammar} grammar ({module}) is missing for {total:,} file(s); probed "
                f"{interpreter} ({source}). graphify would read them as nothing, with no "
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
    parser.add_argument(
        "--detect",
        type=Path,
        help="the extractor's content set (default: graphify-out/.graphify_detect.json)",
    )
    parser.add_argument(
        "--files",
        type=Path,
        help="a newline-delimited path list to check instead of the content set",
    )
    return parser.parse_args(argv)


def main(argv: "list[str] | None" = None, run: "Run | None" = None) -> int:
    arguments = parse_args(argv)
    graph_directory = config.GRAPH_PATH.parent
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
    )
    for line in report:
        print(line)
    for line in refusals:
        print(line, file=sys.stderr)
    return 1 if refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
