"""Node kinds, and how to recognise them across format changes.

A node's `metadata.kind` says what it *is* — a feature, a scenario, a ticket —
and `metadata.format` records which parser produced it. Kinds are deliberately
format-agnostic: a feature parsed from Gherkin and one parsed from some other
specification language are both `feature`, and consumers should not care.

Earlier stores wrote format-specific kinds (`gherkin_feature`, `jira_ticket`).
Those are still accepted on read, so a store built before this change keeps
working until its next `gherkin` run rewrites the nodes. Write the current
kind; read either.
"""

from __future__ import annotations

FEATURE = "feature"
SCENARIO = "scenario"
TICKET = "ticket"

# kind as written now -> every kind that has ever meant it
_ALIASES: dict[str, frozenset[str]] = {
    FEATURE: frozenset({FEATURE, "gherkin_feature"}),
    SCENARIO: frozenset({SCENARIO, "gherkin_scenario"}),
    TICKET: frozenset({TICKET, "jira_ticket"}),
}


def node_kind(node: dict) -> str:
    """The current-form kind of a node, or "" when it carries none."""
    raw = (node.get("metadata") or {}).get("kind", "")
    for current, aliases in _ALIASES.items():
        if raw in aliases:
            return current
    return raw


def is_kind(node: dict, kind: str) -> bool:
    """True when `node` is of `kind`, whichever alias it was written with."""
    return node_kind(node) == kind
