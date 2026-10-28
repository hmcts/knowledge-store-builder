"""The contract with the library: the ticket and answer tools over a store
knowledge-store-builder wrote, read through the query index and, without it,
through the ticket layer (`intent`) and `ticket-descriptions`.

Every expected value is derived by hand from what `library_store/generate.py`
writes, before the store is read:

- Tickets are the keys in commit subjects (`build_intent_index`). DEMO-1 lands on
  alpha-lib's `package.json` and `src/refund.ts` (1 October), on `src/refund.ts`
  again (3 October, so that link counts two commits), and on beta-app's
  `package.json` and `src/pay.ts` (5 October); DEMO-2 on alpha-lib's schema
  (2 October); DEMO-3 on beta-app's four ownership files (6 October) and
  delta-deploy's three files (8 October; its empty `deploy/signing.pem` is
  tracked, so it is linked like any other file); DEMO-5 on delta-deploy's values file
  (7 October, before DEMO-3 rewrote it). DEMO-4 is only on beta-app's branch
  `feature/DEMO-4-retry` (7 October, `src/pay.ts`), so it is `proposed`. DEMO-7
  lands on alpha-lib's `package.json` and `index.ts` (4 October) and DEMO-6 on
  eta-portal's `package.json` (5 October). Every other link is one commit.
- A record's descriptions (`d`, at most two) are the subjects less their key,
  kept when at least 12 characters: "retry" and "ownership" are too short. `d`
  ranks by count, then length, ties in history order (alpha-lib before
  beta-app); the subjects (`s`, at most three) by count, then text. A ticket
  that never landed keeps its branch subjects in `s`.
- The tracker's answers are `tracker_responses.json`'s, cached by
  `fetch_tickets` with `today` 2026-10-20 and empty fields dropped: DEMO-1
  fetched and resolved (on 2026-10-05) so, being final, it carries no `checked`
  date; DEMO-3 fetched, unresolved, so checked 2026-10-20, with no description
  and one comment of a thread of three, so `comments_truncated`; DEMO-2 and
  DEMO-4 not in the response, so recorded `absent`, checked 2026-10-20. DEMO-5,
  DEMO-6 and DEMO-7 make the second request (four keys a request, sorted), and
  the tracker answers it 403, so each is recorded `denied`, checked 2026-10-20.
- Written answers are the authored Markdown, rendered by the library's Markdown
  subset (`build_topic_briefs.markdown_to_html`): `#` is `<h2>`, `##` is `<h3>`,
  and each name in `config/repositories.txt` becomes a link to
  `https://github.com/example-org/<name>` unless it follows a `/`. No ticket
  browse URL is set, so ticket keys stay text. A ```` ``` ```` fence is
  `_fence_block`'s: `<pre><code class="language-<word>">`, its lines joined by a
  newline and escaped with `html.escape(quote=False)`, and nothing inside it
  rendered or linked. `how-refunds-work` ends with a `ts` fence and a `mermaid`
  one.
- `how-refunds-work` is dated by its `date:` and never committed; the store's
  own repository commits `who-approves-refunds` alone, as `pipeline.py` does,
  on 2026-10-19, which `answers-layer` writes with `+00:00` for UTC. That
  commit's sha, like each clone's, is read from `inputs.json`, where the
  generator records it from the repository it made.
- Topic briefs and deep dives record no date, so they are dated by
  `sources_as_of`: delta-deploy's 8 October commit, the newest in the estate.
  The deep dive names the alpha-lib commit it was measured at, the first eight
  characters of `inputs.json`'s sha, as an author writes the commit they read.
"""

import json
import unittest
from typing import Any

from knowledgestore.server import tools_tickets
from library_store import harness
from library_store.generate import EXTRA_LAYER, INPUTS, STORE_ANSWERS_COMMIT
from library_store.harness import SHAS

# 2026-10-08T09:00:00+00:00, delta-deploy's last commit, the newest in the estate.
SOURCES_AS_OF = "2026-10-08T09:00:00Z"

# The store's own commit of docs/answers/who-approves-refunds.md (see above).
WHO_APPROVES_COMMIT: str = json.loads(INPUTS.read_text("utf-8"))[STORE_ANSWERS_COMMIT]


