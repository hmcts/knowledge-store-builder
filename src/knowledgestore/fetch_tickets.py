"""Ask the issue tracker what each discovered ticket is, once per ticket ever.

The `intent` stage finds ticket ids in commit messages; it cannot say what those
tickets were about beyond what the commits said. This stage asks the tracker, and
commits the answer to `knowledge/intent/ticket-tracker.json.gz`. Historic tickets
do not change, so a ticket that has been fetched is never fetched again - the
cache is the artefact, and a later build with no credentials reads it rather than
degrading.

    knowledgestore fetch-tickets

**The stage is opt-in and the pipeline is complete without it.** With no base URL
or token it prints which settings are missing and writes nothing.

Four things in here are deliberate, and each of them is a decision about risk
rather than about convenience.

**Three prefix states, not two.** A ticket prefix in `KSB_TRACKER_PROJECTS` is
fetched. A prefix in `KSB_TRACKER_DENY` is never requested, whatever the
allowlist says. Anything else is *undecided*: not requested, and written to
`knowledge/intent/tracker-undecided.json` with its ticket count for a person to
decide about. Failing closed on unknown prefixes discards tickets while reporting
nothing - on one estate that would have been the majority of them - and failing
open reads projects nobody authorised. Neither is acceptable silently.

**A denial is not an absence.** A token carries one person's permissions, so what
a run can read is whatever its operator can read. A 401 or 403 is cached as
`denied` and retried by later runs, because a run by someone with broader access
should find it. Recorded as absence it would never be retried, and the gap would
be permanent and invisible. A ticket genuinely missing from a response is cached
as `absent` and never re-requested. A 5xx or a dropped connection is not cached
at all: the tracker said nothing about the ticket, and caching that silence would
turn an outage into missing knowledge.

**The field projection is requested, not trimmed.** Description and comments are
added to the request only when their settings are on. A response that never
carried narrative text is a stronger guarantee than one that carried it and had
it discarded locally.

**The token is never written down.** Not in the cache, not in a summary line, not
in an error message, not in the `User-Agent`. A failed request reports its status
and the tickets it covered, and nothing else; the base URL is not echoed either,
because a URL can carry a credential. `tests/test_fetch_tickets.py` has one test
whose whole job is to hold that line.

Everything fetched goes through the same redaction as mined commit text
(`sensitive.redact`): tracker text is republished by the store exactly as commit
text is, and does not earn a weaker filter for being nominally cleaner.

The HTTP boundary is the `opener` argument to `main()`, which is where the tests
stub. Requests go out one at a time, batched as a JQL `key in (...)` search at
`KSB_TRACKER_PAGE_SIZE` keys each, with a pause between pages and `Retry-After`
honoured - a large estate becomes hundreds of requests rather than tens of
thousands, which is what keeps a first run inside rate limits and out of an
alert. The cache is written after every page, so a cancelled run resumes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import __version__, config, io, sensitive


# The projection every request asks for. Enumerations and dates, no narrative.
BASE_FIELDS = (
    "summary",
    "issuetype",
    "status",
    "resolution",
    "created",
    "resolutiondate",
    "parent",
)
# Named so anyone reading an access log can tell what this traffic is and find
# the tool. It carries no credential and no operator identity.
USER_AGENT = (
    f"knowledge-store-builder/{__version__} (+https://github.com/hmcts/knowledge-store-builder)"
)

DENIED_STATUSES = (401, 403)
TOO_MANY_REQUESTS = 429
# Attempts per page while the tracker is asking us to wait. After this the page
# is a failure, which means uncached and retried by the next run.
MAX_ATTEMPTS = 3
# However long a Retry-After asks for, stop short of hanging a build overnight.
MAX_RETRY_WAIT_SECONDS = 300.0
REQUEST_TIMEOUT_SECONDS = 30.0

# A record's date field: what the run knew, and when it knew it.
CHECKED = "checked"


@dataclass(frozen=True)
class Response:
    """What the HTTP boundary returns. Never the request, never a header."""

    status: int
    body: str
    retry_after: str | None = None


Opener = Callable[[str, dict], Response]


def open_url(url: str, headers: dict) -> Response:
    """The real HTTP boundary: one GET, no redirect handling beyond urllib's.

    An HTTP error status is a `Response`, not an exception, because a status is
    an answer the caller has to decide about. A transport failure raises
    `OSError` and is caught by `request_page`, which records it as a failure
    without quoting the error - a urllib error message can name the URL, and the
    URL is the one thing a caller might have put a credential in.
    """
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        # Sonar S310: the scheme is validated by tracker_root() before any URL
        # is built, and the host comes from this store's own configuration.
        with urllib.request.urlopen(  # NOSONAR(S310)
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            return Response(int(response.status), response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        # The error body is deliberately not read: nothing here needs it, and a
        # tracker's error page can echo the request back into a log.
        return Response(int(error.code), "", error.headers.get("Retry-After"))


def tracker_root(base: str) -> str:
    """The API root, validated. Raises for anything this stage will not request.

    The value is never included in the message. A base URL is a plausible place
    for someone to have put credentials, and an error message is a log line.
    """
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        raise ValueError(
            "KSB_TRACKER_BASE_URL must be a plain http(s) URL at the tracker's "
            "API root, for example https://tracker.example/jira, with no user "
            "information in the URL. The value is not repeated here, because a "
            "URL can carry a credential."
        )
    return base.rstrip("/")


def missing_settings() -> list[str]:
    """The settings without which this stage cannot run at all."""
    return [
        name
        for name, value in (
            ("KSB_TRACKER_BASE_URL", config.TRACKER_BASE_URL),
            ("KSB_TRACKER_TOKEN", config.TRACKER_TOKEN),
        )
        if not value
    ]


def request_headers() -> dict[str, str]:
    """The one place the credential appears. A Data Center personal access token
    is a bearer token, with no email or account name beside it."""
    return {
        "Authorization": f"Bearer {config.TRACKER_TOKEN}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def projected_fields() -> tuple[str, ...]:
    """The fields this run will ask for, narrative included only if asked for."""
    extra = [
        name
        for name, wanted in (
            ("description", config.TRACKER_FETCH_DESCRIPTION),
            ("comment", config.TRACKER_FETCH_COMMENTS),
        )
        if wanted
    ]
    return (*BASE_FIELDS, *extra)


def search_url(root: str, keys: Sequence[str], fields: Sequence[str]) -> str:
    """One search for a whole page of keys, projected server-side."""
    query = urllib.parse.urlencode(
        {
            "jql": "key in ({})".format(",".join(keys)),
            "fields": ",".join(fields),
            "maxResults": len(keys),
        }
    )
    return f"{root}/rest/api/2/search?{query}"


# --- which tickets may be asked about ------------------------------------


@dataclass
class Partition:
    """The discovered tickets, split by what their prefix says about them."""

    fetchable: list[str] = field(default_factory=list)
    undecided: dict[str, int] = field(default_factory=dict)
    withheld: dict[str, int] = field(default_factory=dict)

    @property
    def undecided_tickets(self) -> int:
        return sum(self.undecided.values())


def prefix_of(key: str) -> str:
    """`AAA` from `AAA-123`. A key with no separator is its own prefix."""
    return (key.rpartition("-")[0] or key).upper()


def discovered_keys() -> list[str]:
    """Every ticket id the intent stage found, in whatever order it stored them.

    Ordering is `partition()`'s job and only `partition()`'s job: one owner, so
    a sort removed from it is a sort that shows up in a test rather than one
    another function happens to be compensating for.
    """
    records = io.read_gzip_json_dict(config.TICKET_DESCRIPTIONS_PATH)
    return [key for key in records if isinstance(key, str) and "-" in key]


def partition(keys: Iterable[str]) -> Partition:
    """Split keys into fetchable, withheld and undecided.

    Deny wins over allow, deliberately: a prefix is withdrawn by adding it to the
    deny list, and a stale allowlist entry must not be able to undo that.

    The sort is the run's ordering guarantee: it fixes which tickets share a
    page, so two runs on the same estate ask for the same pages in the same
    order however the discovered mapping happens to be laid out.
    """
    allowed = {name.upper() for name in config.TRACKER_PROJECTS}
    refused = {name.upper() for name in config.TRACKER_DENY}
    fetchable: list[str] = []
    undecided: Counter[str] = Counter()
    withheld: Counter[str] = Counter()
    for key in sorted(keys):
        name = prefix_of(key)
        if name in refused:
            withheld[name] += 1
        elif name in allowed:
            fetchable.append(key)
        else:
            undecided[name] += 1
    return Partition(fetchable, dict(sorted(undecided.items())), dict(sorted(withheld.items())))


def write_undecided(part: Partition) -> None:
    """Write the prefixes nobody has decided about, with their ticket counts.

    Written every configured run, including when there are none: an absent file
    and an empty one would otherwise be the same thing, and only one of them
    means "everything discovered has been decided about".
    """
    io.write_json(
        config.TRACKER_UNDECIDED_PATH,
        {
            "undecided_prefixes": part.undecided,
            "tickets": part.undecided_tickets,
            "decide": (
                "Each prefix above appears in this estate's commit messages and is "
                "in neither KSB_TRACKER_PROJECTS nor KSB_TRACKER_DENY, so nothing "
                "was requested for it. Add it to KSB_TRACKER_PROJECTS to fetch it, "
                "or to KSB_TRACKER_DENY to never request it."
            ),
        },
        indent=2,
    )


# --- what a fetched ticket becomes ---------------------------------------

NAMED_FIELDS = (("type", "issuetype"), ("status", "status"), ("resolution", "resolution"))
DAY_FIELDS = (("created", "created"), ("resolved", "resolutiondate"))


def _text(value: object, counts: Counter[str]) -> str:
    """A fetched string, redacted. Anything that is not a string is nothing."""
    return sensitive.redact(value, counts) if isinstance(value, str) else ""


def _named(value: object, attribute: str = "name") -> str:
    return str(value[attribute]) if isinstance(value, dict) and value.get(attribute) else ""


def _day(value: object) -> str:
    """The date part of a tracker timestamp. The time of day is not evidence
    about the change and is one more detail about a person's working day."""
    return value[:10] if isinstance(value, str) else ""


