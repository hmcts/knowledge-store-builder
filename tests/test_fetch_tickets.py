"""Tests for knowledgestore/fetch_tickets.py.

Every test drives the real stage and stubs only the HTTP boundary, through the
`opener` seam `main()` takes. Everything downstream of that seam - the
allow/deny/undecided partition, batching, the four per-ticket outcomes, the
redaction pass, the deterministic writer - is the production code, and the
assertions land on what it produced: the committed cache, the undecided report,
the printed summary, the requests the tracker actually saw.

Fixtures use invented ticket prefixes and the reserved `example.example` host.
`TOKEN` is a made-up string whose only job is to be searched for in everything
the stage emits.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path


from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import io as pio  # noqa: E402
from knowledgestore import fetch_tickets  # noqa: E402


BASE = "https://example.example/jira"
TOKEN = "not-a-real-token-8f3a2b"
TODAY = "2026-01-02"


def requested_keys(url: str) -> list[str]:
    """The keys a search URL asks for, read back out of its JQL."""
    jql = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["jql"][0]
    inside = jql[jql.index("(") + 1 : jql.rindex(")")]
    return [key.strip() for key in inside.split(",") if key.strip()]


def requested_fields(url: str) -> list[str]:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return query["fields"][0].split(",")


class FakeTracker:
    """A tracker at the HTTP boundary: it answers URLs and records them.

    `issues` maps a key to the `fields` object a real search response would
    carry. A key that is not in it is simply absent from the response, which is
    how a real search reports an issue that does not exist. `statuses` queues
    replies to give before the normal one, so a test can hand out a 429 then a
    200, or two 403s.
    """

    def __init__(self, issues: dict | None = None, statuses: list | None = None):
        self.issues = issues or {}
        self.statuses = list(statuses or [])
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def __call__(self, url: str, headers: dict) -> fetch_tickets.Response:
        self.urls.append(url)
        self.headers.append(dict(headers))
        if self.statuses:
            queued = self.statuses.pop(0)
            status, retry_after = queued if isinstance(queued, tuple) else (queued, None)
            return fetch_tickets.Response(status, "", retry_after)
        found = [
            {"key": key, "fields": self.issues[key]}
            for key in requested_keys(url)
            if key in self.issues
        ]
        return fetch_tickets.Response(200, json.dumps({"issues": found}))

    @property
    def keys_requested(self) -> list[str]:
        return [key for url in self.urls for key in requested_keys(url)]


def fields_for(summary: str, **extra) -> dict:
    """A minimal search-response `fields` object."""
    return {
        "summary": summary,
        "issuetype": {"name": "Story"},
        "status": {"name": "Done"},
        "resolution": {"name": "Fixed"},
        "created": "2024-03-04T09:00:00.000+0000",
        "resolutiondate": "2024-03-09T17:30:00.000+0000",
        **extra,
    }


class FetchTicketsTestCase(SettingsIsolated):
    """Shared setup: a store root, discovered tickets, tracker settings."""

    def store(self, tmp, discovered: list[str], **settings) -> Path:
        root = Path(tmp)
        config.configure(root=root)
        defaults = {
            "TRACKER_BASE_URL": BASE,
            "TRACKER_TOKEN": TOKEN,
            "TRACKER_PROJECTS": {"AAA"},
            "TRACKER_DENY": set(),
            "TRACKER_FETCH_DESCRIPTION": False,
            "TRACKER_FETCH_COMMENTS": False,
            "TRACKER_PAGE_SIZE": 100,
            "TRACKER_DELAY_SECONDS": 0.0,
        }
        config.configure(**{**defaults, **settings})
        pio.write_gzip_json(config.TICKET_DESCRIPTIONS_PATH, {key: {"d": []} for key in discovered})
        return root

    def run_stage(self, tracker, sleeps: list | None = None, today: str = TODAY):
        """Run the stage, capturing what it printed. Returns (code, output)."""
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fetch_tickets.main(
                [],
                opener=tracker,
                sleep=(sleeps.append if sleeps is not None else lambda _s: None),
                today=today,
            )
        return code, out.getvalue() + err.getvalue()

    def cache(self) -> dict:
        return pio.read_gzip_json_dict(config.TICKET_TRACKER_PATH)


class NotConfiguredTest(FetchTicketsTestCase):
    def test_unconfigured_run_exits_zero_and_writes_nothing(self):
        """Breaks if the stage becomes mandatory, or writes an empty artefact
        when it has no credentials. The pipeline is complete without this stage,
        so an unconfigured run must be a no-op that names what is missing - not
        a failure that stops a build, and not a committed empty cache that later
        reads as "the tracker knew nothing"."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1"], TRACKER_BASE_URL="", TRACKER_TOKEN="")
            tracker = FakeTracker()
            code, output = self.run_stage(tracker)
            written = sorted(
                p.name for p in (Path(tmp) / "knowledge" / "intent").iterdir() if p.is_file()
            )
        self.assertEqual(code, 0)
        self.assertEqual(tracker.urls, [], "an unconfigured stage must not call the tracker")
        self.assertEqual(written, ["ticket-descriptions.json.gz"], "no artefact may be written")
        self.assertIn("KSB_TRACKER_BASE_URL", output)
        self.assertIn("KSB_TRACKER_TOKEN", output)


