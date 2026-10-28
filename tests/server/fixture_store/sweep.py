"""One call per tool, with arguments the synthetic store answers (DESIGN.md).

The server tests send these over the wire, in-process and as a child process, so
every tool is exercised by every route. `EXPECT` maps each tool to the fields its
answer must carry, as literals derived by hand from DESIGN.md; a test checks each
field it names and nothing else, since the tool tests already pin whole answers.
"""

from __future__ import annotations

from typing import Any

from .build import HOSTILE

# (tool, arguments), in the order the server lists its tools.
CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("store_info", {}),
    ("find_repo", {"name": "payment-api"}),
    ("search", {"text": "RefundHandler"}),
    ("node", {"id": "payments-api::RefundHandler"}),
    ("neighbours", {"id": "payments-api::PaymentService", "depth": 1}),
    ("community", {"id": 0}),
    ("repo_answer", {"repo": "ledger-service"}),
    ("dependents", {"target": "ledger-service"}),
    ("dependencies", {"repo": "payments-api"}),
    ("tickets", {"repo": "payments-api"}),
    ("ticket", {"key": "EX-103"}),
    ("answers", {"query": "refund ledger"}),
    ("minimum_standard", {"repos": "payments-api"}),
)

NEAR_MISSES = [
    {"repository": "payments-api", "near_miss": True},
    {"repository": "payment-apis", "near_miss": True},
]


def _ids(items: list[dict]) -> list[str]:
    return [item["id"] for item in items]


# tool -> (what the field is, how to read it from the answer, the literal).
EXPECT: dict[str, list[tuple[str, Any, Any]]] = {
    "store_info": [
        ("store id (DESIGN.md 1)", lambda r: r["store"]["id"], "example-store"),
        ("flags (DESIGN.md 9.3)", lambda r: r["flags"], {"graph.source_urls": True, "intent.reach": True, "deployments.present": None}),
    ],
    "find_repo": [
        ("not in the estate (DESIGN.md 2)", lambda r: r["answer"], "not in the estate"),
        ("near misses, equal remainder first (DESIGN.md 2)", lambda r: r["near_misses"], NEAR_MISSES),
    ],
    "search": [
        ("one label matches (DESIGN.md 4)", lambda r: _ids(r["items"]), ["payments-api::RefundHandler"]),
        ("counted", lambda r: r["matches"], 1),
    ],
    "node": [
        ("the node (DESIGN.md 3.1)", lambda r: r["node"]["id"], "payments-api::RefundHandler"),
        ("pinned url (DESIGN.md 3.1)", lambda r: r["node"]["url"], "https://github.com/example-org/payments-api/blob/" + "9" * 40 + "/src/main/java/com/example/payments/RefundHandler.java#L9"),
    ],
    "neighbours": [
        ("depth-1 neighbours (DESIGN.md 3.2)", lambda r: [i["node"]["id"] for i in r["items"]], [
            "ledger-service::LedgerClient",
            "payments-api::PaymentRepository",
            "payments-api::PaymentsController",
            "payments-api::RefundHandler",
            "shared-lib-java::IdempotencyKey",
            "shared-lib-java::MoneyAmount",
        ]),
    ],
    "community": [
        ("label (DESIGN.md 3.3)", lambda r: r["community"]["label"], "Payments and the ledger"),
        ("members (DESIGN.md 3.3)", lambda r: r["total"], 20),
    ],
    "repo_answer": [
        ("in the estate", lambda r: r["repository"]["answer"], "in the estate"),
        ("both lists counted (DESIGN.md 5.1, 5.2)", lambda r: r["totals"], {"depends_on": 2, "depended_on_by": 2}),
    ],
    "dependents": [
        ("direct dependents (DESIGN.md 5.2)", lambda r: [i["repository"] for i in r["items"]], ["payments-api", "reporting-worker"]),
        ("counted", lambda r: r["total"], 2),
    ],
    "dependencies": [
        ("held edges and targets outside the estate (DESIGN.md 5.1, 5.4)", lambda r: r["totals"], {"depends_on": 3, "outside_estate": 2}),
    ],
    "tickets": [
        ("landed (DESIGN.md 6.1)", lambda r: sorted({i["ticket"] for i in r["landed"]}), ["EX-101", "EX-102", "EX-103"]),
        ("proposed apart (DESIGN.md 6.1)", lambda r: [(i["ticket"], i["branches"]) for i in r["proposed"]], [("EX-201", ["feature/ex-201-partial-refunds"])]),
    ],
    "ticket": [
        ("hostile text only as data (DESIGN.md 8)", lambda r: r["ticket"]["description"], [HOSTILE]),
    ],
    "answers": [
        ("best answer first (DESIGN.md 7)", lambda r: (r["items"][0]["layer"], r["items"][0]["id"]), ("faq", "how-refunds-reach-the-ledger")),
    ],
    "minimum_standard": [
        ("the name looked up (DESIGN.md 11)", lambda r: r["repositories"][0]["exact"], "payments-api"),
        ("the default store holds no such layer (DESIGN.md 9.4)", lambda r: r["searched"].endswith("; the minimum_standard layer is absent"), True),
    ],
}  # fmt: skip


# `store_info`'s `index`, by the store's index mode (DESIGN.md 4).
INDEX = {
    "gz": {"schema": "1.0", "fallback": None},
    "plain": {"schema": "1.0", "fallback": None},
    "none": {
        "schema": None,
        "fallback": "no index in the store (knowledge/index/store.sqlite.gz or store.sqlite)",
    },
}


def checks(tool: str, index: str = "gz") -> list[tuple[str, Any, Any]]:
    """`EXPECT[tool]`, and for `store_info` the index line of a store in that mode."""
    found = list(EXPECT[tool])
    if tool == "store_info":
        found.append(("which index answers (DESIGN.md 4)", lambda r: r["index"], INDEX[index]))
    return found