def _comments(value: object, counts: Counter[str]) -> list[str]:
    raw = value.get("comments") if isinstance(value, dict) else None
    if not isinstance(raw, list):
        return []
    kept = []
    for comment in raw:
        body = comment.get("body") if isinstance(comment, dict) else None
        if isinstance(body, str) and body.strip():
            kept.append(sensitive.redact(body, counts))
    return kept


def projected_record(issue: dict, counts: Counter[str]) -> dict:
    """One cached ticket: the projected fields, redacted, key-sorted.

    Empty values are dropped rather than stored as empty, the same rule the
    intent stage follows - a field present but empty reads as evidence that was
    found rather than evidence that does not exist. `summary` always stays, so
    that a fetched record is never an empty mapping and cannot be mistaken for
    one of the other three outcomes.
    """
    fields = issue.get("fields")
    fields = fields if isinstance(fields, dict) else {}
    record: dict = {"summary": _text(fields.get("summary"), counts)}
    for name, source in NAMED_FIELDS:
        record[name] = _text(_named(fields.get(source)), counts)
    for name, source in DAY_FIELDS:
        record[name] = _day(fields.get(source))
    record["parent"] = _named(fields.get("parent"), "key")
    if config.TRACKER_FETCH_DESCRIPTION:
        record["description"] = _text(fields.get("description"), counts)
    if config.TRACKER_FETCH_COMMENTS:
        record["comments"] = _comments(fields.get("comment"), counts)
    return {name: value for name, value in sorted(record.items()) if value or name == "summary"}