class PrefixDecisionTest(FetchTicketsTestCase):
    def test_only_allowlisted_prefixes_are_requested(self):
        """Breaks if the allowlist stops bounding the requests - a stage that
        asks about every prefix it discovered reads projects this store was
        never authorised for."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                ["AAA-1", "BBB-2", "AAA-3"],
                TRACKER_PROJECTS={"AAA"},
                TRACKER_DENY={"BBB"},
            )
            tracker = FakeTracker({"AAA-1": fields_for("First"), "AAA-3": fields_for("Third")})
            code, _ = self.run_stage(tracker)
        self.assertEqual(code, 0)
        self.assertEqual(tracker.keys_requested, ["AAA-1", "AAA-3"])

    def test_denied_prefix_is_never_requested_even_when_also_allowlisted(self):
        """Breaks if deny stops winning over allow. A prefix is withdrawn by
        adding it to the deny list; if a stale allowlist entry could re-enable
        it, the withdrawal would be silently undone by the older setting."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                ["AAA-1", "BBB-2"],
                TRACKER_PROJECTS={"AAA", "BBB"},
                TRACKER_DENY={"BBB"},
            )
            tracker = FakeTracker({"AAA-1": fields_for("First")})
            code, output = self.run_stage(tracker)
        self.assertEqual(code, 0)
        self.assertEqual(tracker.keys_requested, ["AAA-1"])
        self.assertNotIn("BBB-2", str(tracker.urls))
        self.assertNotIn("BBB", self.cache())
        self.assertIn("BBB", output, "a withheld prefix must still be accounted for")

    def test_undecided_prefixes_are_reported_and_not_requested(self):
        """Breaks if an unknown prefix is silently skipped, or silently fetched.

        Failing closed on unknowns discards tickets while reporting nothing, and
        failing open reads projects nobody authorised. The third state is the
        product: not requested, written to a file with its ticket count, and the
        path named in the summary so a person can decide."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                ["AAA-1", "CCC-7", "CCC-8", "DDD-9"],
                TRACKER_PROJECTS={"AAA"},
            )
            tracker = FakeTracker({"AAA-1": fields_for("First")})
            code, output = self.run_stage(tracker)
            undecided = json.loads(config.TRACKER_UNDECIDED_PATH.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(tracker.keys_requested, ["AAA-1"])
        self.assertEqual(undecided["undecided_prefixes"], {"CCC": 2, "DDD": 1})
        self.assertEqual(undecided["tickets"], 3)
        self.assertIn("tracker-undecided.json", output)
        self.assertIn("undecided prefixes: 2", output)


class BatchingTest(FetchTicketsTestCase):
    def test_keys_are_batched_at_the_page_size(self):
        """Breaks if the stage returns to one request per ticket. On a large
        estate that is tens of thousands of calls instead of hundreds, which
        breaches rate limits and looks like an attack in the access log."""
        keys = [f"AAA-{n}" for n in range(1, 8)]
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, keys, TRACKER_PAGE_SIZE=3)
            tracker = FakeTracker({key: fields_for(f"Summary {key}") for key in keys})
            code, _ = self.run_stage(tracker)
            cached = self.cache()
        self.assertEqual(code, 0)
        self.assertEqual(len(tracker.urls), 3, "7 keys at 3 per page is 3 requests")
        self.assertEqual(
            [requested_keys(url) for url in tracker.urls],
            [["AAA-1", "AAA-2", "AAA-3"], ["AAA-4", "AAA-5", "AAA-6"], ["AAA-7"]],
            "sorted pages, so two runs request in the same order",
        )
        self.assertEqual(len(cached), 7)

    def test_pages_follow_sorted_order_whatever_order_tickets_were_discovered(self):
        """Breaks if the request order follows the discovered mapping's own
        order. Two runs on the same estate would then ask for different pages,
        so which tickets an interrupted run had cached would depend on mapping
        order rather than on how far it got."""
        discovered = ["AAA-3", "AAA-20", "AAA-1", "AAA-2"]
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, discovered, TRACKER_PAGE_SIZE=2)
            tracker = FakeTracker({key: fields_for(key) for key in discovered})
            self.run_stage(tracker)
        self.assertEqual(
            [requested_keys(url) for url in tracker.urls],
            [["AAA-1", "AAA-2"], ["AAA-20", "AAA-3"]],
        )

    def test_a_page_is_written_before_the_next_is_requested(self):
        """Breaks if the cache is only written at the end of the run. A
        throttled or cancelled run must leave a usable cache and resume where it
        stopped; writing once at the end throws away everything a long run
        fetched before it was interrupted."""
        seen: list[int] = []

        def watching_opener(url, headers):
            seen.append(len(pio.read_gzip_json_dict(config.TICKET_TRACKER_PATH)))
            return FakeTracker({k: fields_for(k) for k in requested_keys(url)})(url, headers)

        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1", "AAA-2", "AAA-3"], TRACKER_PAGE_SIZE=1)
            code, _ = self.run_stage(watching_opener)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [0, 1, 2], "each page is on disk before the next is asked for")


class OutcomeTest(FetchTicketsTestCase):
    def test_absent_key_is_cached_as_absent_and_never_requested_again(self):
        """Breaks if a ticket missing from the response is left uncached. A
        historic ticket that does not exist never starts existing, so re-asking
        every run costs a request per run forever."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1", "AAA-2"])
            first = FakeTracker({"AAA-1": fields_for("First")})
            _, first_output = self.run_stage(first)
            cached = self.cache()
            second = FakeTracker({"AAA-1": fields_for("First")})
            code, output = self.run_stage(second)
        self.assertEqual(cached["AAA-2"], {"absent": True, "checked": TODAY})
        self.assertIn("1 absent", first_output)
        self.assertEqual(code, 0)
        self.assertEqual(second.urls, [], "nothing outstanding, so no request at all")
        self.assertIn("2 already cached", output)

    def test_denied_tickets_are_cached_as_denied_and_requested_again_next_run(self):
        """Breaks if a 401 or a 403 is recorded as absence. A token carries one
        person's permissions, so a denial says what this operator could read -
        not what exists. Cached as absence, a later operator with broader access
        would never retry it and the gap would be permanent and invisible."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1", "AAA-2"], TRACKER_PAGE_SIZE=1)
            first = FakeTracker({}, statuses=[401, 403])
            _, first_output = self.run_stage(first)
            denied = self.cache()
            second = FakeTracker({"AAA-1": fields_for("First"), "AAA-2": fields_for("Second")})
            code, output = self.run_stage(second)
            after = self.cache()
        self.assertEqual(denied["AAA-1"], {"denied": True, "checked": TODAY})
        self.assertEqual(denied["AAA-2"], {"denied": True, "checked": TODAY})
        self.assertIn("2 denied", first_output)
        self.assertIn("waiting on access", first_output)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(second.keys_requested), ["AAA-1", "AAA-2"], "denial is retried")
        self.assertEqual(after["AAA-1"]["summary"], "First")
        self.assertNotIn("denied", after["AAA-1"])

    def test_server_error_is_not_cached_at_all(self):
        """Breaks if a 5xx or a dropped connection is recorded as an outcome. A
        tracker that was briefly unwell has said nothing about the ticket, and
        caching that silence would turn an outage into missing knowledge."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1", "AAA-2"], TRACKER_PAGE_SIZE=1)
            tracker = FakeTracker({"AAA-2": fields_for("Second")}, statuses=[503])
            code, output = self.run_stage(tracker)
            cached = self.cache()
        self.assertEqual(code, 0)
        self.assertNotIn("AAA-1", cached, "an unwell tracker must not be quoted")
        self.assertEqual(cached["AAA-2"]["summary"], "Second")
        self.assertIn("1 failed", output)
        self.assertIn("503", output)

    def test_a_reply_that_is_not_a_search_response_is_not_cached(self):
        """Breaks if a 200 whose body is not a search response is read as an
        empty result set - every ticket in the page would be cached as absent
        and never asked about again. An authenticating proxy answering 200 with
        a sign-in page is the realistic source: the status says success and the
        body says nothing about any ticket."""

        def sign_in_page(url, headers):
            return fetch_tickets.Response(200, "<html>Please sign in</html>")

        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1"])
            code, output = self.run_stage(sign_in_page)
            cached = self.cache()
        self.assertEqual(code, 0)
        self.assertEqual(cached, {}, "a reply that names no ticket is not an answer")
        self.assertIn("1 failed", output)

    def test_transport_failure_is_not_cached(self):
        """Breaks if a connection error is treated as an answer. Same reasoning
        as a 5xx, by a different route: an OSError from the opener carries no
        statement about the ticket."""

        def broken(url, headers):
            raise OSError("connection refused")

        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1"])
            code, output = self.run_stage(broken)
            cached = self.cache()
        self.assertEqual(code, 0)
        self.assertEqual(cached, {})
        self.assertIn("1 failed", output)


