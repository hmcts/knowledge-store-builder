"""The graph tools (spec, Tools): store_info, find_repo, search, node, neighbours and
community.

Every expected value is a literal from DESIGN.md, derived by hand from the fixture's
design. Each store is the synthetic store, built for real and opened through the
real index (or, with `index="none"`, through the real graph fallback); the only
stand-ins are the clock and the cache directory.
"""

import gc
import gzip
import json
import os
import sqlite3
import tempfile
import unittest
import weakref
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from fixture_store.build import GRAPH, INDEX_PLAIN, MANIFEST, build
from knowledgestore.server import graph, results, store, tools_graph
from knowledgestore.server.index import IndexHandle
from knowledgestore.server.tools_graph import ArgumentRefused

# DESIGN.md 9.2: at this instant the store is 4 whole days old.
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

# DESIGN.md 1: each repository's sha is one hex digit repeated forty times.
SHA_DIGIT = {
    "accounts-web": "1",
    "audit-service": "2",
    "chart-web": "3",
    "gateway-service": "4",
    "infra-modules": "5",
    "ledger-service": "6",
    "notifications-service": "7",
    "payment-apis": "8",
    "payments-api": "9",
    "reporting-worker": "a",
    "shared-lib-java": "b",
    "ui-components": "c",
}

PAY = "src/main/java/com/example/payments/"
LEDGER = "src/main/java/com/example/ledger/"
LIB = "src/main/java/com/example/lib/"


def node(repo: str, local: str, label: str, path: str, line: int, community: int, kind=""):
    """A node as every tool returns it, from DESIGN.md 3.1's literal fields."""
    return {
        "id": f"{repo}::{local}",
        "label": label,
        "repo": repo,
        "kind": kind,
        "file": path,
        "url": f"https://github.com/example-org/{repo}/blob/{SHA_DIGIT[repo] * 40}/{path}#L{line}",
        "community": community,
    }


REFUND_HANDLER = node(
    "payments-api", "RefundHandler", "RefundHandler", PAY + "RefundHandler.java", 9, 0
)
PAYMENT_SERVICE = node(
    "payments-api", "PaymentService", "PaymentService", PAY + "PaymentService.java", 14, 0
)
SAVED_CARD = node(
    "accounts-web",
    "scenario:pay-saved-card",
    "Pay with a saved card",
    "features/pay.feature",
    6,
    0,
    "scenario",
)
# DESIGN.md 3.4: the nodes the index normalises, as it stores them.
NEW_CARD = node(
    "accounts-web",
    "scenario:pay-new-card",
    "Pay with a new card",
    "features/pay.feature",
    12,
    0,
    "scenario",
)
REPLICAS = node("chart-web", "value:replicas", "3", "values.yaml", 4, 1)
FORM_FIELD = node(
    "ui-components",
    "FormFieldComponent",
    "FormField",
    "src/form-field/form-field.component.ts",
    4,
    1,
)
# DESIGN.md 3.5: the values the index converts, as it stores them.
NOTIFY = "https://github.com/example-org/notifications-service/blob/" + "7" * 40 + "/"
RETRY = {
    "id": "7",
    "label": "Delivery retry policy",
    "repo": "notifications-service",
    "kind": "",
    "file": "src/retry.py",
    "url": NOTIFY + "src/retry.py#L3",
    "community": 1,
}
SMS_TEMPLATE = {
    "id": "notifications-service::SmsTemplate",
    "label": "SmsTemplate",
    "repo": "notifications-service",
    "kind": "",
    "file": "src/templates/sms.py",
    "url": NOTIFY + "src/templates/sms.py#L2",
    "community": 1,
}
GLOSSARY = {
    "id": "glossary::payments",
    "label": "Payments glossary",
    "repo": "",
    "kind": "",
    "file": "knowledge/glossary/payments.md",
    "url": None,
    "community": None,
}
STRUCTURAL = {
    "id": "ledger-service::com.example.ledger",
    "label": None,
    "repo": "ledger-service",
    "kind": "",
    "file": None,
    "url": None,
    "community": None,
}  # fmt: skip

# DESIGN.md 3.2: PaymentService's neighbours at depth 1, by id.
DEPTH_1 = [
    {
        "depth": 1,
        "node": node("ledger-service", "LedgerClient", "LedgerClient", LEDGER + "LedgerClient.java", 7, 0),
        "via": [{"from": "payments-api::PaymentService", "relation": "calls", "direction": "out"}],
    },
    {
        "depth": 1,
        "node": node("payments-api", "PaymentRepository", "PaymentRepository", PAY + "PaymentRepository.java", 6, 0),
        "via": [{"from": "payments-api::PaymentService", "relation": "calls", "direction": "out"}],
    },
    {
        "depth": 1,
        "node": node("payments-api", "PaymentsController", "PaymentsController", PAY + "PaymentsController.java", 20, 0),
        "via": [{"from": "payments-api::PaymentService", "relation": "calls", "direction": "in"}],
    },
    {
        # The 2-cycle: reached both ways, listed once.
        "depth": 1,
        "node": REFUND_HANDLER,
        "via": [
            {"from": "payments-api::PaymentService", "relation": "calls", "direction": "in"},
            {"from": "payments-api::PaymentService", "relation": "calls", "direction": "out"},
        ],
    },
    {
        "depth": 1,
        "node": node("shared-lib-java", "IdempotencyKey", "IdempotencyKey", LIB + "IdempotencyKey.java", 5, 1),
        "via": [{"from": "payments-api::PaymentService", "relation": "imports", "direction": "out"}],
    },
    {
        "depth": 1,
        "node": node("shared-lib-java", "MoneyAmount", "MoneyAmount", LIB + "MoneyAmount.java", 8, 1),
        "via": [{"from": "payments-api::PaymentService", "relation": "imports", "direction": "out"}],
    },
]  # fmt: skip

# The second hop, derived by hand from DESIGN.md 3.2's edges and `build.LINKS`.
DEPTH_2 = [
    {
        "depth": 2,
        "node": node("gateway-service", "PaymentsProxy", "PaymentsProxy", "src/payments-proxy.ts", 10, 0),
        "via": [{"from": "payments-api::PaymentsController", "relation": "calls", "direction": "in"}],
    },
    {
        "depth": 2,
        "node": node("ledger-service", "JournalEntry", "JournalEntry", LEDGER + "JournalEntry.java", 5, 0),
        "via": [{"from": "ledger-service::LedgerClient", "relation": "imports", "direction": "out"}],
    },
    {
        "depth": 2,
        "node": STRUCTURAL,
        "via": [{"from": "ledger-service::LedgerClient", "relation": "contains", "direction": "in"}],
    },
    {
        "depth": 2,
        "node": node("payment-apis", "doc:refunds-runbook", "Refunds runbook", "docs/runbooks/refunds.md", 1, 0),
        "via": [{"from": "payments-api::RefundHandler", "relation": "references", "direction": "in"}],
    },
    {
        "depth": 2,
        "node": node("reporting-worker", "LedgerExtract", "LedgerExtract", "src/main/java/com/example/reporting/LedgerExtract.java", 22, 1),
        "via": [{"from": "ledger-service::LedgerClient", "relation": "calls", "direction": "in"}],
    },
    {
        "depth": 2,
        "node": node("shared-lib-java", "Currency", "Currency", LIB + "Currency.java", 3, 1),
        "via": [{"from": "shared-lib-java::MoneyAmount", "relation": "imports", "direction": "out"}],
    },
]  # fmt: skip

