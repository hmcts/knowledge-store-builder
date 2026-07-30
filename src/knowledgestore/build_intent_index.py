"""Build the file -> Jira-ticket intent index from the Git-history datasets.

For every source file in the estate, records which Jira tickets appear in the
subjects of non-merge commits that touched it, with first/last touch dates.
This answers "what business change shaped this file?" as a lookup, e.g.:

    import gzip, json
    index = json.load(gzip.open('knowledge/intent/file-tickets.json.gz', 'rt'))
    index['cpp-ui-defence']['src/app/shared/pipes/address.pipe.ts']

Requires knowledge/git-history/ (regenerate with
`knowledgestore export-history` if absent). The output is small and
committed, so consumers get intent lookups without regenerating history.
"""

from __future__ import annotations

import gzip
import json
import re
from collections import defaultdict
from pathlib import Path


from . import config

HISTORY_DIR = config.HISTORY_DIR
OUTPUT = config.INTENT_INDEX_PATH
DESCRIPTIONS = config.TICKET_DESCRIPTIONS_PATH

TICKET = config.TICKET_PATTERN
TICKET_PREFIX = re.compile(r"^[\s\[\(]*[A-Z][A-Z0-9]{1,9}-\d{1,6}[\]\)]*[\s:,\-–]*")
JUNK_DESCRIPTION = re.compile(
    r"^(wip|fix(es|ed)?|updated?|changes?|test(s|ing)?|merge.*|minor.*|"
    r"address(ed|ing)? (pr |review )?comments?.*|pr comments?.*|refactor(ing)?)$",
    re.IGNORECASE,
)


def clean_description(subject: str) -> str:
    """Strip leading ticket references, leaving the human description."""
    cleaned = subject.strip()
    while True:
        stripped = TICKET_PREFIX.sub("", cleaned, count=1)
        if stripped == cleaned:
            break
        cleaned = stripped.strip()
    return cleaned.strip(" .")


def _widen_range(record: dict, date: str) -> None:
    """Widen a first/last date range to include this date."""
    if record["first"] is None or date < record["first"]:
        record["first"] = date
    if record["last"] is None or date > record["last"]:
        record["last"] = date


def apply_commit(commit: dict, files: dict[str, dict], descriptions: dict[str, dict]) -> bool:
    """Fold one commit's tickets into the per-file entries and the per-ticket
    description pool. True if ticketed."""
    if commit.get("is_merge"):
        return False
    subject = commit["subject"]
    tickets = set(TICKET.findall(subject))
    if not tickets:
        return False
    date = commit["author_date"][:10]
    description = clean_description(subject)
    keep_description = len(description) >= 12 and not JUNK_DESCRIPTION.match(description)
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


def index_repository(ndjson: Path, descriptions: dict[str, dict]) -> tuple[dict[str, dict], int]:
    """Build the file -> ticket map for one repository's dataset."""
    files: dict[str, dict] = defaultdict(
        lambda: {"tickets": defaultdict(int), "first": None, "last": None}
    )
    commits_seen = 0
    with ndjson.open(encoding="utf-8") as source:
        for line in source:
            if apply_commit(json.loads(line), files, descriptions):
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


def summarise(index: dict[str, dict[str, dict]], commits_seen: int) -> None:
    total_files = sum(len(files) for files in index.values())
    total_tickets = len(
        {t for files in index.values() for d in files.values() for t in d["tickets"]}
    )
    size_mb = OUTPUT.stat().st_size / 1_048_576
    print(
        f"Indexed {total_files:,} files across {len(index)} repos from "
        f"{commits_seen:,} ticketed commits; {total_tickets:,} distinct tickets."
    )
    print(f"Wrote {OUTPUT} ({size_mb:.1f} MB)")


def main() -> int:
    ndjson_files = sorted(HISTORY_DIR.glob("*/commits.ndjson"))
    if not ndjson_files:
        print(f"No history datasets under {HISTORY_DIR}. Run: knowledgestore export-history")
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
    for ndjson in ndjson_files:
        index[ndjson.parent.name], repo_commits = index_repository(ndjson, descriptions)
        commits_seen += repo_commits

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUTPUT, "wt", encoding="utf-8", compresslevel=9) as out:
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
    with gzip.open(DESCRIPTIONS, "wt", encoding="utf-8", compresslevel=9) as out:
        json.dump(ticket_out, out, ensure_ascii=False)
    described = sum(1 for t in ticket_out.values() if t["d"])
    print(
        f"Ticket descriptions: {len(ticket_out):,} tickets, "
        f"{described:,} with usable descriptions -> {DESCRIPTIONS}"
    )

    summarise(index, commits_seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
