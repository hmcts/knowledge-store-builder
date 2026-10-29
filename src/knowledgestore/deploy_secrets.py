"""Withhold credential-shaped configuration values before a deployment node holds them.

A deployment node publishes its configuration: each value, up to
`KSB_DEPLOY_VALUE_CHARS` characters, reaches the committed graph, the explorer
page and the MCP server's answers. A credential typed literally into an overlay
rather than mounted from a secret store would be published with it.

So every value passes the library's secret rules before it is written, on every
route: the built-in value shapes and key-name rules of `secret_mask`, then the
store's own `KSB_SENSITIVE_PATTERNS` rules of `sensitive`. A value that matches is
replaced whole by `[<rule> withheld]` - the placeholder convention `sensitive`
already derives from a rule name - and its key is listed in `config_withheld`.

Whole, not in place: a configuration value is one scalar, and a fragment of a
credential is still a fragment of it.

The key is read as well as the value. `secret_mask` reads a name before it decides
(`password`, `apiKey`), so a value is tested as the line `<last key segment>:
<value>`; a bare value would miss every credential whose only tell is its name.

**What the run may say.** Repository, file, key and rule - never the value, and
never a fragment of it. `check_evidence` reads a graph through `rule_for` too, and
prints node ids only.
"""

from __future__ import annotations

import re
from collections import Counter

from . import deploy_values, secret_mask, sensitive

# What `sensitive.placeholder` writes, so a value this module already withheld is
# not withheld a second time. It matters on the read side: `[key-name withheld]`
# under a `password` key would otherwise match the key-name rule again and
# `check-evidence` would report the stage's own output as a leak.
_WITHHELD = re.compile(r"\[[a-z0-9-]+(?: [a-z0-9-]+)* withheld\]")


# A value that is wholly a templating placeholder: what `deploy_values` writes for
# a reference it will not render, and the `{{ }}` and `${ }` forms of one it has
# not yet reached. It carries nothing secret, and withholding it would only lose
# the fact that the service takes this value at deploy time. Only the key-name rule
# is set aside for one: a value-shape rule that matches still withholds it.
PLACEHOLDER_VALUES = (
    re.compile(re.escape(deploy_values.PLACEHOLDER)),
    re.compile(r"\{\{.*?\}\}", re.DOTALL),
    re.compile(r"\$?\$\{[^}]*\}", re.DOTALL),
)


def is_placeholder(value: str) -> bool:
    """Whether the whole value is a templating placeholder."""
    stripped = value.strip()
    return any(pattern.fullmatch(stripped) for pattern in PLACEHOLDER_VALUES)


def rule_for(key: str, value: str) -> str:
    """The rule a config value falls foul of, or "" when it falls foul of none.

    The value is read as the line `<key>: <value>`, so the key-name rule fires for
    a literal under `...password`. Secret rules first, in rule-name order so two
    runs agree when a value trips several; the store's sensitive-data rules after.
    """
    if _WITHHELD.fullmatch(value):
        return ""
    leaf = key.rsplit(".", 1)[-1]
    tally: Counter[str] = Counter()
    secret_mask.mask(f"{leaf}: {value}", tally)
    if is_placeholder(value):
        tally.pop(secret_mask.KEY_NAME_RULE, None)
    hits = sorted(tally)
    return hits[0] if hits else sensitive.matched_rule(value)


def withhold(flat: dict[str, str]) -> tuple[deploy_values.Flattened, list[tuple[str, str]]]:
    """`flat` with every matching value replaced, and the (key, rule) of each.

    The result keeps `dropped`, which is how a node says its config is partial.
    Keys come back sorted, because the node lists them in that order.
    """
    out = deploy_values.Flattened()
    out.dropped = getattr(flat, "dropped", 0)
    found: list[tuple[str, str]] = []
    for key, value in flat.items():
        rule = rule_for(key, value)
        out[key] = sensitive.placeholder(rule) if rule else value
        if rule:
            found.append((key, rule))
    return out, sorted(found)


def deployment_values(graph: dict) -> list[tuple[str, str, str]]:
    """Every (node id, key, value) of configuration a deployment node publishes.

    Tolerant of a node, a `metadata` or a `config` that is not what this version
    writes: a committed graph may predate any release, and a gate that crashes on
    an older one gates nothing. Node order, then key order, so a report is stable.
    """
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return []
    found: list[tuple[str, str, str]] = []
    for node in nodes:
        metadata = node.get("metadata") if isinstance(node, dict) else None
        if not isinstance(metadata, dict) or metadata.get("kind") != "deployment":
            continue
        config = metadata.get("config")
        if not isinstance(config, dict):
            continue
        found.extend((str(node.get("id")), str(key), str(config[key])) for key in sorted(config))
    return found


def findings(graph: dict) -> list[tuple[str, str]]:
    """The (node id, rule) of every stored config value that falls foul of a rule.

    Never the key's value and never a fragment of it: this is what a CI gate
    prints, and a build log is read more widely and kept longer than the graph.
    """
    return [
        (node_id, rule)
        for node_id, key, value in deployment_values(graph)
        if (rule := rule_for(key, value))
    ]
