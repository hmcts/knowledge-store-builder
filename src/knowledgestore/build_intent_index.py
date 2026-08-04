"""Build the file -> Jira-ticket intent index from the Git-history datasets.

For every source file in the estate, records which Jira tickets the non-merge
commits that touched it name, with first/last touch dates. Ticket ids and
descriptions come from the commit subject, and - where the subject offers
nothing - from the commit body, once that body has been reduced to prose.
This answers "what business change shaped this file?" as a lookup, e.g.:

    import gzip, json
    index = json.load(gzip.open('knowledge/intent/file-tickets.json.gz', 'rt'))
    index['cpp-ui-defence']['src/app/shared/pipes/address.pipe.ts']

Requires knowledge/git-history/ (regenerate with
`knowledgestore export-history` if absent). The output is small and
committed, so consumers get intent lookups without regenerating history.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


from . import config, io


TICKET_PREFIX = re.compile(r"^[\s\[\(]*[A-Z][A-Z0-9]{1,9}-\d{1,6}[\]\)]*[\s:,\-–]*")
JUNK_DESCRIPTION = re.compile(
    r"^(wip|fix(es|ed)?|updated?|changes?|test(s|ing)?|merge.*|minor.*|"
    r"address(ed|ing)? (pr |review )?comments?.*|pr comments?.*|refactor(ing)?)$",
    re.IGNORECASE,
)
MIN_DESCRIPTION_CHARS = 12

# Commit bodies. Most are not prose: trailers, separators, a merge's list of
# commits, a bot's dependency metadata, or the authors of a squashed range. Each
# rule below removes one of those, so what survives is something a person wrote
# about the change.
TRAILER_PREFIXES = (
    "co-authored-by:",
    "signed-off-by:",
    "reviewed-by:",
    "change-id:",
    "cherry-picked:",
    "on-behalf-of:",
)
SEPARATOR_LINE = re.compile(r"^[-=]{3,}$")
BOT_LINE = re.compile(
    r"^(bumps \[|dependency-type:|- \[release notes\]|updated-dependencies:)", re.IGNORECASE
)
BOT_NAME = re.compile(r"dependabot", re.IGNORECASE)
AUTHOR_LINE = "author: "

# A body is evidence only if a person wrote it, and the reliable signal is who
# the commit says wrote it - not what the body says. Text markers catch the
# phrasing of one bot; identity catches automation as a class, including bots
# this estate has not met yet. Renovate writes "Update dependency X to v2", so
# a filter tuned to Dependabot's "Bumps [" would let all of it through.
BOT_ACCOUNT = "[bot]"
# Automation predating or outside the GitHub App [bot] convention. Whole-word
# matched, so `snyk` also catches `snyk-bot`.
BOT_IDENTITY = re.compile(
    r"\b(jenkins|renovate|snyk|greenkeeper|devops-team|embedded_devops_sa)\b", re.IGNORECASE
)
MERGE_BULLET = "* "
MIN_MERGE_BULLETS = 2
MIN_BODY_CHARS = 25
# One body must not attribute a file to every ticket it happens to list.
BODY_TICKET_LIMIT = 3
BODY_DESCRIPTION_CHARS = 300

# Contributor and pull-request templates differ between teams and between
# estates, so no shipped pattern list can recognise them. They are instead
# learned from the repository being indexed: a template line repeats verbatim
# across commits, and genuine prose does not. Both thresholds must hold, so a
# small repository cannot have real sentences mistaken for boilerplate.
BOILERPLATE_MIN_COMMITS = 5
BOILERPLATE_MIN_PERCENT = 2
BOILERPLATE_MIN_LINE_CHARS = 8


@dataclass
class BodyReport:
    """Why commit bodies were discarded, so a run can say which net caught what."""

    boilerplate_lines: int = 0
    boilerplate_emptied: int = 0
    automated: int = 0
    bot_text: int = 0


def clean_description(subject: str) -> str:
    """Strip leading ticket references, leaving the human description."""
    cleaned = subject.strip()
    while True:
        stripped = TICKET_PREFIX.sub("", cleaned, count=1)
        if stripped == cleaned:
            break
        cleaned = stripped.strip()
    return cleaned.strip(" .")


def normalise_line(line: str) -> str:
    """The counting key for a body line: case- and whitespace-insensitive.

    Only ever a key. A line that survives keeps its original text.
    """
    return re.sub(r"\s+", " ", line.strip()).lower()


def count_body_lines(ndjson: Path) -> tuple[dict[str, int], int]:
    """Per normalised body line, how many of a repository's commits carry it,
    and how many of its commits have a body at all."""
    counts: dict[str, int] = defaultdict(int)
    bodies = 0
    with ndjson.open(encoding="utf-8") as source:
        for line in source:
            body = str(json.loads(line).get("body") or "")
            if not body.strip():
                continue
            bodies += 1
            keys = {normalise_line(text) for text in body.splitlines()}
            for key in sorted(keys):
                if len(key) >= BOILERPLATE_MIN_LINE_CHARS:
                    counts[key] += 1
    return dict(counts), bodies


def learn_boilerplate(counts: dict[str, int], bodies: int) -> frozenset[str]:
    """The normalised lines a repository repeats often enough to be a template.

    A line must appear in at least BOILERPLATE_MIN_COMMITS commits *and* in
    BOILERPLATE_MIN_PERCENT of that repository's bodies. The first keeps a
    handful of coincidental repeats out; the second scales the bar with the
    repository, so a busy one needs far more than five.
    """
    return frozenset(
        key
        for key, seen in sorted(counts.items())
        if seen >= BOILERPLATE_MIN_COMMITS and seen * 100 >= bodies * BOILERPLATE_MIN_PERCENT
    )


def _keep_line(line: str, boilerplate: frozenset[str]) -> bool:
    """Whether one body line is prose rather than plumbing."""
    probe = line.lstrip()
    if probe.lower().startswith(TRAILER_PREFIXES):
        return False
    if SEPARATOR_LINE.match(probe):
        return False
    return normalise_line(line) not in boilerplate


def _rejection(cleaned: str) -> str:
    """Why what survived line filtering is still not usable prose, or "" when
    it is. The reason is reported, so an operator can see which net caught what."""
    lines = [line.lstrip() for line in cleaned.split("\n")]
    if BOT_NAME.search(cleaned) or any(BOT_LINE.match(line) for line in lines):
        return "bot-text"
    if any(line.lower().startswith(AUTHOR_LINE) for line in lines):
        return "squashed-authors"
    if sum(1 for line in lines if line.startswith(MERGE_BULLET)) >= MIN_MERGE_BULLETS:
        return "merge-list"
    return "too-short" if len(cleaned) < MIN_BODY_CHARS else ""


def _body_blocks(body: str, boilerplate: frozenset[str]) -> list[list[str]]:
    """A body's usable lines, grouped into the paragraphs its author wrote."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line:
            if current:
                blocks.append(current)
                current = []
        elif _keep_line(line, boilerplate):
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _flatten(blocks: list[list[str]]) -> str:
    return "\n".join(line for block in blocks for line in block)