# DESIGN.md 3.3: community 0's 20 members, by id; the structural node is in none.
COMMUNITY_0_IDS = [
    "accounts-web::AccountPage",
    "accounts-web::PaymentForm",
    "accounts-web::feature:pay",
    "accounts-web::scenario:pay-new-card",
    "accounts-web::scenario:pay-saved-card",
    "gateway-service::AuthFilter",
    "gateway-service::GatewayRouter",
    "gateway-service::PaymentsProxy",
    "gateway-service::RateLimiter",
    "ledger-service::JournalEntry",
    "ledger-service::LedgerClient",
    "ledger-service::LedgerController",
    "ledger-service::LedgerRepository",
    "payment-apis::doc:refunds-runbook",
    "payment-apis::spec:payments",
    "payments-api::PaymentRepository",
    "payments-api::PaymentService",
    "payments-api::PaymentsController",
    "payments-api::RefundHandler",
    "payments-api::infra:main",
]

COMMUNITY_0 = {
    "id": 0,
    "label": "Payments and the ledger",
    "summary": (
        "Card payments enter through the gateway, are taken by payments-api and "
        "posted to the ledger as journal entries; refunds follow the same path back."
    ),
    # DESIGN.md 7.1: a summary records no date, so the store's `sources_as_of`.
    "summary_as_of": "2026-10-15T23:30:00-02:00",
}


def searched_in(base: str, *more: str) -> str:
    """The `searched` sentence for the layer that answered, and any more it read."""
    layers = " and ".join((base, *more))
    noun = "layers" if more else "layer"
    return f"searched 12 repositories in the {layers} {noun}"


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.cache = self.tmp / "cache"
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, index: str = "gz", edit=None, **options) -> store.Session:
        root = build(
            self.tmp / f"s-{index}-{len(list(self.tmp.iterdir()))}", index=index, **options
        )
        if edit is not None:
            path = root / MANIFEST
            document = json.loads(path.read_text(encoding="utf-8"))
            edit(document)
            path.write_text(json.dumps(document), encoding="utf-8")
        session = store.Session(lambda: store.open_store(root, self.cache), lambda: NOW)
        self.addCleanup(lambda: session.current()[0].close())
        return session


def both(case: ToolTestCase) -> tuple[store.Session, store.Session]:
    return case.session("gz"), case.session("none")


def modes(case: ToolTestCase) -> list[tuple[store.Session, str, str]]:
    """Each mode as (session, the envelope's `index`, the layer that answers)."""
    return [(case.session("gz"), INDEXED, "index"), (case.session("none"), FALLBACK, "graph")]


def envelope(index: str, layers_used: list[str]) -> dict:
    return {
        "store": {
            "id": "example-store",
            "format": {"major": 1, "minor": 0},
            "library_version": "0.0.0+fixture",
            "sources_as_of": "2026-10-15T23:30:00-02:00",
            "index": index,
            "layers_used": layers_used,
            "notice": None,
        }
    }


INDEXED = "present"
FALLBACK = "absent: graph read whole"

# DESIGN.md 1, as provenance records it.
REPOSITORIES = [
    {"repository": "accounts-web", "sha": "1" * 40, "committed": "2026-10-14T09:12:00+01:00"},
    {"repository": "audit-service", "sha": "2" * 40, "committed": "2026-10-13T16:40:00+01:00"},
    {"repository": "chart-web", "sha": "3" * 40, "committed": "2026-09-30T11:00:00+01:00"},
    {"repository": "gateway-service", "sha": "4" * 40, "committed": "2026-10-15T08:05:00+01:00"},
    {"repository": "infra-modules", "sha": "5" * 40, "committed": "2026-10-02T14:20:00+01:00"},
    {"repository": "ledger-service", "sha": "6" * 40, "committed": "2026-10-16T01:00:00+00:00"},
    {"repository": "notifications-service", "sha": "7" * 40, "committed": "2026-10-10T10:10:00+01:00"},
    {"repository": "payment-apis", "sha": "8" * 40, "committed": "2026-08-21T12:00:00+01:00"},
    {"repository": "payments-api", "sha": "9" * 40, "committed": "2026-10-15T17:45:00+01:00"},
    {"repository": "reporting-worker", "sha": "a" * 40, "committed": "2026-10-15T23:30:00-02:00"},
    {"repository": "shared-lib-java", "sha": "b" * 40, "committed": "2026-09-12T09:00:00+01:00"},
    {"repository": "ui-components", "sha": "c" * 40, "committed": "2026-10-07T15:30:00+01:00"},
]  # fmt: skip


def layers(index_present: bool) -> dict:
    """DESIGN.md 9.4: each layer's `present`, as the manifest records it."""
    return {
        "graph": {"present": True},
        "labels": {"present": True},
        "provenance": {"present": True},
        "repository_manifest": {"present": False},
        "context": {"present": False},
        "history": {"present": False},
        "intent": {"present": True},
        "ticket_descriptions": {"present": True},
        "ticket_tracker": {"present": False},
        "summaries": {"present": True},
        "topics": {"present": True},
        "deep_dives": {"present": True},
        "semantic": {"present": False},
        "explorer": {"present": False},
        "relationships": {"present": True},
        "minimum_standard": {"present": False},
        "index": {"present": index_present},
        "deployments": {"present": None},
    }


# A store digest of our own, so `served.digest` is a literal here.
DIGEST = "5e55105e55105e55"


def set_digest(document: dict) -> None:
    document["digest"] = DIGEST


