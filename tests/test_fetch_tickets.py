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
import os
from collections import Counter
import tempfile
import unittest
import unittest.mock
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path


from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import io as pio  # noqa: E402
from knowledgestore import fetch_tickets  # noqa: E402
from knowledgestore import twg_transport  # noqa: E402


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
            {
                "expand": "operations,versionedRepresentations,editmeta,changelog",
                "id": str(10000 + number),
                "self": f"{BASE}/rest/api/2/issue/{10000 + number}",
                "key": key,
                "fields": self.issues[key],
            }
            for number, key in enumerate(requested_keys(url))
            if key in self.issues
        ]
        # The Data Center search envelope: paging counters beside the issues.
        body = {
            "expand": "schema,names",
            "startAt": 0,
            "maxResults": len(requested_keys(url)),
            "total": len(found),
            "issues": found,
        }
        return fetch_tickets.Response(200, json.dumps(body))

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

    def _store(self, tmp, discovered: list[str], **settings) -> Path:
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

    def _run_stage(self, tracker, sleeps: list | None = None, today: str = TODAY):
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

    def _cache(self) -> dict:
        return pio.read_gzip_json_dict(config.TICKET_TRACKER_PATH)


class NotConfiguredTest(FetchTicketsTestCase):
    def test_unconfigured_run_exits_zero_and_writes_nothing(self):
        """Breaks if the stage becomes mandatory, or writes an empty artefact
        when it has no credentials. The pipeline is complete without this stage,
        so an unconfigured run must be a no-op that names what is missing - not
        a failure that stops a build, and not a committed empty cache that later
        reads as "the tracker knew nothing"."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"], TRACKER_BASE_URL="", TRACKER_TOKEN="")
            tracker = FakeTracker()
            code, output = self._run_stage(tracker)
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
            self._store(
                tmp,
                ["AAA-1", "BBB-2", "AAA-3"],
                TRACKER_PROJECTS={"AAA"},
                TRACKER_DENY={"BBB"},
            )
            tracker = FakeTracker({"AAA-1": fields_for("First"), "AAA-3": fields_for("Third")})
            code, _ = self._run_stage(tracker)
        self.assertEqual(code, 0)
        self.assertEqual(tracker.keys_requested, ["AAA-1", "AAA-3"])

    def test_denied_prefix_is_never_requested_even_when_also_allowlisted(self):
        """Breaks if deny stops winning over allow. A prefix is withdrawn by
        adding it to the deny list; if a stale allowlist entry could re-enable
        it, the withdrawal would be silently undone by the older setting."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(
                tmp,
                ["AAA-1", "BBB-2"],
                TRACKER_PROJECTS={"AAA", "BBB"},
                TRACKER_DENY={"BBB"},
            )
            tracker = FakeTracker({"AAA-1": fields_for("First")})
            code, output = self._run_stage(tracker)
        self.assertEqual(code, 0)
        self.assertEqual(tracker.keys_requested, ["AAA-1"])
        self.assertNotIn("BBB-2", str(tracker.urls))
        self.assertNotIn("BBB", self._cache())
        self.assertIn("BBB", output, "a withheld prefix must still be accounted for")

    def test_undecided_prefixes_are_reported_and_not_requested(self):
        """Breaks if an unknown prefix is silently skipped, or silently fetched.

        Failing closed on unknowns discards tickets while reporting nothing, and
        failing open reads projects nobody authorised. The third state is the
        product: not requested, written to a file with its ticket count, and the
        path named in the summary so a person can decide."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(
                tmp,
                ["AAA-1", "CCC-7", "CCC-8", "DDD-9"],
                TRACKER_PROJECTS={"AAA"},
            )
            tracker = FakeTracker({"AAA-1": fields_for("First")})
            code, output = self._run_stage(tracker)
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
            self._store(tmp, keys, TRACKER_PAGE_SIZE=3)
            tracker = FakeTracker({key: fields_for(f"Summary {key}") for key in keys})
            code, _ = self._run_stage(tracker)
            cached = self._cache()
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
            self._store(tmp, discovered, TRACKER_PAGE_SIZE=2)
            tracker = FakeTracker({key: fields_for(key) for key in discovered})
            self._run_stage(tracker)
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
            self._store(tmp, ["AAA-1", "AAA-2", "AAA-3"], TRACKER_PAGE_SIZE=1)
            code, _ = self._run_stage(watching_opener)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [0, 1, 2], "each page is on disk before the next is asked for")


class OutcomeTest(FetchTicketsTestCase):
    def test_absent_key_is_cached_as_absent_and_never_requested_again(self):
        """Breaks if a ticket missing from the response is left uncached. A
        historic ticket that does not exist never starts existing, so re-asking
        every run costs a request per run forever."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1", "AAA-2"])
            first = FakeTracker({"AAA-1": fields_for("First")})
            _, first_output = self._run_stage(first)
            cached = self._cache()
            second = FakeTracker({"AAA-1": fields_for("First")})
            code, output = self._run_stage(second)
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
            self._store(tmp, ["AAA-1", "AAA-2"], TRACKER_PAGE_SIZE=1)
            first = FakeTracker({}, statuses=[401, 403])
            _, first_output = self._run_stage(first)
            denied = self._cache()
            second = FakeTracker({"AAA-1": fields_for("First"), "AAA-2": fields_for("Second")})
            code, output = self._run_stage(second)
            after = self._cache()
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
            self._store(tmp, ["AAA-1", "AAA-2"], TRACKER_PAGE_SIZE=1)
            tracker = FakeTracker({"AAA-2": fields_for("Second")}, statuses=[503])
            code, output = self._run_stage(tracker)
            cached = self._cache()
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
            self._store(tmp, ["AAA-1"])
            code, output = self._run_stage(sign_in_page)
            cached = self._cache()
        self.assertEqual(code, 0)
        self.assertEqual(cached, {}, "a reply that names no ticket is not an answer")
        self.assertIn("1 failed", output)

    def test_a_missing_search_endpoint_says_which_jira_this_stage_speaks(self):
        """Breaks if a 404 or 410 from the search endpoint is reported as a bare
        status. That reply is what Jira Cloud gives to the Data Center search
        path, and an operator who reads only "404" will debug the URL or the
        token rather than learn the stage speaks one Jira flavour."""
        for status in (404, 410):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                self._store(tmp, ["AAA-1"])
                code, output = self._run_stage(FakeTracker(statuses=[status]))
                cached = self._cache()
            self.assertEqual(code, 0)
            self.assertEqual(cached, {})
            self.assertIn(f"{status} (1 tickets)", output)
            self.assertIn("this is a Jira Cloud deployment", output)
            self.assertIn("Jira Data Center or Server", output)

    def test_other_statuses_do_not_claim_the_tracker_is_jira_cloud(self):
        """Breaks if the Jira Cloud hint is attached to every failure. A 503 is
        an unwell tracker, and naming a flavour mismatch would send the operator
        the wrong way."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"])
            _code, output = self._run_stage(FakeTracker(statuses=[503]))
        self.assertNotIn("Jira Cloud", output)

    def test_transport_failure_is_not_cached(self):
        """Breaks if a connection error is treated as an answer. Same reasoning
        as a 5xx, by a different route: an OSError from the opener carries no
        statement about the ticket."""

        def broken(url, headers):
            raise OSError("connection refused")

        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"])
            code, output = self._run_stage(broken)
            cached = self._cache()
        self.assertEqual(code, 0)
        self.assertEqual(cached, {})
        self.assertIn("1 failed", output)


def open_fields(summary: str, status: str = "In Progress") -> dict:
    """A search-response `fields` object for a ticket nobody has resolved."""
    return {
        "summary": summary,
        "issuetype": {"name": "Bug"},
        "status": {"name": status},
        "resolution": None,
        "created": "2025-01-05T08:00:00.000+0000",
        "resolutiondate": None,
    }


# Seven days after TODAY, the shipped default interval, and the day before it.
A_WEEK_LATER = "2026-01-09"
SIX_DAYS_LATER = "2026-01-08"


class RefreshTest(FetchTicketsTestCase):
    """An open ticket's record is asked about again once it is old enough (#421).

    A resolved ticket is history and is never re-requested. An unresolved one is
    still changing, so a record fetched while it was open freezes its status,
    description and comments at that moment unless a later run asks again.
    """

    def test_an_open_ticket_is_fetched_again_once_its_record_is_a_week_old(self):
        """Breaks if a fetched record is final again, which is the defect: the
        ticket resolved in the tracker and the store still says In Progress,
        for ever. Also pins the shipped seven-day default, since this test
        configures no interval."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"])
            self._run_stage(FakeTracker({"AAA-1": open_fields("Still open")}))
            first = self._cache()["AAA-1"]
            later = FakeTracker({"AAA-1": fields_for("Now finished")})
            code, output = self._run_stage(later, today=A_WEEK_LATER)
            record = self._cache()["AAA-1"]
        self.assertEqual(first["checked"], TODAY, "an open record carries the day it was read")
        self.assertEqual(code, 0)
        self.assertEqual(later.keys_requested, ["AAA-1"])
        self.assertEqual(
            record,
            {
                "created": "2024-03-04",
                "resolution": "Fixed",
                "resolved": "2024-03-09",
                "status": "Done",
                "summary": "Now finished",
                "type": "Story",
            },
            "a resolved record is final, so it carries no checked date",
        )
        self.assertIn("refreshed 1 of 1 due, with 0 left for later runs, in 1 requests", output)
        self.assertIn("Tracker: 0 fetched, 1 refreshed,", output, "a refresh is not a new fetch")

    def test_an_open_ticket_is_not_fetched_again_inside_the_interval(self):
        """Breaks if the minimum age is ignored, so every run re-requests every
        open ticket - on a large estate that is most of a first run's traffic
        again on every build, for answers that rarely changed overnight."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"])
            self._run_stage(FakeTracker({"AAA-1": open_fields("Still open")}))
            later = FakeTracker({"AAA-1": fields_for("Now finished")})
            code, output = self._run_stage(later, today=SIX_DAYS_LATER)
            record = self._cache()["AAA-1"]
        self.assertEqual(code, 0)
        self.assertEqual(later.urls, [])
        self.assertEqual(record["summary"], "Still open")
        self.assertIn("refreshed 0 of 0 due", output, "reported at zero too")

    def test_a_resolved_ticket_is_never_fetched_again(self):
        """Breaks if the refresh stops telling resolved from unresolved and
        re-requests the whole cache once it ages - historic tickets do not
        change, and a refresh of every one of them is a first run every week."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"])
            self._run_stage(FakeTracker({"AAA-1": fields_for("Long finished")}))
            later = FakeTracker({"AAA-1": fields_for("Edited since")})
            self._run_stage(later, today="2027-06-01")
            record = self._cache()["AAA-1"]
        self.assertEqual(later.urls, [])
        self.assertEqual(record["summary"], "Long finished")
        self.assertNotIn("checked", record)

    def test_a_ticket_resolved_without_a_resolution_name_is_still_resolved(self):
        """Breaks if only the resolution name counts. A workflow can set the
        resolution date without a named resolution, and that ticket is as
        finished as any other."""
        dated = {**open_fields("Closed by date"), "resolutiondate": "2024-03-09T17:30:00.000+0000"}
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"])
            self._run_stage(FakeTracker({"AAA-1": dated}))
            later = FakeTracker({"AAA-1": dated})
            self._run_stage(later, today="2027-06-01")
        self.assertEqual(later.urls, [])

    def test_the_interval_is_read_from_its_setting(self):
        """Breaks if `KSB_TRACKER_REFRESH_DAYS` is not what decides the age - an
        estate that wants daily status could not have it, and one on a strict
        rate limit could not lengthen it."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"], TRACKER_REFRESH_DAYS=1)
            self._run_stage(FakeTracker({"AAA-1": open_fields("Still open")}))
            later = FakeTracker({"AAA-1": open_fields("Still open", status="In Review")})
            self._run_stage(later, today="2026-01-03")
            record = self._cache()["AAA-1"]
        self.assertEqual(later.keys_requested, ["AAA-1"])
        self.assertEqual(record["status"], "In Review")
        self.assertEqual(record["checked"], "2026-01-03", "the date moves with the refresh")

    def test_an_open_record_with_no_checked_date_is_due(self):
        """Breaks if a record written before refresh existed counts as fresh.
        Every open record in a cache committed by an older release has no date,
        and treating "no date" as "just read" would leave exactly those records
        frozen - the defect, surviving the fix."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"])
            pio.write_gzip_json(
                config.TICKET_TRACKER_PATH, {"AAA-1": {"status": "Open", "summary": "Old"}}
            )
            tracker = FakeTracker({"AAA-1": fields_for("Now finished")})
            self._run_stage(tracker)
            record = self._cache()["AAA-1"]
        self.assertEqual(tracker.keys_requested, ["AAA-1"])
        self.assertEqual(record["summary"], "Now finished")

    def test_a_refresh_the_tracker_does_not_answer_keeps_the_earlier_record(self):
        """Breaks if a refresh can downgrade what the store knows. A search
        omits tickets the token cannot browse, so a run by someone with narrower
        access sees a fetched ticket as missing; cached as absent, it would never
        be asked about again and its text would be gone. A denial is the same
        loss by another route."""
        for name, later in (
            ("missing from the reply", FakeTracker({})),
            ("denied", FakeTracker({}, statuses=[403])),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                self._store(tmp, ["AAA-1"])
                self._run_stage(FakeTracker({"AAA-1": open_fields("Still open")}))
                before = self._cache()["AAA-1"]
                code, output = self._run_stage(later, today=A_WEEK_LATER)
                after = self._cache()["AAA-1"]
            self.assertEqual(code, 0)
            self.assertEqual(later.keys_requested, ["AAA-1"])
            self.assertEqual(
                after, {**before, "checked": A_WEEK_LATER}, "content kept, only re-dated"
            )
            self.assertIn("1 kept their earlier record", output)


class RefreshKeepsTest(FetchTicketsTestCase):
    def test_a_kept_refresh_waits_its_interval_and_does_not_starve_the_queue(self):
        """Breaks if a refresh the tracker did not answer leaves the record's
        date alone. It is then still due, still oldest, and heads the queue on
        every run: two unreadable tickets and a cap of two would take every
        refresh for ever, and the readable one would never be refreshed. Three
        daily runs: the unreadable pair once, the readable one next, then
        nothing until the interval passes."""
        seeded = {
            "AAA-1": open_record("unreadable one", "2025-11-01"),
            "AAA-2": open_record("unreadable two", "2025-11-02"),
            "AAA-3": open_record("readable", "2025-11-03"),
        }
        asked = []
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, list(seeded), TRACKER_REFRESH_MAX=2)
            pio.write_gzip_json(config.TICKET_TRACKER_PATH, seeded)
            for today in ("2026-01-02", "2026-01-03", "2026-01-04"):
                tracker = FakeTracker({"AAA-3": open_fields("readable", status="In Review")})
                self._run_stage(tracker, today=today)
                asked.append(tracker.keys_requested)
            cache = self._cache()
        self.assertEqual(asked, [["AAA-1", "AAA-2"], ["AAA-3"], []])
        self.assertEqual(cache["AAA-3"]["status"], "In Review")
        self.assertEqual(
            cache["AAA-1"], {**seeded["AAA-1"], "checked": "2026-01-02"}, "content untouched"
        )

    def test_a_refresh_never_leaves_a_record_holding_less(self):
        """Breaks if a refresh replaces the record wholesale. A refresh with
        descriptions turned off would erase every stored description, and a
        transport returning only a recent page of comments would cut a thread
        another fetch stored whole. The refreshed fields still win."""
        stored = {
            "checked": "2025-12-01",
            "comments": ["first", "second", "third"],
            "description": "What the ticket was for",
            "status": "Open",
            "summary": "Old title",
        }
        later = open_fields("New title", status="In Review")
        later["comment"] = {"comments": [{"body": "second"}, {"body": "third"}]}
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"], TRACKER_FETCH_COMMENTS=True)
            pio.write_gzip_json(config.TICKET_TRACKER_PATH, {"AAA-1": stored})
            self._run_stage(FakeTracker({"AAA-1": later}))
            record = self._cache()["AAA-1"]
        self.assertEqual(record["summary"], "New title")
        self.assertEqual(record["status"], "In Review")
        self.assertEqual(record["description"], "What the ticket was for")
        self.assertEqual(record["comments"], ["first", "second", "third"])
        self.assertEqual(record["checked"], TODAY)