def link(repo: str) -> str:
    """A repository name as the library's renderer links it."""
    return (
        f'<a href="https://github.com/example-org/{repo}" target="_blank" rel="noopener">{repo}</a>'
    )


ALPHA, BETA = link("alpha-lib"), link("beta-app")

# The answer's two fences, as `_fence_block` renders each: the language a class,
# the lines joined by a newline, `&`, `<` and `>` escaped and nothing else - no
# quote, no Markdown, and no repository link inside a fence.
FENCED_CODE = (
    '<pre><code class="language-ts">'
    "const owed = refund(amount); // amount &gt; 0 &amp;&amp; owed &lt; 0, in beta-app"
    "</code></pre>"
)
MERMAID = (
    '<pre><code class="language-mermaid">'
    "flowchart LR\n"
    '  pay["beta-app: PaymentService.pay"] --&gt; refund["alpha-lib: refund"]'
    "</code></pre>"
)

HOW_REFUNDS_WORK = {
    "layer": "answers",
    "id": "how-refunds-work",
    "store": "library-contract-store",
    "source": "docs/answers/how-refunds-work.md",
    "summary": "How do refunds work?",
    "text": (
        "<h2>How do refunds work?</h2>"
        f"<p>A refund is the negative of the amount paid. {ALPHA}'s <code>refund</code> "
        f"function computes it, and {BETA} calls it through the "
        "<code>@example/alpha-lib</code> package.</p>"
        f"{FENCED_CODE}{MERMAID}"
    ),
    "as_of": "2026-10-18",
    "commit": None,
    "score": 1,
}
WHO_APPROVES_REFUNDS = {
    "layer": "answers",
    "id": "who-approves-refunds",
    "store": "library-contract-store",
    "source": "docs/answers/who-approves-refunds.md",
    "summary": "Who approves refunds?",
    "text": (
        "<h2>Who approves refunds?</h2>"
        "<p>The payments team approves refunds over the daily limit. The code does not "
        "enforce an approval: it is a team practice.</p>"
    ),
    "as_of": "2026-10-19T09:00:00+00:00",
    "commit": WHO_APPROVES_COMMIT,
    "score": 1,
}
REFUNDS_TOPIC = {
    "layer": "topics",
    "id": "refunds",
    "store": "library-contract-store",
    "source": "docs/topics/refunds.md",
    # The title `config/topics.txt` gives the topic.
    "summary": "Refunds",
    "text": (
        "<h2>Refunds</h2>"
        "<p><b>A refund is the negative of the amount paid, computed in one place: the "
        f"<code>refund</code> function in {ALPHA}.</b> Every caller in this estate "
        "reaches it, either directly or through <code>RefundHandler</code>.</p>"
        "<h3>Mechanisms</h3>"
        "<ul>"
        f"<li><code>refund(amount)</code> in {ALPHA}'s <code>src/refund.ts</code> returns "
        "<code>-amount</code>. It holds no state and performs no checks.</li>"
        "<li><code>RefundHandler.handle(amount)</code> in the same file passes the amount "
        "straight to <code>refund</code>.</li>"
        f"<li>{BETA}'s <code>PaymentService.pay</code> in <code>src/pay.ts</code> imports "
        "<code>refund</code> from the <code>@example/alpha-lib</code> package, so a "
        f"payment in {BETA} is computed by the library rather than by its own code.</li>"
        f"<li>The refund schema in {ALPHA}, <code>schemas/refund.schema.json</code>, "
        "describes a refund request: a <code>requestedOn</code> date and a "
        "<code>total</code> that refers to the <code>money</code> definition.</li>"
        "</ul>"
        "<h3>Evidence</h3>"
        f"<p>DEMO-1 introduced the handler in {ALPHA} and the call from {BETA}. DEMO-2 "
        "added the schema.</p>"
        "<h3>What this is NOT</h3>"
        "<p>This is not a payment ledger. Nothing in the estate stores a refund, approves "
        "one or sends money: those questions need another source.</p>"
    ),
    "as_of": SOURCES_AS_OF,
    "commit": None,
    "score": 1,
}
ALPHA_DIVE = {
    "layer": "deep_dives",
    "id": "alpha-lib",
    "store": "library-contract-store",
    "source": "docs/deep-dives/alpha-lib.md",
    "summary": "Deep dive: alpha-lib",
    "text": (
        f"<h2>{ALPHA}</h2>"
        f"<p>Measured at {ALPHA} <code>{SHAS['alpha-lib'][:8]}</code>.</p>"
        f"<p><b>{ALPHA} is the estate's refund library.</b> It publishes the "
        f"<code>@example/alpha-lib</code> npm package, which {BETA} depends on, and it "
        "holds the refund schema.</p>"
        "<h3>What it holds</h3>"
        "<ul>"
        "<li><code>src/refund.ts</code>: the <code>refund</code> function and the "
        "<code>RefundHandler</code> class whose <code>handle</code> method calls it.</li>"
        "<li><code>schemas/refund.schema.json</code>: the Refund JSON Schema, with a "
        "<code>money</code> definition that the <code>total</code> property refers to "
        "and the <code>credit</code> definition extends.</li>"
        f"<li><code>package.json</code>: the package name, version 1.2.0 and a dependency "
        f"on {BETA}.</li>"
        "<li><code>index.ts</code>: the package's entry point, whose <code>currency</code> "
        "function names the currency, GBP.</li>"
        "</ul>"
        "<h3>History</h3>"
        "<p>Four commits on <code>main</code>. DEMO-1 added the handler and the package "
        "manifest on 1 October 2026; DEMO-2 added the schema the next day, DEMO-1 "
        "returned on 3 October with a note on rounding, and DEMO-7 added the dependency "
        f"on {BETA} and the entry point on 4 October. Nothing has changed it since.</p>"
        "<h3>Who depends on it</h3>"
        f"<p>{BETA} declares <code>@example/alpha-lib</code> in its "
        "<code>package.json</code> and imports <code>refund</code> in "
        "<code>src/pay.ts</code>. No other repository in the estate refers to it.</p>"
        "<h3>Limits</h3>"
        "<p>This dossier describes the code at the commit above and says nothing about "
        "how the package is released or who maintains it.</p>"
    ),
    "as_of": SOURCES_AS_OF,
    "commit": None,
    "score": 1,
}
# The store's own layer, kept in the relationships directory: written as JSON by
# hand, so its `html` is returned as written, and it records no date or commit.
WHO_OWNS_ALPHA_LIB = {
    "layer": "ownership_notes",
    "id": "who-owns-alpha-lib",
    "store": "library-contract-store",
    "source": "docs/answers/who-owns-alpha-lib.md",
    "summary": "Who owns alpha-lib?",
    "text": "<p>The platform team publishes alpha-lib for every web application.</p>",
    "as_of": None,
    "commit": None,
    "score": 1,
}

