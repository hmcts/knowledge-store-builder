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
from collections.abc import Iterator
from pathlib import Path

# The key at a structural position, not the bare word: a node label can contain
# `"nodes"`, and starting the scan there would stream the wrong array.

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


class _TopLevelKeyScanner:
    """Finds `"<key>": [` at nesting depth 1, streaming, one character at a time.

    Depth-aware on purpose. The previous implementation regex-matched the pattern
    anywhere in the file, so it locked onto the first occurrence in byte order at
    any depth - and a merged graph carrying hyperedges has
    `graph.hyperedges[].nodes`, a list of id *strings*, before its top-level node
    array. The iterator then yielded strings, every consumer that type-checks the
    item saw nothing, and `graph_counts` returned `(0, 0)` for a fully clustered
    graph.

    Silently, which is what made it expensive: two guards built on those counts read
    "the two files agree" on exactly the graphs a real estate has. One was a refusal
    protecting against an irreversible overwrite, and a data-loss guard that cannot
    fire is worse than none because it is believed.

    A graph with no hyperedges has exactly one `"nodes"` key, so the defect is
    invisible in any fixture without them - which is why the tests passed. The
    fixture that would have caught it is the one nobody writes, because hyperedges
    look like an unrelated feature.

    `iter_array`'s docstring already promised "the named top-level array". This makes
    the code do what it said.
    """

    def __init__(self, key: str):
        self.key = key
        self.depth = 0
        self.in_string = False
        self.escaped = False
        self.pending_key: str | None = None
        self.token_start = -1
        self.after_colon = False

    def _end_string(self, buffer: str, pos: int) -> None:
        """Remember a completed string as a candidate key.

        **The single depth test.** An earlier version repeated it here, at the
        capture start and again at the bracket; every one-line mutation of any of
        the three survived, because the other two still blocked. Three guards that
        hide each other cannot be shown to matter, so there is one.
        """
        self.in_string = False
        if self.depth == 1 and self.token_start >= 0:
            self.pending_key = buffer[self.token_start : pos]
        self.token_start = -1

    def _in_string_step(self, buffer: str, pos: int) -> None:
        char = buffer[pos]
        if self.escaped:
            self.escaped = False
        elif char == "\\":
            self.escaped = True
        elif char == '"':
            self._end_string(buffer, pos)

    def feed(self, buffer: str, pos: int) -> tuple[int | None, int]:
        """(offset just past the opening bracket, or None; the new scan position)."""
        while pos < len(buffer):
            char = buffer[pos]
            if self.in_string:
                self._in_string_step(buffer, pos)
                pos += 1
                continue
            if char == '"':
                self.in_string = True
                self.token_start = pos + 1
            elif char == ":":
                self.after_colon = True
            elif char in "{[":
                # No depth test here: `pending_key` is only set at depth 1 and is
                # cleared on entering any container, so a match implies depth 1.
                if char == "[" and self.after_colon and self.pending_key == self.key:
                    return pos + 1, pos + 1
                self.depth += 1
                self.pending_key, self.after_colon = None, False
            elif char in "}]":
                self.depth -= 1
                self.pending_key, self.after_colon = None, False
            elif char == ",":
                self.pending_key, self.after_colon = None, False
            pos += 1
        return None, pos


def _seek_array(handle, key: str) -> tuple[str, int] | None:
    """Buffer and offset just past the top-level `"<key>": [`, or None if absent."""
    scanner = _TopLevelKeyScanner(key)
    buffer, pos = "", 0
    while True:
        found, pos = scanner.feed(buffer, pos)
        if found is not None:
            return buffer, found
        chunk = handle.read(READ_SIZE)
        if not chunk:
            return None
        # Trimming must not cut a key mid-capture, so keep from its start.
        keep = scanner.token_start if scanner.token_start >= 0 else pos
        buffer, pos = buffer[keep:] + chunk, pos - keep
        if scanner.token_start >= 0:
            scanner.token_start = 0


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
