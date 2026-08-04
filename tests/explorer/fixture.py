"""Build an explorer page from a synthetic estate, for page-regression.mjs.

Writes a small but complete knowledge store to .fixture-store/ - a graph with
duplicated components, a Gherkin feature, tickets, a community summary, a
semantic neighbour and a topic brief - then runs the explorer stage over it.

    python3 tests/explorer/fixture.py
    node tests/explorer/page-regression.mjs
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from knowledgestore import config, io  # noqa: E402

STORE = ROOT / ".fixture-store"


def node(nid, label, repo, source_file, community, kind=None, file_type="code"):
    return {
        "id": nid,
        "label": label,
        "repo": repo,
        "source_file": source_file,
        "community": community,
        "file_type": file_type,
        "metadata": {"kind": kind} if kind else {},
    }


GRAPH = {
    "nodes": [
        # the same pipe implemented independently in two applications
        node("app-a::pipe", "AddressPipe", "demo-app-a", "src/pipes/address.pipe.ts", 1),
        node("app-b::pipe", "AddressPipe", "demo-app-b", "src/pipes/address.pipe.ts", 2),
        node(
            "app-a::form",
            "AddressFormComponent",
            "demo-app-a",
            "src/address/address-form.component.ts",
            1,
        ),
        node(
            "app-b::form",
            "AddressEntryComponent",
            "demo-app-b",
            "src/address/address-entry.component.ts",
            2,
        ),
        # a service both applications call
        node("core::pay", "PaymentService", "demo-core", "src/payment.service.ts", 3),
        node(
            "app-a::checkout",
            "CheckoutContainer",
            "demo-app-a",
            "src/checkout/checkout.container.ts",
            3,
        ),
        node("app-b::checkout", "PayContainer", "demo-app-b", "src/pay/pay.container.ts", 3),
        # business layer
        node(
            "e2e::feature",
            "Pay a fine online",
            "demo-e2e",
            "features/payment.feature",
            4,
            kind="gherkin_feature",
            file_type="concept",
        ),
        node(
            "e2e::scenario",
            "Card payment succeeds",
            "demo-e2e",
            "features/payment.feature",
            4,
            kind="gherkin_scenario",
            file_type="concept",
        ),
        node("jira::DEMO-1", "DEMO-1", "", None, 4, kind="jira_ticket", file_type="concept"),
    ],
    "links": [
        {"source": "app-a::pipe", "target": "app-a::form"},
        {"source": "app-b::pipe", "target": "app-b::form"},
        {"source": "core::pay", "target": "app-a::checkout"},
        {"source": "core::pay", "target": "app-b::checkout"},
        {"source": "e2e::feature", "target": "e2e::scenario"},
        {"source": "e2e::feature", "target": "core::pay"},
        {"source": "jira::DEMO-1", "target": "e2e::feature"},
        {"source": "jira::DEMO-1", "target": "app-a::pipe"},
    ],
}

LABELS = {
    "1": "Address handling (app A)",
    "2": "Address handling (app B)",
    "3": "Payments",
    "4": "Business Features: Payments",
}

SUMMARIES = {
    "3": (
        "The payment path in demo-core and both applications: PaymentService is "
        "called from the checkout containers in demo-app-a and demo-app-b, so a "
        "change to it reaches both user-facing flows."
    ),
}

# file -> tickets, as build_intent_index would emit
INTENT = {
    "demo-app-a": {
        "src/pipes/address.pipe.ts": {
            "tickets": {"DEMO-1": 3},
            "first": "2024-01-05",
            "last": "2024-02-01",
        },
    },
    "demo-core": {
        "src/payment.service.ts": {
            "tickets": {"DEMO-2": 2},
            "first": "2024-03-02",
            "last": "2024-03-09",
        },
    },
}
# The three evidence fields of ticket-descriptions.json.gz. DEMO-2 carries
# vocabulary that exists nowhere else in this estate - one word only in a
# subject, one only in a body - so a question using either can only be
# answered from ticket evidence. Its body also holds markup and an ampersand,
# and spans several lines, because that is what real bodies do.
TICKET_DESCRIPTIONS = {
    "DEMO-1": {
        "d": ["Add address formatting to the payment confirmation screen"],
        "s": [
            "Add address formatting to the payment confirmation screen",
            "wip",
        ],
        "first": "2024-01-05",
        "last": "2024-02-01",
        "repos": ["demo-app-a", "demo-e2e"],
        "n": 3,
    },
    "DEMO-2": {
        # a quote, an apostrophe and an event handler, because a description
        # also lands in a title attribute: an unescaped quote would close the
        # attribute and what follows would be a live handler
        "d": ['Charge the basket\'s card once, not per "line item" onmouseover=alert(1)'],
        "s": [
            'Charge the basket\'s card once, not per "line item" onmouseover=alert(1)',
            "Log the settlement reference",
        ],
        "b": [
            "BREAKING CHANGE: the postalCode field replaces postcode in the "
            "confirmation payload.\n"
            "- callers must send postalCode\n"
            "- see ADR 0007 <script>alert(1)</script> & the migration plan"
        ],
        "first": "2024-03-02",
        "last": "2024-03-09",
        "repos": ["demo-core"],
        "n": 2,
    },
}
SYNONYMS = {"payment": [["fine", 0.71], ["card", 0.63]]}

BRIEF = """# Addresses in the demo estate

