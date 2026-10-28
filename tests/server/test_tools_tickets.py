"""The ticket and answer tools (spec, Tools): tickets, ticket and answers.

Every expected value is a literal from DESIGN.md 6, 7 and 8, derived by hand from
the fixture's ticket layer and written answers. Each store is the synthetic store,
built for real and opened through the real index (or, with `index="none"`, the
real ticket layer and graph); the only stand-ins are the clock and the cache
directory.
"""

import gzip
import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from fixture_store.build import (
    BRIEFS,
    BRIEFS_DATA,
    DIVES_DATA,
    FAQ,
    FAQ_DATA,
    FILE_TICKETS,
    HOSTILE,
    MANIFEST,
    TICKET_DESCRIPTIONS_DATA,
    TICKET_TRACKER,
    build,
)
from knowledgestore.server import results, store, tools_graph, tools_tickets
from knowledgestore.server.tools_graph import ArgumentRefused

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

INDEXED = "present"
FALLBACK = "absent: graph read whole"
MODES = (("gz", INDEXED), ("none", FALLBACK))

PAY = "src/main/java/com/example/payments/"
SERVICE = PAY + "PaymentService.java"
REFUNDS = PAY + "RefundHandler.java"
LEDGER = "src/main/java/com/example/ledger/LedgerController.java"

# DESIGN.md 6.1 and 6.2.
EX_201_BRANCH = "feature/ex-201-partial-refunds"
EX_102_BRANCH = "feature/ex-102-retry-budget"

# DESIGN.md 6.1: payments-api's tickets, by (ticket, path).
PAYMENTS_LANDED = [
    {"ticket": "EX-101", "path": SERVICE, "commits": 3},
    {"ticket": "EX-102", "path": REFUNDS, "commits": 2},
    {"ticket": "EX-103", "path": SERVICE, "commits": 1},
]
PAYMENTS_PROPOSED = [
    {"ticket": "EX-201", "path": SERVICE, "commits": 2, "branches": [EX_201_BRANCH]},
]

UNKNOWN = {
    "answer": "not in the estate",
    "exact": None,
    "near_misses": [
        {"repository": "payments-api", "near_miss": True},
        {"repository": "payment-apis", "near_miss": True},
    ],
}
NOT_HELD = {
    "answer": "referenced by the estate but not held",
    "exact": None,
    "target": "legacy-network",
    "referenced_by": ["ledger-service", "payments-api"],
}

# DESIGN.md 9.2: the store's `sources_as_of`, which dates the library's written
# layers (DESIGN.md 7.1).
SOURCES_AS_OF = "2026-10-15T23:30:00-02:00"

# The written answers of DESIGN.md 7, as `answers` returns each match.
FAQ_MATCH = {
    "layer": "faq",
    "id": "how-refunds-reach-the-ledger",
    "store": "example-store",
    "source": "docs/answers/how-refunds-reach-the-ledger.md",
    "summary": "How does a refund reach the ledger?",
    "text": (
        "<p>RefundHandler in payments-api calls LedgerClient, which posts a "
        "reversing journal entry in ledger-service.</p>"
    ),
    "as_of": "2026-09-28",
    "commit": "5ca1ab1e" * 5,
}
PAYMENTS_BRIEF = {
    "layer": "topics",
    "id": "payments",
    "store": "example-store",
    "source": "docs/topics/payments.md",
    "summary": "How a card payment is taken",
    "text": (
        "<p>A card payment enters through gateway-service, is taken by "
        "payments-api, and is posted to ledger-service as a journal entry.</p>"
    ),
    "as_of": SOURCES_AS_OF,
    "commit": None,
}
PAYMENTS_DIVE = {
    "layer": "deep_dives",
    "id": "payments-api",
    "store": "example-store",
    "source": "docs/deep-dives/payments-api.md",
    "summary": "Deep dive: payments-api",
    "text": (
        "<p>payments-api at 99999999 takes card payments and hands refunds to "
        "RefundHandler, which retries a timed-out refund.</p>"
    ),
    "as_of": SOURCES_AS_OF,
    "commit": None,
}
ANSWER_LAYERS = ["deep_dives", "faq", "topics"]

# Store text the fixture holds, by field: each may be returned only under a field
# named `text`, `summary` or `description` (Task 8, Store text).
STORE_TEXT = sorted(
    {
        text
        for record in TICKET_DESCRIPTIONS_DATA.values()
        for field in ("d", "s", "b", "ps", "pb")
        for text in record.get(field, [])
    }
    | {
        entry[field]
        for entries in (BRIEFS_DATA, DIVES_DATA, FAQ_DATA)
        for entry in entries.values()
        for field in ("title", "html")
    }
)
TEXT_FIELDS = {"text", "summary", "description"}


def envelope(index: str, layers_used: list[str]) -> dict:
    return {
        "store": {
            "id": "example-store",
            "format": {"major": 1, "minor": 0},
            "library_version": "0.0.0+fixture",
            "sources_as_of": SOURCES_AS_OF,
            "index": index,
            "layers_used": layers_used,
            "notice": None,
        }
    }


def known(name: str) -> dict:
    return {"answer": "in the estate", "exact": name, "matched_case_insensitively": False}