class RateLimitTest(FetchTicketsTestCase):
    def test_retry_after_is_honoured_before_the_page_is_retried(self):
        """Breaks if a 429 is treated as a failure, or retried immediately.
        Ignoring the wait the tracker asked for is how a client gets blocked
        outright, and treating the throttle as a failure discards a page the
        tracker was willing to serve a moment later."""
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1"], TRACKER_DELAY_SECONDS=0.25)
            tracker = FakeTracker({"AAA-1": fields_for("First")}, statuses=[(429, "7")])
            code, output = self.run_stage(tracker, sleeps=sleeps)
            cached = self.cache()
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [7.0], "the tracker's own Retry-After, not the page delay")
        self.assertEqual(len(tracker.urls), 2, "the page is retried, not abandoned")
        self.assertEqual(cached["AAA-1"]["summary"], "First")
        self.assertIn("1 fetched", output)

    def test_pages_are_separated_by_the_configured_delay(self):
        """Breaks if the delay between pages disappears. One request in flight
        is not by itself polite: a first run over a large estate with no pause
        is a burst that gets a client throttled or noticed."""
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1", "AAA-2", "AAA-3"], TRACKER_PAGE_SIZE=1)
            config.configure(TRACKER_DELAY_SECONDS=0.5)
            tracker = FakeTracker({f"AAA-{n}": fields_for(str(n)) for n in (1, 2, 3)})
            self.run_stage(tracker, sleeps=sleeps)
        self.assertEqual(sleeps, [0.5, 0.5], "between pages, not before the first")