def needs_request(record: object) -> bool:
    """Whether a cached record should be asked about again.

    Fetched and absent are final. Denied is not: it records what one token could
    read, and a later run may hold broader permissions. Anything that is not a
    mapping is a cache written by another version, and is re-fetched.
    """
    if not isinstance(record, dict):
        return True
    return bool(record.get("denied"))


# --- the run -------------------------------------------------------------


@dataclass
class Report:
    fetched: int = 0
    absent: int = 0
    denied: int = 0
    failed: int = 0
    cached: int = 0
    requests: int = 0
    redactions: Counter[str] = field(default_factory=Counter)
    # Statuses that carried no answer, and how many tickets each covered.
    failures: Counter[int] = field(default_factory=Counter)


def pages(keys: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(keys), size):
        yield list(keys[start : start + size])


def retry_delay(retry_after: str | None) -> float:
    """How long to wait before retrying a throttled page."""
    try:
        wait = float(str(retry_after).strip())
    except ValueError:
        wait = config.TRACKER_DELAY_SECONDS
    return min(max(wait, 0.0), MAX_RETRY_WAIT_SECONDS)


def request_page(
    url: str, headers: dict, opener: Opener, sleep: Callable[[float], None]
) -> Response | None:
    """One page, waiting as long as the tracker asks. None means no answer.

    A transport failure and repeated throttling are the same outcome - the
    tracker said nothing about these tickets - and neither is recorded in the
    cache. The error itself is not quoted anywhere.
    """
    for _ in range(MAX_ATTEMPTS):
        try:
            response = opener(url, headers)
        except OSError:
            return None
        if response.status != TOO_MANY_REQUESTS:
            return response
        sleep(retry_delay(response.retry_after))
    return None