def leaves(value: Any, path: tuple = ()) -> Iterator[tuple[tuple, str]]:
    """Every string in a response, keys included, with the path that reaches it."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield (*path, key), key
            yield from leaves(item, (*path, key))
    elif isinstance(value, list):
        for position, item in enumerate(value):
            yield from leaves(item, (*path, position))
    elif isinstance(value, str):
        yield path, value


class TicketsTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.cache = self.tmp / "cache"
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, index: str = "gz", edit=None, files=None, **options) -> store.Session:
        """The fixture store. `edit` changes the manifest document, and `files` maps
        store-relative paths to bytes written over the built ones."""
        root = build(
            self.tmp / f"s-{index}-{len(list(self.tmp.iterdir()))}", index=index, **options
        )
        for rel, data in (files or {}).items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)
        if edit is not None:
            path = root / MANIFEST
            document = json.loads(path.read_text(encoding="utf-8"))
            edit(document)
            path.write_text(json.dumps(document), encoding="utf-8")
        session = store.Session(lambda: store.open_store(root, self.cache), lambda: NOW)
        self.addCleanup(lambda: session.current()[0].close())
        return session


class TicketsTest(TicketsTestCase):
    def test_landed_and_proposed_are_two_lists_and_the_proposed_ticket_is_only_proposed(self):
        """Catches: EX-201, proposed on a branch, listed among the landed tickets or
        merged with them into one list; its branch dropped; or the index and the
        ticket layer read whole answering differently (DESIGN.md 6.1)."""
        for index, state in MODES:
            with self.subTest(index=index):
                layers = (
                    ["index", "ticket_descriptions"]
                    if index == "gz"
                    else ["graph", "intent", "ticket_descriptions"]
                )
                self.assertEqual(
                    tools_tickets.tickets(self.session(index), "payments-api"),
                    {
                        **envelope(state, layers),
                        "repository": known("payments-api"),
                        "reach": True,
                        "reach_note": None,
                        "totals": {"landed": 3, "proposed": 1},
                        "landed": PAYMENTS_LANDED,
                        "proposed": PAYMENTS_PROPOSED,
                        "total": 4,
                        "page": 1,
                        "next_page": None,
                    },
                )

    def test_a_path_keeps_that_file_only(self):
        """Catches: `path` ignored, so RefundHandler.java answers with the tickets of
        every file in the repository; or a path with no tickets answered as a bare
        empty list rather than saying what was searched."""
        for index, _ in MODES:
            with self.subTest(index=index):
                session = self.session(index)
                one = tools_tickets.tickets(session, "payments-api", path=REFUNDS)
                self.assertEqual(
                    (one["landed"], one["proposed"]),
                    ([{"ticket": "EX-102", "path": REFUNDS, "commits": 2}], []),
                )
                none = tools_tickets.tickets(session, "payments-api", path="README.md")
                self.assertEqual((none["landed"], none["proposed"], none["total"]), ([], [], 0))
                base = "index" if index == "gz" else "graph"
                self.assertEqual(
                    none["searched"], f"searched 12 repositories in the {base} and intent layers"
                )

    def test_reach_is_passed_through_as_recorded_null_included(self):
        """Catches: an unrecorded reach (`null`) reported as true or false, a false
        one reported as true, or a store whose landed list may hold branch work
        not saying so (DESIGN.md 6.3)."""
        for recorded in (True, False, None):
            with self.subTest(reach=recorded):

                def edit(document, recorded=recorded):
                    document["layers"]["intent"]["reach"] = recorded

                found = tools_tickets.tickets(self.session(edit=edit), "ledger-service")
                self.assertIs(found["reach"], recorded)
                if recorded is True:
                    self.assertIsNone(found["reach_note"])
                else:
                    self.assertEqual(found["reach_note"], tools_tickets.REACH_NOTE)
                self.assertEqual(
                    found["landed"], [{"ticket": "EX-104", "path": LEDGER, "commits": 1}]
                )

    def test_a_missing_reach_flag_is_null(self):
        """Catches: a manifest that never recorded the flag answered as reach
        true, which would present the landed list as certain."""

        def edit(document):
            del document["layers"]["intent"]["reach"]

        self.assertIsNone(tools_tickets.tickets(self.session(edit=edit), "payments-api")["reach"])

    def test_branches_are_unknown_when_the_descriptions_layer_is_absent(self):
        """Catches: a proposed ticket's branches reported as `[]` - "no branch" -
        when the layer that records them was not there to read."""

        def edit(document):
            document["layers"]["ticket_descriptions"]["present"] = False

        found = tools_tickets.tickets(self.session(edit=edit), "payments-api")
        self.assertEqual(
            found["proposed"],
            [{"ticket": "EX-201", "path": SERVICE, "commits": 2, "branches": None}],
        )
        self.assertEqual(found["store"]["layers_used"], ["index"])

    def test_an_unknown_name_has_near_misses_and_a_known_one_zero_results(self):
        """Catches: a name the estate does not hold answered as a bare empty list,
        a repository with no tickets answered as unknown, or a target outside the
        estate answered as unknown (spec, Unknown names; DESIGN.md 2 and 5.4)."""
        session = self.session()
        unknown = tools_tickets.tickets(session, "payment-api")
        self.assertEqual(unknown["repository"], UNKNOWN)
        self.assertEqual(
            unknown["searched"],
            "searched 12 repositories in the index, intent and relationships layers",
        )
        zero = tools_tickets.tickets(session, "payment-apis")
        self.assertEqual(zero["repository"], known("payment-apis"))
        self.assertEqual((zero["landed"], zero["proposed"], zero["total"]), ([], [], 0))
        self.assertEqual(
            zero["searched"], "searched 12 repositories in the index and intent layers"
        )
        held = tools_tickets.tickets(session, "legacy-network")
        self.assertEqual(held["repository"], NOT_HELD)
        self.assertEqual((held["landed"], held["proposed"]), ([], []))

    def test_a_repository_only_the_ticket_layer_names_has_its_tickets(self):
        """Catches: tickets answered only for repositories with graph nodes, so a
        repository the ticket layer alone names reads as not in the estate
        (DESIGN.md 6.4)."""
        for index, _ in MODES:
            with self.subTest(index=index):
                found = tools_tickets.tickets(
                    self.session(index, ticket_only=True), "retired-batch"
                )
                self.assertEqual(found["repository"], known("retired-batch"))
                self.assertEqual(
                    found["landed"],
                    [{"ticket": "EX-105", "path": "jobs/nightly-settlement.sh", "commits": 1}],
                )

    def test_a_repository_only_provenance_records_is_in_the_estate(self):
        """Catches: tickets for a cloned repository with no graph node and no ticket,
        recorded only in provenance, answered "not in the estate" with near misses
        (DESIGN.md 1.1); the answer is in the estate, zero results, and says the
        graph holds no nodes for it."""
        for index, _ in MODES:
            with self.subTest(index=index):
                found = tools_tickets.tickets(
                    self.session(index, provenance_only=True), "release-scripts"
                )
                self.assertEqual(
                    found["repository"],
                    {
                        **known("release-scripts"),
                        "graph_nodes": 0,
                        "cloned_at": "d" * 40,
                        "note": "in the estate, but the graph holds no nodes for it",
                    },
                )
                self.assertEqual((found["landed"], found["proposed"], found["total"]), ([], [], 0))
                self.assertIn("searched 13 repositories", found["searched"])

    def test_an_empty_name_is_refused(self):
        """Catches: an empty repository name looked up, and answered with every
        repository's near misses."""
        session = self.session()
        with self.assertRaises(ArgumentRefused):
            tools_tickets.tickets(session, "  ")

    def test_pages_stay_under_every_cap_and_reassemble_to_both_lists(self):
        """Catches: two lists cut from one page without allowing for their extra
        bytes, so a page goes over the cap; `oversize` judged on the one list the
        pager measured rather than the response as sent; an item lost or repeated
        between pages; or a landed ticket paged into `proposed`. Every cap from the
        frame alone to the whole response is tried."""
        session = self.session()
        whole = tools_tickets.tickets(session, "payments-api")
        size = len(results.encode(whole))
        frame = len(results.encode({**whole, "landed": [], "proposed": []}))
        for cap in range(frame, size + 1):
            with self.subTest(cap=cap):
                landed, proposed, cursor, pages = [], [], None, 0
                while True:
                    found = tools_tickets.tickets(
                        session, "payments-api", cursor=cursor, cap_bytes=cap
                    )
                    pages += 1
                    over = len(results.encode(found)) > cap
                    self.assertEqual(found.get("oversize", False), over)
                    if over:
                        # Only one item that does not fit alone may go over.
                        self.assertEqual(len(found["landed"]) + len(found["proposed"]), 1)
                    landed += found["landed"]
                    proposed += found["proposed"]
                    cursor = found["next_page"]
                    if cursor is None:
                        break
                self.assertEqual((landed, proposed), (PAYMENTS_LANDED, PAYMENTS_PROPOSED))
                if cap < size:
                    self.assertGreater(pages, 1)

    def test_a_ticket_in_both_sections_of_a_file_is_landed_once(self):
        """Catches: the ticket layer read whole listing a ticket that a file holds
        under both `tickets` and `proposed` twice, or as proposed; the index builder
        keeps it once, as landed (`store_index._ticket_rows`)."""
        layer = {
            "payments-api": {
                SERVICE: {
                    "tickets": {"EX-101": 3},
                    "proposed": {"EX-101": 1, "EX-201": 2},
                    "first": "2026-08-04",
                    "last": "2026-10-15",
                }
            }
        }
        written = gzip.compress(json.dumps(layer).encode("utf-8"))
        found = tools_tickets.tickets(
            self.session("none", files={FILE_TICKETS: written}), "payments-api"
        )
        self.assertEqual(found["landed"], [{"ticket": "EX-101", "path": SERVICE, "commits": 3}])
        self.assertEqual([i["ticket"] for i in found["proposed"]], ["EX-201"])