class StoreInfoTest(ToolTestCase):
    def test_store_info_with_the_index(self):
        """Catches: a missing or wrong provenance row, `stale_days` not read from the
        injected clock (4 at 2026-10-20T12:00Z, DESIGN.md 9.2), a layer's state
        dropped or coerced, or a null flag reported as false (DESIGN.md 9.3)."""
        self.assertEqual(
            tools_graph.store_info(self.session(edit=set_digest)),
            {
                **envelope(INDEXED, ["provenance"]),
                "stale_days": 4,
                "index": {"schema": "1.0", "fallback": None},
                "items": REPOSITORIES,
                "total": 12,
                "page": 1,
                "next_page": None,
                "layers": layers(index_present=True),
                "extra_layers": {"faq": {"present": True, "kind": "answers"}},
                "unread": [],
                "flags": {
                    "graph.source_urls": True,
                    "intent.reach": True,
                    "deployments.present": None,
                },
                # The fixture writes the library's digests; its one present
                # directory layer is relationships.
                "digest_mismatch": [],
                "digest_missing": [],
                "digest_unchecked": ["relationships"],
                # A session built here is told no source: none is claimed.
                "store_source": None,
                "served": {
                    "store_id": "example-store",
                    "digest": DIGEST,
                    "sources_as_of": "2026-10-15T23:30:00-02:00",
                    "plugin_root": False,
                },
            },
        )

    def test_store_info_without_the_index_says_why_the_graph_answers(self):
        """Catches: a fallback store that looks indexed, or one that does not say why
        it fell back (spec, Start-up step 3: says so in store_info)."""
        self.assertEqual(
            tools_graph.store_info(self.session("none", edit=set_digest)),
            {
                **envelope(FALLBACK, ["provenance"]),
                "stale_days": 4,
                "index": {
                    "schema": None,
                    "fallback": (
                        "no index in the store (knowledge/index/store.sqlite.gz or store.sqlite)"
                    ),
                },
                "items": REPOSITORIES,
                "total": 12,
                "page": 1,
                "next_page": None,
                "layers": layers(index_present=False),
                "extra_layers": {"faq": {"present": True, "kind": "answers"}},
                "unread": [],
                "flags": {
                    "graph.source_urls": True,
                    "intent.reach": True,
                    "deployments.present": None,
                },
                # The fixture writes the library's digests; its one present
                # directory layer is relationships.
                "digest_mismatch": [],
                "digest_missing": [],
                "digest_unchecked": ["relationships"],
                # A session built here is told no source: none is claimed.
                "store_source": None,
                "served": {
                    "store_id": "example-store",
                    "digest": DIGEST,
                    "sources_as_of": "2026-10-15T23:30:00-02:00",
                    "plugin_root": False,
                },
            },
        )

    def test_an_unrecognised_index_is_stated_in_the_terms_of_every_unrecognised_layer(self):
        """Catches: an index of an unknown schema reported only as a fallback, so
        `store_info` lists the index layer as plainly present and a tool's envelope
        calls it absent, in other terms than an unrecognised relationships layer is
        stated in (spec, Absence semantics). The graph still answers."""
        root = build(self.tmp / "s", index="plain")
        with closing(sqlite3.connect(root / INDEX_PLAIN)) as db, db:
            db.execute("UPDATE meta SET value = '2.0' WHERE key = 'index_schema'")
        session = store.Session(lambda: store.open_store(root, self.cache), lambda: NOW)
        self.addCleanup(lambda: session.current()[0].close())
        reason = "index layer present but format unrecognised (found 2.0, supports 1.x); not read"
        info = tools_graph.store_info(session)
        self.assertEqual(info["index"], {"schema": None, "fallback": reason})
        self.assertEqual(info["layers"]["index"], {"present": True, "unrecognised": reason})
        unrecognised = "present but format unrecognised: graph read whole"
        self.assertEqual(info["store"]["index"], unrecognised)
        found = tools_graph.search(session, "RefundHandler")
        self.assertEqual(found["store"]["index"], unrecognised)
        self.assertEqual([item["id"] for item in found["items"]], ["payments-api::RefundHandler"])

    def test_thousands_of_repositories_page_under_the_cap(self):
        """Catches: `store_info` returning its repository list whole, so an estate of
        thousands outgrows the cap, or paging that drops, repeats or reorders one.
        The provenance file is replaced by 3,000 invented repositories, `repo-0000`
        to `repo-2999`, each at the sha of forty `0`s, written in reverse."""
        session = self.session()
        root = session.current()[0].root
        names = [f"repo-{n:04d}" for n in range(3000)]
        provenance = {
            name: {"sha": "0" * 40, "branch": "main", "committed": "2026-10-01T00:00:00+00:00"}
            for name in reversed(names)
        }
        (root / "knowledge" / "provenance.json").write_text(
            json.dumps({"repositories": provenance}), encoding="utf-8"
        )
        cap = 16 * 1024
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            answer = tools_graph.store_info(session, cursor, cap_bytes=cap)
            pages += 1
            self.assertEqual(answer["page"], pages)
            self.assertEqual(answer["total"], 3000)
            self.assertLessEqual(len(results.encode(answer)), cap)
            self.assertNotIn("oversize", answer)
            self.assertEqual(answer["store"]["id"], "example-store")
            seen.extend(row["repository"] for row in answer["items"])
            cursor = answer["next_page"]
            if cursor is None:
                break
        self.assertEqual(seen, names)
        self.assertGreater(pages, 1)

    def test_a_newer_minor_lists_its_layer_as_present_but_unread(self):
        """Catches: a layer a newer library added that store_info drops, or lists
        without saying this server cannot read it (spec, The one rule)."""
        info = tools_graph.store_info(
            self.session(
                manifest_minor=1, extra={"newer": {"paths": [], "present": True, "digest": None}}
            )
        )
        self.assertEqual(info["store"]["format"], {"major": 1, "minor": 1})
        self.assertEqual(info["unread"], ["newer"])
        self.assertEqual(info["layers"]["newer"], {"present": True})

    def test_flags_are_reported_as_recorded_not_coerced(self):
        """Catches: a recorded `false` flag turned into null, or a missing one into
        false: false means measured and absent, null means unknown."""

        def edit(document):
            document["layers"]["graph"]["source_urls"] = False
            del document["layers"]["intent"]["reach"]
            document["sources_as_of"] = None

        info = tools_graph.store_info(self.session(edit=edit))
        self.assertEqual(
            info["flags"],
            {"graph.source_urls": False, "intent.reach": None, "deployments.present": None},
        )
        self.assertIsNone(info["stale_days"])


class FindRepoTest(ToolTestCase):
    def test_the_three_answers_in_both_modes(self):
        """Catches: find_repo substituting a near miss for a name the estate does not
        hold, missing the case-only match, or calling an out-of-estate target
        unknown (DESIGN.md 2, 5.4) - with the index or without it."""
        for session, index, base in modes(self):
            with self.subTest(index=index):
                self.assertEqual(
                    tools_graph.find_repo(session, "payment-api"),
                    {
                        **envelope(index, [base, "relationships"]),
                        "answer": "not in the estate",
                        "exact": None,
                        "near_misses": [
                            {"repository": "payments-api", "near_miss": True},
                            {"repository": "payment-apis", "near_miss": True},
                        ],
                        "searched": searched_in(base, "relationships"),
                    },
                )
                self.assertEqual(
                    tools_graph.find_repo(session, "Ledger-Service"),
                    {
                        **envelope(index, [base]),
                        "answer": "in the estate",
                        "exact": "ledger-service",
                        "matched_case_insensitively": True,
                    },
                )
                self.assertEqual(
                    tools_graph.find_repo(session, "legacy-network"),
                    {
                        **envelope(index, [base, "relationships"]),
                        "answer": "referenced by the estate but not held",
                        "exact": None,
                        "target": "legacy-network",
                        "referenced_by": ["ledger-service", "payments-api"],
                        "searched": searched_in(base, "relationships"),
                    },
                )


def body(response: dict) -> dict:
    """A response without what may differ between the indexed and fallback modes: the
    envelope, the sentence naming the layer searched, and the store-bound cursor."""
    return {k: v for k, v in response.items() if k not in ("store", "searched", "next_page")}