def _issues(body: str) -> dict[str, dict] | None:
    """Issues by key, or None when the body is not a search response."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    issues = payload.get("issues") if isinstance(payload, dict) else None
    if not isinstance(issues, list):
        return None
    return {
        str(issue["key"]): issue for issue in issues if isinstance(issue, dict) and issue.get("key")
    }


def _record_outcomes(
    keys: Sequence[str], issues: dict, cache: dict, report: Report, today: str
) -> None:
    for key in keys:
        issue = issues.get(key)
        if issue is None:
            cache[key] = {"absent": True, CHECKED: today}
            report.absent += 1
        else:
            cache[key] = projected_record(issue, report.redactions)
            report.fetched += 1


def apply_page(
    keys: Sequence[str], response: Response | None, cache: dict, report: Report, today: str
) -> None:
    """Turn one page's reply into cache entries, one of the four outcomes each."""
    if response is None:
        report.failed += len(keys)
        report.failures[0] += len(keys)
        return
    if response.status in DENIED_STATUSES:
        for key in keys:
            cache[key] = {"denied": True, CHECKED: today}
        report.denied += len(keys)
        return
    issues = _issues(response.body) if response.status == 200 else None
    if issues is None:
        report.failed += len(keys)
        report.failures[response.status] += len(keys)
        return
    _record_outcomes(keys, issues, cache, report, today)


def write_cache(cache: dict) -> None:
    """Key-sorted, through the deterministic gzip writer, so an unchanged cache
    is byte-identical and a committed artefact does not churn."""
    io.write_gzip_json(config.TICKET_TRACKER_PATH, dict(sorted(cache.items())))


def fetch(
    keys: Sequence[str],
    cache: dict,
    *,
    root: str,
    opener: Opener,
    sleep: Callable[[float], None],
    today: str,
) -> Report:
    """Fetch every outstanding key, a page at a time, writing as it goes."""
    report = Report()
    outstanding = [key for key in keys if needs_request(cache.get(key))]
    report.cached = len(keys) - len(outstanding)
    fields = projected_fields()
    headers = request_headers()
    for index, page in enumerate(pages(outstanding, max(1, config.TRACKER_PAGE_SIZE))):
        if index:
            sleep(config.TRACKER_DELAY_SECONDS)
        response = request_page(search_url(root, page, fields), headers, opener, sleep)
        report.requests += 1
        apply_page(page, response, cache, report, today)
        # After every page, not at the end of the run: a throttled or cancelled
        # run must leave a usable cache and resume where it stopped.
        write_cache(cache)
    return report