class TicketTest(TicketsTestCase):
    def test_a_proposed_ticket_is_proposed_with_its_branch_and_its_file(self):
        """Catches: the record's `proposed` flag or branches dropped, or the file a
        proposed ticket touched listed as landed (DESIGN.md 6.1)."""
        for index, state in MODES:
            with self.subTest(index=index):
                layers = (
                    ["index", "ticket_descriptions"]
                    if index == "gz"
                    else [
                        "intent",
                        "ticket_descriptions",
                    ]
                )
                self.assertEqual(
                    tools_tickets.ticket(self.session(index), "EX-201"),
                    {
                        **envelope(state, layers),
                        "ticket": {
                            "key": "EX-201",
                            "proposed": True,
                            "branches": [EX_201_BRANCH],
                            "description": ["Partial refunds for split payments"],
                            "text": {"subjects": ["EX-201: partial refunds for split payments"]},
                            "first": "2026-10-12",
                            "last": "2026-10-14",
                            "repos": ["payments-api"],
                            "commits": 2,
                        },
                        "tracker": None,
                        "tracker_note": tools_tickets.TRACKER_ABSENT,
                        "reach": True,
                        "reach_note": None,
                        "totals": {"landed": 0, "proposed": 1},
                        "landed": [],
                        "proposed": [{"repository": "payments-api", "path": SERVICE, "commits": 2}],
                        "total": 1,
                        "page": 1,
                        "next_page": None,
                    },
                )

    def test_branch_only_text_on_a_landed_ticket_is_kept_apart(self):
        """Catches: `ps`/`pb` dropped, or merged into the landed subjects, or the
        ticket called proposed because it has a branch (DESIGN.md 6.2)."""
        found = tools_tickets.ticket(self.session(), "EX-102")
        self.assertEqual(
            found["ticket"],
            {
                "key": "EX-102",
                "proposed": False,
                "branches": [EX_102_BRANCH],
                "description": ["Retry refunds that time out"],
                "text": {
                    "subjects": ["EX-102: retry refunds that time out"],
                    "branch_only_subjects": ["EX-102: try a retry budget of five"],
                    "branch_only_bodies": [
                        "Caps refund retries at five before the handler gives up."
                    ],
                },
                "first": "2026-09-10",
                "last": "2026-09-24",
                "repos": ["payments-api"],
                "commits": 3,
            },
        )
        self.assertEqual(
            (found["landed"], found["proposed"]),
            ([{"repository": "payments-api", "path": REFUNDS, "commits": 2}], []),
        )

    def test_an_unknown_key_is_null_and_says_what_was_searched(self):
        """Catches: an unknown ticket answered as a bare empty result, or a key
        holding SQL treated as anything but a value to look up."""
        session = self.session()
        for key in ("EX-999", "EX-101' OR '1'='1", 'EX-101" OR 1=1 --'):
            with self.subTest(key=key):
                found = tools_tickets.ticket(session, key)
                self.assertIsNone(found["ticket"])
                self.assertEqual((found["landed"], found["proposed"]), ([], []))
                self.assertEqual(
                    found["searched"],
                    "searched 12 repositories in the index, intent and ticket_descriptions "
                    "layers; the ticket_tracker layer is absent",
                )