def open_record(summary: str, checked: str | None = None) -> dict:
    """A cached open ticket as `fetch-tickets` writes it, `checked` optional."""
    record = {"status": "Open", "summary": summary}
    if checked is not None:
        record["checked"] = checked
    return record


class RefreshBoundsTest(FetchTicketsTestCase):
    """Refreshing open tickets must not inflate a run's cost or time.

    Four bounds: age, a per-run cap taken oldest first, batching through the
    same paged request, and candidates limited to tickets the store still cites.
    """

    def _seed(self, records: dict) -> None:
        pio.write_gzip_json(config.TICKET_TRACKER_PATH, records)

    def test_the_cap_bounds_refreshes_per_run_and_the_rest_carry_over(self):
        """Breaks if the cap is ignored, so every due record refreshes in one
        run. A store upgrading from a release without refresh has every open
        record due at once, and that whole backlog would land on one build.
        Also pins the three telemetry measurements."""
        keys = [f"AAA-{n}" for n in range(1, 6)]
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, keys, TRACKER_REFRESH_MAX=2)
            self._seed({key: open_record(key, "2025-12-01") for key in keys})
            first = FakeTracker({key: open_fields(key) for key in keys})
            _code, output = self._run_stage(first)
            measured = json.loads(config.TELEMETRY_PATH.read_text(encoding="utf-8"))
            second = FakeTracker({key: open_fields(key) for key in keys})
            self._run_stage(second)
        self.assertEqual(first.keys_requested, ["AAA-1", "AAA-2"])
        self.assertIn("refreshed 2 of 5 due, with 3 left for later runs, in 1 requests", output)
        self.assertEqual(second.keys_requested, ["AAA-3", "AAA-4"], "the rest drain next run")
        self.assertEqual(
            {k: v for k, v in measured["measurements"].items() if k.startswith("tickets.")},
            {"tickets.refresh_due": 5, "tickets.refresh_requests": 1, "tickets.refreshed": 2},
        )

    def test_the_oldest_records_are_refreshed_first(self):
        """Breaks if the cap takes records in key order, or newest first: an old
        record could then wait behind fresher ones indefinitely while the cap
        keeps refreshing the same few. No date counts as the oldest of all."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1", "AAA-2", "AAA-3", "AAA-4"], TRACKER_REFRESH_MAX=2)
            self._seed(
                {
                    "AAA-1": open_record("one", "2025-12-20"),
                    "AAA-2": open_record("two", "2025-11-01"),
                    "AAA-3": open_record("three", "2025-12-01"),
                    "AAA-4": open_record("four"),
                }
            )
            tracker = FakeTracker({f"AAA-{n}": open_fields(str(n)) for n in range(1, 5)})
            self._run_stage(tracker)
        self.assertEqual(tracker.keys_requested, ["AAA-4", "AAA-2"])

    def test_the_selection_is_the_same_whatever_order_keys_arrive_in(self):
        """Breaks if two records read on the same day are chosen by the order
        the keys happened to arrive in rather than by key. The selection decides
        which records change in a committed artefact, so it must not depend on
        iteration order. Called directly, because the stage hands it keys that
        are already sorted, which would hide a missing tiebreak."""
        cache = {key: open_record(key, "2025-12-01") for key in ("AAA-1", "AAA-2", "AAA-3")}
        config.configure(TRACKER_REFRESH_DAYS=7, TRACKER_REFRESH_MAX=2)
        forwards = fetch_tickets.refresh_plan(["AAA-1", "AAA-2", "AAA-3"], cache, TODAY)
        backwards = fetch_tickets.refresh_plan(["AAA-3", "AAA-2", "AAA-1"], cache, TODAY)
        self.assertEqual(forwards, (["AAA-1", "AAA-2"], 3))
        self.assertEqual(backwards, forwards)

    def test_zero_days_turns_refresh_off(self):
        """Breaks if 0 means "every run" instead of off. 0 is the documented way
        back to the behaviour before refresh existed, and reading it as the
        opposite would make the escape hatch the most expensive setting."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"], TRACKER_REFRESH_DAYS=0)
            self._seed({"AAA-1": open_record("Still open")})
            tracker = FakeTracker({"AAA-1": open_fields("Changed")})
            _code, output = self._run_stage(tracker)
            record = self._cache()["AAA-1"]
        self.assertEqual(tracker.urls, [])
        self.assertEqual(record["summary"], "Still open")
        self.assertIn("refreshed 0 of 0 due", output)
        self.assertIn("refresh is off", output)

    def test_refreshes_are_batched_into_pages_of_their_own(self):
        """Breaks if a refresh costs a request per ticket rather than sharing
        the paged search - three due records must be one request, not three -
        or if refresh requests stop being counted apart from new fetches."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1", "AAA-2", "AAA-3", "AAA-4"])
            self._seed({f"AAA-{n}": open_record(str(n), "2025-12-01") for n in (1, 2, 3)})
            tracker = FakeTracker({f"AAA-{n}": open_fields(str(n)) for n in range(1, 5)})
            _code, output = self._run_stage(tracker)
        self.assertEqual(
            [requested_keys(url) for url in tracker.urls],
            [["AAA-4"], ["AAA-1", "AAA-2", "AAA-3"]],
            "new tickets first, then one page of refreshes",
        )
        self.assertIn("in 1 requests", output)
        self.assertIn("(2 requests", output)

    def test_a_ticket_the_store_no_longer_cites_is_not_refreshed(self):
        """Breaks if refresh candidates are drawn from the cache rather than
        from the tickets the intent stage found this time. A ticket no commit
        cites any more costs a request and changes nothing anyone can reach,
        and a prefix withdrawn into the deny list must not be read again by
        the back door."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(
                tmp, ["AAA-1", "BBB-1"], TRACKER_PROJECTS={"AAA", "BBB"}, TRACKER_DENY={"BBB"}
            )
            self._seed(
                {
                    "AAA-1": open_record("cited", "2025-12-01"),
                    "AAA-9": open_record("orphaned", "2025-12-01"),
                    "BBB-1": open_record("withdrawn", "2025-12-01"),
                }
            )
            tracker = FakeTracker({k: open_fields(k) for k in ("AAA-1", "AAA-9", "BBB-1")})
            self._run_stage(tracker)
            cache = self._cache()
        self.assertEqual(tracker.keys_requested, ["AAA-1"])
        self.assertEqual(cache["AAA-9"]["summary"], "orphaned", "kept, just not paid for")


