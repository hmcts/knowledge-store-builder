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
reach the store. Unknown inline nodes render as nothing for the same reason - a
node type this module has not seen is not trusted to carry only text. Comment
authors are dropped before the library sees the comment.

**The exit code is not the answer.** `bulk-get` exits 3 when any key in the page
is unreturned, and stdout still carries the complete per-item result. stdout is
parsed whatever the exit code; only output that does not parse - or that does
not mention every key asked about - is no answer, which the library leaves
uncached and retries next run.

**Moves need a second request.** `bulk-get` does not follow a ticket moved to
another project: it reports the old key as unreturned. Each unreturned key is
asked about once more with `twg jira workitem get`, which does follow a move. A
moved ticket is filed under the key the commits cite, and only when the library
says its new project may be read; one that moved to an undecided or denied
project stays absent.

The process boundary is the `runner` argument: a callable taking `twg`'s
arguments and returning its stdout. Tests stub it there; `run_twg` is the real
one.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse
from collections.abc import Callable, Sequence

# The root `fetch_tickets` builds its request URL on when the transport is twg.
# Nothing is ever sent to it: the opener reads the keys and fields back out.
ROOT = "twg://jira"
# One page of keys, plus Jira's own latency. Past this the page is no answer.
TIMEOUT_SECONDS = 300.0
# The status `twg jira workitem get` reports for a key that does not exist, or
# that this sign-in cannot see. Any other error says nothing about the ticket.
NOT_FOUND = 404

Runner = Callable[[list[str]], str]

# Inline ADF nodes, and what each contributes to its line. Anything else is a
# block, and its children are read.
INLINE_NODES = frozenset(
    {"text", "mention", "hardBreak", "emoji", "inlineCard", "date", "status", "mediaInline"}
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
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise OSError("twg did not answer in time") from error
    return completed.stdout


# --- ADF to text ---------------------------------------------------------


def _inline(node: dict) -> str:
    """One inline node's text. A mention is its account id, never its name."""
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


def requested(url: str) -> tuple[list[str], list[str]]:
    """The keys and fields in a search URL `fetch_tickets.search_url` built."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    jql = query.get("jql", [""])[0]
    inside = jql[jql.find("(") + 1 : jql.rfind(")")] if "(" in jql else ""
    keys = [key.strip() for key in inside.split(",") if key.strip()]
    fields = [name for name in query.get("fields", [""])[0].split(",") if name]
    return keys, fields


def _comment(value: object) -> object:
    """Comment bodies flattened, authors dropped. The library reads only `body`."""
    if not isinstance(value, dict) or not isinstance(value.get("comments"), list):
        return value
    return {
        "comments": [
            {"body": flatten(comment.get("body"))}
            for comment in value["comments"]
            if isinstance(comment, dict)
        ]
    }


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
    """`twg` printed nothing this module can read. Raised through the opener,
    where `fetch_tickets` records it as no answer and caches nothing."""


def followed(runner: Runner, key: str, fields: Sequence[str]) -> dict | None:
    """Ask `get`, which follows a move, about one key `bulk-get` did not return.

    None when the tracker says the key does not exist for this sign-in, which
    is an answer. Anything unreadable is not, and raises."""
    payload = _parsed(runner(_arguments("get", [key], fields)))
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        for issue in payload["data"]:
            if isinstance(issue, dict) and issue.get("key"):
                return issue
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("statusCode") == NOT_FOUND:
        return None
    raise NoAnswer("twg get gave no readable answer")


def _arguments(command: str, keys: Sequence[str], fields: Sequence[str]) -> list[str]:
    return [
        "jira",
        "workitem",
        command,
        *keys,
        "--fields",
        ",".join(fields),
        "-o",
        "json",
        "--output-summary",
        "none",
    ]


def answer(runner: Runner, url: str, may_read: Callable[[str], bool]) -> str:
    """The v2 search body for one page the library asked for.

    `may_read` is the library's own allowlist decision, applied to the key a
    moved ticket now has. Raises `NoAnswer` when `twg` said nothing readable
    about every key in the page.
    """
    keys, fields = requested(url)
    items = bulk_items(runner(_arguments("bulk-get", keys, fields)))
    # Every key must be mentioned, as returned or as unreturned. One that is not
    # is output this module does not understand - including output that did not
    # parse at all - and absent is final, so it must not be read as absence.
    if any(key not in items for key in keys):
        raise NoAnswer("twg bulk-get gave no readable answer for the whole page")
    issues = []
    for key in keys:
        item = items.get(key, {})
        issue = item.get("data") if item.get("ok") else None
        if not isinstance(issue, dict):
            issue = followed(runner, key, fields)
        if issue is None:
            continue
        found = str(issue.get("key") or key)
        if found.upper() != key.upper() and not may_read(found):
            continue
        issues.append({"key": key, "fields": projected(issue, fields)})
    return json.dumps({"issues": issues})