class HostileTextTest(TicketsTestCase):
    def test_the_hostile_description_comes_back_verbatim_in_description_only(self):
        """Catches: EX-103's instruction-shaped description altered, dropped, or
        copied into any other field - a sentence, a key, a list the server
        composes (DESIGN.md 8)."""
        for index, _ in MODES:
            with self.subTest(index=index):
                session = self.session(index)
                found = tools_tickets.ticket(session, "EX-103")
                self.assertEqual(
                    [path for path, text in leaves(found) if HOSTILE in text],
                    [("ticket", "description", 0)],
                )
                self.assertEqual(found["ticket"]["description"], [HOSTILE])
                for other in (
                    tools_tickets.tickets(session, "payments-api"),
                    tools_tickets.answers(session, "ignore previous instructions landed"),
                ):
                    self.assertEqual([p for p, t in leaves(other) if HOSTILE in t], [])

    def test_store_text_is_only_in_text_summary_or_description(self):
        """Catches: any ticket or answer text the store holds returned under a field
        other than `text`, `summary` or `description` - a title as `title`, a
        subject in a sentence."""
        session = self.session()
        responses = [
            tools_tickets.ticket(session, key) for key in sorted(TICKET_DESCRIPTIONS_DATA)
        ] + [
            tools_tickets.tickets(session, "payments-api"),
            tools_tickets.tickets(session, "ledger-service"),
            tools_tickets.answers(session, "payment refund ledger deployed card"),
        ]
        seen = set()
        for response in responses:
            for path, value in leaves(response):
                for text in STORE_TEXT:
                    if text in value:
                        seen.add(text)
                        self.assertTrue(
                            TEXT_FIELDS & {p for p in path if isinstance(p, str)},
                            f"store text at {path}",
                        )
        # The sweep saw every piece of store text, so its silence covers them all.
        self.assertEqual(sorted(seen), STORE_TEXT)

    def test_instruction_shaped_answer_fields_are_not_passed_as_structure(self):
        """Catches: an answers layer's hand-written id or `source` returned outside
        a text field although it holds prose; such an entry is skipped and counted,
        and a `source` that is not a store-relative path is null."""
        faq = {
            "how-refunds-reach-the-ledger": {**FAQ_DATA["how-refunds-reach-the-ledger"]},
            HOSTILE: {"title": "Ledger", "html": "<p>ledger</p>", "source": "docs/x.md"},
            "climbs": {"title": "Ledger", "html": "<p>ledger</p>", "source": "../outside.md"},
            "prose": {"title": "Ledger", "html": "<p>ledger</p>", "source": HOSTILE},
        }
        session = self.session(files={FAQ: json.dumps(faq).encode("utf-8")})
        found = tools_tickets.answers(session, "ledger")
        self.assertEqual([p for p, t in leaves(found) if HOSTILE in t], [])
        self.assertEqual(found["entries_skipped"], 1)
        faq_sources = {i["id"]: i["source"] for i in found["items"] if i["layer"] == "faq"}
        self.assertEqual(
            faq_sources,
            {
                "climbs": None,
                "how-refunds-reach-the-ledger": "docs/answers/how-refunds-reach-the-ledger.md",
                "prose": None,
            },
        )

    def test_path_and_id_segments_keep_any_script_and_refuse_dots_alone(self):
        """Catches: a non-ASCII source or id losing its citation (`é.md`, `café`);
        or a segment of dots alone, a control character, a backslash or a Unicode
        line separator passed as structure."""
        entry = {"title": "Ledger", "html": "<p>ledger</p>"}
        faq = {
            "café": {**entry, "source": "docs/é.md"},
            "dots": {**entry, "source": "docs/.../x.md"},
            "dot": {**entry, "source": "./x.md"},
            "bell": {**entry, "source": "docs/a\u0007.md"},
            "slash": {**entry, "source": "docs\\x.md"},
            "line": {**entry, "source": "docs/a\u2028b.md"},
            "...": {**entry, "source": "docs/x.md"},
            "tab\tid": {**entry, "source": "docs/x.md"},
        }
        session = self.session(files={FAQ: json.dumps(faq).encode("utf-8")})
        found = tools_tickets.answers(session, "ledger")
        self.assertEqual(
            {i["id"]: i["source"] for i in found["items"] if i["layer"] == "faq"},
            {
                "bell": None,
                "café": "docs/é.md",
                "dot": None,
                "dots": None,
                "line": None,
                "slash": None,
            },
        )
        self.assertEqual(found["entries_skipped"], 2)

    def test_one_malformed_answers_layer_is_reported_and_the_others_answer(self):
        """Catches: invalid JSON, text that is not UTF-8, or a damaged gzip file in
        one extra answers layer failing the whole call, or being searched as empty
        without saying so."""
        gz = "knowledge/answers/faq.json.gz"
        whole = gzip.compress(json.dumps(FAQ_DATA).encode("utf-8"))

        def gzipped(document):
            document["extra_layers"]["faq"]["path"] = gz

        for name, rel, written, edit in (
            ("invalid json", FAQ, b'{"how": ', None),
            ("not utf-8", FAQ, b'{"how": "\xff"}', None),
            ("not gzip", gz, b"not gzip at all", gzipped),
            ("truncated gzip", gz, whole[: len(whole) // 2], gzipped),
            ("corrupt deflate", gz, whole[:10] + b"\xff" * 20 + whole[30:], gzipped),
        ):
            with self.subTest(name):
                session = self.session(edit=edit, files={rel: written})
                found = tools_tickets.answers(session, "ledger")
                self.assertEqual(found["not_read"], {"faq": tools_tickets.NOT_JSON})
                self.assertEqual(
                    [(i["layer"], i["id"]) for i in found["items"]], [("topics", "payments")]
                )
                self.assertEqual(found["store"]["layers_used"], ["deep_dives", "topics"])

    def test_a_missing_answers_file_still_fails(self):
        """Catches: a layer the manifest says is present, whose file is gone,
        answered as though it were empty: that is a store fault Task 9 reports."""

        def missing(document):
            document["extra_layers"]["faq"]["path"] = "knowledge/answers/gone.json"

        session = self.session(edit=missing)
        with self.assertRaises(FileNotFoundError):
            tools_tickets.answers(session, "ledger")


class AnswersTest(TicketsTestCase):
    def test_answers_find_the_extra_layer_entry(self):
        """Catches: extra layers of kind `answers` not searched, or a match cited by
        anything but its store-relative source and the store's id (DESIGN.md 7)."""
        for index, state in MODES:
            with self.subTest(index=index):
                self.assertEqual(
                    tools_tickets.answers(self.session(index), "ledger reach"),
                    {
                        **envelope(state, ANSWER_LAYERS),
                        "matches": 2,
                        "items": [
                            {**FAQ_MATCH, "score": 2},
                            {**PAYMENTS_BRIEF, "score": 1},
                        ],
                        "total": 2,
                        "page": 1,
                        "next_page": None,
                    },
                )

    def test_ranked_by_term_overlap_then_id(self):
        """Catches: ranking by id, layer or file order rather than by how many words
        matched, or a tie broken other than by id. `card payment ledger` matches all
        three words of the payments brief, whose id sorts last, and two of the FAQ
        entry and the deep dive, tied and broken by id. Words match as prefixes, so
        `payment` finds `payments`."""
        found = tools_tickets.answers(self.session(), "card payment ledger")
        self.assertEqual(
            [(i["layer"], i["id"], i["score"]) for i in found["items"]],
            [
                ("topics", "payments", 3),
                ("faq", FAQ_MATCH["id"], 2),
                ("deep_dives", "payments-api", 2),
            ],
        )

    def test_a_question_ranks_by_its_subject_words_not_common_ones(self):
        """Catches: common words scoring, so `how does a refund reach the ledger`
        returns the deployments brief, which shares only `how`, `a` and `the` with
        it. The FAQ entry matches all three subject words."""
        found = tools_tickets.answers(self.session(), "how does a refund reach the ledger")
        self.assertEqual(
            [(i["layer"], i["id"], i["score"]) for i in found["items"]],
            [
                ("faq", FAQ_MATCH["id"], 3),
                ("topics", "payments", 1),
                ("deep_dives", "payments-api", 1),
            ],
        )

    def test_a_short_word_matches_a_whole_word_only(self):
        """Catches: a query word under three letters matched as a prefix, so `le`
        finds every answer that mentions the ledger; it says what it searched."""
        found = tools_tickets.answers(self.session(), "le")
        self.assertEqual(found["items"], [])
        self.assertEqual(
            found["searched"], "searched 4 answers in the deep_dives, faq and topics layers"
        )

    def test_the_sentence_counts_answers_and_names_absent_layers(self):
        """Catches: the sentence counting repositories, or anything but the entries
        searched; a singular miscounted; or an absent library layer left unnamed."""
        faq = {"one": {"title": "Settlement", "html": "<p>nightly</p>", "source": "a.md"}}

        def absent(document):
            for name in ("topics", "deep_dives"):
                document["layers"][name]["present"] = False

        session = self.session(edit=absent, files={FAQ: json.dumps(faq).encode("utf-8")})
        self.assertEqual(
            tools_tickets.answers(session, "kubernetes")["searched"],
            "searched 1 answer in the faq layer; the deep_dives and topics layers are absent",
        )

    def test_limit_keeps_the_best_and_counts_every_match(self):
        """Catches: `limit` ignored, or `matches` counting only what was returned."""
        found = tools_tickets.answers(self.session(), "refund ledger", limit=1)
        self.assertEqual([i["id"] for i in found["items"]], [FAQ_MATCH["id"]])
        self.assertEqual(found["matches"], 3)

    def test_markup_is_not_searched_and_entities_are(self):
        """Catches: html tags searched as words, so `strong` matches an entry that
        only uses the tag; or an entity searched as written, so `&amp;` hides the
        word: `caf&eacute;` is `café`. The text is returned as the store wrote it."""
        faq = {
            "markup": {
                "title": "Settlement",
                "html": "<p><strong>Settlement</strong> runs nightly at the caf&eacute;</p>",
                "source": "docs/answers/markup.md",
            }
        }
        session = self.session(files={FAQ: json.dumps(faq).encode("utf-8")})
        self.assertEqual(tools_tickets.answers(session, "strong")["items"], [])
        found = tools_tickets.answers(session, "nightly café")
        self.assertEqual([(i["id"], i["score"]) for i in found["items"]], [("markup", 2)])
        self.assertEqual(found["items"][0]["text"], faq["markup"]["html"])

    def test_no_match_says_which_layers_were_searched(self):
        """Catches: an empty answer that does not name the layers it searched, or
        names one it did not."""
        found = tools_tickets.answers(self.session(), "kubernetes")
        self.assertEqual((found["items"], found["matches"]), ([], 0))
        self.assertEqual(
            found["searched"],
            "searched 4 answers in the deep_dives, faq and topics layers",
        )

    def test_an_absent_or_other_kind_extra_layer_is_not_searched(self):
        """Catches: an extra layer read although the manifest says it is absent, or
        one of another kind read as answers."""

        def absent(document):
            document["extra_layers"]["faq"]["present"] = False

        found = tools_tickets.answers(self.session(edit=absent), "reach")
        self.assertEqual(found["items"], [])
        self.assertEqual(
            found["searched"],
            "searched 3 answers in the deep_dives and topics layers; the faq layer is absent",
        )

        def other(document):
            document["extra_layers"]["faq"]["kind"] = "notes"

        found = tools_tickets.answers(self.session(edit=other), "reach")
        self.assertEqual(found["items"], [])
        self.assertEqual(
            found["searched"], "searched 3 answers in the deep_dives and topics layers"
        )

    def test_an_answers_layer_that_is_not_a_file_of_entries_is_named_not_read(self):
        """Catches: a directory or a non-object file declared as an answers layer
        failing every call, or silently searched as empty; or a file holding one
        string taken for the reason, so its text is returned outside a text field."""
        for written in (b"[1, 2]", json.dumps(HOSTILE).encode("utf-8")):
            with self.subTest(written=written):
                found = tools_tickets.answers(self.session(files={FAQ: written}), "reach")
                self.assertEqual(found["not_read"], {"faq": tools_tickets.NOT_ENTRIES})
                self.assertEqual(found["store"]["layers_used"], ["deep_dives", "topics"])

        def directory(document):
            document["extra_layers"]["faq"]["path"] = "knowledge/answers"

        found = tools_tickets.answers(self.session(edit=directory), "reach")
        self.assertEqual(found["not_read"], {"faq": tools_tickets.A_DIRECTORY})

    def test_unusable_queries_are_refused(self):
        """Catches: a query with no word, or only common words, searched as "match
        everything" or answered empty without a reason; or a limit below one
        accepted."""
        session = self.session()
        for query in ("", "  ", "<>"):
            with self.subTest(query=query), self.assertRaises(ArgumentRefused):
                tools_tickets.answers(session, query)
        with self.assertRaises(ArgumentRefused) as raised:
            tools_tickets.answers(session, "how does the A")
        self.assertIn("only common words", str(raised.exception))
        with self.assertRaises(ArgumentRefused):
            tools_tickets.answers(session, "ledger", limit=0)


# DESIGN.md 6.5: the tracker's own records, as `ticket` returns them.
EX_101_TRACKER = {
    "key": "EX-101",
    "state": "fetched",
    # Resolved, so the stage stores no `checked`: dated when resolved (DESIGN.md 6.5).
    "as_of": "2026-10-16",
    "text": {
        "title": "Take card payments through the new provider",
        "status": "Done",
        "resolution": "Done",
        "description": "Move card payments from the old provider to the new one.",
        "comments": ["Live for every card scheme since the August release."],
    },
    "comments_truncated": False,
}
EX_201_TRACKER = {
    "key": "EX-201",
    "state": "fetched",
    "as_of": "2026-10-15",
    "text": {
        "title": "Partial refunds for split payments",
        "status": "In Progress",
        "comments": [
            "Split payments need a refund per part.",
            "Blocked on the ledger's partial reversal.",
        ],
    },
    "comments_truncated": True,
}


def without_tracker(response: dict) -> dict:
    """A `ticket` response less what the tracker layer adds: its two fields, and
    its name in `layers_used`."""
    rest = {k: v for k, v in response.items() if k not in ("tracker", "tracker_note")}
    layers = [name for name in rest["store"]["layers_used"] if name != "ticket_tracker"]
    return {**rest, "store": {**rest["store"], "layers_used": layers}}


class TrackerTest(TicketsTestCase):
    """The tracker's own record of a ticket (#17, DESIGN.md 6.5): returned under
    `tracker`, apart from the commit-mined record under `ticket`."""

    def test_a_store_with_a_tracker_returns_the_record_under_its_own_key(self):
        """Catches: the tracker layer never read; its record merged into the
        commit-mined one, so an answer cannot say which source it quotes; or the
        title, status, description or comments dropped."""
        for index, _ in MODES:
            with self.subTest(index=index):
                found = tools_tickets.ticket(self.session(index, tracker=True), "EX-101")
                self.assertEqual(found["tracker"], EX_101_TRACKER)
                self.assertIsNone(found["tracker_note"])
                self.assertIn("ticket_tracker", found["store"]["layers_used"])
                self.assertEqual(
                    found["ticket"]["description"],
                    ["Take card payments through the new provider"],
                )

    def test_a_partial_thread_says_so_and_an_open_ticket_has_its_status(self):
        """Catches: two comments of a thread of three returned as the whole
        conversation, or an open ticket's status lost."""
        found = tools_tickets.ticket(self.session(tracker=True), "EX-201")
        self.assertEqual(found["tracker"], EX_201_TRACKER)

    def test_a_denied_or_absent_record_says_which(self):
        """Catches: a ticket the build's token could not read, or one the tracker
        said does not exist, returned as null - "no record" - when the tracker
        answered, and the two answers are different."""
        session = self.session(tracker=True)
        for key, state in (("EX-102", "denied"), ("EX-104", "absent")):
            with self.subTest(key=key):
                found = tools_tickets.ticket(session, key)
                self.assertEqual(
                    found["tracker"],
                    {
                        "key": key,
                        "state": state,
                        "as_of": "2026-10-15",
                        "text": {},
                        "comments_truncated": False,
                    },
                )
                self.assertIsNone(found["tracker_note"])

    def test_without_a_tracker_the_field_is_null_and_the_rest_unchanged(self):
        """Catches: an absent tracker layer answered as "no record", or its
        absence unsaid; or reading the tracker changing anything else `ticket`
        returns."""
        for index, _ in MODES:
            for key in ("EX-101", "EX-102", "EX-201"):
                with self.subTest(index=index, key=key):
                    absent = tools_tickets.ticket(self.session(index), key)
                    self.assertIsNone(absent["tracker"])
                    self.assertEqual(absent["tracker_note"], "the ticket_tracker layer is absent")
                    self.assertNotIn("ticket_tracker", absent["store"]["layers_used"])
                    present = tools_tickets.ticket(self.session(index, tracker=True), key)
                    self.assertEqual(without_tracker(present), without_tracker(absent))

    def test_a_ticket_the_tracker_holds_no_record_of_is_null_and_says_so(self):
        """Catches: a key the layer does not hold answered as though the layer were
        absent, or not said at all."""
        found = tools_tickets.ticket(self.session(tracker=True), "EX-103")
        self.assertIsNone(found["tracker"])
        self.assertEqual(found["tracker_note"], tools_tickets.TRACKER_NO_RECORD)
        self.assertIn("ticket_tracker", found["store"]["layers_used"])

    def test_an_unrecorded_tracker_layer_is_not_called_absent(self):
        """Catches: a tracker layer whose presence the manifest did not record
        (`present: null`) called absent, which says the store was measured and
        lacks it."""

        def unrecorded(document):
            document["layers"]["ticket_tracker"]["present"] = None

        found = tools_tickets.ticket(self.session(edit=unrecorded), "EX-101")
        self.assertIsNone(found["tracker"])
        self.assertEqual(found["tracker_note"], "the ticket_tracker layer is not recorded")

    def test_an_unknown_key_says_the_tracker_was_searched(self):
        """Catches: the sentence for a key no layer holds leaving out the tracker
        layer `ticket` now reads."""
        found = tools_tickets.ticket(self.session(tracker=True), "EX-999")
        self.assertEqual(
            found["searched"],
            "searched 12 repositories in the index, intent, ticket_descriptions and "
            "ticket_tracker layers",
        )

    def test_an_unrecognised_tracker_layer_reads_as_absent(self):
        """Catches: a tracker file in a shape this server does not read - not an
        object of records, not JSON, not gzip - failing the call, guessed at, or
        listed in `layers_used` as though it answered; the reason is given in
        place of a record, and the rest of the answer is as without the layer."""
        records = json.dumps(["EX-101"]).encode("utf-8")
        for name, written, reason in (
            ("a list", gzip.compress(records), tools_tickets.TRACKER_NOT_RECORDS),
            ("not json", gzip.compress(b'{"EX-101": '), tools_tickets.TRACKER_NOT_JSON),
            ("not gzip", b"not gzip at all", tools_tickets.TRACKER_NOT_JSON),
        ):
            with self.subTest(name):
                session = self.session(tracker=True, files={TICKET_TRACKER: written})
                found = tools_tickets.ticket(session, "EX-101")
                self.assertIsNone(found["tracker"])
                self.assertEqual(found["tracker_note"], reason)
                self.assertNotIn("ticket_tracker", found["store"]["layers_used"])
                self.assertEqual(
                    without_tracker(found),
                    without_tracker(tools_tickets.ticket(self.session(), "EX-101")),
                )
                unknown = tools_tickets.ticket(session, "EX-999")
                self.assertEqual(
                    unknown["searched"],
                    "searched 12 repositories in the index, intent and ticket_descriptions layers",
                )

    def test_a_record_is_dated_when_checked_else_when_resolved(self):
        """Catches: a resolved record, which the stage never re-checks and so
        stores without `checked`, returned undated; `resolved` preferred over a
        later `checked`; or a date that is not one passed on."""
        layer = {
            "EX-101": {"summary": "Resolved", "resolved": "2026-10-16"},
            "EX-102": {"summary": "Both", "checked": "2026-10-18", "resolved": "2026-10-16"},
            "EX-103": {"summary": "Neither"},
            "EX-104": {"summary": "Bad check", "checked": "2026-W42-3", "resolved": "2026-10-16"},
            "EX-201": {"summary": "Bad resolve", "resolved": "16/10/2026"},
        }
        written = gzip.compress(json.dumps(layer).encode("utf-8"))
        session = self.session(tracker=True, files={TICKET_TRACKER: written})
        self.assertEqual(
            {key: tools_tickets.ticket(session, key)["tracker"]["as_of"] for key in layer},
            {
                "EX-101": "2026-10-16",
                "EX-102": "2026-10-18",
                "EX-103": None,
                "EX-104": "2026-10-16",
                "EX-201": None,
            },
        )

    def test_a_tracker_declared_at_a_directory_is_named_not_read(self):
        """Catches: a tracker layer declared at a directory - with or without the
        trailing slash a directory layer is listed with - reported as invalid
        JSON, a reason that sends its reader to the wrong fault."""
        for declared in ("knowledge/intent/", "knowledge/intent"):
            with self.subTest(declared=declared):

                def directory(document, declared=declared):
                    document["layers"]["ticket_tracker"]["paths"] = [declared]

                found = tools_tickets.ticket(self.session(tracker=True, edit=directory), "EX-101")
                self.assertIsNone(found["tracker"])
                self.assertEqual(found["tracker_note"], tools_tickets.TRACKER_A_DIRECTORY)
                self.assertNotIn("ticket_tracker", found["store"]["layers_used"])

    def test_the_tracker_is_parsed_once_per_store_load(self):
        """Catches: the whole tracker file - every ticket's comments - read and
        parsed again on every `ticket` call rather than once per loaded store."""
        session = self.session(tracker=True)
        real = store.Store.read
        names: list[str] = []

        def counted(self_: store.Store, name: str) -> Any:
            names.append(name)
            return real(self_, name)

        with mock.patch.object(store.Store, "read", counted):
            first = tools_tickets.ticket(session, "EX-101")
            second = tools_tickets.ticket(session, "EX-201")
        self.assertEqual((first["tracker"], second["tracker"]), (EX_101_TRACKER, EX_201_TRACKER))
        self.assertEqual(names.count("ticket_tracker"), 1)

    def test_a_record_in_a_shape_this_server_does_not_read_is_null_and_says_so(self):
        """Catches: one record of another shape - a string, an object with none of
        the stage's outcomes - returned as a record, or failing the call."""
        layer = {"EX-101": "Done", "EX-201": {"status": "Done"}}
        written = gzip.compress(json.dumps(layer).encode("utf-8"))
        session = self.session(tracker=True, files={TICKET_TRACKER: written})
        for key in ("EX-101", "EX-201"):
            with self.subTest(key=key):
                found = tools_tickets.ticket(session, key)
                self.assertIsNone(found["tracker"])
                self.assertEqual(found["tracker_note"], tools_tickets.TRACKER_RECORD_UNREAD)
                self.assertIn("ticket_tracker", found["store"]["layers_used"])

    def test_tracker_text_is_only_in_text_and_mistyped_fields_are_dropped(self):
        """Catches: tracker text - written by people, and untrusted as all store
        text is - returned outside `text`, such as a title as `title` beside
        `key`; or a field of the wrong type passed on as the record's."""
        layer = {
            "EX-101": {
                "summary": HOSTILE,
                "status": HOSTILE,
                "description": HOSTILE,
                "comments": [HOSTILE, 7, None],
                "parent": HOSTILE,
                "type": HOSTILE,
                "resolution": HOSTILE,
                "comments_truncated": HOSTILE,
                "checked": HOSTILE,
            },
            "EX-201": {"summary": "Partial", "status": 3, "comments": HOSTILE},
        }
        written = gzip.compress(json.dumps(layer).encode("utf-8"))
        session = self.session(tracker=True, files={TICKET_TRACKER: written})
        hostile = tools_tickets.ticket(session, "EX-101")
        self.assertEqual(
            sorted(p for p, t in leaves(hostile) if HOSTILE in t),
            [
                ("tracker", "text", "comments", 0),
                ("tracker", "text", "description"),
                ("tracker", "text", "resolution"),
                ("tracker", "text", "status"),
                ("tracker", "text", "title"),
            ],
        )
        self.assertEqual(hostile["tracker"]["text"]["comments"], [HOSTILE])
        self.assertFalse(hostile["tracker"]["comments_truncated"])
        self.assertIsNone(hostile["tracker"]["as_of"])
        typed = tools_tickets.ticket(session, "EX-201")
        self.assertEqual(typed["tracker"]["text"], {"title": "Partial"})


class DatedAnswersTest(TicketsTestCase):
    """Written analysis is dated (#19): an entry's own `as_of` and `commit`; the
    library's briefs and deep dives, which record no date, by the store's
    `sources_as_of` (DESIGN.md 7.1); anything else undated, as null."""

    def match(self, found: dict, layer: str, entry_id: str) -> dict:
        return next(i for i in found["items"] if (i["layer"], i["id"]) == (layer, entry_id))

    def test_a_dated_entry_returns_its_as_of_and_commit(self):
        """Catches: an answers entry's `as_of` or `commit` dropped, so an assistant
        presents analysis written on 2026-09-28 as current (DESIGN.md 7.1)."""
        found = self.match(
            tools_tickets.answers(self.session(), "ledger reach"),
            "faq",
            "how-refunds-reach-the-ledger",
        )
        self.assertEqual((found["as_of"], found["commit"]), ("2026-09-28", "5ca1ab1e" * 5))

    def test_an_undated_entry_returns_null(self):
        """Catches: an entry of a declared answers layer with no `as_of` given a
        date it does not have - the store's, or today's - so undated analysis
        reads as dated."""
        faq = {
            "undated": {"title": "Ledger", "html": "<p>ledger</p>", "source": "a.md"},
            "commit-only": {
                "title": "Ledger",
                "html": "<p>ledger</p>",
                "source": "b.md",
                "commit": "5ca1ab1e" * 5,
            },
        }
        session = self.session(files={FAQ: json.dumps(faq).encode("utf-8")})
        found = tools_tickets.answers(session, "ledger")
        self.assertEqual(
            {i["id"]: (i["as_of"], i["commit"]) for i in found["items"] if i["layer"] == "faq"},
            {"commit-only": (None, "5ca1ab1e" * 5), "undated": (None, None)},
        )

    def test_briefs_and_dives_are_dated_by_the_stores_sources_as_of(self):
        """Catches: a topic brief or deep dive, which records no date of its own,
        returned undated although the manifest dates the build that holds it; or
        dated from anything but the manifest's `sources_as_of`. A changed
        manifest date moves theirs, and an unrecorded one leaves them null."""
        for recorded in (SOURCES_AS_OF, "2026-07-01T00:00:00+00:00", None):
            with self.subTest(sources_as_of=recorded):

                def edit(document, recorded=recorded):
                    document["sources_as_of"] = recorded

                found = tools_tickets.answers(self.session(edit=edit), "card payment")
                self.assertEqual(
                    {
                        (i["layer"], i["id"]): (i["as_of"], i["commit"])
                        for i in found["items"]
                        if i["layer"] != "faq"
                    },
                    {
                        ("topics", "payments"): (recorded, None),
                        ("deep_dives", "payments-api"): (recorded, None),
                    },
                )

    def test_a_sources_as_of_that_is_not_a_date_dates_nothing(self):
        """Catches: the manifest's `sources_as_of` passed to a brief, a deep dive or
        a summary unchecked, so a value that is not a date is presented as when
        the analysis was written."""
        for recorded in (HOSTILE, 20261015, "20261015", "2026-W42-3"):
            with self.subTest(sources_as_of=recorded):

                def edit(document, recorded=recorded):
                    document["sources_as_of"] = recorded

                session = self.session(edit=edit)
                found = tools_tickets.answers(session, "card payment")
                self.assertEqual(
                    [i["as_of"] for i in found["items"] if i["layer"] != "faq"], [None, None]
                )
                community = tools_graph.community(session, 0)["community"]
                self.assertIsNone(community["summary_as_of"])

    def test_only_a_calendar_date_in_extended_form_is_a_date(self):
        """Catches: an ISO form a reader does not take for a date - the basic
        `20261020`, the week date `2026-W42-3`, a space for the `T` - or a
        date-shaped value that is no date, such as `2026-02-30`, passed as
        written, so an assistant quotes `as_of: 2026-W42-3` as a date."""
        forms = {
            "basic": "20261020",
            "week": "2026-W42-3",
            "ordinal": "2026-293",
            "space": "2026-10-20 10:15:00",
            "impossible": "2026-02-30",
            "untimed": "2026-10-20Tnoon",
            "day": "2026-10-20",
            "instant": "2026-10-20T10:15:00+01:00",
        }
        entry = {"title": "Ledger", "html": "<p>ledger</p>", "source": "a.md"}
        faq = {name: {**entry, "as_of": value} for name, value in forms.items()}
        session = self.session(files={FAQ: json.dumps(faq).encode("utf-8")})
        found = tools_tickets.answers(session, "ledger")
        self.assertEqual(
            {i["id"]: i["as_of"] for i in found["items"] if i["layer"] == "faq"},
            {
                "basic": None,
                "week": None,
                "ordinal": None,
                "space": None,
                "impossible": None,
                "untimed": None,
                "day": "2026-10-20",
                "instant": "2026-10-20T10:15:00+01:00",
            },
        )

    def test_a_briefs_own_date_wins_over_the_stores(self):
        """Catches: a library layer's entry that does carry its own `as_of`
        overwritten by the store's `sources_as_of`, a later and less exact date."""
        briefs = {"payments": {**BRIEFS_DATA["payments"], "as_of": "2026-08-02"}}
        session = self.session(files={BRIEFS: json.dumps(briefs).encode("utf-8")})
        found = self.match(tools_tickets.answers(session, "card"), "topics", "payments")
        self.assertEqual((found["as_of"], found["commit"]), ("2026-08-02", None))

    def test_a_date_or_commit_that_is_not_one_is_null_and_never_returned(self):
        """Catches: an `as_of` or `commit` holding prose returned as structure,
        outside a text field: they are written by hand, so only a date and a hex
        commit id are passed."""
        faq = {
            "prose": {
                "title": "Ledger",
                "html": "<p>ledger</p>",
                "source": "a.md",
                "as_of": HOSTILE,
                "commit": HOSTILE,
            },
            "typed": {
                "title": "Ledger",
                "html": "<p>ledger</p>",
                "source": "b.md",
                "as_of": 20260928,
                "commit": ["5ca1ab1e"],
            },
            "timestamp": {
                "title": "Ledger",
                "html": "<p>ledger</p>",
                "source": "c.md",
                "as_of": "2026-09-28T10:15:00+01:00",
                "commit": "5ca1ab1e on main",
            },
        }
        session = self.session(files={FAQ: json.dumps(faq).encode("utf-8")})
        found = tools_tickets.answers(session, "ledger")
        self.assertEqual([p for p, t in leaves(found) if HOSTILE in t], [])
        self.assertEqual(
            {i["id"]: (i["as_of"], i["commit"]) for i in found["items"] if i["layer"] == "faq"},
            {
                "prose": (None, None),
                "timestamp": ("2026-09-28T10:15:00+01:00", None),
                "typed": (None, None),
            },
        )


if __name__ == "__main__":
    unittest.main()