class RateLimitTest(FetchTicketsTestCase):
    def test_retry_after_is_honoured_before_the_page_is_retried(self):
        """Breaks if a 429 is treated as a failure, or retried immediately.
        Ignoring the wait the tracker asked for is how a client gets blocked
        outright, and treating the throttle as a failure discards a page the
        tracker was willing to serve a moment later."""
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"], TRACKER_DELAY_SECONDS=0.25)
            tracker = FakeTracker({"AAA-1": fields_for("First")}, statuses=[(429, "7")])
            code, output = self._run_stage(tracker, sleeps=sleeps)
            cached = self._cache()
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
            self._store(tmp, ["AAA-1", "AAA-2", "AAA-3"], TRACKER_PAGE_SIZE=1)
            config.configure(TRACKER_DELAY_SECONDS=0.5)
            tracker = FakeTracker({f"AAA-{n}": fields_for(str(n)) for n in (1, 2, 3)})
            self._run_stage(tracker, sleeps=sleeps)
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
            self._store(tmp, ["AAA-1"])
            self._run_stage(default)

        asked = FakeTracker({"AAA-1": fields_for("First")})
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"], TRACKER_FETCH_DESCRIPTION=True, TRACKER_FETCH_COMMENTS=True)
            self._run_stage(asked)

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
            self._store(tmp, ["AAA-1"])
            self._run_stage(FakeTracker({"AAA-1": volunteered}))
            record = self._cache()["AAA-1"]
        self.assertNotIn("description", record)
        self.assertNotIn("comments", record)

    def test_projected_fields_are_stored_in_reduced_form(self):
        """Breaks if the record shape changes. Consumers read these keys, and a
        renamed field is a change to their data, not a refactor."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"], TRACKER_FETCH_DESCRIPTION=True, TRACKER_FETCH_COMMENTS=True)
            fields = fields_for(
                "Amend the hearing outcome",
                parent={"key": "AAA-900"},
                description="Some narrative",
                comment={"comments": [{"body": "First note"}, {"body": "  "}]},
            )
            self._run_stage(FakeTracker({"AAA-1": fields}))
            record = self._cache()["AAA-1"]
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
            self._store(tmp, ["AAA-1"])
            self._run_stage(FakeTracker({"AAA-1": open_ticket}))
            record = self._cache()["AAA-1"]
        self.assertEqual(
            record,
            {
                "checked": TODAY,
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
            self._store(tmp, ["AAA-1"], TRACKER_FETCH_DESCRIPTION=True, TRACKER_FETCH_COMMENTS=True)
            fields = fields_for(
                "Raised by xxxxx@xxxxx.example",
                description="Chase yyyyy@yyyyy.example about it",
                comment={"comments": [{"body": "cc zzzzz@zzzzz.example"}]},
            )
            code, output = self._run_stage(FakeTracker({"AAA-1": fields}))
            record = self._cache()["AAA-1"]
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
            self._store(
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
            code, output = self._run_stage(tracker)
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
                self._store(tmp, keys, TRACKER_PAGE_SIZE=2)
                self._run_stage(FakeTracker(dict(issues)))
                outputs.append(
                    (
                        config.TICKET_TRACKER_PATH.read_bytes(),
                        config.TRACKER_UNDECIDED_PATH.read_bytes(),
                    )
                )
                cached.append(list(self._cache()))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(cached[0], ["AAA-1", "AAA-2", "AAA-20", "AAA-3"], "keys sorted on write")

    def test_a_ticket_added_later_is_still_written_in_sorted_order(self):
        """Breaks if the cache is written in insertion order. A second run
        appends to a cache loaded from disk, so a ticket that sorts before the
        existing ones lands last unless the writer sorts - and the committed
        artefact then reorders itself during some later unrelated rebuild,
        producing a diff nobody can attribute to a change."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-9"])
            self._run_stage(FakeTracker({"AAA-9": fields_for("Ninth")}))
            pio.write_gzip_json(
                config.TICKET_DESCRIPTIONS_PATH, {"AAA-9": {"d": []}, "AAA-1": {"d": []}}
            )
            self._run_stage(FakeTracker({"AAA-1": fields_for("First")}))
            order = list(self._cache())
        self.assertEqual(order, ["AAA-1", "AAA-9"])


