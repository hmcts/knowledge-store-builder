"""The one verdict the required `tests` context reports, over the jobs that do the work.

The unit tests and the mutation gate are separate jobs so the gate's shards run in
parallel, and a matrix leg cannot be a required status check. This job is the
context the `main` ruleset requires, so it has to say pass or fail for both.

The failure it exists to prevent is a skip read as a pass. A job that `needs` a
failed one is skipped, and a required context reads a skipped check as satisfied;
and a gate that never started reports the same `skipped` as a gate that was
correctly not wanted. So the two are told apart by the event: the gate is wanted on
a pull request and a push, and only there is `skipped` a failure. Anything else -
`failure`, `cancelled`, an empty result - fails everywhere, and an event this module
does not recognise is treated as one that wants the gate, because the asymmetry runs
one way: a gate wrongly required is a red check somebody reads, and a gate wrongly
excused is a green one nobody does.

Written as a module rather than shell for the reason `mapping_summary.py` is: a
shell verdict once reported success over legs that never ran (#331), and a verdict
nothing in the suite can drive is one that can do it again.
"""

from __future__ import annotations

import os
import sys

SUCCESS = "success"
SKIPPED = "skipped"

# The events on which the `mutation-gate` job runs. The gate is not wanted on a
# schedule or a dispatch because the nightly `mapping` legs verify the table there.
GATE_EVENTS = frozenset({"pull_request", "push"})
GATE_NOT_WANTED_EVENTS = frozenset({"schedule", "workflow_dispatch"})


def problems(unit: str, gate: str, event: str) -> tuple[str, ...]:
    """What is wrong with this run, one line each; empty means the context passes."""
    found = []
    if unit != SUCCESS:
        found.append(f"the unit job reported '{unit}', not '{SUCCESS}'.")
    if gate == SKIPPED and event in GATE_NOT_WANTED_EVENTS:
        pass
    elif gate != SUCCESS:
        found.append(
            f"the mutation gate reported '{gate}' on a '{event}' run, which is not "
            f"'{SUCCESS}'; a skipped gate passes only on {sorted(GATE_NOT_WANTED_EVENTS)}."
        )
    return tuple(found)


def main() -> int:
    unit = os.environ.get("UNIT", "")
    gate = os.environ.get("GATE", "")
    event = os.environ.get("EVENT", "")
    found = problems(unit, gate, event)
    if found:
        print("TESTS FAILED:")
        for line in found:
            print(f"  {line}")
        return 1
    print(f"TESTS PASSED: unit '{unit}', mutation gate '{gate}', on a '{event}' run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