class ProjectionTest(FetchTicketsTestCase):
    def test_description_and_comments_are_absent_from_the_request_unless_enabled(self):
        """Breaks if narrative text is requested by default, or trimmed locally
        instead of never being asked for. A response that never carried a
        description cannot leak one; a response that carried it and had it
        dropped is a weaker guarantee, and the difference is visible only in the
        request."""
        default = FakeTracker({"AAA-1": fields_for("First")})
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1"])
            self.run_stage(default)

        asked = FakeTracker({"AAA-1": fields_for("First")})
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1"], TRACKER_FETCH_DESCRIPTION=True, TRACKER_FETCH_COMMENTS=True)
            self.run_stage(asked)

        self.assertEqual(
            requested_fields(default.urls[0]),
            [
                "summary",
                "issuetype",
                "status",
                "resolution",
                "created",
                "resolutiondate",
                "parent",
            ],
        )
        self.assertNotIn("description", requested_fields(default.urls[0]))
        self.assertNotIn("comment", requested_fields(default.urls[0]))
        self.assertEqual(requested_fields(asked.urls[0])[-2:], ["description", "comment"])

    def test_narrative_is_stored_only_when_it_was_requested(self):
        """Breaks if a tracker that volunteers a description gets it stored
        anyway. The store's contents must follow the setting, not the server."""
        volunteered = fields_for(
            "First",
            description="Long narrative nobody asked for",
            comment={"comments": [{"body": "A comment nobody asked for"}]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1"])
            self.run_stage(FakeTracker({"AAA-1": volunteered}))
            record = self.cache()["AAA-1"]
        self.assertNotIn("description", record)
        self.assertNotIn("comments", record)

    def test_projected_fields_are_stored_in_reduced_form(self):
        """Breaks if the record shape changes. Consumers read these keys, and a
        renamed field is a change to their data, not a refactor."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1"], TRACKER_FETCH_DESCRIPTION=True, TRACKER_FETCH_COMMENTS=True)
            fields = fields_for(
                "Amend the hearing outcome",
                parent={"key": "AAA-900"},
                description="Some narrative",
                comment={"comments": [{"body": "First note"}, {"body": "  "}]},
            )
            self.run_stage(FakeTracker({"AAA-1": fields}))
            record = self.cache()["AAA-1"]
        self.assertEqual(
            record,
            {
                "comments": ["First note"],
                "created": "2024-03-04",
                "description": "Some narrative",
                "parent": "AAA-900",
                "resolution": "Fixed",
                "resolved": "2024-03-09",
                "status": "Done",
                "summary": "Amend the hearing outcome",
                "type": "Story",
            },
        )

    def test_an_open_ticket_stores_no_empty_fields(self):
        """Breaks if fields the tracker had nothing for are stored as empty. An
        unresolved ticket has no resolution and no resolution date, and a key
        present but empty reads as evidence that was found rather than evidence
        that does not exist - the same rule the intent stage follows."""
        open_ticket = {
            "summary": "Still open",
            "issuetype": {"name": "Bug"},
            "status": {"name": "In Progress"},
            "resolution": None,
            "created": "2025-01-05T08:00:00.000+0000",
            "resolutiondate": None,
            "parent": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1"])
            self.run_stage(FakeTracker({"AAA-1": open_ticket}))
            record = self.cache()["AAA-1"]
        self.assertEqual(
            record,
            {
                "created": "2025-01-05",
                "status": "In Progress",
                "summary": "Still open",
                "type": "Bug",
            },
        )


class RedactionTest(FetchTicketsTestCase):
    def test_fetched_text_passes_through_redaction(self):
        """Breaks if fetched text bypasses the redaction the same store applies
        to mined commit text. Tracker text is republished by the store exactly
        as commit text is, so it does not earn a weaker filter for being
        nominally cleaner - and the count has to be reported, because a silent
        filter cannot be told from an estate with nothing to withhold."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1"], TRACKER_FETCH_DESCRIPTION=True, TRACKER_FETCH_COMMENTS=True)
            fields = fields_for(
                "Raised by xxxxx@xxxxx.example",
                description="Chase yyyyy@yyyyy.example about it",
                comment={"comments": [{"body": "cc zzzzz@zzzzz.example"}]},
            )
            code, output = self.run_stage(FakeTracker({"AAA-1": fields}))
            record = self.cache()["AAA-1"]
            raw = config.TICKET_TRACKER_PATH.read_bytes()
        self.assertEqual(code, 0)
        self.assertEqual(record["summary"], "Raised by [email address withheld]")
        self.assertEqual(record["description"], "Chase [email address withheld] about it")
        self.assertEqual(record["comments"], ["cc [email address withheld]"])
        self.assertNotIn(b"xxxxx@xxxxx.example", raw)
        self.assertIn("redacted 3 identifiers", output)
        self.assertIn("email-address (3)", output)