class WireContractTest(FetchTicketsTestCase):
    """The request this stage sends is the Jira Data Center/Server contract.

    The request is the artefact: a different path, verb or auth scheme would
    still parse the same fake responses, so only an assertion on what was sent
    can notice the stage drifting off the one flavour it speaks.
    """

    def test_search_is_a_bearer_authenticated_get_on_the_v2_search_path(self):
        """Catches the search moving off `/rest/api/2/search` (for example to
        Cloud's `/rest/api/3/search/jql`), losing its JQL, key projection or page
        size, or the credential leaving the Bearer scheme (for example to Basic).
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1", "AAA-2"])
            tracker = FakeTracker({"AAA-1": fields_for("First"), "AAA-2": fields_for("Second")})
            code, _ = self._run_stage(tracker)

            self.assertEqual(code, 0)
            self.assertEqual(len(tracker.urls), 1)
            parts = urllib.parse.urlsplit(tracker.urls[0])
            self.assertEqual(parts.path, "/jira/rest/api/2/search")
            query = urllib.parse.parse_qs(parts.query)
            self.assertEqual(sorted(query), ["fields", "jql", "maxResults"])
            self.assertEqual(query["jql"], ["key in (AAA-1,AAA-2)"])
            self.assertEqual(
                query["fields"],
                ["summary,issuetype,status,resolution,created,resolutiondate,parent"],
            )
            self.assertEqual(query["maxResults"], ["2"])
            self.assertEqual(tracker.headers[0]["Authorization"], f"Bearer {TOKEN}")
            self.assertEqual(tracker.headers[0]["Accept"], "application/json")
            self.assertEqual(self._cache()["AAA-1"]["summary"], "First")

    def test_the_real_boundary_sends_a_get(self):
        """Catches `open_url` changing the verb (for example to POST with a JQL
        body), which the `opener` seam cannot see because it sits above it.

        Stubs `urllib.request.urlopen`, the true network edge, and drives the
        stage through its default opener.
        """
        sent: list[urllib.request.Request] = []

        class Reply:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self):
                return json.dumps(
                    {"startAt": 0, "maxResults": 1, "total": 0, "issues": []}
                ).encode()

        def urlopen(request, timeout=None):
            sent.append(request)
            return Reply()

        real = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self._store(tmp, ["AAA-1"])
                out = StringIO()
                with redirect_stdout(out), redirect_stderr(out):
                    code = fetch_tickets.main([], sleep=lambda _s: None, today=TODAY)
        finally:
            urllib.request.urlopen = real

        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].get_method(), "GET")
        self.assertIsNone(sent[0].data)
        self.assertEqual(urllib.parse.urlsplit(sent[0].full_url).path, "/jira/rest/api/2/search")
        self.assertEqual(sent[0].get_header("Authorization"), f"Bearer {TOKEN}")


class CredentialTest(FetchTicketsTestCase):
    """The token appears in exactly one place: the Authorization header.

    Not in an artefact, not in a summary line, not in an error message, not in
    the User-Agent, not in a URL. A build log and a committed file both outlive
    the run and reach a wider audience than the credential was issued to, so
    this is the test that would fail if the token ever reached either.
    """

    def test_credential_reaches_the_request_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1", "AAA-2", "CCC-3"], TRACKER_PAGE_SIZE=1)
            tracker = FakeTracker({"AAA-1": fields_for("First")}, statuses=[500])
            code, output = self._run_stage(tracker)
            # A second run with nothing queued, so a page actually succeeds. Without
            # it the 500 means no issue is ever fetched, `projected_record` never
            # runs, and the most likely place for a credential to be copied - the
            # stored record itself - is never reached by this test.
            fetched = FakeTracker({"AAA-1": fields_for("First"), "AAA-2": fields_for("Second")})
            code, second = self._run_stage(fetched)
            output += second
            # Decompressed, deliberately. The cache is gzip, so reading its raw
            # bytes cannot find a token inside it - the assertion would read as a
            # check and verify nothing. This is the highest-consequence artefact
            # of the two: it is committed, so a credential written here would
            # persist in a repository's history rather than in a transient log.
            artefacts = json.dumps(
                pio.read_gzip_json(config.TICKET_TRACKER_PATH, default={})
            ) + config.TRACKER_UNDECIDED_PATH.read_text(encoding="utf-8")

            # The header is the one legitimate carrier - assert it, so the rest
            # of this test cannot pass by the token never being sent at all.
            self.assertEqual(tracker.headers[0]["Authorization"], f"Bearer {TOKEN}")
            self.assertNotIn(TOKEN, tracker.headers[0]["User-Agent"])
            self.assertNotIn(TOKEN, "".join(tracker.urls))
            self.assertEqual(code, 0)
            self.assertNotIn(TOKEN, output)
            self.assertNotIn(TOKEN, artefacts)

            # A raised message must not carry it either, whatever went wrong.
            config.configure(TRACKER_BASE_URL=f"ftp://{TOKEN}@example.example/jira")
            _, refused = self._run_stage(FakeTracker())
            self.assertNotIn(TOKEN, refused)
            with self.assertRaises(ValueError) as raised:
                fetch_tickets.tracker_root(f"ftp://{TOKEN}@example.example/jira")
            self.assertNotIn(TOKEN, str(raised.exception))


if __name__ == "__main__":
    unittest.main()


class CommentVolumeTest(SettingsIsolated):
    """Comment volume is bounded, because the interesting part is not the long tail.

    Measured on one estate's first live fetch: 38,681 comments holding
    14.4 M characters, of which 778 comments - 2% - held 37% of the characters.
    Those are stack traces and log dumps pasted into a ticket, not knowledge. A
    further 1% read as build automation and held 12%.

    Capping each comment and dropping the automated ones took the payload from
    14.4 M to 10.0 M characters, 6.0 MB to 5.2 MB gzipped, while leaving 93% of
    tickets with every comment they had. Limiting the *count* was measured too and
    rejected: keeping only the first five saved a further 1.7 MB and cost 37% of
    tickets their threads, and a ticket with eighteen comments is one where
    something went wrong and got argued about - the resolution is at the end.
    """

    def test_a_long_comment_is_capped_at_a_word_boundary(self):
        config.configure(TRACKER_FETCH_COMMENTS=True, TRACKER_COMMENT_CHARS=40)
        issue = {"fields": {"comment": {"comments": [{"body": "word " * 100}]}}}
        record = fetch_tickets.projected_record(issue, Counter())
        comment = record["comments"][0]
        self.assertLessEqual(len(comment), 41)
        self.assertNotIn("wor…", comment)

    def test_a_short_comment_is_untouched(self):
        config.configure(TRACKER_FETCH_COMMENTS=True, TRACKER_COMMENT_CHARS=2000)
        issue = {"fields": {"comment": {"comments": [{"body": "Root cause was a null URN."}]}}}
        record = fetch_tickets.projected_record(issue, Counter())
        self.assertEqual(record["comments"], ["Root cause was a null URN."])

    def test_build_noise_is_dropped_entirely(self):
        """A truncated log dump is still a log dump - drop, do not shorten."""
        config.configure(TRACKER_FETCH_COMMENTS=True, TRACKER_COMMENT_CHARS=2000)
        issue = {
            "fields": {
                "comment": {
                    "comments": [
                        {"body": "Jenkins build #4412 failed"},
                        {"body": "Caused by: java.lang.NullPointerException"},
                        {"body": "\tat org.example.platform.Thing.run(Thing.java:42)"},
                        {"body": "The URN was missing from the payload, so listing rejected it."},
                    ]
                }
            }
        }
        record = fetch_tickets.projected_record(issue, Counter())
        self.assertEqual(
            record["comments"], ["The URN was missing from the payload, so listing rejected it."]
        )

    def test_a_stack_frame_is_noise_whatever_its_package_root(self):
        """Every common Java/Scala package root must be recognised as a stack
        frame, not just the ones the estate that wrote the default happened to
        use. A root missing from the alternation lets that vendor's or
        in-house code's traces survive into the record as if they were
        narrative - see issue #95, which found `com.` missing."""
        config.configure(TRACKER_FETCH_COMMENTS=True, TRACKER_COMMENT_CHARS=2000)
        for root in ("uk", "com", "net", "io", "java", "org"):
            with self.subTest(root=root):
                issue = {
                    "fields": {
                        "comment": {
                            "comments": [
                                {"body": f"\tat {root}.example.widget.Thing.run(Thing.java:42)"},
                                {"body": "The URN was missing, so listing rejected it."},
                            ]
                        }
                    }
                }
                record = fetch_tickets.projected_record(issue, Counter())
                self.assertEqual(
                    record["comments"],
                    ["The URN was missing, so listing rejected it."],
                    f"a stack frame rooted at {root!r} was not dropped as noise",
                )

    def test_every_surviving_comment_is_kept_not_just_the_first_few(self):
        """No count limit: the resolution is usually the last comment, not the first."""
        config.configure(TRACKER_FETCH_COMMENTS=True, TRACKER_COMMENT_CHARS=2000)
        bodies = [{"body": f"comment number {i}"} for i in range(30)]
        issue = {"fields": {"comment": {"comments": bodies}}}
        record = fetch_tickets.projected_record(issue, Counter())
        self.assertEqual(len(record["comments"]), 30)
        self.assertIn("comment number 29", record["comments"][-1])

    def test_a_ticket_whose_every_comment_is_noise_has_no_comments_field(self):
        """An empty list would read as evidence found, matching the stage's rule."""
        config.configure(TRACKER_FETCH_COMMENTS=True, TRACKER_COMMENT_CHARS=2000)
        issue = {
            "fields": {
                "summary": "s",
                "comment": {"comments": [{"body": "Jenkins build #1 failed"}]},
            }
        }
        record = fetch_tickets.projected_record(issue, Counter())
        self.assertNotIn("comments", record)

    def test_redaction_still_runs_on_a_capped_comment(self):
        """Truncation must not be a way for an identifier to survive."""
        config.configure(
            TRACKER_FETCH_COMMENTS=True,
            TRACKER_COMMENT_CHARS=2000,
            SENSITIVE_PATTERNS={"case-reference": r"\b\d{2}[A-Z]{2}\d{7}\b"},
        )
        issue = {"fields": {"comment": {"comments": [{"body": "see 01AB1234567 for detail"}]}}}
        record = fetch_tickets.projected_record(issue, Counter())
        self.assertNotIn("01AB1234567", record["comments"][0])
        self.assertIn("withheld", record["comments"][0])


