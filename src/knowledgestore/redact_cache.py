"""Bring the cached tracker text under the redaction rules in force now.

`fetch-tickets` redacts what it fetches, once. A rule added to
`KSB_SENSITIVE_PATTERNS` afterwards - or a default this library gains in a new
release - never reached a record already cached, and a resolved ticket is never
fetched again, so the text stayed in the committed artefact and in the page built
from it. This stage re-applies the rules to the whole cache.

    knowledgestore redact-cache

A stage of its own rather than a step of `fetch-tickets`, because `fetch-tickets`
is opt-in and stops before touching the cache when no tracker credentials are set
- and a store reading a committed cache is exactly the store that has no
credentials and still publishes the text. It needs no network and no token.

Idempotent: a second run changes nothing and leaves the file byte-identical. It
writes only when something changed. **Only counts are reported** - spans changed
per rule and records touched - never the text, for the reason `check-evidence`
gives: a log is read more widely and kept longer than the artefact.

Covers summary, description, resolution, type, status, parent and comments. It
cleans the cache only: when it changed anything it names the stages to re-run so
the copies built from the old text (intent layer, answers, index, page) are
rebuilt too.
"""

from __future__ import annotations

import argparse
from collections import Counter

from . import config, fetch_tickets, io, sensitive

# What still holds the old text after the cache is clean, in the order that
# carries the redaction to every copy: the tracker text was copied into the intent
# layer, the answers layer, the index and the page when they were built, and this
# stage rewrites only the cache.
REBUILD = (
    "intent",
    "answers-layer",
    "index",
    "explorer",
    "manifest",
    "check-evidence",
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    cache = io.read_gzip_json_dict(config.TICKET_TRACKER_PATH)
    if not cache:
        print(f"No tracker cache at {config.TICKET_TRACKER_PATH} - nothing to redact")
        return
    counts: Counter[str] = Counter()
    touched = sensitive.redact_tracker(cache, counts)
    if touched:
        fetch_tickets.write_cache(cache)
    named = ", ".join(f"{rule} {count:,}" for rule, count in sorted(counts.items()))
    print(
        f"redacted {sum(counts.values()):,} spans in {touched:,} of {len(cache):,} "
        f"cached tracker records" + (f": {named}" if named else " (nothing matched a rule)")
    )
    if touched:
        print(
            "The cache is clean; copies built from it are not, until rebuilt. Re-run, in order:\n"
            + "".join(f"  knowledgestore {stage}\n" for stage in REBUILD)
            + "Then `knowledgestore summaries verify`: a summary or brief written from the old "
            "text must be regenerated."
        )


if __name__ == "__main__":
    main()