class SearchTest(ToolTestCase):
    def test_a_label_is_found_with_its_pinned_url(self):
        """Catches: a search that misses the one node DESIGN.md 4 says matches, or
        returns it without the pinned url (spec: corpus paths as citations)."""
        for session, index, base in modes(self):
            with self.subTest(index=index):
                self.assertEqual(
                    tools_graph.search(session, "RefundHandler"),
                    {
                        **envelope(index, [base]),
                        "matches": 1,
                        "items": [REFUND_HANDLER],
                        "total": 1,
                        "page": 1,
                        "next_page": None,
                    },
                )

    def test_a_search_ignores_case_in_both_modes(self):
        """Catches: a case-sensitive fallback, where `refundhandler` would miss the
        node the index (whose tokenizer folds case) finds."""
        for session in both(self):
            self.assertEqual(
                tools_graph.search(session, "refundhandler")["items"], [REFUND_HANDLER]
            )

    def test_both_modes_rank_by_bm25_then_id(self):
        """Catches: ignoring the rank, reversing it, breaking a tie by anything but
        the id, or a fallback that orders differently from the index. The
        two-word labels outrank the three-word one and tie with each other, so the
        id decides: database before network."""
        app = node(
            "infra-modules",
            "module:app-service",
            "app-service module",
            "modules/app-service/main.tf",
            1,
            1,
        )
        database = node(
            "infra-modules", "module:database", "database module", "modules/database/main.tf", 1, 1
        )
        network = node(
            "infra-modules", "module:network", "network module", "modules/network/main.tf", 1, 1
        )
        for session in both(self):
            self.assertEqual(
                tools_graph.search(session, "module")["items"], [database, network, app]
            )

    def test_both_modes_match_word_prefixes_alike(self):
        """Catches: the index and the fallback disagreeing on what matches or in what
        order (ruling: word-prefix matching in both); a query read as a phrase, so
        `Pay` misses `PaymentForm`; words joined by OR rather than AND, so `pay
        card` gains every payment label; or punctuation not splitting words."""
        expected = {
            # Every label with a word beginning `pay`, derived from DESIGN.md 3.1.
            "Pay": {
                "accounts-web::PaymentForm",
                "accounts-web::feature:pay",
                "accounts-web::scenario:pay-new-card",
                "accounts-web::scenario:pay-saved-card",
                "gateway-service::PaymentsProxy",
                "glossary::payments",
                "payment-apis::spec:payments",
                "payments-api::PaymentRepository",
                "payments-api::PaymentService",
                "payments-api::PaymentsController",
                "payments-api::infra:main",
            },
            "pay card": {
                "accounts-web::scenario:pay-new-card",
                "accounts-web::scenario:pay-saved-card",
            },
            "payments-api": {"payment-apis::spec:payments", "payments-api::infra:main"},
            "Refund*": {"payment-apis::doc:refunds-runbook", "payments-api::RefundHandler"},
            "app, service!": {"infra-modules::module:app-service"},
        }
        indexed, fallback = both(self)
        for text, ids in expected.items():
            with self.subTest(text=text):
                with_index = tools_graph.search(indexed, text)["items"]
                without = tools_graph.search(fallback, text)["items"]
                self.assertEqual({item["id"] for item in with_index}, ids)
                self.assertEqual(with_index, without)

    def test_a_limit_cuts_the_ranking_and_says_how_many_matched(self):
        """Catches: a limit that silently hides matches (plan, No silent truncation):
        three labels match `module`, two are returned, and `matches` says three."""
        for session in both(self):
            response = tools_graph.search(session, "module", limit=2)
            self.assertEqual((response["matches"], response["total"]), (3, 2))
            self.assertEqual(len(response["items"]), 2)

    def test_a_kind_filter_keeps_only_that_kind(self):
        """Catches: the kind filter ignored, or applied to the wrong column, or the
        fallback missing a scenario written in the legacy form (DESIGN.md 3.1,
        3.4). The two scenarios tie on rank, so the id orders them."""
        for session in both(self):
            self.assertEqual(
                tools_graph.search(session, "Pay with", kind="scenario")["items"],
                [NEW_CARD, SAVED_CARD],
            )
            empty = tools_graph.search(session, "Pay with", kind="feature")
            self.assertEqual((empty["items"], empty["total"]), ([], 0))
            self.assertIn("searched", empty)

    def test_a_repo_filter_goes_through_the_three_way_answer(self):
        """Catches: an unknown repo filter returning a bare empty list instead of
        "not in the estate" with its near misses (spec, Unknown names), or a
        case-only spelling filtering out every hit."""
        for session, _, base in modes(self):
            with self.subTest(base=base):
                unknown = tools_graph.search(session, "module", repo="payment-api")
                self.assertEqual(
                    {k: unknown[k] for k in ("repository", "items", "total", "searched")},
                    {
                        "repository": {
                            "answer": "not in the estate",
                            "exact": None,
                            "near_misses": [
                                {"repository": "payments-api", "near_miss": True},
                                {"repository": "payment-apis", "near_miss": True},
                            ],
                        },
                        "items": [],
                        "total": 0,
                        "searched": searched_in(base, "relationships"),
                    },
                )
                held = tools_graph.search(session, "module", repo="legacy-network")
                self.assertEqual(
                    held["repository"]["answer"], "referenced by the estate but not held"
                )
                self.assertEqual(held["items"], [])

                known = tools_graph.search(session, "module", repo="payments-api")
                self.assertEqual(
                    {k: known[k] for k in ("repository", "items", "total", "searched")},
                    {
                        "repository": {
                            "answer": "in the estate",
                            "exact": "payments-api",
                            "matched_case_insensitively": False,
                        },
                        "items": [],
                        "total": 0,
                        "searched": searched_in(base),
                    },
                )

                folded = tools_graph.search(session, "module", repo="Infra-Modules")
                self.assertEqual(folded["repository"]["exact"], "infra-modules")
                self.assertTrue(folded["repository"]["matched_case_insensitively"])
                self.assertEqual(len(folded["items"]), 3)

    def test_fts_syntax_in_the_text_is_only_ever_words(self):
        """Catches: caller text reaching FTS5 as query syntax, where an unbalanced
        quote or `NEAR(` errors and `OR`, `NOT` or a column filter changes the
        query. Unquoted, each of these raises or matches; as quoted prefix words,
        each needs a label word that none has, so each matches nothing."""
        for session, _, base in modes(self):
            searched = searched_in(base)
            for text in (
                'Refund"Handler',
                "NEAR(",
                "NEAR(RefundHandler PaymentService)",
                "RefundHandler OR PaymentService",
                "RefundHandler NOT PaymentService",
                "RefundHandler AND",
                "label:RefundHandler",
                "{label}: RefundHandler",
            ):
                with self.subTest(text=text, searched=searched):
                    response = tools_graph.search(session, text)
                    self.assertEqual(
                        {k: response[k] for k in ("matches", "items", "total", "searched")},
                        {"matches": 0, "items": [], "total": 0, "searched": searched},
                    )

    def test_text_that_is_not_searchable_is_refused_with_the_reason(self):
        """Catches: text with no word matching every label (an empty AND), or a NUL
        or lone surrogate crashing the SQLite bind."""
        for session in both(self):
            for text in ("", "   ", '"', "*", "-", "Refund\x00Handler", "Refund\ud800"):
                with self.subTest(text=text), self.assertRaises(ArgumentRefused):
                    tools_graph.search(session, text)
            for limit in (0, -1):
                with self.subTest(limit=limit), self.assertRaises(ArgumentRefused):
                    tools_graph.search(session, "module", limit=limit)

    def test_a_large_result_pages_under_the_cap(self):
        """Catches: a search response over the cap, or pages that drop or repeat a
        hit. Twelve labels have a word beginning `p` (with "Payments glossary" and
        "Delivery retry policy", DESIGN.md 3.5)."""
        for session in both(self):
            whole = tools_graph.search(session, "p", limit=100)
            self.assertIsNone(whole["next_page"])
            pages = [tools_graph.search(session, "p", limit=100, cap_bytes=2048)]
            while pages[-1]["next_page"] is not None:
                cursor = pages[-1]["next_page"]
                pages.append(
                    tools_graph.search(session, "p", limit=100, cursor=cursor, cap_bytes=2048)
                )
            self.assertGreater(len(pages), 1)
            for page in pages:
                self.assertLessEqual(len(results.encode(page)), 2048)
            self.assertEqual([i for p in pages for i in p["items"]], whole["items"])
            self.assertEqual(len(whole["items"]), 12)