# --- the twg transport (#420) ----------------------------------------------

TWG_FIXTURES = Path(__file__).resolve().parent / "twg_fixtures"
# Every name in the fixtures. None of them may reach the store, in any form.
TWG_NAMES = ("Alex Example", "Bea Sample", "Cal Placeholder", "Dee Invented")
NARRATIVE = "summary,issuetype,status,resolution,created,resolutiondate,parent,description,comment"


def twg_fixture(name: str) -> str:
    """A captured `twg` stdout: the real structure, every value invented."""
    return (TWG_FIXTURES / name).read_text(encoding="utf-8")


class FakeTwg:
    """`twg` at the process boundary: answers argument lists with stdout.

    `bulk` is what `bulk-get` prints, whatever it is asked; `gets` maps a key to
    what `get` prints for it. Every call is recorded, so a test can assert on
    what the library actually asked for.
    """

    def __init__(
        self,
        bulk: str,
        gets: dict[str, str] | None = None,
        threads: dict[str, str] | None = None,
    ):
        self.bulk = bulk
        self.gets = gets or {}
        self.threads = threads or {}
        self.calls: list[list[str]] = []

    def __call__(self, arguments: list[str]) -> str:
        self.calls.append(list(arguments))
        if arguments[2] == "get" and "--comments" in arguments:
            return self.threads.get(arguments[-1], "")
        if arguments[2] == "get":
            return self.gets.get(arguments[-1], twg_fixture("get-missing.json"))
        return self.bulk


class TwgTransportTestCase(FetchTicketsTestCase):
    def _twg_store(self, tmp, **settings) -> Path:
        defaults = {
            "TRACKER_TRANSPORT": "twg",
            "TRACKER_BASE_URL": "",
            "TRACKER_TOKEN": "",
            "TRACKER_PROJECTS": {"ABC"},
        }
        return self._store(tmp, ["ABC-1", "ABC-2", "ABC-3"], **{**defaults, **settings})

    def _run_twg(self, runner, sleeps: list | None = None, today: str = TODAY):
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = fetch_tickets.main(
                [],
                runner=runner,
                sleep=(sleeps.append if sleeps is not None else lambda _s: None),
                today=today,
            )
        return code, out.getvalue()


class TwgRequestTest(TwgTransportTestCase):
    def test_a_page_is_one_bulk_get_answered_in_the_v2_shape(self):
        """Breaks if the transport stops asking for exactly the library's keys
        and projection - a wider field list fetches narrative nobody enabled -
        or stops answering in the shape the library reads, which would cache
        every ticket as absent. Also breaks if the twg route still demands a
        base URL and token it never uses."""
        twg = FakeTwg(twg_fixture("bulk-get.json"))
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp)
            code, output = self._run_twg(twg)
            cache = self._cache()
        self.assertEqual(code, 0, output)
        self.assertEqual(
            twg.calls[0],
            [
                "jira",
                "workitem",
                "bulk-get",
                "--fields",
                "summary,issuetype,status,resolution,created,resolutiondate,parent",
                "-o",
                "json",
                "--output-summary",
                "none",
                "--",
                "ABC-1",
                "ABC-2",
                "ABC-3",
            ],
        )
        self.assertEqual(
            cache["ABC-1"],
            {
                "created": "2024-03-04",
                "parent": "ABC-900",
                "resolution": "Done",
                "resolved": "2024-03-09",
                "status": "Done",
                "summary": "Sort the widgets",
                "type": "Story",
            },
        )
        self.assertEqual(
            cache["ABC-2"],
            {
                "checked": TODAY,
                "created": "2024-03-04",
                "status": "In Progress",
                "summary": "Still being argued about",
                "type": "Story",
            },
        )
        self.assertEqual(cache["ABC-3"], {"absent": True, "checked": TODAY})

    def test_an_unknown_transport_is_refused_before_anything_is_asked(self):
        """Breaks if a misspelt transport falls back to one of the others. An
        operator who wrote `TWG ` or `twig` would otherwise send a bearer
        request they did not mean, or none, and read the result as the tracker's."""
        twg = FakeTwg(twg_fixture("bulk-get.json"))
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp, TRACKER_TRANSPORT="twig")
            code, output = self._run_twg(twg)
            written = config.TICKET_TRACKER_PATH.exists()
        self.assertEqual(code, 1)
        self.assertEqual(twg.calls, [])
        self.assertFalse(written)
        self.assertIn("KSB_TRACKER_TRANSPORT", output)


class TwgRefreshTest(TwgTransportTestCase):
    def test_a_refresh_through_twg_is_capped_and_batched_like_any_other(self):
        """Breaks if the twg route grows its own refresh path - one `get` per
        open ticket, or the cap not applied - instead of answering the library's
        bounded, paged request. Three due, capped at two, is one `bulk-get`
        naming exactly the first two."""
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp, TRACKER_REFRESH_MAX=2)
            pio.write_gzip_json(
                config.TICKET_TRACKER_PATH,
                {key: open_record(key, "2025-12-01") for key in ("ABC-1", "ABC-2", "ABC-3")},
            )
            twg = FakeTwg(twg_fixture("bulk-get.json"))
            _code, output = self._run_twg(twg)
            cache = self._cache()
        self.assertEqual(len(twg.calls), 1)
        self.assertEqual(
            twg.calls[0][2:4] + twg.calls[0][-3:], ["bulk-get", "--fields", "--", "ABC-1", "ABC-2"]
        )
        self.assertEqual(cache["ABC-1"]["summary"], "Sort the widgets")
        self.assertEqual(
            cache["ABC-3"]["summary"], "ABC-3", "over the cap, so left for a later run"
        )
        self.assertIn("refreshed 2 of 3 due, with 1 left for later runs, in 1 requests", output)


