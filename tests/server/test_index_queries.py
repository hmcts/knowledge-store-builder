"""The server reads the index with the library's documented queries, verbatim.

`docs/how-it-works.md`, the "Query index" section, is the reading contract: its
`<!-- index-query: ... -->` blocks are the queries, and its
`<!-- index-helper: match_argument -->` block is how free text becomes a `MATCH`
argument. These tests extract both from the real document, so a server query that
drifts from the documented one fails here, and so does a documented change the
server has not taken.
"""

import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fixture_store.build import build
from knowledgestore.server import queries, store, tools_graph, tools_tickets
from knowledgestore.server.index import IndexHandle

DOC = Path(__file__).resolve().parents[2] / "docs" / "how-it-works.md"

QUERY = re.compile(r"<!-- index-query: (\w+) -->\n```sql\n(.*?)\n```", re.S)
HELPER = re.compile(r"<!-- index-helper: match_argument -->\n```python\n(.*?)\n```", re.S)


def documented() -> dict[str, str]:
    return dict(QUERY.findall(DOC.read_text(encoding="utf-8")))


class DocumentedQueriesTest(unittest.TestCase):
    def test_the_documented_queries_are_the_five_the_server_runs(self):
        """Catches: a query added to or dropped from the documented contract that
        the server neither runs nor declares, or a document whose tags no
        longer parse (so every comparison below would pass vacuously)."""
        self.assertEqual(
            sorted(documented()),
            ["files_for_ticket", "find", "neighbours", "node", "tickets_for_file"],
        )

    def test_each_server_query_is_the_documented_block_verbatim(self):
        """Catches: a server query edited away from the library's documented SQL -
        a different filter, join or ORDER BY - or a documented change the server
        has not taken."""
        for name, sql in documented().items():
            with self.subTest(query=name):
                self.assertEqual(getattr(queries, name.upper()), sql)

    def test_the_term_builder_is_the_documented_match_argument_helper(self):
        """Catches: the server turning free text into a `MATCH` argument otherwise
        than the documented helper does - another tokenizer, no prefix star, a
        quote not doubled, a repeated word kept - so the server and the library
        disagree on what a search finds. Each input is chosen to exercise one of
        those: FTS5 operators and a stray quote, joined words, a repeat in another
        case, and letters outside ASCII."""
        (source,) = HELPER.findall(DOC.read_text(encoding="utf-8"))
        namespace: dict[str, Any] = {}
        exec(compile(source, str(DOC), "exec"), namespace)
        match_argument = namespace["match_argument"]
        for text in (
            "RefundHandler",
            'NEAR(pay gate) OR ledger* "unbalanced',
            "pay_gate feature-gateway",
            "Ledger ledger LEDGER",
            "Straße Ünïcode café",
            "column:filter ^start",
        ):
            with self.subTest(text=text):
                self.assertEqual(tools_graph._query(tools_graph._words(text)), match_argument(text))


def executed_as(sql: str) -> re.Pattern[str]:
    """`sql` as SQLite's trace reports it: each `?N` is shown bound to its value."""
    return re.compile(re.sub(r"\\\?\d", lambda _: ".*?", re.escape(sql)), re.S)


class QueriesRunTest(unittest.TestCase):
    def test_the_tools_run_each_documented_query_against_the_index(self):
        """Catches: a tool that stops running the documented constant - inline SQL
        put back that returns the same rows on this store, so no tool test can
        see it. SQLite's own trace of the index connection must show each
        documented query executed while the tools answer."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        root = build(base / "store")
        session = store.Session(
            lambda: store.open_store(root, base / "cache"),
            lambda: datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),
        )
        self.addCleanup(lambda: session.current()[0].close())
        index = session.current()[0].index
        assert isinstance(index, IndexHandle)
        traced: list[str] = []
        index.connection.set_trace_callback(traced.append)
        tools_graph.search(session, "RefundHandler")
        tools_graph.node(session, "payments-api::RefundHandler")
        tools_graph.neighbours(session, "payments-api::PaymentService")
        tools_tickets.tickets(session, "payments-api")
        tools_tickets.ticket(session, "EX-201")
        for name in sorted(documented()):
            with self.subTest(query=name):
                pattern = executed_as(getattr(queries, name.upper()))
                self.assertTrue(any(pattern.fullmatch(sql) for sql in traced), name)


if __name__ == "__main__":
    unittest.main()
