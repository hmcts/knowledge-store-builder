"""When each entry of a written layer was authored.

Community summaries, topic briefs and deep dives are written once and kept across
rebuilds: `summaries remap` carries a summary onto a new community id, and a merge
keeps every entry whose text it did not change. So the prose in a store can be
older than the build holding it, and the store's own `sources_as_of` - the newest
source of that build - over-dates it. A reader that presents written analysis has
to be able to say when it was written.

Each layer's metadata block therefore records, per entry id, the build that last
wrote that entry's text:

    "_metadata": {"authored": {"<id>": {"authored_as_of": ..., "authored_commit": ...}}}

`authored_as_of` is the build's commit date and `authored_commit` that commit's
SHA, both read from provenance by `store_manifest.build_commit` - the commit that
sets the manifest's `sources_as_of`, so an entry written in this build carries
the same date the manifest gives the build.

The rules, in the order they are applied:

- **Carried text keeps its date.** An entry whose text is unchanged, or which a
  remap moved to a new id, keeps the record it had - including having none.
- **New or changed text takes this build's date,** or no date when this build
  records none. A changed entry never keeps the old date: the old text was
  written then, and this text was not.
- **Never invent a date.** An entry written before this record existed has none,
  and keeping it is the only honest answer.

The block sits in the metadata rather than on the entries, because summaries are
plain strings and the three layers should be read one way, and because every
reader of the entries already strips the metadata (`io.read_prose_layer`).

**Briefs and dives are judged on their source, not their HTML.** Their stages
re-render every entry from Markdown on every run, so a renderer change alters the
HTML of entries nobody rewrote, and dating those would make old analysis look new.
Each of their records therefore also carries `source_sha256`, the digest of the
Markdown, and an entry counts as rewritten only when that digest changes. An entry
with no recorded digest - written before it existed - gets one on its next run,
and is dated only if its HTML changed too: after that run, no renderer change can
date it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from . import io

# The key inside a layer's `_metadata` block, and the two fields of each record.
AUTHORED_KEY = "authored"
AS_OF = "authored_as_of"
COMMIT = "authored_commit"
# Briefs and dives only: the sha256 of the Markdown an entry was rendered from.
SOURCE = "source_sha256"


def recorded(document: dict) -> dict[str, dict]:
    """The authored records a committed layer holds, by entry id; {} when none.

    A record that is not an object is dropped rather than refused: the file is
    committed and hand-edited, and a damaged record is an undated entry.
    """
    metadata = document.get(io.SUMMARIES_METADATA_KEY)
    block = metadata.get(AUTHORED_KEY) if isinstance(metadata, dict) else None
    if not isinstance(block, dict):
        return {}
    return {str(key): value for key, value in block.items() if isinstance(value, dict)}


def dated(
    prose: dict[str, str],
    before: dict[str, str],
    records: dict[str, dict],
    stamp: dict[str, str] | None,
) -> dict[str, dict]:
    """The authored record of each entry in `prose`, in its order.

    `before` is the committed text by id and `records` the committed records. An
    entry whose text is what `before` holds keeps its record, or stays without one;
    any other entry takes `stamp`, this build's, or stays without one when the
    build records no date.
    """
    result: dict[str, dict] = {}
    for key, text in prose.items():
        if key in before and before[key] == text:
            if key in records:
                result[key] = records[key]
        elif stamp is not None:
            result[key] = dict(stamp)
    return result


def source_digest(markdown: str) -> str:
    """The digest a brief or dossier records of the Markdown it was rendered from."""
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def rendered_dated(
    sources: dict[str, str],
    html: dict[str, str],
    before: dict[str, str],
    records: dict[str, dict],
    stamp: dict[str, str] | None,
) -> dict[str, dict]:
    """`dated`, for a layer rendered from Markdown: each entry's record, in order.

    `sources` is each entry's `source_digest`, `html` what it renders to now and
    `before` the committed HTML. Rewritten means the source digest changed; where
    no digest is recorded, that the HTML changed (a new entry has no HTML before).
    A rewritten entry takes `stamp`, or no date; any other keeps the date it had,
    or none. Every entry records its digest, so the next run can judge it.
    """
    result: dict[str, dict] = {}
    for key, digest in sources.items():
        record = records.get(key, {})
        recorded_source = record.get(SOURCE)
        if recorded_source is not None:
            rewritten = recorded_source != digest
        else:
            rewritten = before.get(key) != html[key]
        if rewritten:
            dates = dict(stamp) if stamp is not None else {}
        else:
            dates = {field: record[field] for field in (AS_OF, COMMIT) if field in record}
        result[key] = {**dates, SOURCE: digest}
    return result


def carried(moves: Iterable[tuple[str, str]], records: dict[str, dict]) -> dict[str, dict]:
    """The records that move with their prose, as `(new id, old id)` pairs.

    A carry writes no text, so it has no date of its own to give: an entry that
    had no record still has none.
    """
    return {new: records[old] for new, old in moves if old in records}


def block(records: dict[str, dict]) -> dict:
    """The metadata entry for `records`, or nothing when no entry is dated.

    Omitted rather than empty, so a layer with no dated entry is written exactly
    as it was before this record existed.
    """
    return {AUTHORED_KEY: records} if records else {}