class TwgBodyTest(TwgTransportTestCase):
    def test_document_bodies_are_flattened_one_line_per_block(self):
        """Breaks if a Cloud description or comment is stored as nothing - the
        library keeps only strings, and an ADF body is a mapping - or if blocks
        run together so a heading, a list item and a table cell read as one
        sentence."""
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp, TRACKER_FETCH_DESCRIPTION=True, TRACKER_FETCH_COMMENTS=True)
            twg = FakeTwg(twg_fixture("bulk-get.json"))
            self._run_twg(twg)
            record = self._cache()["ABC-1"]
        self.assertEqual(twg.calls[0][4], NARRATIVE)
        self.assertEqual(
            record["description"],
            "Background\n"
            "Widgets are sorted by colour. See the design note.\n"
            "Raised by [~accountid:acct-0000-alpha]\n"
            "Second point\n"
            "Owner\n"
            "[~accountid:acct-0000-bravo]",
        )
        self.assertEqual(
            record["comments"], ["Agreed with [~accountid:acct-0000-charlie], closing."]
        )

    def test_no_person_s_name_reaches_the_store(self):
        """Breaks if a mention is rendered from `attrs.text`, which holds the
        display name, or if a comment's author travels with its body. This is
        the privacy requirement in #420: the store is republished, and a name
        in it is a name published. Every mention in the fixture is nested - in
        a list item, a table cell, a comment - so a renderer that only handles
        top-level paragraphs fails here too."""
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp, TRACKER_FETCH_DESCRIPTION=True, TRACKER_FETCH_COMMENTS=True)
            _code, output = self._run_twg(FakeTwg(twg_fixture("bulk-get.json")))
            stored = json.dumps(self._cache())
        for name in TWG_NAMES:
            self.assertNotIn(name, stored)
            self.assertNotIn(name, output)
        for account in ("acct-0000-alpha", "acct-0000-bravo", "acct-0000-charlie"):
            self.assertIn(f"[~accountid:{account}]", stored)
        self.assertNotIn("acct-0000-delta", stored, "a comment author is not part of its body")

    def test_a_mention_deep_inside_other_nodes_is_still_an_account_id(self):
        """Breaks if mentions are recognised only at some depths: a panel holding
        a list holding a marked-up paragraph is ordinary Cloud content. Also
        breaks if a mention with no id falls back to its display name."""
        body = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "panel",
                    "attrs": {"panelType": "info"},
                    "content": [
                        {
                            "type": "bulletList",
                            "content": [
                                {
                                    "type": "listItem",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [
                                                {
                                                    "type": "text",
                                                    "text": "ask ",
                                                    "marks": [{"type": "strong"}],
                                                },
                                                {
                                                    "type": "mention",
                                                    "attrs": {
                                                        "id": "acct-0000-echo",
                                                        "text": "@Eve Madeup",
                                                    },
                                                },
                                                {
                                                    "type": "mention",
                                                    "attrs": {"text": "@Fay Noid"},
                                                },
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        self.assertEqual(twg_transport.flatten(body), "ask [~accountid:acct-0000-echo]")


class TwgExitCodeTest(TwgTransportTestCase):
    def test_stdout_is_read_whatever_twg_exits_with(self):
        """Breaks if a non-zero exit is read as no answer. `twg` exits 3 when any
        key in the page is unreturned, which on a real estate is most pages, so
        reading the exit code would leave nearly every ticket uncached for ever.

        Drives the real runner against a real process: a `twg` on PATH that
        prints a captured answer and exits 3, or 1 for `get`, as the real one
        does."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as bin_dir:
            fake = Path(bin_dir) / "twg"
            fake.write_text(
                "#!/bin/sh\n"
                'if [ "$3" = get ]; then cat "$TWG_MISSING"; exit 1; fi\n'
                'cat "$TWG_BULK"\n'
                "exit 3\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            self._twg_store(tmp)
            environment = {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "TWG_BULK": str(TWG_FIXTURES / "bulk-get.json"),
                "TWG_MISSING": str(TWG_FIXTURES / "get-missing.json"),
            }
            with unittest.mock.patch.dict(os.environ, environment):
                code, output = self._run_twg(twg_transport.run_twg)
            cache = self._cache()
        self.assertEqual(code, 0, output)
        self.assertEqual(cache["ABC-1"]["summary"], "Sort the widgets")
        self.assertEqual(cache["ABC-3"], {"absent": True, "checked": TODAY})
        self.assertIn("2 fetched, 0 refreshed, 1 absent", output)

    def test_output_that_does_not_parse_is_no_answer(self):
        """Breaks if unreadable output is read as an empty answer, which caches
        every key in the page as absent - final, and never asked about again -
        because `twg` was signed out for one run."""
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp)
            code, output = self._run_twg(FakeTwg("Error: sign in with twg login\n"))
            cache = self._cache()
        self.assertEqual(code, 0)
        self.assertEqual(cache, {})
        self.assertIn("3 failed", output)
        self.assertIn("KSB_TRACKER_TRANSPORT=twg", output, "the failure names the transport")

    def test_an_answer_that_omits_a_key_is_no_answer(self):
        """Breaks if a key missing from `bulk-get`'s items altogether is read as
        absent. An unreturned key is listed with `ok: false`; one that is not
        listed at all is output this module does not understand, and caching
        that as a fact would be permanent."""
        partial = json.loads(twg_fixture("bulk-get.json"))
        partial["data"]["items"] = partial["data"]["items"][:2]
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp)
            self._run_twg(FakeTwg(json.dumps(partial)))
            cache = self._cache()
        self.assertEqual(cache, {})


class TwgMovedTicketTest(TwgTransportTestCase):
    def test_a_ticket_moved_to_an_allowlisted_project_is_filed_under_the_cited_key(self):
        """Breaks if an unreturned key is not followed with `get` - `bulk-get`
        does not follow moves, so every moved ticket would read as absent and
        never be asked about again - or if the record is filed under the new
        key, which no commit cites, so nothing in the store would join to it."""
        twg = FakeTwg(twg_fixture("bulk-get.json"), {"ABC-3": twg_fixture("get-moved.json")})
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp, TRACKER_PROJECTS={"ABC", "XYZ"})
            self._run_twg(twg)
            cache = self._cache()
        self.assertEqual(
            twg.calls[1],
            [
                "jira",
                "workitem",
                "get",
                "--fields",
                "summary,issuetype,status,resolution,created,resolutiondate,parent",
                "-o",
                "json",
                "--output-summary",
                "none",
                "--",
                "ABC-3",
            ],
        )
        self.assertEqual(len(twg.calls), 2, "only the unreturned key is followed")
        self.assertEqual(cache["ABC-3"]["summary"], "Moved to another project")
        self.assertNotIn("XYZ-7", cache)

    def test_a_ticket_moved_to_a_project_nobody_allowed_stays_absent(self):
        """Breaks if a move can carry a ticket past the allowlist. The prefix the
        commits cite was allowed; the project it moved to was not, and reading
        it would read a project this store was never authorised for."""
        for name, settings in (
            ("undecided", {"TRACKER_PROJECTS": {"ABC"}}),
            ("denied", {"TRACKER_PROJECTS": {"ABC", "XYZ"}, "TRACKER_DENY": {"XYZ"}}),
        ):
            twg = FakeTwg(twg_fixture("bulk-get.json"), {"ABC-3": twg_fixture("get-moved.json")})
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                self._twg_store(tmp, **settings)
                self._run_twg(twg)
                cache = self._cache()
                stored = json.dumps(cache)
            self.assertEqual(cache["ABC-3"], {"absent": True, "checked": TODAY})
            self.assertNotIn("Moved to another project", stored)

    def test_an_unreadable_follow_up_is_no_answer_not_absence(self):
        """Breaks if a `get` that printed nothing readable is taken as "does not
        exist". Absent is final, so one bad follow-up would lose the ticket for
        good; only the tracker's own not-found is an answer. The rest of the
        page still lands: the no-answer is that key's alone."""
        twg = FakeTwg(twg_fixture("bulk-get.json"), {"ABC-3": "Error: rate limited\n"})
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp)
            _code, output = self._run_twg(twg)
            cache = self._cache()
        self.assertNotIn("ABC-3", cache)
        self.assertEqual(sorted(cache), ["ABC-1", "ABC-2"])
        self.assertIn("1 failed", output)

    def test_a_follow_up_reply_that_is_not_twg_s_envelope_is_no_answer(self):
        """Breaks if JSON that parses but is not twg's envelope - a bare list,
        a string - is read as the tracker's not-found: absent is final, so the
        ticket would be lost for good on one odd reply."""
        for reply in ("[]", '"signed out"', "null"):
            twg = FakeTwg(twg_fixture("bulk-get.json"), {"ABC-3": reply})
            with self.subTest(reply=reply), tempfile.TemporaryDirectory() as tmp:
                self._twg_store(tmp)
                _code, output = self._run_twg(twg)
                cache = self._cache()
            self.assertNotIn("ABC-3", cache)
            self.assertIn("1 failed", output)

    def test_only_a_not_found_from_get_is_absence(self):
        """Breaks if any error twg reports from `get` is read as not-found. A
        throttled (429) or signed-out (401) follow-up prints the same JSON error
        envelope as a missing ticket, and caching it as absent - which is final -
        would lose the ticket for good. Built from the captured not-found reply
        with only its status changed."""
        for status in (429, 401, 503):
            error = json.loads(twg_fixture("get-missing.json"))
            error["error"]["statusCode"] = status
            error["error"]["downstreamError"]["statusCode"] = status
            twg = FakeTwg(twg_fixture("bulk-get.json"), {"ABC-3": json.dumps(error)})
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                self._twg_store(tmp)
                _code, output = self._run_twg(twg)
                cache = self._cache()
            self.assertNotIn("ABC-3", cache, "no answer, so asked again next run")
            self.assertIn("1 failed", output)
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp)
            self._run_twg(FakeTwg(twg_fixture("bulk-get.json")))
            self.assertEqual(self._cache()["ABC-3"], {"absent": True, "checked": TODAY})


def with_unreturned(*keys: str) -> str:
    """The captured `bulk-get` answer with `keys` reported as unreturned."""
    bulk = json.loads(twg_fixture("bulk-get.json"))
    bulk["data"]["items"] = [
        {"input": item["input"], "ok": False, "error": {"message": "not returned"}}
        if item["input"] in keys
        else item
        for item in bulk["data"]["items"]
    ]
    return json.dumps(bulk)


class TwgFollowUpCostTest(TwgTransportTestCase):
    def test_a_follow_up_is_counted_and_paced_like_a_request(self):
        """Breaks if a follow-up `get` is free in the run's accounting, or
        fired without the pause the library keeps between requests. One page
        with one unreturned key is two tracker calls, and the report must say
        two - a page of a hundred unreturned keys would otherwise read as one."""
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp, TRACKER_DELAY_SECONDS=0.5)
            _code, output = self._run_twg(FakeTwg(twg_fixture("bulk-get.json")), sleeps)
        self.assertIn("(2 requests", output)
        self.assertEqual(sleeps, [0.5], "the get waits the page delay after the bulk-get")

    def test_a_refresh_counts_its_follow_ups_in_its_cost(self):
        """Breaks if the refresh cost the run reports and records leaves out
        the follow-ups a refresh page needed. The kept record is re-dated, so
        it is not asked again next run."""
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp)
            pio.write_gzip_json(
                config.TICKET_TRACKER_PATH,
                {key: open_record(key, "2025-12-01") for key in ("ABC-1", "ABC-2", "ABC-3")},
            )
            _code, output = self._run_twg(FakeTwg(twg_fixture("bulk-get.json")))
            measured = json.loads(config.TELEMETRY_PATH.read_text(encoding="utf-8"))
            cache = self._cache()
        self.assertIn("refreshed 2 of 3 due, with 1 left for later runs, in 2 requests", output)
        self.assertEqual(measured["measurements"]["tickets.refresh_requests"], 2)
        self.assertEqual(cache["ABC-3"]["checked"], TODAY)

    def test_follow_ups_stop_at_the_per_run_budget(self):
        """Breaks if follow-ups are unbounded, so one page of unreturned keys
        becomes a call per key. Past `KSB_TRACKER_FOLLOWUP_MAX` a key is no
        answer - not absent, which would be final - and is asked next run."""
        twg = FakeTwg(with_unreturned("ABC-2", "ABC-3"))
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp, TRACKER_FOLLOWUP_MAX=1)
            _code, output = self._run_twg(twg)
            cache = self._cache()
        self.assertEqual([call[2] for call in twg.calls], ["bulk-get", "get"])
        self.assertEqual(twg.calls[1][-1], "ABC-2", "follow-ups in key order")
        self.assertEqual(cache["ABC-2"], {"absent": True, "checked": TODAY})
        self.assertNotIn("ABC-3", cache)
        self.assertIn("1 failed", output)