class DamagedIndexTest(ToolTestCase):
    @unittest.skipUnless(hasattr(sqlite3.Connection, "setconfig"), "needs Python 3.12+")
    def test_a_damaged_full_text_table_falls_back_to_the_graph_and_says_why(self):
        """Catches: a search that fails outright, or answers wrongly without saying
        so, when the index's FTS5 data is damaged (ruling 3). The index's other
        tables still answer `node`.

        The damage is an emptied shadow table, which SQLite 3.53 and 3.54 both
        report as corruption when it is searched. Zeroed blocks are not: 3.53 reads
        them as no matches, without an error."""
        session = self.session("plain")
        root = session.current()[0].root
        session.current()[0].close()
        with closing(sqlite3.connect(root / INDEX_PLAIN)) as db:
            # FTS5's shadow tables are guarded against writes; lift the guard to damage one.
            db.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, False)
            db.execute("DELETE FROM labels_data")
            db.commit()
        session = store.Session(lambda: store.open_store(root, self.cache), lambda: NOW)
        self.addCleanup(lambda: session.current()[0].close())

        response = tools_graph.search(session, "RefundHandler")
        self.assertEqual(response["items"], [REFUND_HANDLER])
        self.assertEqual(response["store"]["index"], "absent: graph read whole")
        self.assertEqual(response["store"]["layers_used"], ["graph"])
        self.assertTrue(response["fallback"].startswith("the index could not be searched ("))
        self.assertIn("fts5: corruption found", response["fallback"])
        self.assertTrue(response["fallback"].endswith("): graph read whole"))
        node_response = tools_graph.node(session, "payments-api::RefundHandler")
        self.assertEqual(node_response["store"]["index"], "present")


class NodeTest(ToolTestCase):
    def test_a_node_carries_its_url(self):
        """Catches: a node returned without its pinned url or its community."""
        for session, index, base in modes(self):
            with self.subTest(index=index):
                self.assertEqual(
                    tools_graph.node(session, "payments-api::RefundHandler"),
                    {**envelope(index, [base]), "node": REFUND_HANDLER},
                )

    def test_a_structural_node_is_tolerated(self):
        """Catches: failing on a node with no label, no source file and no url
        (DESIGN.md 3.1; library CLAUDE.md 6.4)."""
        for session in both(self):
            self.assertEqual(
                tools_graph.node(session, "ledger-service::com.example.ledger")["node"],
                STRUCTURAL,
            )

    def test_an_unknown_id_is_null_and_says_what_was_searched(self):
        """Catches: an unknown id raising, or answering with nothing said, or an id
        shaped like SQL reaching the query."""
        for session, _, base in modes(self):
            for missing in ("payments-api::Nope", "' OR 1=1 --"):
                response = tools_graph.node(session, missing)
                self.assertIsNone(response["node"])
                self.assertEqual(response["searched"], searched_in(base))


class NeighboursTest(ToolTestCase):
    def test_depth_one_lists_each_neighbour_once_with_how_it_was_reached(self):
        """Catches: a neighbour reached both ways (the 2-cycle) listed twice, an
        incoming edge missed, or a direction reversed (DESIGN.md 3.2)."""
        for session, index, base in modes(self):
            with self.subTest(index=index):
                self.assertEqual(
                    tools_graph.neighbours(session, "payments-api::PaymentService"),
                    {
                        **envelope(index, [base]),
                        "node": PAYMENT_SERVICE,
                        "items": DEPTH_1,
                        "total": 6,
                        "page": 1,
                        "next_page": None,
                    },
                )

    def test_depth_two_visits_the_cycle_once_and_never_the_start(self):
        """Catches: a walk that loops on the 2-cycle, or returns the start node as
        its own neighbour (DESIGN.md 3.2)."""
        for session in both(self):
            response = tools_graph.neighbours(session, "payments-api::PaymentService", depth=2)
            self.assertEqual(response["items"], DEPTH_1 + DEPTH_2)
            self.assertEqual(response["total"], 12)

    def test_a_relation_filter_follows_only_that_relation(self):
        """Catches: the relation filter applied to the first hop only, or ignored.
        Following `calls` alone, the second hop reaches PaymentsProxy and
        LedgerExtract; unfiltered, it would also reach JournalEntry (imports), the
        structural node (contains) and the runbook (references)."""
        for session in both(self):
            response = tools_graph.neighbours(
                session, "payments-api::PaymentService", depth=2, relation="calls"
            )
            self.assertEqual(
                [(i["depth"], i["node"]["id"]) for i in response["items"]],
                [
                    (1, "ledger-service::LedgerClient"),
                    (1, "payments-api::PaymentRepository"),
                    (1, "payments-api::PaymentsController"),
                    (1, "payments-api::RefundHandler"),
                    (2, "gateway-service::PaymentsProxy"),
                    (2, "reporting-worker::LedgerExtract"),
                ],
            )

    def test_a_node_reached_at_depth_one_is_not_listed_again_at_depth_two(self):
        """Catches: a walk that forgets the first hop, so a depth-1 node reached again
        from another depth-1 node is listed twice. From JournalEntry, the
        structural node is a depth-1 neighbour and also contains LedgerClient and
        LedgerController, the other two."""
        for session in both(self):
            response = tools_graph.neighbours(session, "ledger-service::JournalEntry", depth=2)
            self.assertEqual(
                [(i["depth"], i["node"]["id"]) for i in response["items"]],
                [
                    (1, "ledger-service::LedgerClient"),
                    (1, "ledger-service::LedgerController"),
                    (1, "ledger-service::com.example.ledger"),
                    (2, "ledger-service::LedgerRepository"),
                    (2, "payments-api::PaymentService"),
                    (2, "reporting-worker::LedgerExtract"),
                ],
            )

    def test_depth_above_two_is_refused_with_the_reason(self):
        """Catches: a walk deeper than the spec's `depth<=2`, or a refusal that does
        not say why."""
        session = self.session()
        for depth in (3, 0, True):
            with self.subTest(depth=depth), self.assertRaises(ArgumentRefused) as raised:
                tools_graph.neighbours(session, "payments-api::PaymentService", depth=depth)
            self.assertIn("depth", str(raised.exception))
            self.assertIn("1 or 2", str(raised.exception))

    def test_an_unknown_start_or_relation_says_what_was_searched(self):
        """Catches: a bare empty list for an id or relation the store does not hold."""
        for session in both(self):
            missing = tools_graph.neighbours(session, "payments-api::Nope")
            self.assertEqual((missing["node"], missing["items"]), (None, []))
            self.assertIn("searched", missing)
            other = tools_graph.neighbours(session, "payments-api::PaymentService", relation="x")
            self.assertEqual((other["node"], other["items"]), (PAYMENT_SERVICE, []))
            self.assertIn("searched", other)


