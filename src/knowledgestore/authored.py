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
"""

from __future__ import annotations

from collections.abc import Iterable

from . import io

# The key inside a layer's `_metadata` block, and the two fields of each record.
AUTHORED_KEY = "authored"
AS_OF = "authored_as_of"
COMMIT = "authored_commit"


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
