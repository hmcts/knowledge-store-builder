"""Merge Jira CSV exports into knowledge/intent/ticket-titles.json.gz.

Direct Jira API access is not available in this environment, so ticket
titles arrive offline: someone with Jira access exports issues as CSV
(Jira issue search -> Export -> CSV; only "Issue key" and "Summary"
columns are needed) and runs:

    knowledgestore ticket-titles path/to/export1.csv [more.csv ...]

Repeat runs merge: existing titles are kept and new/updated ones override.
The main Jira projects referenced by the estate's commits (from
knowledge/intent/file-tickets.json.gz) are DD, GPE, RQA, ATCM, CRC, CCT,
CHD, SNI and SPRDT — a per-project export of key+summary covers everything.

Consumers then resolve intent end-to-end:

    import gzip, json
    titles = json.load(gzip.open('knowledge/intent/ticket-titles.json.gz', 'rt'))
    titles.get('CRC-12016')   # -> the Jira summary
"""

from __future__ import annotations

import csv
import gzip
import json
import re
import sys
from pathlib import Path


from . import config

TICKET = re.compile("^" + config.TICKET_PATTERN.pattern.strip("\\b") + "$")


def find_columns(header: list[str]) -> tuple[int, int]:
    key_idx = summary_idx = -1
    for index, name in enumerate(header):
        cell = name.strip().lower()
        if cell in ("issue key", "key") and key_idx < 0:
            key_idx = index
        elif cell == "summary" and summary_idx < 0:
            summary_idx = index
    if key_idx < 0 or summary_idx < 0:
        raise ValueError(f"CSV needs 'Issue key' and 'Summary' columns; found: {header}")
    return key_idx, summary_idx


def merge_csv(path: Path, titles: dict[str, str]) -> int:
    """Merge one Jira CSV export into titles; returns titles added/updated."""
    added = 0
    # Sonar S2083: opening a caller-supplied path is this CLI importer's
    # purpose; it runs offline against a local clone with no privilege
    # boundary to cross.
    with path.open(encoding="utf-8-sig", newline="") as source:  # NOSONAR(S2083)
        reader = csv.reader(source)
        key_idx, summary_idx = find_columns(next(reader))
        for row in reader:
            if len(row) <= max(key_idx, summary_idx):
                continue
            key = row[key_idx].strip().upper()
            summary = row[summary_idx].strip()
            if TICKET.match(key) and summary:
                if titles.get(key) != summary:
                    added += 1
                titles[key] = summary
    return added


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    titles: dict[str, str] = {}
    if config.TICKET_TITLES_PATH.exists():
        titles = json.load(gzip.open(config.TICKET_TITLES_PATH, "rt", encoding="utf-8"))
        print(f"Loaded {len(titles):,} existing titles")

    added = 0
    for argument in sys.argv[1:]:
        path = Path(argument).resolve()
        added += merge_csv(path, titles)
        print(f"{path}: merged")

    config.TICKET_TITLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(config.TICKET_TITLES_PATH, "wt", encoding="utf-8", compresslevel=9) as out:
        json.dump(dict(sorted(titles.items())), out, ensure_ascii=False)

    print(f"{added:,} titles added/updated; {len(titles):,} total -> {config.TICKET_TITLES_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
