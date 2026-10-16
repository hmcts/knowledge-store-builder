"""The `twg` transport for `fetch-tickets`: Jira Cloud through the Atlassian TWG CLI.

    KSB_TRACKER_TRANSPORT=twg knowledgestore fetch-tickets

`fetch_tickets` speaks Jira Data Center over HTTP, and Cloud has removed the
search endpoint it uses. This module is the other transport behind the same
`opener` seam, and it is only a transport: it answers the request the library
built - a page of keys and a field projection - with the Data Center search
shape, `{"issues": [{"key", "fields"}]}`. Every decision stays in
`fetch_tickets`: which prefixes may be read, what is projected, redaction, the
four cache outcomes and the writer. `twg` holds its own sign-in, so no token
passes through this library at all.

Five things in here were each found on a real run (#420):

**The request is translated, not widened.** The keys come out of the JQL the
library built and the fields out of its projection, and `bulk-get` is asked for
exactly those. Narrative fields are therefore requested only when their
settings are on, exactly as on the HTTP route.

**Cloud bodies are documents, not strings.** Descriptions and comments arrive as
Atlassian Document Format, and the library keeps only strings, so an unflattened
body would store nothing. Each block becomes one line.

**A mention is an account id, never a name.** An ADF mention node carries the
person's display name in `attrs.text`. It is rendered as `[~accountid:<id>]`,
the form Data Center bodies already use, and `attrs.text` is never read. That is
a privacy requirement rather than a formatting choice: a person's name must not
reach the store. For the same reason no node's `attrs.text` is read except
an emoji's, which holds the emoji's character: status, date and placeholder
nodes render as nothing, and a node type this module does not know is read as a
block - its `content` children render, its attributes do not. Comment authors are dropped before the library sees the
comment.

**The exit code is not the answer.** `bulk-get` exits 3 when any key in the page
is unreturned, and stdout still carries the complete per-item result. stdout is
parsed whatever the exit code, as UTF-8 with undecodable bytes replaced; only
output that does not parse - or that does not mention every key asked about - is
no answer, which the library leaves uncached and retries next run.

**Moves need a second request.** `bulk-get` does not follow a ticket moved to
another project: it reports the old key as unreturned. Each unreturned key is
asked about once more with `twg jira workitem get`, which does follow a move. A
moved ticket is filed under the key the commits cite, and only when the library
says its new project may be read; one that moved to an undecided or denied
project stays absent. Only `get`'s own not-found (404) is absence: any other
error is no answer for that key. Follow-ups are paced like pages, counted into
the run's requests, and bounded per run by `KSB_TRACKER_FOLLOWUP_MAX` - see
`Session`.

The process boundary is the `runner` argument: a callable taking `twg`'s
arguments and returning its stdout. Tests stub it there; `run_twg` is the real
one.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# The root `fetch_tickets` builds its request URL on when the transport is twg.
# Nothing is ever sent to it: the opener reads the keys and fields back out.
ROOT = "twg://jira"
# One page of keys, plus Jira's own latency. Past this the page is no answer.
TIMEOUT_SECONDS = 300.0
# The status `twg jira workitem get` reports for a key that does not exist, or
# that this sign-in cannot see. Any other error - a 429, a 401, a 5xx - says
# nothing about the ticket, and is no answer for that key.
NOT_FOUND = 404
# What a key must look like before it is put on twg's command line. The keys
# come from commit messages through a configurable pattern; anything else is
# refused rather than handed to a process as an argument.
TICKET_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[0-9]+")

Runner = Callable[[list[str]], str]

# Inline ADF nodes, and what each contributes to its line - see `_inline`.
# Anything else is read as a block: its `content` children are rendered and its
# attributes never are.
INLINE_NODES = frozenset(
    {
        "text",
        "mention",
        "hardBreak",
        "emoji",
        "inlineCard",
        "date",
        "status",
        "placeholder",
        "mediaInline",
    }
)


def run_twg(arguments: list[str]) -> str:
    """The real process boundary: run `twg` and return its stdout.

    The exit code is deliberately not read - see the module docstring. A missing
    executable and a timeout both raise `OSError`, which `fetch_tickets` records
    as no answer. stderr is not kept: nothing here needs it, and it is not
    quoted anywhere.
    """
    try:
        completed = subprocess.run(
            ["twg", *arguments],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise OSError("twg did not answer in time") from error
    return completed.stdout


# --- ADF to text ---------------------------------------------------------


def _inline(node: dict) -> str:
    """One inline node's text. A mention is its account id, never its name.

    Text, an emoji and a link card render. Every other inline node - status,
    date, placeholder, media and any type not listed - renders as nothing,
    because what it would show lives in `attrs`, and `attrs.text` is where ADF
    puts a display name. Nothing here reads it except for an emoji, where it
    holds the emoji's character.
    """
    kind = node.get("type")
    raw = node.get("attrs")
    attrs: dict = raw if isinstance(raw, dict) else {}
    if kind == "text":
        value = node.get("text")
        return value if isinstance(value, str) else ""
    if kind == "mention":
        account = attrs.get("id")
        return f"[~accountid:{account}]" if isinstance(account, str) and account else ""
    if kind == "hardBreak":
        return " "
    if kind == "emoji":
        value = attrs.get("text") or attrs.get("shortName")
        return value if isinstance(value, str) else ""
    if kind == "inlineCard":
        value = attrs.get("url")
        return value if isinstance(value, str) else ""
    return ""


def _lines(node: dict, out: list[str]) -> None:
    """Append one line per block under `node`. A run of inline nodes is a line."""
    run: list[str] = []
    for child in node.get("content") or []:
        if not isinstance(child, dict):
            continue
        if child.get("type") in INLINE_NODES:
            run.append(_inline(child))
            continue
        _flush(run, out)
        run = []
        _lines(child, out)
    _flush(run, out)


def _flush(run: list[str], out: list[str]) -> None:
    line = "".join(run).strip()
    if line:
        out.append(line)


def flatten(body: object) -> object:
    """An ADF document as text, one line per block. Anything that is not a
    document is returned unchanged, so a plain string body stays a string."""
    if not isinstance(body, dict):
        return body
    out: list[str] = []
    _lines(body, out)
    return "\n".join(out)


# --- the request the library built, and the answer it expects -------------


def requested(url: str) -> tuple[list[str], list[str], list[str]]:
    """The keys and fields in a search URL `fetch_tickets.search_url` built, and
    the entries refused as keys.

    Every key must look like a ticket key before it goes near a command line.
    Anything else is refused on its own - no answer for it, asked about again
    next run - and the rest of its page is still asked about.
    """
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    jql = query.get("jql", [""])[0]
    inside = jql[jql.find("(") + 1 : jql.rfind(")")] if "(" in jql else ""
    entries = [key.strip() for key in inside.split(",") if key.strip()]
    keys = [key for key in entries if TICKET_KEY.fullmatch(key)]
    refused = [key for key in entries if not TICKET_KEY.fullmatch(key)]
    fields = [name for name in query.get("fields", [""])[0].split(",") if name]
    return keys, fields, refused


def _comment(value: object) -> object:
    """Comment bodies flattened, authors dropped. The library reads only `body`,
    and `total`, which is how it tells a partial thread from a whole one."""
    if not isinstance(value, dict) or not isinstance(value.get("comments"), list):
        return value
    out: dict = {
        "comments": [
            {"body": flatten(comment.get("body"))}
            for comment in value["comments"]
            if isinstance(comment, dict)
        ]
    }
    if isinstance(value.get("total"), int):
        out["total"] = value["total"]
    return out


def partial_thread(issue: dict) -> bool:
    """Whether `bulk-get` returned fewer comments than the ticket has. It
    returns only the most recent page of a long thread (#431)."""
    comment = issue.get("comment")
    if not isinstance(comment, dict):
        return False
    total, comments = comment.get("total"), comment.get("comments")
    return isinstance(total, int) and isinstance(comments, list) and total > len(comments)


def projected(issue: dict, fields: Sequence[str]) -> dict:
    """The v2 `fields` object for one `twg` work item: the requested fields
    only, with document bodies flattened. `twg` puts the fields beside the key
    rather than under `fields`; both layouts are read."""
    source = issue["fields"] if isinstance(issue.get("fields"), dict) else issue
    out: dict = {}
    for name in fields:
        if name not in source:
            continue
        value = source[name]
        if name == "description":
            value = flatten(value)
        elif name == "comment":
            value = _comment(value)
        out[name] = value
    return out


def _parsed(stdout: str) -> object:
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None


def bulk_items(stdout: str) -> dict[str, dict]:
    """`bulk-get`'s items by the key asked about. Output that does not parse
    mentions no key, which `answer` refuses like any other omission."""
    payload = _parsed(stdout)
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}
    return {
        str(item["input"]): item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("input"), str)
    }


