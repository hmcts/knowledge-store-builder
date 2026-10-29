"""Gate a committed evidence artefact: does it still hold an identifier?

The `intent` stage redacts them as it mines, but that only helps the next
refresh. A store's artefact is already in version control and already embedded
in a published page, so there has to be a way to check what is *there* - and to
fail a build over it, which is why this is a stage of its own rather than a flag
on `status`. `status` never returns non-zero by design: drift and coverage gaps
are normal operating conditions there, the stage reports and humans decide. A
gate has the opposite contract, and the two cannot share an entry point.

**The matched value is never printed.** The finding names the ticket, the field
and the rule, which is enough to find the value in the artefact and enough to
decide what to do. Printing the text would copy it into a build log - wider read
and longer kept than the artefact - so a gate that did that would republish
exactly what it was called to protect.

A clean result is not a certificate. The rules match identifier shapes, not
personal data in general, and personal names are deliberately not detected - so a
redacted value can still describe an identifiable person's case with the
reference taken out (`sensitive.py` sets out the limits). Read a pass as "nothing
matched the rules", never as "this file holds no personal data".

**Deployment configuration is evidence too.** A graph named here, or the store's
own by default, is checked for config values on deployment nodes that match the
secret rules in force (`deploy_secrets.py`). The `deployments` stage withholds
what matched when it ran, so a finding here means a rule was added since, or the
graph predates the stage's masking. Node ids and counts are printed, never a key's
value.

Run: knowledgestore check-evidence [artefact.json.gz | graph.json.gz ...]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, deploy_secrets, io, sensitive


# How many nodes a graph finding names before the rest are only counted.
NODES_SHOWN = 20


def check_graph(path: Path, graph: dict) -> tuple[int, int]:
    """Report on a graph's deployment config; return (values checked, findings)."""
    checked = len(deploy_secrets.deployment_values(graph))
    found = deploy_secrets.findings(graph)
    if not found:
        print(f"{path.name}: {checked:,} deployment config values checked, none matches a rule")
        return checked, 0
    print(
        f"{path.name}: {len(found):,} of {checked:,} deployment config values match a secret rule:",
        file=sys.stderr,
    )
    by_node: dict[str, list[str]] = {}
    for node_id, rule in found:
        by_node.setdefault(node_id, []).append(rule)
    for node_id in sorted(by_node)[:NODES_SHOWN]:
        rules = ", ".join(sorted(set(by_node[node_id])))
        print(f"  {node_id}  {len(by_node[node_id])} value(s)  rule {rules}", file=sys.stderr)
    if len(by_node) > NODES_SHOWN:
        print(f"  ... and {len(by_node) - NODES_SHOWN} more node(s)", file=sys.stderr)
    return checked, len(found)


def check(path: Path) -> tuple[int, int]:
    """Report on one artefact; return (values checked, findings printed).

    A graph is told from a ticket artefact by its content, not its name: both are
    committed as `.json.gz` and a store may name either anything.
    """
    records = io.read_json_dict(path)
    if isinstance(records.get("nodes"), list):
        return check_graph(path, records)
    if not records:
        print(f"No ticket records or graph at {path} - nothing to check")
        return 0, 0
    checked = sum(1 for _ in sensitive.mined_values(records))
    found = sensitive.findings(records)
    if not found:
        print(f"{path.name}: {checked:,} mined values checked, none matches a redaction rule")
        return checked, 0
    # Findings go to stderr, with the advice that follows them, so a failing run
    # reads in order on one stream.
    print(
        f"{path.name}: {len(found):,} of {checked:,} mined values match a redaction rule:",
        file=sys.stderr,
    )
    for ticket, field, rule in found:
        print(f"  {ticket}  field {field}  rule {rule}", file=sys.stderr)
    return checked, len(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="ticket-description artefacts to check (default: this store's own)",
    )
    arguments = parser.parse_args(argv)
    paths = [Path(p) for p in arguments.paths] or [
        config.TICKET_DESCRIPTIONS_PATH,
        config.GRAPH_PATH.with_suffix(".json.gz"),
    ]

    findings = sum(check(path)[1] for path in paths)
    if not findings:
        return 0
    print(
        "\nThe matched text is deliberately not printed. Re-run"
        "\n`knowledgestore intent` (mined text) or `knowledgestore deployments`"
        "\n(deployment config) to rebuild the artefact with these withheld,"
        "\nthen `knowledgestore explorer` so the page stops embedding them."
        "\nA credential found in configuration should still be rotated at source.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