INVENTED_NAMES = ("Gil Fictive", "Hal Notreal", "Ida Imaginary", "Jo Madeup")


class TwgPrivacyTest(TwgTransportTestCase):
    def test_no_node_s_attrs_text_reaches_the_flattened_text(self):
        """Breaks if any inline node falls back to `attrs.text`, which is where
        ADF keeps a display name - the helpful fallback someone adds to show a
        status lozenge or a date would publish a name from any node carrying
        one. An unknown node's content children still render; its attributes
        never do."""

        def node(kind: str, name: str) -> dict:
            return {"type": kind, "attrs": {"text": name, "timestamp": "1700000000000"}}

        body = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "before "},
                        node("status", INVENTED_NAMES[0]),
                        node("date", INVENTED_NAMES[1]),
                        node("placeholder", INVENTED_NAMES[2]),
                        node("inventedInline", INVENTED_NAMES[3]),
                        {"type": "text", "text": " after"},
                    ],
                },
                {
                    "type": "inventedBlock",
                    "attrs": {"text": INVENTED_NAMES[0]},
                    "content": [{"type": "text", "text": "visible child"}],
                },
            ],
        }
        flat = twg_transport.flatten(body)
        for name in INVENTED_NAMES:
            self.assertNotIn(name, str(flat))
        self.assertIn("before", str(flat))
        self.assertIn("visible child", str(flat))

    def test_the_transport_hands_the_library_no_comment_author(self):
        """Breaks if the transport passes a comment's author through. The
        library happens to read only `body` today; this holds the transport to
        its own promise, so the name is gone before the library could see it."""
        bulk = json.loads(twg_fixture("bulk-get.json"))
        issue = bulk["data"]["items"][0]["data"]
        handed = json.dumps(twg_transport.projected(issue, ["comment"]))
        self.assertIn("Agreed with", handed)
        for leaked in ("author", "updateAuthor", "displayName", "Dee Invented", "acct-0000-delta"):
            self.assertNotIn(leaked, handed)

    def test_an_emoji_and_a_link_card_render(self):
        """Breaks if the emoji or link-card branch stops rendering: an emoji is
        its character (or its short name), and a link card is its URL."""
        body = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "emoji", "attrs": {"shortName": ":tick:", "text": "✅"}},
                        {"type": "text", "text": " "},
                        {"type": "emoji", "attrs": {"shortName": ":cross:"}},
                        {"type": "text", "text": " see "},
                        {"type": "inlineCard", "attrs": {"url": "https://docs.example/a"}},
                    ],
                }
            ],
        }
        self.assertEqual(twg_transport.flatten(body), "✅ :cross: see https://docs.example/a")