class CommunityTest(ToolTestCase):
    def test_a_community_carries_its_label_summary_and_members(self):
        """Catches: a community without its summary, a member without its url, the
        structural node counted as a member, or a member of the other community
        (DESIGN.md 3.3: 20 members)."""
        for session, index, base in modes(self):
            with self.subTest(index=index):
                response = tools_graph.community(session, 0)
                self.assertEqual(
                    response["store"], envelope(index, [base, "labels", "summaries"])["store"]
                )
                self.assertEqual(response["community"], COMMUNITY_0)
                self.assertEqual([m["id"] for m in response["items"]], COMMUNITY_0_IDS)
                self.assertEqual(response["total"], 20)
                self.assertIn(REFUND_HANDLER, response["items"])

    def test_a_summary_is_dated_by_the_stores_sources_as_of_and_none_is_undated(self):
        """Catches: a written summary returned undated, so an assistant presents it
        as current; dated from anything but the manifest's `sources_as_of`; or a
        community with no summary given a date, as though written analysis were
        there (#19, DESIGN.md 7.1)."""

        def moved(document):
            document["sources_as_of"] = "2026-07-01T00:00:00+00:00"

        def unrecorded(document):
            document["sources_as_of"] = None

        def no_summaries(document):
            document["layers"]["summaries"]["present"] = False

        for edit, as_of, summarised in (
            (moved, "2026-07-01T00:00:00+00:00", True),
            (unrecorded, None, True),
            (no_summaries, None, False),
        ):
            with self.subTest(edit=edit.__name__):
                header = tools_graph.community(self.session(edit=edit), 0)["community"]
                self.assertEqual(header["summary_as_of"], as_of)
                self.assertIs(header["summary"] is not None, summarised)

    def test_an_unknown_community_is_null_and_says_what_was_searched(self):
        """Catches: inventing a community, or a bare empty list."""
        for session in both(self):
            response = tools_graph.community(session, 7)
            self.assertEqual((response["community"], response["items"]), (None, []))
            self.assertIn("searched", response)

    def test_members_page_under_the_cap_and_reassemble(self):
        """Catches: a community response over the cap, or pages that drop or repeat
        a member (DESIGN.md 3.3: community 1 has 24)."""
        session = self.session()
        pages = [tools_graph.community(session, 1, cap_bytes=4096)]
        while pages[-1]["next_page"] is not None:
            cursor = pages[-1]["next_page"]
            pages.append(tools_graph.community(session, 1, cursor=cursor, cap_bytes=4096))
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertLessEqual(len(results.encode(page)), 4096)
        members = [m["id"] for p in pages for m in p["items"]]
        self.assertEqual(len(members), 24)
        self.assertEqual(members, sorted(set(members)))


class NormalisedNodeTest(ToolTestCase):
    def test_both_modes_build_nodes_as_the_index_does(self):
        """Catches: a fallback that keeps a legacy kind as written, returns a
        numeric label as a number, or keeps the first- or last-listed copy of a
        repeated id rather than the one the index keeps (DESIGN.md 3.4; library
        `store_index._node_rows` and `_fill`)."""
        for session, index, _ in modes(self):
            with self.subTest(index=index):
                for expected in (NEW_CARD, REPLICAS, FORM_FIELD):
                    self.assertEqual(tools_graph.node(session, expected["id"])["node"], expected)
                self.assertEqual(tools_graph.search(session, "3")["items"], [REPLICAS])
                self.assertEqual(tools_graph.search(session, "FormField")["items"], [FORM_FIELD])
                members = tools_graph.community(session, 1)["items"]
                self.assertEqual([m for m in members if m["id"] == FORM_FIELD["id"]], [FORM_FIELD])

    def test_both_modes_convert_values_as_the_index_does(self):
        """Catches: a fallback that drops or mis-keys a node whose id is a number,
        keeps a community written as text as text (so it is in no community), or
        leaves a node with no repository out of the repositories the index lists
        (DESIGN.md 3.5; the index builder's staging affinity)."""
        for session, index, base in modes(self):
            with self.subTest(index=index):
                for expected in (RETRY, SMS_TEMPLATE, GLOSSARY):
                    self.assertEqual(tools_graph.node(session, expected["id"])["node"], expected)
                neighbours = tools_graph.neighbours(session, "notifications-service::EmailChannel")
                self.assertIn(
                    {
                        "depth": 1,
                        "node": RETRY,
                        "via": [
                            {
                                "from": "notifications-service::EmailChannel",
                                "relation": "references",
                                "direction": "out",
                            }
                        ],
                    },
                    neighbours["items"],
                )
                members = tools_graph.community(session, 1)["items"]
                self.assertIn(RETRY, members)
                self.assertIn(SMS_TEMPLATE, members)
                self.assertEqual(results.repository_names(session.current()[0]), sorted(SHA_DIGIT))
                self.assertEqual(tools_graph.search(session, "glossary")["items"], [GLOSSARY])

    def test_a_graph_the_index_builder_refuses_is_refused(self):
        """Catches: a fallback that answers from a graph the index builder cannot
        store - here a community that is a JSON object, which SQLite cannot bind -
        so that the store would answer differently once indexed; or a refusal that
        quotes store text."""
        live = {"id": "r::b", "repo": "r"}
        for name, refused_graph in {
            "community object": {"nodes": [{"id": "r::secret", "repo": "r", "community": {}}]},
            "id missing": {"nodes": [{"repo": "r"}]},
            "kind null": {"nodes": [{"id": "r::a", "metadata": {"kind": None}}]},
            "null end beside a live one": {
                "nodes": [live],
                "links": [{"source": None, "relation": "x", "target": "r::b"}],
            },
        }.items():
            with self.subTest(graph=name), self.assertRaises(graph.GraphRefused):
                graph.GraphReader(refused_graph)
        session = self.session("none")
        root = session.current()[0].root
        session.current()[0].close()
        refused = {"nodes": [{"id": "r::secret", "label": "Secret", "repo": "r", "community": {}}]}
        (root / GRAPH).write_bytes(gzip.compress(json.dumps(refused).encode("utf-8")))
        session = store.Session(lambda: store.open_store(root, self.cache), lambda: NOW)
        self.addCleanup(lambda: session.current()[0].close())
        for _ in range(2):
            with self.assertRaises(graph.GraphRefused) as raised:
                tools_graph.node(session, "r::secret")
            self.assertNotIn("secret", str(raised.exception).lower())

    def test_a_remembered_refusal_keeps_no_graph_alive(self):
        """Catches: remembering the caught `GraphRefused` itself, whose traceback and
        context hold the frames that parsed the graph, so a refused graph stays in
        memory for the store's lifetime - the whole graph, in the one mode that
        reads it whole. The refusal must still be remembered, not re-read."""

        class Parsed(dict):
            """The parsed graph, as a type a weak reference can watch."""

        loaded = self.session("none").current()[0]
        # A node with no id, which the index builder cannot store.
        held = [Parsed({"nodes": [{"repo": "r"}]})]
        watch = weakref.ref(held[0])
        with mock.patch.object(loaded, "read", return_value=held.pop()):
            with self.assertRaises(graph.GraphRefused):
                graph.read(loaded)
        gc.collect()
        self.assertIsNone(watch())
        # Remembered: the store's own graph, which is valid, is not read now.
        with self.assertRaises(graph.GraphRefused):
            graph.read(loaded)

    def test_repositories_are_listed_from_every_copy_of_a_node(self):
        """Catches: repositories listed from surviving nodes only, where the index's
        `repos` lists every staged copy's - so a repeated id's losing copy in
        another repository is still a repository (library `_fill`)."""
        reader = graph.GraphReader(
            {
                "nodes": [
                    {"id": "r::a", "label": "A", "repo": "r"},
                    {"id": "r::a", "label": "A", "repo": "s"},
                ]
            }
        )
        self.assertEqual(reader.repositories(), ["r", "s"])
        # The reader keeps the empty name on purpose, although the library's index no
        # longer lists it in `repos` (e4eab86): results.repository_names leaves ""
        # out above both readers, so the two modes list the same estate.
        corpus = graph.GraphReader(
            {"nodes": [{"id": "page", "label": "Page"}, {"id": "r::a", "repo": "r"}]}
        )
        self.assertEqual(corpus.repositories(), ["", "r"])
        self.assertEqual(
            reader.node("r::a"),
            {"id": "r::a", "label": "A", "repo": "r", "kind": "", "file": None, "url": None, "community": None},
        )  # fmt: skip

    def test_a_label_that_is_not_a_string_is_its_python_text(self):
        """Catches: a label handed to SQLite as it is, where the builder first takes
        its `str()` (`store_index._text`): SQLite would store `true` as "1" and
        refuse an object, where the index holds "True" and "{'a': 1}"."""
        reader = graph.GraphReader(
            {
                "nodes": [
                    {"id": "r::flag", "repo": "r", "label": True},
                    {"id": "r::object", "repo": "r", "label": {"a": 1}},
                ]
            }
        )
        labels = [(reader.node(i) or {}).get("label") for i in ("r::flag", "r::object")]
        self.assertEqual(labels, ["True", "{'a': 1}"])

    def test_edges_are_read_from_either_key(self):
        """Catches: a fallback that reads only `links`, so a graph whose edges sit
        under `edges` (graphify's extract files) has no neighbours, as the index
        builder would not (library `graph_files.iter_edges`)."""
        graph_reader = graph.GraphReader(
            {
                "nodes": [{"id": "r::a", "repo": "r"}, {"id": "r::b", "repo": "r"}],
                "edges": [{"source": "r::a", "target": "r::b"}],
            }
        )
        self.assertEqual(graph_reader.edges("r::a", None), [("out", "", "r::b")])

    def test_the_fallback_does_not_keep_the_parsed_graph(self):
        """Catches: the parsed graph JSON cached on the store beside the reader built
        from it, holding the graph in memory twice (review: one graph copy)."""
        session = self.session("none")
        loaded = session.current()[0]
        tools_graph.search(session, "Pay")
        self.assertNotIn("graph", loaded._layers)

    def test_closing_the_store_closes_the_in_memory_label_index(self):
        """Catches: the fallback's in-memory SQLite connection outliving its store, so
        each reload leaks one."""
        session = self.session("none")
        loaded = session.current()[0]
        tools_graph.search(session, "Pay")
        reader = graph.read(loaded)
        loaded.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            reader.search('"pay"*', None, None)