# DEMO-1 as `build_intent_index._ticket_record` writes it, as `ticket` returns it.
DEMO_1 = {
    "key": "DEMO-1",
    "proposed": False,
    "branches": [],
    # "refund rounding" is the longest; "refund handler" ties "pay via refund" on
    # length and comes first in history; two are kept.
    "description": ["refund rounding", "refund handler"],
    "text": {"subjects": ["pay via refund", "refund handler", "refund rounding"]},
    "first": "2026-10-01",
    "last": "2026-10-05",
    "repos": ["alpha-lib", "beta-app"],
    "commits": 3,
}
DEMO_2 = {
    "key": "DEMO-2",
    "proposed": False,
    "branches": [],
    "description": ["refund schema"],
    "text": {"subjects": ["refund schema"]},
    "first": "2026-10-02",
    "last": "2026-10-02",
    "repos": ["alpha-lib"],
    "commits": 1,
}
DEMO_3 = {
    "key": "DEMO-3",
    "proposed": False,
    "branches": [],
    "description": ["production values"],
    "text": {"subjects": ["ownership", "production values"]},
    "first": "2026-10-06",
    "last": "2026-10-08",
    "repos": ["beta-app", "delta-deploy"],
    "commits": 2,
}
DEMO_5 = {
    "key": "DEMO-5",
    "proposed": False,
    "branches": [],
    "description": ["first production values"],
    "text": {"subjects": ["first production values"]},
    "first": "2026-10-07",
    "last": "2026-10-07",
    "repos": ["delta-deploy"],
    "commits": 1,
}
# DEMO-7's one commit, on alpha-lib on 4 October: "alpha reads app constants" is
# over 12 characters, so it is both description and subject.
DEMO_7 = {
    "key": "DEMO-7",
    "proposed": False,
    "branches": [],
    "description": ["alpha reads app constants"],
    "text": {"subjects": ["alpha reads app constants"]},
    "first": "2026-10-04",
    "last": "2026-10-04",
    "repos": ["alpha-lib"],
    "commits": 1,
}
DEMO_4 = {
    "key": "DEMO-4",
    "proposed": True,
    "branches": ["feature/DEMO-4-retry"],
    "description": [],
    "text": {"subjects": ["retry"]},
    "first": "2026-10-07",
    "last": "2026-10-07",
    "repos": ["beta-app"],
    "commits": 1,
}


