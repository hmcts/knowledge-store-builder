"""One verdict over the sharded mapping legs, and what the run did not read.

GitHub reports each matrix leg as its own check, so a reader gets four results and
no answer. This is the one verdict over them - and the thing it has to get right is
not the failing case but the skipped one.

`mapping-decision` skips the legs when nothing has landed that could have moved an
observer set, and it says so honestly in its own summary. The defect was here: the
summary job then reported plain `success`, so a run in which four legs verified
every entry against a whole-suite run and a run in which nothing looked at the
table at all produced the same green check (#331). That is the rule in `CLAUDE.md`
turned on the workflow itself - **a check's silence only licenses a claim about the
artefact it read** - and the reader of a checks list has no way to tell which claim
they are being offered.

So there are three states here and not two, and the middle one is the point:

- **VERIFIED** - the legs ran and agreed. The only state that licenses "the table
  is checked as of this commit".
- **NOT LOOKED AT** - the legs did not run, correctly, and nothing in this run read
  the table. Still `success`, because a neutral check reads as broken and gets
  ignored, and because the skip *was* right. What it carries instead is the
  provenance: the commit the table was last actually verified at and when, so the
  run says what it did not read rather than implying it read it.
- **FAILED** - a leg failed, or the wiring did. Unchanged, and it already worked.

The verdict words are pairwise non-substrings, deliberately. A reader greps the
step output for one of them, and `"VERIFIED" in text` matching `"NOT VERIFIED"` is
exactly the wrong-quantity check this repository keeps catching: correct code
answering a neighbouring question. `test_the_three_verdicts_cannot_be_confused`
holds them apart, so the checks that assert one state is not the other cannot go
vacuous by a rewording.

The date and commit come from `mapping_trigger.last_verification`, not from a
second walk of the API: that function already reads the matrix legs rather than a
run's conclusion, because a run in which `mapping` was skipped is green and
verified nothing - and a summary built on the other definition would name this
kind of run as the last verification, which is the bug reporting itself as the fix.
"""

from __future__ import annotations

import os
import subprocess
import sys

import mapping_trigger as trigger

# The three states. Pairwise non-substrings: see the module docstring, and the
# check that holds them that way.
VERIFIED = "VERIFIED"
NOT_LOOKED_AT = "NOT LOOKED AT"
# Not the bare word: this prints to a log, and `FAILED` at the start of a line is
# what `unittest` writes when a suite fails. A reader or a scan grepping a combined
# log for one would find the other.
FAILED = "CHECK FAILED"

# What the decision job writes to its output, and what Actions writes for a matrix
# that never started. Compared as the strings they are, because that is what
# reaches this through the environment.
RAN = "true"
DID_NOT_RUN = "false"
SKIPPED = "skipped"
SUCCESS = "success"


def state(decision: str, result: str) -> str:
    """Which of the three states a run is in, from the decision and the legs.

    A skipped `mapping` means two different things and only one of them is a pass.
    Skipped because the decision said nothing could have invalidated a mapping is a
    correct skip; skipped for any other reason - the decision failed, or said run
    and no leg started - is the wiring failing silently, which is the shape a
    conditional 28-minute check invites.
    """
    if decision == RAN and result == SUCCESS:
        return VERIFIED
    if decision == DID_NOT_RUN and result == SKIPPED:
        return NOT_LOOKED_AT
    return FAILED


def wants_provenance(condition: str) -> bool:
    """Whether the verdict has to say when the table was last verified elsewhere.

    Only the skipped state does, and asking here keeps the API walk out of the two
    states that would print a misleading answer: in the verified state the last
    verification is *this* run, and in the failed state the reader is being sent to
    a leg's log, not to a date.
    """
    return condition == NOT_LOOKED_AT


def verified_lines() -> tuple[str, ...]:
    return (
        f"{VERIFIED}: every shard's entries agree with what the suite observed.",
        "The legs ran in this run and applied every entry in their slice against a",
        "whole-suite run, so the table's observer sets are checked as of this commit.",
    )


def not_looked_at_lines(verification: trigger.Verification | None) -> tuple[str, ...]:
    """The skipped verdict, which has to name what it did not read.

    The second sentence is the whole fix. Without it this state is `success` with
    no distinguishing content, which is what let a run that read nothing be taken
    as a run that verified the table.
    """
    return (
        f"{NOT_LOOKED_AT}: the mapping legs did not run, so nothing in this run read",
        "the table's observer sets. That was the right call - nothing landed that",
        "could move one, and the decision job's summary says what it compared",
        "against and what it found - but this check verifies no entry.",
        "",
        *last_known_good(verification),
        "",
        "An edited test or a source change since then can still have invalidated an",
        "entry, and the nightly run is what covers those.",
    )


def last_known_good(verification: trigger.Verification | None) -> tuple[str, ...]:
    """When the table was last actually verified, or that this cannot say.

    The absent case gets a sentence of its own rather than a blank: an unanswerable
    question rendered as a clean answer is the failure mode the whole mapping-
    trigger policy is built around, and a provenance line quietly missing its date
    is that failure mode wearing this fix's clothes.
    """
    if verification is None:
        return (
            "This run cannot say when the table was last verified: no run of this",
            "workflow on main has a successful set of mapping legs that it could read,",
            "or the API did not answer. Treat the table as unverified until a nightly",
            "or a dispatched run reports VERIFIED.",
        )
    return (
        f"The table was last verified at {verification.head_sha[:12]}, by the run of "
        f"{verification.ran_at}.",
    )


def failed_lines(decision: str, result: str) -> tuple[str, ...]:
    return (
        f"{FAILED}: the decision was '{decision}' and the shards reported '{result}'.",
        "Each shard verifies its own slice of the table against a whole-suite run per",
        "entry, so the entries in a leg that did not pass are entries whose observers",
        "nothing has checked. A decision of 'true' with no result at all is the legs",
        "never having started, which verifies nothing.",
    )


def report(
    condition: str, decision: str, result: str, verification: trigger.Verification | None
) -> tuple[str, ...]:
    """The verdict as the lines a reader sees."""
    if condition == VERIFIED:
        return verified_lines()
    if condition == NOT_LOOKED_AT:
        return not_looked_at_lines(verification)
    return failed_lines(decision, result)


def announce(lines: tuple[str, ...]) -> None:
    """Print the verdict, and put it where Actions shows it.

    `mapping_trigger`'s writer rather than a second one: two files that each decide
    what an Actions variable is called are two files that can disagree about it, and
    only one of them would be noticed.
    """
    text = "\n".join(lines)
    print(text)
    trigger._append("GITHUB_STEP_SUMMARY", f"{text}\n")  # noqa: SLF001


def main(argv: list[str] | None = None, *, runner=subprocess.run) -> int:
    decision = os.environ.get("DECISION", "")
    result = os.environ.get("RESULT", "")
    condition = state(decision, result)
    verification = (
        trigger.last_verification(os.environ.get("GITHUB_REPOSITORY", ""), runner=runner)
        if wants_provenance(condition)
        else None
    )
    announce(report(condition, decision, result, verification))
    return 1 if condition == FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