**Headline verdict:** each application formats addresses with its own copy of
`AddressPipe`; there is no shared implementation.

## How it works

### 1. Two independent pipes

`demo-app-a` defines `AddressPipe` in `src/pipes/address.pipe.ts`, and
`demo-app-b` defines a same-named pipe at the same relative path. No edge
connects them, so they are independent implementations.

### 2. Entry components differ

The form components differ in name and location: `AddressFormComponent` in
`demo-app-a` and `AddressEntryComponent` in `demo-app-b`.

## Where it lives

| Repository | Role |
|---|---|
| `demo-app-a` | Own pipe and address form |
| `demo-app-b` | Own pipe and address entry component |

## What this is NOT

- Not a shared component: nothing in the graph connects the two pipes.

**Sources:** the synthetic fixture estate used by the library's own tests.
"""

# A stamp-bearing deep dive for demo-core, so a question naming the
# repository serves this dossier (build_deep_dives.merge() below validates
# it against a bundle recording the same short sha, same as production).
DIVE_SHA = "abcd1234" + "0" * 32
DIVE = (
    "# Deep dive: demo-core\n\n"
    "**Headline verdict:** demo-core's PaymentService is the single shared "
    f"payment implementation both applications call. Measured at `{DIVE_SHA[:8]}`.\n\n"
    + "Evidence paragraph about payment coupling and churn. " * 40
    + "\n\n**Sources:** the synthetic fixture estate used by the library's own tests.\n"
)


def main() -> int:
    if STORE.exists():
        shutil.rmtree(STORE)
    (STORE / "graphify-out").mkdir(parents=True)
    (STORE / "docs" / "topics").mkdir(parents=True)
    (STORE / "docs" / "deep-dives").mkdir(parents=True)
    (STORE / "knowledge" / "deep-dives").mkdir(parents=True)

    config.configure(
        root=STORE,
        EXPLORER_TITLE="Demo Estate Explorer",
        BRIEF_REQUEST_URL="https://example.invalid/issues/new",
        # A tracker URL is estate configuration that reaches the page and is
        # interpolated into an href. This one carries an ampersand and an
        # attempt to close the attribute and inject an event handler, so the
        # regression can prove the page escapes what it embeds.
        TICKET_BROWSE_URL='https://example.invalid/browse/?a=1&b="><img src=x onerror=alert(1)>&id=',
        MIN_ENTRY_DEGREE=1,
    )

    io.write_json(config.GRAPH_PATH, GRAPH)
    io.write_json(config.LABELS_PATH, LABELS)
    io.write_json(config.SUMMARIES_PATH, SUMMARIES)
    io.write_gzip_json(config.INTENT_INDEX_PATH, INTENT)
    io.write_gzip_json(config.TICKET_DESCRIPTIONS_PATH, TICKET_DESCRIPTIONS)
    io.write_gzip_json(config.SYNONYMS_PATH, SYNONYMS)
    (config.TOPICS_DOCS_DIR / "addresses.md").write_text(BRIEF, encoding="utf-8")
    (STORE / "config").mkdir(exist_ok=True)
    config.TOPICS_CONFIG_PATH.write_text(
        "addresses | Addresses in the demo estate | address, addresses, postcode\n",
        encoding="utf-8",
    )
    (config.DEEPDIVES_DOCS_DIR / "demo-core.md").write_text(DIVE, encoding="utf-8")
    io.write_json(
        config.DEEPDIVES_INPUT_DIR / "demo-core-input.json",
        {"repo": "demo-core", "provenance": {"sha": DIVE_SHA}},
    )

    # Stage modules snapshot config at import time, so import after configure().
    from knowledgestore import build_deep_dives, build_explorer, build_topic_briefs

    for module in (build_topic_briefs, build_deep_dives, build_explorer):
        _repoint(module)

    if build_topic_briefs.merge() != 0:
        print("fixture: topic brief did not validate", file=sys.stderr)
        return 1
    if build_deep_dives.merge() != 0:
        print("fixture: deep dive did not validate", file=sys.stderr)
        return 1
    build_explorer.TOPICS_PATH = config.TOPICS_BRIEFS_PATH
    build_explorer.DIVES_PATH = config.DEEPDIVES_PATH
    if build_explorer.main() != 0:
        return 1
    print(f"fixture store -> {STORE}")
    return 0


def _repoint(module) -> None:
    """Refresh a stage module's snapshot of the config paths."""
    for name in dir(module):
        if name.isupper() and hasattr(config, name):
            setattr(module, name, getattr(config, name))
    # stage-specific aliases that do not share the config name
    shared_aliases = {
        "GRAPH_PATH": "GRAPH_PATH",
        "LABELS_PATH": "LABELS_PATH",
        "INTENT_PATH": "INTENT_INDEX_PATH",
        "TITLES_PATH": "TICKET_TITLES_PATH",
        "TICKET_DESC_PATH": "TICKET_DESCRIPTIONS_PATH",
        "DESCRIPTIONS_PATH": "TICKET_DESCRIPTIONS_PATH",
        "SUMMARIES_PATH": "SUMMARIES_PATH",
        "SYNONYMS_PATH": "SYNONYMS_PATH",
    }
    # module-specific aliases: the same attribute name (e.g. DOCS_DIR) means a
    # different config setting depending which stage module owns it.
    per_module_aliases = {
        "build_topic_briefs": {
            "TOPICS_CONFIG": "TOPICS_CONFIG_PATH",
            "TOPICS_INPUT": "TOPICS_INPUT_PATH",
            "BRIEFS_PATH": "TOPICS_BRIEFS_PATH",
            "DOCS_DIR": "TOPICS_DOCS_DIR",
        },
        "build_deep_dives": {
            "INPUT_DIR": "DEEPDIVES_INPUT_DIR",
            "DOCS_DIR": "DEEPDIVES_DOCS_DIR",
            "DIVES_PATH": "DEEPDIVES_PATH",
        },
        "build_explorer": {
            "OUTPUT": "EXPLORER_PATH",
        },
    }
    aliases = dict(shared_aliases)
    aliases.update(per_module_aliases.get(module.__name__.rsplit(".", 1)[-1], {}))
    for attr, setting in aliases.items():
        if hasattr(module, attr):
            setattr(module, attr, getattr(config, setting))


if __name__ == "__main__":
    raise SystemExit(main())
