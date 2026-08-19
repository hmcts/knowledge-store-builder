"""Gate a store on whether it still answers the questions it exists to answer.

Two store operators built this independently, neither knowing the other had
(#134). The library owns the runner; the estate owns the questions - a question
like "what is crime case readiness?" means nothing on another estate, so
generalising the questions makes every store fight the result while generalising
the runner gives every store the gate for free.

A gate, not a report. `status` never returns non-zero by design - drift and
coverage gaps are normal there and humans decide. This has the opposite
contract, so it is a stage of its own, the same split `check-evidence` makes.

**It drives the shipped scorer.** `assets/app.js` is the ranker every consumer
uses. An earlier attempt approximated it in Python with keyword overlap and had
to be discarded: at one shared term "what is the data retention policy?" routed
to tickets on the word "data"; at two, a genuine graph question collapsed to
nothing. Failures in opposite directions, so not a tuning problem - it was a
second implementation of routing, and this codebase has been bitten four times by
that shape. So the assertion runs in Node against the real page.

**Two positions in the pipeline, same assertions.** Reading only the published
page makes this a post-mortem tool wearing a gate's clothes: one estate's suite
reported 12/12 while a rebuild sat unexamined, because every check read the
*published* artefact and would have gone on reporting 12/12 however bad the
candidate was.

    knowledgestore check-answers                      # did we publish something broken?
    knowledgestore check-answers --candidate PATH     # are we about to?

The question file is `config/questions.txt`:

    how are addresses validated?  | brief
    which repositories use X?     | graph
    what changed in ABC-123?      | ticket
    <a question with no answer>   | abstain

Modes are answer *shapes*, not text - a harness pinning prose is red after every
refresh that legitimately reworded something, and one that is always red is one
nobody reads. Several modes separated by commas means any of them is acceptable.

Requires Node (the same requirement the explorer tests already carry).

Run: knowledgestore check-answers [--candidate PATH] [--questions PATH]
                                  [--baseline PATH] [--write-baseline] [--json]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import config

PACKAGE = "knowledgestore"
RUNNER = "answer_regression.mjs"


def runner_path() -> Path:
    """The shipped runner, beside app.js so the two cannot come from different versions."""
    return Path(str(resources.files(PACKAGE) / "assets" / RUNNER))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore check-answers",
        description="Assert this store still answers its declared questions.",
    )
    parser.add_argument(
        "--candidate",
        type=Path,
        help="a page built but not yet published; without it the published page is read, "
        "which can diagnose a bad publish but never prevent one",
    )
    parser.add_argument("--questions", type=Path, help="default: config/questions.txt")
    parser.add_argument("--baseline", type=Path, help="default: knowledge/answers/baseline.json")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="record the modes observed now; review the diff like any other change",
    )
    parser.add_argument("--json", action="store_true", help="also emit the full report as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)

    node = shutil.which("node")
    if not node:
        print(
            "Node is not on PATH, and the assertions run against the shipped page in Node "
            "deliberately: app.js is the ranker every consumer uses, and a Python "
            "approximation of it was measured and discarded (#134). Install Node and re-run.",
            file=sys.stderr,
        )
        return 2

    page = arguments.candidate or config.EXPLORER_PATH
    if not page.is_file():
        which = "candidate page" if arguments.candidate else "published page"
        print(
            f"No {which} at {page} - run `knowledgestore explorer` first, or pass "
            "--candidate to check one built elsewhere.",
            file=sys.stderr,
        )
        return 2

    questions = arguments.questions or config.QUESTIONS_PATH
    if not questions.is_file():
        print(
            f"No question set at {questions}. The estate owns its questions, because a "
            "question is estate-specific and a generic one asserts nothing worth gating. "
            "Create it as lines of `question | mode`, where mode is one of "
            "brief, dive, tickets, graph, ticket, abstain.",
            file=sys.stderr,
        )
        return 2

    baseline = arguments.baseline or (config.ROOT / "knowledge" / "answers" / "baseline.json")
    if arguments.write_baseline:
        baseline.parent.mkdir(parents=True, exist_ok=True)

    # Every argument is resolved to an absolute path and confirmed to exist before
    # it is passed, and the flags are literals - so the argument vector handed to
    # Node contains nothing that came through unchecked. This matters because the
    # caller may be an agent rather than a person: a path that does not resolve is
    # refused here rather than handed onward to be interpreted somewhere else.
    def settled(path: Path, must_exist: bool = True) -> str:
        resolved = path.expanduser().resolve()
        if must_exist and not resolved.is_file():
            raise ValueError(f"not a file: {resolved}")
        return str(resolved)

    try:
        command = [
            str(Path(node).resolve()),
            settled(runner_path()),
            "--page",
            settled(page),
            "--questions",
            settled(questions),
            "--baseline",
            settled(baseline, must_exist=False),
        ]
    except ValueError as error:
        print(f"Refusing to run: {error}", file=sys.stderr)
        return 2
    if arguments.write_baseline:
        command.append("--write-baseline")
    if arguments.json:
        command.append("--json")

    completed = subprocess.run(command, check=False, shell=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
