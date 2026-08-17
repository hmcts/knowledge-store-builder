"""Assert that the suite fails when the product is broken.

A test suite reports two things with the same output: that the product works,
and that nothing is checking it. `0 errors` is printed either way. Every gap
found in this library over one week was of the second kind — the behaviour was
tested and the *call site* was not, or the default that ships was never
exercised — and every one was caught by hand, after the code was written.

This gate closes that by construction. Each entry below is a **real** defect
that was written, passed review, and was caught late. The value is entirely in
that: an invented mutation proves a test can fail, a real one proves the suite
would have stopped the thing that actually happened.

    python3 tests/mutation_gate.py          # all mutations
    python3 tests/mutation_gate.py --list   # names only

A surviving mutation is a failure of this gate, not a curiosity: it means the
behaviour it describes could be removed today and the suite would stay green.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "knowledgestore"


@dataclass(frozen=True)
class Mutation:
    """One real defect, and the escape it represents."""

    name: str
    module: str
    find: str
    replace: str
    escaped_as: str


# Ordered by the escape they represent rather than by module, because the
# categories are the finding: wiring never asserted, and shipped defaults never
# exercised. Both classes passed review repeatedly.
MUTATIONS = (
    Mutation(
        "graph-report check unwired",
        "status.py",
        "    _report_graph_report(arguments.verify_graph)",
        "    pass",
        "behaviour tested through the function; nothing drove `main()`",
    ),
    Mutation(
        "contentless check unwired",
        "status.py",
        "    _report_contentless(nodes)",
        "    pass",
        "same escape as above, three days later, in a different check",
    ),
    Mutation(
        "manifest scope statement unwired",
        "build_knowledge_context.py",
        "            *scope_statement(len(repository_dirs)),",
        "",
        "the statement was tested; that it reached the written file was not",
    ),
    Mutation(
        "precision floor defaults to off",
        "build_community_summaries.py",
        "DEFAULT_PRECISION = 0.2",
        "DEFAULT_PRECISION = 0.0",
        "every test passed the floor explicitly, so the shipped default was unheld",
    ),
    Mutation(
        "symlink exclusions ignored",
        "status.py",
        "        elif ignored(path):",
        "        elif False:",
        "a check that could not see its own mitigation, reported by an operator",
    ),
    Mutation(
        "distinct-target count dropped",
        "status.py",
        'found["targets"] = len(targets)',
        'found["targets"] = 0',
        "the test drove a stub, so it pinned the wording and not the computation",
    ),
    Mutation(
        "retained failure double-counted",
        "sync_repositories.py",
        "total = len({*entries, *(name for name, _ in failures)})",
        "total = len(entries) + len(failures)",
        "shipped in v0.11.5; found by an estate, not by this suite",
    ),
)


def run_suite() -> bool:
    """True when the suite passes. Run as a subprocess so imports are fresh."""
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "."],
        cwd=ROOT / "tests",
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def apply(mutation: Mutation) -> str:
    """Apply one mutation, returning the original text for restoration."""
    path = SRC / mutation.module
    original = path.read_text(encoding="utf-8")
    if mutation.find not in original:
        raise SystemExit(
            f"mutation '{mutation.name}' no longer applies: its target is absent from "
            f"{mutation.module}. Either the code moved - update the mutation - or the "
            "behaviour was removed, in which case decide deliberately rather than "
            "letting this gate quietly stop testing it."
        )
    path.write_text(original.replace(mutation.find, mutation.replace, 1), encoding="utf-8")
    return original


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the mutations and exit")
    arguments = parser.parse_args(argv)

    if arguments.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name:<38} {mutation.module:<32} {mutation.escaped_as}")
        return 0

    if not run_suite():
        print(
            "The suite is already failing, so nothing can be concluded about any "
            "mutation. Fix that first.",
            file=sys.stderr,
        )
        return 1

    survived = []
    for mutation in MUTATIONS:
        path = SRC / mutation.module
        original = apply(mutation)
        try:
            caught = not run_suite()
        finally:
            # Always, including on interrupt: a mutation left in place is a
            # corrupted working tree that reads as a real defect.
            path.write_text(original, encoding="utf-8")
        print(f"  {'caught ' if caught else 'SURVIVED'}  {mutation.name}")
        if not caught:
            survived.append(mutation)

    print(f"\n{len(MUTATIONS) - len(survived)} of {len(MUTATIONS)} mutations caught.")
    for mutation in survived:
        print(f"  SURVIVED: {mutation.name} - {mutation.escaped_as}", file=sys.stderr)
    if survived:
        print(
            "\nA surviving mutation means that behaviour could be removed today with the "
            "suite still green. It is not a curiosity.",
            file=sys.stderr,
        )
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