class SummaryTest(FetchTicketsTestCase):
    def test_summary_reports_every_outcome_and_the_redaction_count(self):
        """Breaks if an outcome stops being reported. A run whose denials or
        failures are invisible looks like a complete one, and the operator has
        no way to know a project is waiting on access they do not have."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                ["AAA-1", "AAA-2", "AAA-3", "AAA-4", "CCC-5"],
                TRACKER_PAGE_SIZE=1,
            )
            # Queued replies are consumed in request order, one key per page:
            # AAA-1 is denied, AAA-2 fails, AAA-3 is answered, AAA-4 is not in
            # the response and so is absent. CCC-5 is undecided.
            tracker = FakeTracker(
                {"AAA-3": fields_for("Raised by xxxxx@xxxxx.example")},
                statuses=[403, 500],
            )
            code, output = self.run_stage(tracker)
        self.assertEqual(code, 0)
        for expected in (
            "1 fetched",
            "1 absent",
            "1 denied",
            "1 failed",
            "redacted 1 identifiers",
            "undecided prefixes: 1",
            "ticket-tracker.json.gz",
        ):
            self.assertIn(expected, output, expected)


class DeterminismTest(FetchTicketsTestCase):
    def test_two_runs_on_the_same_inputs_are_byte_identical(self):
        """Breaks if the cache stops being written deterministically - an
        unsorted mapping, or Python's default gzip header carrying the time and
        the filename. A committed artefact that churns on every rebuild defeats
        the diff that tells an operator what actually changed."""
        keys = ["AAA-3", "AAA-1", "AAA-20", "AAA-2"]
        issues = {key: fields_for(f"Summary {key}") for key in keys}
        outputs = []
        cached: list[list[str]] = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                self.store(tmp, keys, TRACKER_PAGE_SIZE=2)
                self.run_stage(FakeTracker(dict(issues)))
                outputs.append(
                    (
                        config.TICKET_TRACKER_PATH.read_bytes(),
                        config.TRACKER_UNDECIDED_PATH.read_bytes(),
                    )
                )
                cached.append(list(self.cache()))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(cached[0], ["AAA-1", "AAA-2", "AAA-20", "AAA-3"], "keys sorted on write")

    def test_a_ticket_added_later_is_still_written_in_sorted_order(self):
        """Breaks if the cache is written in insertion order. A second run
        appends to a cache loaded from disk, so a ticket that sorts before the
        existing ones lands last unless the writer sorts - and the committed
        artefact then reorders itself during some later unrelated rebuild,
        producing a diff nobody can attribute to a change."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-9"])
            self.run_stage(FakeTracker({"AAA-9": fields_for("Ninth")}))
            pio.write_gzip_json(
                config.TICKET_DESCRIPTIONS_PATH, {"AAA-9": {"d": []}, "AAA-1": {"d": []}}
            )
            self.run_stage(FakeTracker({"AAA-1": fields_for("First")}))
            order = list(self.cache())
        self.assertEqual(order, ["AAA-1", "AAA-9"])