def body_rejection(body: str, boilerplate: frozenset[str] = frozenset()) -> str:
    """Why a body yields no usable prose, or "" when it yields some."""
    return _rejection(_flatten(_body_blocks(body, boilerplate)))


def _usable_blocks(body: str, boilerplate: frozenset[str]) -> list[list[str]]:
    """A body's paragraphs, or none at all when the body is not prose."""
    blocks = _body_blocks(body, boilerplate)
    return [] if _rejection(_flatten(blocks)) else blocks


def clean_body(body: str, boilerplate: frozenset[str] = frozenset()) -> str:
    """Reduce a raw commit body to usable prose, or to empty when it holds none."""
    return _flatten(_usable_blocks(body, boilerplate))


def _truncate(text: str, limit: int) -> str:
    """Cut to at most limit characters, at a word boundary."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    if " " in head:
        head = head[: head.rindex(" ")]
    return head.rstrip()


def body_ticket_ids(body: str, boilerplate: frozenset[str] = frozenset()) -> list[str]:
    """Ticket ids a body's prose names, in first-appearance order, capped.

    First-appearance rather than set order because output has to be identical
    between runs, and set iteration order is not.
    """
    ids: list[str] = []
    for ticket in config.TICKET_PATTERN.findall(clean_body(body, boilerplate)):
        if ticket not in ids:
            ids.append(ticket)
        if len(ids) == BODY_TICKET_LIMIT:
            break
    return ids


def body_description(body: str, boilerplate: frozenset[str] = frozenset()) -> str:
    """A body's opening paragraph, truncated at a word boundary."""
    blocks = _usable_blocks(body, boilerplate)
    if not blocks:
        return ""
    return _truncate(" ".join(blocks[0]), BODY_DESCRIPTION_CHARS)