# --- what the run says ---------------------------------------------------


def _report_failures(report: Report) -> None:
    if not report.failures:
        return
    named = ", ".join(
        f"{status or 'no response'} ({count:,} tickets)"
        for status, count in sorted(report.failures.items())
    )
    print(f"  replies that carried no answer, so nothing was cached: {named}")


def _report_redactions(report: Report) -> None:
    """Always, including nothing-redacted: a silent filter cannot be told from
    an estate with nothing to withhold, and the two call for opposite responses.
    The identifiers themselves are never printed."""
    total = sum(report.redactions.values())
    named = ", ".join(
        f"{rule} ({count:,})"
        for rule, count in sorted(report.redactions.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    tail = f": {named}" if named else " (no fetched text matched a redaction rule)"
    print(f"  redacted {total:,} identifiers in fetched text{tail}")


def _report_prefixes(part: Partition) -> None:
    if part.withheld:
        named = ", ".join(f"{name} ({count:,})" for name, count in part.withheld.items())
        print(f"  never requested, KSB_TRACKER_DENY: {named}")
    print(
        f"  undecided prefixes: {len(part.undecided)} covering "
        f"{part.undecided_tickets:,} tickets, none requested "
        f"-> {config.TRACKER_UNDECIDED_PATH}"
    )


def summarise(report: Report, cache: dict, part: Partition) -> None:
    print(
        f"Tracker: {report.fetched:,} fetched, {report.absent:,} absent, "
        f"{report.denied:,} denied, {report.failed:,} failed "
        f"({report.requests:,} requests, {report.cached:,} already cached)"
    )
    _report_failures(report)
    _report_redactions(report)
    _report_prefixes(part)
    # Reported every run, even at zero. A denial says what this token could read,
    # so an unreported one becomes a permanent invisible gap in the store.
    waiting = sum(
        1 for record in cache.values() if isinstance(record, dict) and record.get("denied")
    )
    print(
        f"  {waiting:,} tickets are waiting on access this token does not have; "
        "a run with broader permissions will retry them"
    )
    print(f"  wrote {config.TICKET_TRACKER_PATH} ({len(cache):,} tickets)")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def main(
    argv: list[str] | None = None,
    opener: Opener = open_url,
    sleep: Callable[[float], None] = time.sleep,
    today: str = "",
) -> int:
    parser = argparse.ArgumentParser(prog="knowledgestore fetch-tickets", add_help=False)
    parser.add_argument("-h", "--help", action="help")
    parser.parse_args(sys.argv[1:] if argv is None else argv)

    missing = missing_settings()
    if missing:
        print(f"fetch-tickets is not configured: {' and '.join(missing)} not set.")
        print(
            "Nothing was written. The stage is optional - the pipeline is "
            "complete without it, and a store with no tracker credentials reads "
            "whatever a credentialled run last committed."
        )
        return 0
    try:
        root = tracker_root(config.TRACKER_BASE_URL)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1

    keys = discovered_keys()
    if not keys:
        print(
            f"No discovered tickets in {config.TICKET_DESCRIPTIONS_PATH}. "
            "Run: knowledgestore intent",
            file=sys.stderr,
        )
        return 1

    part = partition(keys)
    write_undecided(part)
    cache = io.read_gzip_json_dict(config.TICKET_TRACKER_PATH)
    report = fetch(
        part.fetchable,
        cache,
        root=root,
        opener=opener,
        sleep=sleep,
        today=today or _today(),
    )
    write_cache(cache)
    summarise(report, cache, part)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