class CredentialTest(FetchTicketsTestCase):
    """The token appears in exactly one place: the Authorization header.

    Not in an artefact, not in a summary line, not in an error message, not in
    the User-Agent, not in a URL. A build log and a committed file both outlive
    the run and reach a wider audience than the credential was issued to, so
    this is the test that would fail if the token ever reached either.
    """

    def test_credential_reaches_the_request_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, ["AAA-1", "AAA-2", "CCC-3"], TRACKER_PAGE_SIZE=1)
            tracker = FakeTracker({"AAA-1": fields_for("First")}, statuses=[500])
            code, output = self.run_stage(tracker)
            artefacts = (
                config.TICKET_TRACKER_PATH.read_bytes() + config.TRACKER_UNDECIDED_PATH.read_bytes()
            )

            # The header is the one legitimate carrier - assert it, so the rest
            # of this test cannot pass by the token never being sent at all.
            self.assertEqual(tracker.headers[0]["Authorization"], f"Bearer {TOKEN}")
            self.assertNotIn(TOKEN, tracker.headers[0]["User-Agent"])
            self.assertNotIn(TOKEN, "".join(tracker.urls))
            self.assertEqual(code, 0)
            self.assertNotIn(TOKEN, output)
            self.assertNotIn(TOKEN.encode(), artefacts)

            # A raised message must not carry it either, whatever went wrong.
            config.configure(TRACKER_BASE_URL=f"ftp://{TOKEN}@example.example/jira")
            _, refused = self.run_stage(FakeTracker())
            self.assertNotIn(TOKEN, refused)
            with self.assertRaises(ValueError) as raised:
                fetch_tickets.tracker_root(f"ftp://{TOKEN}@example.example/jira")
            self.assertNotIn(TOKEN, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