class TwgBoundaryTest(TwgTransportTestCase):
    def test_something_that_is_not_a_ticket_key_never_reaches_twg(self):
        """Breaks if anything but a ticket key can reach twg's command line.
        Keys come from commit messages through a configurable pattern; one that
        looks like an option, or carries a comma or a bracket, is refused."""
        for odd in ("--help", "ABC-1;x", "ABC-", "1-ABC"):
            url = fetch_tickets.search_url(twg_transport.ROOT, ["ABC-1", odd], ["summary"])
            with self.subTest(odd=odd):
                self.assertEqual(twg_transport.requested(url), (["ABC-1"], ["summary"], [odd]))
        url = fetch_tickets.search_url(twg_transport.ROOT, ["ABC-1", "abc_2-30"], ["summary"])
        self.assertEqual(twg_transport.requested(url), (["ABC-1", "abc_2-30"], ["summary"], []))

    def test_a_key_with_a_non_ascii_letter_or_digit_is_refused(self):
        """Breaks if the key pattern matches Unicode letters or digits - which
        `\\w` and `\\d` do in Python 3 unless the pattern is compiled ASCII-only -
        widening what reaches twg's command line to keys no tracker issues."""
        for odd in ("ÄB-1", "AÄB-1", "Aé-1", "AB-\u0661", "AB-1\u0663", "A\u0663-1"):
            url = fetch_tickets.search_url(twg_transport.ROOT, ["ABC-1", odd], ["summary"])
            with self.subTest(odd=odd):
                self.assertEqual(twg_transport.requested(url), (["ABC-1"], ["summary"], [odd]))

    def test_a_refused_key_costs_only_itself(self):
        """Breaks if one entry that is not a ticket key refuses its whole page:
        keys are sorted into pages, so the same page would fail every run and
        the valid tickets beside it would never be fetched. A page of nothing
        but refused keys runs twg not at all, and is not counted as a request."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(
                tmp,
                ["ABC-1", "ABC-2", "ABC-3", "ABC-9x"],
                TRACKER_TRANSPORT="twg",
                TRACKER_BASE_URL="",
                TRACKER_TOKEN="",
                TRACKER_PROJECTS={"ABC"},
            )
            twg = FakeTwg(twg_fixture("bulk-get.json"))
            _code, output = self._run_twg(twg)
            cache = self._cache()
        self.assertEqual(twg.calls[0][-4:], ["--", "ABC-1", "ABC-2", "ABC-3"])
        self.assertEqual(sorted(cache), ["ABC-1", "ABC-2", "ABC-3"])
        self.assertIn("1 failed", output)
        with tempfile.TemporaryDirectory() as tmp:
            self._store(
                tmp,
                ["ABC-9x"],
                TRACKER_TRANSPORT="twg",
                TRACKER_BASE_URL="",
                TRACKER_TOKEN="",
                TRACKER_PROJECTS={"ABC"},
            )
            twg = FakeTwg(twg_fixture("bulk-get.json"))
            _code, output = self._run_twg(twg)
        self.assertEqual(twg.calls, [])
        self.assertIn("(0 requests", output)

    def test_output_that_is_not_valid_utf_8_is_still_read(self):
        """Breaks if twg's output is decoded with the locale's codec or strictly:
        one undecodable byte in a ticket's text would raise out of the stage
        and stop the run. Drives the real runner against a real process."""
        raw = twg_fixture("bulk-get.json").encode("utf-8")
        raw = raw.replace(b"Sort the widgets", b"Sort the widgets \xff")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as bin_dir:
            answer = Path(bin_dir) / "answer.json"
            answer.write_bytes(raw)
            fake = Path(bin_dir) / "twg"
            fake.write_text(
                "#!/bin/sh\n"
                'if [ "$3" = get ]; then cat "$TWG_MISSING"; exit 1; fi\n'
                'cat "$TWG_BULK"\n'
                "exit 3\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            self._twg_store(tmp)
            environment = {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "TWG_BULK": str(answer),
                "TWG_MISSING": str(TWG_FIXTURES / "get-missing.json"),
            }
            with unittest.mock.patch.dict(os.environ, environment):
                code, output = self._run_twg(twg_transport.run_twg)
            cache = self._cache()
        self.assertEqual(code, 0, output)
        self.assertEqual(cache["ABC-1"]["summary"], "Sort the widgets �")


def with_partial_thread() -> str:
    """The captured `bulk-get` answer with ABC-1's thread reported as three
    comments of which only the most recent came back - what `bulk-get` does
    with a long thread."""
    bulk = json.loads(twg_fixture("bulk-get.json"))
    bulk["data"]["items"][0]["data"]["comment"]["total"] = 3
    return json.dumps(bulk)


WHOLE_THREAD = [
    "Opened to track the widget ordering.",
    "Asked [~accountid:acct-0000-foxtrot] to confirm the colours.",
    "Agreed with [~accountid:acct-0000-charlie], closing.",
]


class TwgThreadTest(TwgTransportTestCase):
    def test_a_partial_thread_is_completed_by_one_counted_follow_up(self):
        """Breaks if a long thread is stored as only its most recent page - the
        gap #431 names - or if completing it costs a call the run does not count.
        One `get --comments` returns the whole thread."""
        twg = FakeTwg(with_partial_thread(), threads={"ABC-1": twg_fixture("get-comments.json")})
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp, TRACKER_FETCH_COMMENTS=True)
            _code, output = self._run_twg(twg)
            cache = self._cache()
        self.assertEqual(cache["ABC-1"]["comments"], WHOLE_THREAD)
        self.assertNotIn("comments_truncated", cache["ABC-1"])
        self.assertIn(
            [
                "jira",
                "workitem",
                "get",
                "--fields",
                "comment",
                "--comments",
                "-o",
                "json",
                "--output-summary",
                "none",
                "--",
                "ABC-1",
            ],
            twg.calls,
        )
        self.assertIn("(3 requests", output, "bulk-get, the move check and the thread")
        self.assertNotIn("Fen Untrue", json.dumps(cache))

    def test_a_thread_left_partial_is_marked_truncated(self):
        """Breaks if a thread that could not be completed - no follow-up budget
        left - is stored as if it were whole. The stored comments would read as
        the full conversation when its start is missing."""
        twg = FakeTwg(with_partial_thread(), threads={"ABC-1": twg_fixture("get-comments.json")})
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp, TRACKER_FETCH_COMMENTS=True, TRACKER_FOLLOWUP_MAX=0)
            self._run_twg(twg)
            cache = self._cache()
        self.assertEqual(cache["ABC-1"]["comments"], WHOLE_THREAD[-1:])
        self.assertIs(cache["ABC-1"]["comments_truncated"], True)
        self.assertFalse(any("--comments" in call for call in twg.calls))

    def test_no_thread_is_fetched_unless_comments_are_requested(self):
        """Breaks if thread completion runs with comments switched off, which
        fetches narrative nobody enabled and spends follow-ups on it."""
        twg = FakeTwg(with_partial_thread(), threads={"ABC-1": twg_fixture("get-comments.json")})
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp)
            self._run_twg(twg)
            record = self._cache()["ABC-1"]
        self.assertFalse(any("--comments" in call for call in twg.calls))
        self.assertNotIn("comments_truncated", record)


class ThreadTruncationTest(FetchTicketsTestCase):
    def test_a_thread_the_tracker_returned_part_of_is_marked_on_any_transport(self):
        """Breaks if the stored comments can read as a whole thread when the
        tracker said there were more. The mark is the library's, from the
        tracker's own total, so it holds for the HTTP route as well."""
        for total, marked in ((4, True), (1, False)):
            fields = fields_for(
                "First", comment={"comments": [{"body": "only one"}], "total": total}
            )
            with self.subTest(total=total), tempfile.TemporaryDirectory() as tmp:
                self._store(tmp, ["AAA-1"], TRACKER_FETCH_COMMENTS=True)
                self._run_stage(FakeTracker({"AAA-1": fields}))
                record = self._cache()["AAA-1"]
            self.assertEqual(record.get("comments_truncated", False), marked)
            self.assertEqual(record["comments"], ["only one"])

    def test_a_kept_longer_thread_is_marked_when_the_tracker_reports_more(self):
        """Breaks if a stored thread kept over a shorter refresh is shown as
        whole while the tracker has just reported more comments than it holds -
        the store would present 3 comments as the conversation when the tracker
        says there are 9. Against a reported total it already matches, it stays
        unmarked. Checked for a record that stored its count, and for one
        written before counts were stored."""
        for stored_total in (3, None):
            for reported, marked in ((9, True), (3, False)):
                stored = {
                    "checked": "2025-12-01",
                    "comments": ["one", "two", "three"],
                    "status": "Open",
                    "summary": "s",
                }
                if stored_total is not None:
                    stored["comments_total"] = stored_total
                later = open_fields("s")
                later["comment"] = {"comments": [{"body": "three"}], "total": reported}
                case = f"stored {stored_total}, reported {reported}"
                with self.subTest(case), tempfile.TemporaryDirectory() as tmp:
                    self._store(tmp, ["AAA-1"], TRACKER_FETCH_COMMENTS=True)
                    pio.write_gzip_json(config.TICKET_TRACKER_PATH, {"AAA-1": stored})
                    self._run_stage(FakeTracker({"AAA-1": later}))
                    record = self._cache()["AAA-1"]
                self.assertEqual(record["comments"], ["one", "two", "three"])
                self.assertEqual(record.get("comments_truncated", False), marked)
                self.assertEqual(record["comments_total"], reported, "the latest count is kept")

    def test_a_comment_dropped_as_noise_is_not_read_as_missing(self):
        """Breaks if the truncation mark compares the comments kept with the
        tracker's count: a whole thread with one automation comment dropped
        would read as partial on every ticket that had a build notice."""
        fields = fields_for(
            "s",
            comment={
                "comments": [{"body": "Jenkins build #1 failed"}, {"body": "real"}],
                "total": 2,
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, ["AAA-1"], TRACKER_FETCH_COMMENTS=True)
            self._run_stage(FakeTracker({"AAA-1": fields}))
            record = self._cache()["AAA-1"]
        self.assertEqual(record["comments"], ["real"])
        self.assertEqual(record["comments_total"], 2)
        self.assertNotIn("comments_truncated", record)


class RaisingTwg(FakeTwg):
    """A twg whose `get` - either kind - fails as a process: a timeout or a
    missing executable, which the real runner raises as OSError."""

    def __call__(self, arguments: list[str]) -> str:
        if arguments[2] == "get":
            self.calls.append(list(arguments))
            raise OSError("twg did not answer in time")
        return super().__call__(arguments)


class TwgFollowUpFailureTest(TwgTransportTestCase):
    def test_a_follow_up_that_fails_as_a_process_costs_only_its_key(self):
        """Breaks if a `get` that raises - a timeout - escapes the page: the
        bulk-get answers for every other key would be thrown away, and the run
        would count one call where twg ran two."""
        twg = RaisingTwg(twg_fixture("bulk-get.json"))
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp)
            _code, output = self._run_twg(twg)
            cache = self._cache()
        self.assertEqual(sorted(cache), ["ABC-1", "ABC-2"])
        self.assertIn("1 failed", output)
        self.assertIn("(2 requests", output, "the bulk-get and the get that ran")

    def test_a_thread_completion_that_fails_as_a_process_leaves_the_thread_marked(self):
        """Breaks if a failed `get --comments` escapes the page, losing every
        ticket in it, rather than leaving that one thread as returned and
        marked."""
        twg = RaisingTwg(with_partial_thread())
        with tempfile.TemporaryDirectory() as tmp:
            self._twg_store(tmp, TRACKER_FETCH_COMMENTS=True)
            _code, output = self._run_twg(twg)
            cache = self._cache()
        self.assertIs(cache["ABC-1"]["comments_truncated"], True)
        self.assertEqual(cache["ABC-1"]["comments_total"], 3)
        self.assertIn("ABC-2", cache)
        self.assertIn("(3 requests", output)