def absent(key: str) -> dict:
    """A key the tracker did not return, as `fetch-tickets` caches it."""
    return {
        "key": key,
        "state": "absent",
        "as_of": "2026-10-20",
        "text": {},
        "comments_truncated": False,
    }


def known(name: str) -> dict:
    """A repository the graph holds nodes for."""
    return {"answer": "in the estate", "exact": name, "matched_case_insensitively": False}


class TicketsContract(harness.Contract):
    def _store(self, layers_used: list[str]) -> dict[str, Any]:
        """The envelope every answer carries in this mode."""
        return {
            "id": "library-contract-store",
            "format": {"major": 1, "minor": 4},
            "library_version": "0.19.1",
            "sources_as_of": SOURCES_AS_OF,
            "index": "present" if self.INDEXED else "absent: graph read whole",
            "layers_used": sorted(layers_used),
            "notice": None,
        }

    def _ticket_files(self) -> list[str]:
        """The layer `ticket` reads a key's files from in this mode."""
        return ["index"] if self.INDEXED else ["intent"]

    def _repo_files(self) -> list[str]:
        """The layers `tickets` reads a repository's files from in this mode: the
        graph for the estate's names when there is no index."""
        return ["index"] if self.INDEXED else ["graph", "intent"]

    # --- tickets ------------------------------------------------------------------

    def test_tickets_for_a_file_list_the_landed_ticket(self):
        """Catches: the rows of another repository returned for alpha-lib, so its
        file's ticket is missing; a link's commit count not read, so the two
        DEMO-1 commits on `src/refund.ts` are shown as one; or `intent.reach` not
        read, so a landed list the library vouches for is flagged as doubtful."""
        found = tools_tickets.tickets(self.session, "alpha-lib", "src/refund.ts")
        self.assertEqual(
            found,
            {
                "store": self._store(self._repo_files()),
                "repository": known("alpha-lib"),
                "reach": True,
                "reach_note": None,
                "totals": {"landed": 1, "proposed": 0},
                "landed": [{"ticket": "DEMO-1", "path": "src/refund.ts", "commits": 2}],
                "proposed": [],
                "total": 1,
                "page": 1,
                "next_page": None,
            },
        )

    def test_tickets_for_a_repository_are_every_file_s_tickets_there(self):
        """Catches: `repo` not used to filter when no `path` is given, so alpha-lib's
        and delta-deploy's rows join beta-app's, or the repository's rows are lost.
        beta-app's commits are DEMO-1 on `package.json` and `src/pay.ts`, DEMO-3
        on its four ownership files, and DEMO-4 on `src/pay.ts` only on its
        branch. Each link is one commit, and items order by ticket, then path."""
        found = tools_tickets.tickets(self.session, "beta-app")
        self.assertEqual(found["repository"], known("beta-app"))
        self.assertEqual(
            found["landed"],
            [
                {"ticket": "DEMO-1", "path": "package.json", "commits": 1},
                {"ticket": "DEMO-1", "path": "src/pay.ts", "commits": 1},
                {"ticket": "DEMO-3", "path": ".github/CODEOWNERS", "commits": 1},
                {"ticket": "DEMO-3", "path": ".github/dependabot.yml", "commits": 1},
                {"ticket": "DEMO-3", "path": ".github/workflows/codeql.yml", "commits": 1},
                {"ticket": "DEMO-3", "path": "SECURITY.md", "commits": 1},
            ],
        )
        self.assertEqual(
            found["proposed"],
            [
                {
                    "ticket": "DEMO-4",
                    "path": "src/pay.ts",
                    "commits": 1,
                    "branches": ["feature/DEMO-4-retry"],
                }
            ],
        )
        self.assertEqual(found["totals"], {"landed": 6, "proposed": 1})

    def test_tickets_keep_a_branch_only_ticket_apart_with_its_branch(self):
        """Catches: the ticket layer's per-file `proposed` map, or the index's
        `landed` flag, not read, so DEMO-4 - only ever on beta-app's
        `feature/DEMO-4-retry` - is listed as landed or not at all; or its
        branches not read from the library's ticket-descriptions record."""
        found = tools_tickets.tickets(self.session, "beta-app", "src/pay.ts")
        self.assertEqual(
            found,
            {
                "store": self._store([*self._repo_files(), "ticket_descriptions"]),
                "repository": known("beta-app"),
                "reach": True,
                "reach_note": None,
                "totals": {"landed": 1, "proposed": 1},
                "landed": [{"ticket": "DEMO-1", "path": "src/pay.ts", "commits": 1}],
                "proposed": [
                    {
                        "ticket": "DEMO-4",
                        "path": "src/pay.ts",
                        "commits": 1,
                        "branches": ["feature/DEMO-4-retry"],
                    }
                ],
                "total": 2,
                "page": 1,
                "next_page": None,
            },
        )

    def test_tickets_page_both_lists_by_their_cursor(self):
        """Catches: `cursor` not followed over the two lists, so the branch-only
        DEMO-4 is lost or repeated on the second page, or a page's `totals` count
        only what it shows. Items order by ticket then path: DEMO-1 before DEMO-4."""
        found = harness.pages(
            lambda **paging: tools_tickets.tickets(self.session, "beta-app", "src/pay.ts", **paging)
        )
        self.assertEqual(
            [(page["landed"], page["proposed"]) for page in found],
            [
                ([{"ticket": "DEMO-1", "path": "src/pay.ts", "commits": 1}], []),
                (
                    [],
                    [
                        {
                            "ticket": "DEMO-4",
                            "path": "src/pay.ts",
                            "commits": 1,
                            "branches": ["feature/DEMO-4-retry"],
                        }
                    ],
                ),
            ],
        )
        self.assertEqual([page["totals"] for page in found], [{"landed": 1, "proposed": 1}] * 2)

    # --- ticket -------------------------------------------------------------------

    def test_ticket_pages_its_files_by_its_cursor(self):
        """Catches: `cursor` not followed over a ticket's files, so DEMO-3's seven
        files across beta-app and delta-deploy come back repeated, out of their
        repository-then-path order, or short of delta-deploy's."""
        found = harness.pages(
            lambda **paging: tools_tickets.ticket(self.session, "DEMO-3", **paging)
        )
        self.assertEqual(
            [(item["repository"], item["path"]) for page in found for item in page["landed"]],
            [
                ("beta-app", ".github/CODEOWNERS"),
                ("beta-app", ".github/dependabot.yml"),
                ("beta-app", ".github/workflows/codeql.yml"),
                ("beta-app", "SECURITY.md"),
                ("delta-deploy", "ansible/group_vars/prd/beta-app_values.yaml.j2"),
                ("delta-deploy", "config/application.yml"),
                ("delta-deploy", "deploy/signing.pem"),
            ],
        )
        self.assertEqual([page["page"] for page in found], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual({page["ticket"]["key"] for page in found}, {"DEMO-3"})

    def test_ticket_has_its_files_record_and_tracker_record(self):
        """Catches: a key's files read from the wrong layer or column, so DEMO-1
        is not shown on both repositories; the library's ticket-descriptions
        fields (`d`, `s`, `first`, `last`, `repos`, `n`) mis-read; or the
        tracker's cached record - title, status, resolution, description,
        comments - read from the wrong keys, or dated by anything but the day it
        resolved, since a resolved record carries no `checked` date."""
        found = tools_tickets.ticket(self.session, "DEMO-1")
        self.assertEqual(
            found,
            {
                "store": self._store(
                    [*self._ticket_files(), "ticket_descriptions", "ticket_tracker"]
                ),
                "ticket": DEMO_1,
                "tracker": {
                    "key": "DEMO-1",
                    "state": "fetched",
                    "as_of": "2026-10-05",
                    "text": {
                        "title": "Refund customers",
                        "status": "Done",
                        "resolution": "Fixed",
                        "description": "Let staff refund a payment.",
                        "comments": ["Refunds over the daily limit need a second approver."],
                    },
                    "comments_truncated": False,
                },
                "tracker_note": None,
                "reach": True,
                "reach_note": None,
                "totals": {"landed": 4, "proposed": 0},
                "landed": [
                    {"repository": "alpha-lib", "path": "package.json", "commits": 1},
                    {"repository": "alpha-lib", "path": "src/refund.ts", "commits": 2},
                    {"repository": "beta-app", "path": "package.json", "commits": 1},
                    {"repository": "beta-app", "path": "src/pay.ts", "commits": 1},
                ],
                "proposed": [],
                "total": 4,
                "page": 1,
                "next_page": None,
            },
        )

    def test_ticket_the_tracker_records_absent(self):
        """Catches: `fetch-tickets`' `absent` outcome not recognised, so DEMO-2 -
        which the tracker did not return - is reported as a record in a shape the
        server does not read, or as fetched with no text; or its `checked` date
        not read, so the answer does not say when the tracker was asked."""
        found = tools_tickets.ticket(self.session, "DEMO-2")
        self.assertEqual(found["ticket"], DEMO_2)
        self.assertEqual(found["tracker"], absent("DEMO-2"))
        self.assertIsNone(found["tracker_note"])
        self.assertEqual(
            found["landed"],
            [{"repository": "alpha-lib", "path": "schemas/refund.schema.json", "commits": 1}],
        )
        self.assertEqual(found["proposed"], [])

    def test_ticket_an_unresolved_tracker_record(self):
        """Catches: an unresolved record dated by anything but its `checked` day,
        text the stage dropped as empty (resolution, description) returned as
        found, or `comments_truncated` not read, so one comment of a thread of
        three reads as the whole thread; and a ticket's files in two repositories
        not all listed."""
        found = tools_tickets.ticket(self.session, "DEMO-3")
        self.assertEqual(
            found,
            {
                "store": self._store(
                    [*self._ticket_files(), "ticket_descriptions", "ticket_tracker"]
                ),
                "ticket": DEMO_3,
                "tracker": {
                    "key": "DEMO-3",
                    "state": "fetched",
                    "as_of": "2026-10-20",
                    "text": {
                        "title": "Ready beta-app for production",
                        "status": "In Progress",
                        "comments": ["The production values live in delta-deploy."],
                    },
                    "comments_truncated": True,
                },
                "tracker_note": None,
                "reach": True,
                "reach_note": None,
                "totals": {"landed": 7, "proposed": 0},
                "landed": [
                    {"repository": "beta-app", "path": ".github/CODEOWNERS", "commits": 1},
                    {"repository": "beta-app", "path": ".github/dependabot.yml", "commits": 1},
                    {
                        "repository": "beta-app",
                        "path": ".github/workflows/codeql.yml",
                        "commits": 1,
                    },
                    {"repository": "beta-app", "path": "SECURITY.md", "commits": 1},
                    {
                        "repository": "delta-deploy",
                        "path": "ansible/group_vars/prd/beta-app_values.yaml.j2",
                        "commits": 1,
                    },
                    {"repository": "delta-deploy", "path": "config/application.yml", "commits": 1},
                    {"repository": "delta-deploy", "path": "deploy/signing.pem", "commits": 1},
                ],
                "proposed": [],
                "total": 7,
                "page": 1,
                "next_page": None,
            },
        )

    def test_ticket_the_tracker_denied(self):
        """Catches: `fetch-tickets`' `denied` outcome - the tracker refused the
        request - not recognised, so DEMO-5 is reported as a record in a shape the
        server does not read, or as absent, which the stage treats as final when a
        denial is retried; or its `checked` date not read. DEMO-6 and DEMO-7 shared
        the refused request, so they are denied too."""
        for key in ("DEMO-6", "DEMO-7"):
            with self.subTest(key=key):
                self.assertEqual(
                    tools_tickets.ticket(self.session, key)["tracker"],
                    {
                        "key": key,
                        "state": "denied",
                        "as_of": "2026-10-20",
                        "text": {},
                        "comments_truncated": False,
                    },
                )
        found = tools_tickets.ticket(self.session, "DEMO-5")
        self.assertEqual(
            found,
            {
                "store": self._store(
                    [*self._ticket_files(), "ticket_descriptions", "ticket_tracker"]
                ),
                "ticket": DEMO_5,
                "tracker": {
                    "key": "DEMO-5",
                    "state": "denied",
                    "as_of": "2026-10-20",
                    "text": {},
                    "comments_truncated": False,
                },
                "tracker_note": None,
                "reach": True,
                "reach_note": None,
                "totals": {"landed": 1, "proposed": 0},
                "landed": [
                    {
                        "repository": "delta-deploy",
                        "path": "ansible/group_vars/prd/beta-app_values.yaml.j2",
                        "commits": 1,
                    }
                ],
                "proposed": [],
                "total": 1,
                "page": 1,
                "next_page": None,
            },
        )

    def test_a_ticket_lists_every_file_its_commit_landed(self):
        """Catches: a commit's second file lost from the ticket layer's or the
        index's rows, so DEMO-7 - one commit landing alpha-lib's `index.ts` and
        `package.json` - shows one file, or `index.ts` names no ticket."""
        found = tools_tickets.ticket(self.session, "DEMO-7")
        self.assertEqual(found["ticket"], DEMO_7)
        self.assertEqual(
            found["landed"],
            [
                {"repository": "alpha-lib", "path": "index.ts", "commits": 1},
                {"repository": "alpha-lib", "path": "package.json", "commits": 1},
            ],
        )
        self.assertEqual((found["proposed"], found["totals"]), ([], {"landed": 2, "proposed": 0}))
        by_file = tools_tickets.tickets(self.session, "alpha-lib", "index.ts")
        self.assertEqual(
            (by_file["landed"], by_file["proposed"]),
            ([{"ticket": "DEMO-7", "path": "index.ts", "commits": 1}], []),
        )

    def test_ticket_that_never_landed(self):
        """Catches: the library's `proposed` flag or `branches` not read from the
        ticket-descriptions record, so DEMO-4 reads as landed work or loses its
        branch; or the index's `landed` column, or the ticket layer's `proposed`
        section, misread, so its one file is listed as landed."""
        found = tools_tickets.ticket(self.session, "DEMO-4")
        self.assertEqual(
            found,
            {
                "store": self._store(
                    [*self._ticket_files(), "ticket_descriptions", "ticket_tracker"]
                ),
                "ticket": DEMO_4,
                "tracker": absent("DEMO-4"),
                "tracker_note": None,
                "reach": True,
                "reach_note": None,
                "totals": {"landed": 0, "proposed": 1},
                "landed": [],
                "proposed": [{"repository": "beta-app", "path": "src/pay.ts", "commits": 1}],
                "total": 1,
                "page": 1,
                "next_page": None,
            },
        )

    def test_a_key_no_layer_holds(self):
        """Catches: an unknown key answered with another ticket's files or records,
        or as anything but nothing, with what was searched."""
        found = tools_tickets.ticket(self.session, "DEMO-9")
        # The ticket layer is named as searched with or without the index.
        searched = "intent, ticket_descriptions and ticket_tracker layers"
        if self.INDEXED:
            searched = "index, " + searched
        self.assertEqual(
            found,
            {
                "store": self._store(
                    [*self._ticket_files(), "ticket_descriptions", "ticket_tracker"]
                ),
                "ticket": None,
                "tracker": None,
                "tracker_note": "the ticket_tracker layer holds no record of this ticket",
                "reach": True,
                "reach_note": None,
                "searched": f"searched 4 repositories in the {searched}",
                "totals": {"landed": 0, "proposed": 0},
                "landed": [],
                "proposed": [],
                "total": 0,
                "page": 1,
                "next_page": None,
            },
        )

    # --- answers ------------------------------------------------------------------

    def test_answers_from_every_library_answer_layer(self):
        """Catches: the library's topic briefs, deep dives or answers-layer file
        read from the wrong keys (`title`, `html`, `source`); the answers layer's
        `as_of` or `commit` not read, so the dated, uncommitted answer and the
        committed one lose their dates; or briefs and dives dated by anything but
        `sources_as_of`."""
        found = tools_tickets.answers(self.session, "refund")
        self.assertEqual(
            found,
            {
                "store": self._store(["answers", "deep_dives", "ownership_notes", "topics"]),
                "matches": 4,
                "items": [ALPHA_DIVE, HOW_REFUNDS_WORK, REFUNDS_TOPIC, WHO_APPROVES_REFUNDS],
                "total": 4,
                "page": 1,
                "next_page": None,
            },
        )

    def test_answers_limit_keeps_the_first_by_id_and_counts_every_match(self):
        """Catches: `limit` ignored, or applied before the rank, or `matches`
        counting only what was returned. Each of the four answers holding `refund`
        matches its one word, so they rank by id: `alpha-lib`, `how-refunds-work`,
        `refunds`, `who-approves-refunds`."""
        found = tools_tickets.answers(self.session, "refund", 2)
        self.assertEqual(found["matches"], 4)
        self.assertEqual(found["items"], [ALPHA_DIVE, HOW_REFUNDS_WORK])

    def test_answers_page_by_their_cursor(self):
        """Catches: `cursor` not followed over the ranked answers, so a page
        repeats an answer from another layer or drops one."""
        found = harness.pages(
            lambda **paging: tools_tickets.answers(self.session, "refund", **paging)
        )
        self.assertEqual(
            [page["items"] for page in found],
            [[ALPHA_DIVE], [HOW_REFUNDS_WORK], [REFUNDS_TOPIC], [WHO_APPROVES_REFUNDS]],
        )

    def test_an_answer_query_nothing_matches(self):
        """Catches: the `_metadata` block the library writes into the briefs and
        deep-dives files counted as an answer, so an empty answer overstates what
        was searched (5 answers: one brief, one dive, two written answers and one
        ownership note)."""
        found = tools_tickets.answers(self.session, "invoice")
        self.assertEqual(
            found,
            {
                "store": self._store(["answers", "deep_dives", "ownership_notes", "topics"]),
                "matches": 0,
                "searched": "searched 5 answers in the answers, deep_dives, ownership_notes "
                "and topics layers",
                "items": [],
                "total": 0,
                "page": 1,
                "next_page": None,
            },
        )

    def test_an_answer_keeps_its_fenced_code_and_mermaid_diagram_intact(self):
        """Catches: an answer's `html` reshaped on the way out - a fence's `<pre>`
        or language class stripped, its newline collapsed, its escaped `&gt;`
        unescaped into markup, or a repository name inside it linked - so a
        mermaid diagram no longer renders, or code reads as prose. A word only
        the diagram holds finds the answer: fences are searched like prose."""
        found = tools_tickets.answers(self.session, "flowchart")
        self.assertEqual(found["matches"], 1)
        self.assertEqual(found["items"], [HOW_REFUNDS_WORK])

    def test_answers_from_the_extra_layer_in_the_relationships_directory(self):
        """Catches: the store's extra layer kept beside the library's relationships
        files not read as answers, or read with a date it does not record."""
        self.assertEqual(EXTRA_LAYER, "knowledge/relationships/ownership-notes.json")
        found = tools_tickets.answers(self.session, "owns")
        self.assertEqual(found["matches"], 1)
        self.assertEqual(found["items"], [WHO_OWNS_ALPHA_LIB])


TicketsWithIndex, TicketsWithoutIndex = harness.both_modes(TicketsContract)


if __name__ == "__main__":
    unittest.main()