def _automated_identity(person: object) -> bool:
    """Whether one author or committer identity is a machine."""
    if not isinstance(person, dict):
        return False
    name = str(person.get("name") or "")
    email = str(person.get("email") or "")
    if BOT_ACCOUNT in name.lower() or BOT_ACCOUNT in email.lower():
        return True
    # The local part only: real contributors use NNNNNN+user@users.noreply.github.com
    # addresses, so the host is never a bot signal.
    return bool(BOT_IDENTITY.search(name) or BOT_IDENTITY.search(email.split("@")[0]))


def is_automated(commit: dict) -> bool:
    """Whether a commit's body was written by a machine rather than a person.

    Author and committer are both checked: a bot's commit can be committed by a
    person's merge, and a person's commit can be committed by automation.
    """
    return any(_automated_identity(commit.get(role)) for role in ("author", "committer"))


def commit_body(commit: dict) -> str:
    """The body to read, which is nothing when a machine wrote it.

    Only the body is discarded. The commit's files, dates and any ticket its
    subject names still count, exactly as before.
    """
    if is_automated(commit):
        return ""
    return str(commit.get("body") or "")


def commit_description(subject: str, body: str, boilerplate: frozenset[str] = frozenset()) -> str:
    """The description to store: the subject's own words where they say
    something, otherwise the body's opening paragraph."""
    description = clean_description(subject)
    if len(description) >= MIN_DESCRIPTION_CHARS and not JUNK_DESCRIPTION.match(description):
        return description
    return body_description(body, boilerplate)


def _widen_range(record: dict, date: str) -> None:
    """Widen a first/last date range to include this date."""
    if record["first"] is None or date < record["first"]:
        record["first"] = date
    if record["last"] is None or date > record["last"]:
        record["last"] = date


def commit_tickets(commit: dict, boilerplate: frozenset[str] = frozenset()) -> list[str]:
    """The tickets a commit names, in the order it names them.

    The body is read only when the subject names nothing, so a body mentioning
    neighbouring work cannot dilute links the subject already states.
    """
    subject_tickets = list(dict.fromkeys(config.TICKET_PATTERN.findall(commit["subject"])))
    if subject_tickets:
        return subject_tickets
    return body_ticket_ids(commit_body(commit), boilerplate)


def apply_commit(
    commit: dict,
    files: dict[str, dict],
    descriptions: dict[str, dict],
    boilerplate: frozenset[str] = frozenset(),
) -> bool:
    """Fold one commit's tickets into the per-file entries and the per-ticket
    description pool. True if ticketed.

    Merge commits are skipped: a merge's file list is the whole branch, so
    indexing one would attribute hundreds of files to a single ticket.
    """
    if commit.get("is_merge"):
        return False
    tickets = commit_tickets(commit, boilerplate)
    if not tickets:
        return False
    date = commit["author_date"][:10]
    description = commit_description(commit["subject"], commit_body(commit), boilerplate)
    keep_description = bool(description)
    for ticket in tickets:
        info = descriptions[ticket]
        info["count"] += 1
        info["repos"].add(commit["repository"])
        _widen_range(info, date)
        if keep_description:
            info["descriptions"][description] += 1
    for changed in commit.get("files", []):
        entry = files[changed["path"]]
        for ticket in tickets:
            entry["tickets"][ticket] += 1
        _widen_range(entry, date)
    return True


def _record_discarded_body(commit: dict, boilerplate: frozenset[str], report: BodyReport) -> None:
    """Note why a commit's body was discarded, so the run can report it."""
    raw = str(commit.get("body") or "")
    if commit.get("is_merge") or not raw.strip():
        return
    if is_automated(commit):
        report.automated += 1
        return
    if clean_body(raw, boilerplate):
        return
    if clean_body(raw):
        report.boilerplate_emptied += 1
    elif body_rejection(raw) == "bot-text":
        report.bot_text += 1