class NoAnswer(OSError):
    """`twg` printed nothing this module can read about a whole page. Raised
    through the opener, where `fetch_tickets` records it as no answer and caches
    nothing. `calls` is how many times twg ran before the page was given up."""

    def __init__(self, message: str, calls: int = 0) -> None:
        super().__init__(message)
        self.calls = calls


# What a follow-up `get` said about one key.
ABSENT = "absent"  # the tracker's own not-found: an answer
UNANSWERED = "unanswered"  # anything else: no answer, asked again next run


def _arguments(command: str, keys: Sequence[str], fields: Sequence[str], *extra: str) -> list[str]:
    """twg's argument list. The keys go after `--`, so none can be read as an
    option whatever it looks like."""
    return [
        "jira",
        "workitem",
        command,
        "--fields",
        ",".join(fields),
        *extra,
        "-o",
        "json",
        "--output-summary",
        "none",
        "--",
        *keys,
    ]


@dataclass
class Session:
    """One run's use of twg: every call paced and counted, follow-ups budgeted.

    `bulk-get` answers a page in one call, but an unreturned key costs a second
    `get` to learn whether it moved. Those follow-ups are what could turn a page
    into a hundred calls, so a run may make at most `followups` of them; a key
    past the budget is no answer and is asked about again next run. Every call
    after the first waits `delay` seconds, the same pause the library keeps
    between pages, and `calls` is what the page cost - which the library adds to
    its request counts.
    """

    runner: Runner
    may_read: Callable[[str], bool]
    sleep: Callable[[float], None]
    delay: float
    followups: int
    calls: int = 0

    def run(self, arguments: list[str]) -> str:
        if self.calls:
            self.sleep(self.delay)
        self.calls += 1
        return self.runner(arguments)

    def followed(self, key: str, fields: Sequence[str]) -> dict | str:
        """Ask `get`, which follows a move, about one key `bulk-get` did not
        return: the issue, `ABSENT` on the tracker's own not-found, or
        `UNANSWERED` for anything else - including no budget left to ask."""
        if self.followups <= 0:
            return UNANSWERED
        self.followups -= 1
        try:
            payload = _parsed(self.run(_arguments("get", [key], fields)))
        except OSError:
            # A timeout or a missing executable: counted, and no answer for
            # this key alone - the rest of the page stands.
            return UNANSWERED
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            for issue in payload["data"]:
                if isinstance(issue, dict) and issue.get("key"):
                    return issue
            return UNANSWERED
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and error.get("statusCode") == NOT_FOUND:
            return ABSENT
        return UNANSWERED

    def completed(self, key: str, issue: dict, fields: Sequence[str]) -> dict:
        """The issue with its whole comment thread, when `bulk-get` returned
        part of it: one `get --comments`, which returns the complete thread,
        from the same follow-up budget. Anything short of a longer thread -
        no budget, unreadable output, an error - leaves the issue as it was,
        and the library marks the thread truncated."""
        if "comment" not in fields or not partial_thread(issue) or self.followups <= 0:
            return issue
        self.followups -= 1
        try:
            payload = _parsed(self.run(_arguments("get", [key], ["comment"], "--comments")))
        except OSError:
            return issue
        data = payload.get("data") if isinstance(payload, dict) else None
        whole = (
            data[0].get("comment")
            if isinstance(data, list) and data and isinstance(data[0], dict)
            else None
        )
        held = issue["comment"].get("comments") or []
        if isinstance(whole, dict) and len(whole.get("comments") or []) > len(held):
            return {**issue, "comment": whole}
        return issue

    def answer(self, url: str) -> tuple[str, list[str]]:
        """The v2 search body for one page the library asked for, and the keys
        it has no answer for.

        `may_read` is the library's own allowlist decision, applied to the key a
        moved ticket now has. Raises `NoAnswer` when twg said nothing readable
        about the page as a whole.
        """
        self.calls = 0
        keys, fields, refused = requested(url)
        if not keys:
            return json.dumps({"issues": []}), refused
        items = bulk_items(self.run(_arguments("bulk-get", keys, fields)))
        # Every key must be mentioned, as returned or as unreturned. One that is
        # not is output this module does not understand - including output that
        # did not parse at all - and absent is final, so it must not be read as
        # absence.
        if any(key not in items for key in keys):
            raise NoAnswer("twg bulk-get gave no readable answer", self.calls)
        issues, unanswered = [], list(refused)
        for key in keys:
            item = items.get(key, {})
            issue = item.get("data") if item.get("ok") else None
            if not isinstance(issue, dict):
                issue = self.followed(key, fields)
            if issue == UNANSWERED:
                unanswered.append(key)
                continue
            if not isinstance(issue, dict):
                continue
            found = str(issue.get("key") or key)
            if found.upper() != key.upper() and not self.may_read(found):
                continue
            issue = self.completed(found, issue, fields)
            issues.append({"key": key, "fields": projected(issue, fields)})
        return json.dumps({"issues": issues}), unanswered
