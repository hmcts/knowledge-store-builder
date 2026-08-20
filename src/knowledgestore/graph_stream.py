"""Read one top-level array of a large JSON graph without holding it in memory.

A graph file is the biggest thing this pipeline handles, and most of what reads
it wants a few fields per node rather than the graph. `merge-graphs` output on
one estate is 1.6 GB on disk and 627,737 nodes; loading it to decide an estate
cut and count phantoms - a pure per-field scan over `id`, `label`, `source_file`
and `repo` - is what makes a store unable to read its own build product (#111).

    from . import graph_stream

    for node in graph_stream.iter_array(path):                 # "nodes"
        ...
    for edge in graph_stream.iter_array(path, key="links"):
        ...

Gzipped or not: the suffix decides, so the committed `.gz` costs no more to scan
than the uncompressed form.

Measured on a 785,493-node graph, counting communities:

    streamed   2.1s   0.035 GB peak RSS
    loaded     5.3s   3.75  GB peak RSS

## Two things a reimplementation gets wrong

Both were got wrong here first, and neither is visible from a passing test.

**Advance an index; never re-slice per object.** `buffer = buffer[end:]` copies
the remainder once per object - 785,493 times on that graph - and measured
**13.1s against 5.3s for simply loading the file**. Two and a half times slower
than the thing it replaced, with a beautiful memory graph, and it would have
shipped as an optimisation. `json.JSONDecoder().raw_decode` takes a start index.

**Read size sets peak memory; the compaction threshold barely matters.** Swept on
the real graph, varying one at a time:

    64 KiB  0.030 GB     256 KiB  0.032 GB     1 MiB  0.037 GB     4 MiB  0.239 GB

with wall clock flat across the whole range, and the threshold moving peak by
about 1 MB across a 16x change. Two operators measured a 6x difference in peak
between their implementations and attributed it to one machine being under memory
pressure; it was this constant. Peak allocation is a property of the code.

## The contract on a truncated file

**A file whose array opens and never closes raises.** It does not return the
objects it managed to read. That direction is deliberate: a caller counting nodes
would otherwise receive a smaller number that looks exactly like a real one, and
this pipeline's expensive mistakes are all of that shape - a count that was
correct about something other than what the sentence claimed.

An array that is simply *absent* yields nothing and does not raise. Absent and
empty are both legitimately "no such content", and callers already report that in
their own words.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import re
from collections.abc import Iterator
from pathlib import Path

# The key at a structural position, not the bare word: a node label can contain
# `"nodes"`, and starting the scan there would stream the wrong array.
_ARRAY_AT = '"{key}"\\s*:\\s*\\['

# Sets peak memory. See the sweep above before changing it.
READ_SIZE = 1 << 18

# When to drop the consumed prefix. Measured as barely mattering, so it is one
# read and not a knob worth tuning. What it must not be is *nothing*: compacting
# per object rather than per threshold is the copy described above.
COMPACT_AFTER = READ_SIZE


class TruncatedJson(ValueError):
    """An array that opened and never closed. A ValueError, so existing
    `except ValueError` handlers around file reads keep working."""


def _skip_gaps(buffer: str, pos: int) -> int:
    """Past whitespace and separators, without copying."""
    while pos < len(buffer) and buffer[pos] in " \t\r\n,":
        pos += 1
    return pos


def _seek_array(handle, key: str) -> tuple[str, int] | None:
    """Buffer and offset just past `"<key>": [`, or None if there is no such array.

    The tail is kept when trimming, because the pattern can straddle a read.
    """
    pattern = re.compile(_ARRAY_AT.format(key=re.escape(key)))
    buffer = ""
    while True:
        found = pattern.search(buffer)
        if found:
            return buffer, found.end()
        chunk = handle.read(READ_SIZE)
        if not chunk:
            return None
        buffer += chunk
        if len(buffer) > 2 * READ_SIZE:
            buffer = buffer[-64:]


def _iter_objects(handle, buffer: str, pos: int, source: str) -> Iterator[dict]:
    decoder = json.JSONDecoder()
    while True:
        pos = _skip_gaps(buffer, pos)
        if pos < len(buffer) and buffer[pos] == "]":
            return
        value = None
        if pos < len(buffer):
            with contextlib.suppress(ValueError):
                value, pos = decoder.raw_decode(buffer, pos)
        if value is not None:
            yield value
            if pos > COMPACT_AFTER:
                buffer, pos = buffer[pos:], 0
            continue
        buffer, pos = buffer[pos:], 0
        chunk = handle.read(READ_SIZE)
        if not chunk:
            # Ran out of input inside the array: the closing bracket was never
            # reached. Raising rather than returning what was read - see the
            # module docstring on why the quiet direction is the dangerous one.
            raise TruncatedJson(f"{source}: the array ends without a closing bracket")
        buffer += chunk


def iter_array(path: Path, key: str = "nodes") -> Iterator[dict]:
    """Yield each object of the named top-level array, holding one at a time.

    Nothing at all if the file does not exist or holds no such array; raises
    `TruncatedJson` if the array opens and never closes.
    """
    if not path.is_file():
        return
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        found = _seek_array(handle, key)
        if found is None:
            return
        yield from _iter_objects(handle, *found, source=str(path))