class RepositoryListTest(ToolTestCase):
    def test_the_empty_name_is_never_a_repository(self):
        """Catches: the empty name, which the graph holds for the glossary page that
        has no repository (DESIGN.md 1, 3.5), counted or listed as a repository in
        either mode, or looked up as one. The library's index stopped listing it at
        d94d33f, so an index built before that, which does, is checked too."""
        older = build(self.tmp / "older-index", index="plain")
        with closing(sqlite3.connect(older / INDEX_PLAIN)) as db, db:
            db.execute("INSERT INTO repos(name) VALUES ('')")
        older_session = store.Session(lambda: store.open_store(older, self.cache), lambda: NOW)
        self.addCleanup(lambda: older_session.current()[0].close())
        sessions = [(s, i) for s, i, _ in modes(self)] + [(older_session, "older index")]
        for session, index in sessions:
            with self.subTest(index=index):
                self.assertEqual(results.repository_names(session.current()[0]), sorted(SHA_DIGIT))
                for empty in ("", "   ", "\t"):
                    with self.assertRaises(ArgumentRefused):
                        tools_graph.find_repo(session, empty)
                    with self.assertRaises(ArgumentRefused):
                        tools_graph.search(session, "Pay", repo=empty)

    def test_a_repository_only_the_ticket_layer_names_is_listed_in_both_modes(self):
        """Catches: a fallback that lists only the graph's repositories, where the
        index also lists those its builder staged from the ticket layer - so
        `retired-batch`, named only in `file-tickets.json.gz`, would be "not in
        the estate" without the index and "in the estate" with it (DESIGN.md 6.4)."""
        for index, base in (("gz", "index"), ("none", "graph")):
            with self.subTest(index=index):
                session = self.session(index, ticket_only=True)
                self.assertEqual(
                    results.repository_names(session.current()[0]),
                    sorted([*SHA_DIGIT, "retired-batch"]),
                )
                self.assertEqual(
                    tools_graph.find_repo(session, "retired-batch"),
                    {
                        **envelope(INDEXED if index == "gz" else FALLBACK, [base]),
                        "answer": "in the estate",
                        "exact": "retired-batch",
                        "matched_case_insensitively": False,
                    },
                )
                missing = tools_graph.node(session, "retired-batch::nothing")
                self.assertEqual(
                    missing["searched"], f"searched 13 repositories in the {base} layer"
                )

    def test_a_repository_only_provenance_records_is_in_the_estate_with_no_nodes(self):
        """Catches: a cloned repository that provenance records, with no graph node,
        no ticket and no by-repo file, answered "not in the estate" because the
        index's `repos` does not list it; or answered in the estate without saying
        the graph holds nothing for it, so an empty search reads as a finding
        (DESIGN.md 1.1). The estate is then thirteen, in both modes."""
        for index, base in (("gz", "index"), ("none", "graph")):
            with self.subTest(index=index):
                session = self.session(index, provenance_only=True)
                self.assertEqual(
                    results.repository_names(session.current()[0]),
                    sorted([*SHA_DIGIT, "release-scripts"]),
                )
                self.assertEqual(
                    tools_graph.find_repo(session, "release-scripts"),
                    {
                        **envelope(INDEXED if index == "gz" else FALLBACK, [base, "provenance"]),
                        "answer": "in the estate",
                        "exact": "release-scripts",
                        "matched_case_insensitively": False,
                        "graph_nodes": 0,
                        "cloned_at": "d" * 40,
                        "note": "in the estate, but the graph holds no nodes for it",
                    },
                )
                # A repository the graph does hold says nothing of the kind.
                self.assertNotIn("graph_nodes", tools_graph.find_repo(session, "payment-apis"))
                missing = tools_graph.node(session, "release-scripts::nothing")
                self.assertEqual(
                    missing["searched"], f"searched 13 repositories in the {base} layer"
                )

    def test_ticket_repositories_are_collected_as_the_index_builder_stages_them(self):
        """Catches: a repository listed from the ticket layer that the builder would
        stage no row for - a path with no tickets, empty sections, or an entry that
        is not an object (library `store_index._ticket_rows`)."""
        layer = {
            "landed": {"a.py": {"tickets": {"EX-1": 1}}},
            "proposed": {"a.py": {"proposed": {"EX-2": 1}}},
            "dates-only": {"a.py": {"first": "2026-01-01", "last": "2026-01-02"}},
            "empty-sections": {"a.py": {"tickets": {}, "proposed": {}}},
            "list-section": {"a.py": {"tickets": ["EX-3"]}},
            "not-an-object": "x",
            "entry-not-an-object": {"a.py": ["EX-4"]},
        }
        self.assertEqual(graph.ticket_repositories(layer), {"landed", "proposed"})