def index_repository(
    ndjson: Path,
    descriptions: dict[str, dict],
    report: BodyReport | None = None,
) -> tuple[dict[str, dict], int]:
    """Build the file -> ticket map for one repository's dataset.

    Two passes: the first learns which body lines this repository repeats, the
    second indexes with those lines removed. Boilerplate is per repository
    because a template belongs to a team, and teams map to repositories far
    better than to a whole estate - a line that is a template in one repository
    can be a genuine one-off sentence in another.
    """
    counts, bodies = count_body_lines(ndjson)
    boilerplate = learn_boilerplate(counts, bodies)
    if report is not None:
        report.boilerplate_lines += len(boilerplate)
    files: dict[str, dict] = defaultdict(
        lambda: {"tickets": defaultdict(int), "first": None, "last": None}
    )
    commits_seen = 0
    with ndjson.open(encoding="utf-8") as source:
        for line in source:
            commit = json.loads(line)
            if report is not None:
                _record_discarded_body(commit, boilerplate, report)
            if apply_commit(commit, files, descriptions, boilerplate):
                commits_seen += 1
    finalised = {
        path: {
            "tickets": dict(sorted(data["tickets"].items(), key=lambda kv: -kv[1])),
            "first": data["first"],
            "last": data["last"],
        }
        for path, data in files.items()
    }
    return finalised, commits_seen


def summarise(
    index: dict[str, dict[str, dict]],
    commits_seen: int,
    report: BodyReport | None = None,
) -> None:
    total_files = sum(len(files) for files in index.values())
    total_tickets = len(
        {t for files in index.values() for d in files.values() for t in d["tickets"]}
    )
    size_mb = config.INTENT_INDEX_PATH.stat().st_size / 1_048_576
    bodies = report or BodyReport()
    print(
        f"Indexed {total_files:,} files across {len(index)} repos from "
        f"{commits_seen:,} ticketed commits; {total_tickets:,} distinct tickets."
    )
    print(
        f"Bodies discarded: {bodies.automated:,} by automated authorship, "
        f"{bodies.bot_text:,} by bot-generated text."
    )
    print(
        f"Learned boilerplate: {bodies.boilerplate_lines:,} repeated body lines suppressed, "
        f"emptying {bodies.boilerplate_emptied:,} bodies."
    )
    print(f"Wrote {config.INTENT_INDEX_PATH} ({size_mb:.1f} MB)")


def main() -> int:
    ndjson_files = sorted(config.HISTORY_DIR.glob("*/commits.ndjson"))
    if not ndjson_files:
        print(f"No history datasets under {config.HISTORY_DIR}. Run: knowledgestore export-history")
        return 1

    index: dict[str, dict[str, dict]] = {}
    descriptions: dict[str, dict] = defaultdict(
        lambda: {
            "descriptions": defaultdict(int),
            "repos": set(),
            "first": None,
            "last": None,
            "count": 0,
        }
    )
    commits_seen = 0
    report = BodyReport()
    for ndjson in ndjson_files:
        index[ndjson.parent.name], repo_commits = index_repository(ndjson, descriptions, report)
        commits_seen += repo_commits

    with io.gzip_text(config.INTENT_INDEX_PATH) as out:
        json.dump(index, out, ensure_ascii=False)

    # Per-ticket: the best commit-message descriptions (most repeated, then
    # longest), plus dates, repos and commit count. This is the estate's own
    # record of what each ticket changed - the fallback business detail
    # while Jira titles remain an offline CSV import.
    ticket_out = {
        ticket: {
            "d": [
                d
                for d, _ in sorted(
                    info["descriptions"].items(),
                    key=lambda kv: (-kv[1], -len(kv[0])),
                )[:2]
            ],
            "first": info["first"],
            "last": info["last"],
            "repos": sorted(info["repos"]),
            "n": info["count"],
        }
        for ticket, info in descriptions.items()
    }
    with io.gzip_text(config.TICKET_DESCRIPTIONS_PATH) as out:
        json.dump(ticket_out, out, ensure_ascii=False)
    described = sum(1 for t in ticket_out.values() if t["d"])
    print(
        f"Ticket descriptions: {len(ticket_out):,} tickets, "
        f"{described:,} with usable descriptions -> {config.TICKET_DESCRIPTIONS_PATH}"
    )

    summarise(index, commits_seen, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