class NullRepositoryTest(ToolTestCase):
    def test_a_node_whose_file_has_no_repository_is_still_found(self):
        """Catches: index queries that inner-join `repos`, so a node whose
        `files.repo` is NULL - as an index may hold for a node with no repository -
        vanishes from `node`, `search`, `community` and the neighbour walk with the
        index, while the fallback still returns it."""
        session = self.session("plain")
        root = session.current()[0].root
        session.current()[0].close()
        with closing(sqlite3.connect(root / INDEX_PLAIN)) as db:
            # Schema 1.0 declares files.repo NOT NULL, so the copy rebuilds `files`
            # without it, then clears RefundHandler's file's repository.
            db.executescript(
                "CREATE TABLE files_copy(k INTEGER PRIMARY KEY, repo INTEGER, path TEXT NOT NULL);"
                "INSERT INTO files_copy SELECT k, repo, path FROM files;"
                "DROP TABLE files;"
                "ALTER TABLE files_copy RENAME TO files;"
            )
            db.execute("UPDATE files SET repo = NULL WHERE path = ?", (REFUND_HANDLER["file"],))
            db.commit()
        indexed = store.Session(lambda: store.open_store(root, self.cache), lambda: NOW)
        self.addCleanup(lambda: indexed.current()[0].close())
        self.assertIsInstance(indexed.current()[0].index, IndexHandle)
        nameless = {**REFUND_HANDLER, "repo": ""}

        for session, expected in ((indexed, nameless), (self.session("none"), REFUND_HANDLER)):
            with self.subTest(expected_repo=expected["repo"]):
                self.assertEqual(tools_graph.node(session, REFUND_HANDLER["id"])["node"], expected)
                self.assertEqual(tools_graph.search(session, "RefundHandler")["items"], [expected])
                self.assertIn(expected, tools_graph.community(session, 0)["items"])
                walked = tools_graph.neighbours(session, "payments-api::PaymentService")["items"]
                self.assertIn(expected, [item["node"] for item in walked])


class BoundsTest(ToolTestCase):
    def test_search_text_is_bounded_in_bytes_and_words(self):
        """Catches: unbounded search text, each word of which is a prefix scan of the
        label index; or a bound counted in characters, not UTF-8 bytes, or off by
        one at either limit (ruling: 1 KiB, 32 words)."""
        for session in both(self):
            tools_graph.search(session, "a" * 1024)
            tools_graph.search(session, "\u00e9" * 512)
            tools_graph.search(session, " ".join(["pay"] * 32))
            for text in ("a" * 1025, "\u00e9" * 513, " ".join(["pay"] * 33)):
                with self.subTest(text=text[:12]), self.assertRaises(ArgumentRefused) as raised:
                    tools_graph.search(session, text)
                self.assertRegex(str(raised.exception), "1024 bytes|32 words")

    def test_a_community_id_must_fit_sqlite_integer(self):
        """Catches: an id outside int64 crashing the index's bind (`OverflowError`)
        while the fallback answers null - the two modes disagreeing."""
        for session in both(self):
            for outside in (2**63, -(2**63) - 1, 2**64):
                with self.subTest(id=outside), self.assertRaises(ArgumentRefused):
                    tools_graph.community(session, outside)
            for edge in (2**63 - 1, -(2**63)):
                self.assertIsNone(tools_graph.community(session, edge)["community"])


class BothModesTest(ToolTestCase):
    def test_every_tool_gives_the_same_items_with_or_without_the_index(self):
        """Catches: the fallback answering differently from the index in anything
        but the envelope and the layer it names (plan, Fallback without the index),
        or two calls on one store giving different bytes (plan, Deterministic)."""
        indexed, fallback = both(self)
        calls: list[tuple[str, Any]] = [
            ("find_repo payments-api", lambda s: tools_graph.find_repo(s, "payments-api")),
            ("find_repo payment-api", lambda s: tools_graph.find_repo(s, "payment-api")),
            ("search RefundHandler", lambda s: tools_graph.search(s, "RefundHandler")),
            ("search repo", lambda s: tools_graph.search(s, "Pay with", repo="accounts-web")),
            ("search none", lambda s: tools_graph.search(s, "nothing-matches")),
            ("node", lambda s: tools_graph.node(s, "payments-api::RefundHandler")),
            (
                "node structural",
                lambda s: tools_graph.node(s, "ledger-service::com.example.ledger"),
            ),
            ("neighbours 1", lambda s: tools_graph.neighbours(s, "payments-api::PaymentService")),
            (
                "neighbours 2",
                lambda s: tools_graph.neighbours(s, "ledger-service::LedgerClient", depth=2),
            ),
            ("node legacy kind", lambda s: tools_graph.node(s, NEW_CARD["id"])),
            ("node numeric label", lambda s: tools_graph.node(s, REPLICAS["id"])),
            ("node repeated id", lambda s: tools_graph.node(s, FORM_FIELD["id"])),
            ("node numeric id", lambda s: tools_graph.node(s, "7")),
            ("node no repository", lambda s: tools_graph.node(s, GLOSSARY["id"])),
            (
                "neighbours numeric id",
                lambda s: tools_graph.neighbours(s, "notifications-service::EmailChannel"),
            ),
            ("search scenario", lambda s: tools_graph.search(s, "pay", kind="scenario")),
            ("community 0", lambda s: tools_graph.community(s, 0)),
            ("community 1", lambda s: tools_graph.community(s, 1)),
        ]
        for name, call in calls:
            with self.subTest(call=name):
                with_index, without = call(indexed), call(fallback)
                self.assertEqual(body(with_index), body(without))
                self.assertEqual(with_index["store"]["index"], INDEXED)
                self.assertEqual(without["store"]["index"], FALLBACK)
                self.assertEqual(results.encode(call(indexed)), results.encode(with_index))
                self.assertEqual(results.encode(call(fallback)), results.encode(without))

    def test_a_reload_notice_reaches_the_tool_result(self):
        """Catches: a tool that takes the store without the session's notice, so a
        reload goes unmentioned (spec, Freshness)."""
        session = self.session()

        def unreadable() -> None:
            root = session.current()[0].root
            (root / MANIFEST).write_text("{", encoding="utf-8")

        unreadable()
        response = tools_graph.node(session, "payments-api::RefundHandler")
        self.assertEqual(response["store"]["notice"], "reload deferred: manifest unreadable")
        self.assertEqual(response["node"], REFUND_HANDLER)


if __name__ == "__main__":
    unittest.main()
