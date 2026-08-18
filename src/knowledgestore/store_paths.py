#!/usr/bin/env python3
"""One rule for paths in a knowledge store's own files.

    AT REST      relative to the store root
    IN FLIGHT    absolute, when handed to an agent or to graphify

Everything a store persists about itself — the chunk plan, the corpus inventory, the
archived extractions — describes files inside the store, so a path to them is only ever
meaningful relative to the store. Writing them absolute records the build machine's
directory layout in a published artefact, and it breaks the moment the store moves.

Measured on this estate before the fix, in *tracked* files:

    the chunk plan               tens of thousands of absolute paths
    the corpus inventory        the same again
    semantic-chunks.tar.gz       every archived chunk, and already pointing at a root
                                 that no longer existed after the second relocation

Each relocation cost a full rewrite of every one of them. None of that work was necessary; the
paths simply should not have been absolute at rest.

**Why in-flight paths must stay absolute.** graphify's extraction spec is a contract with
the extraction agents:

> set `source_file` to the path of the originating file EXACTLY as it appears in
> FILE_LIST — verbatim and absolute.

So the FILE_LIST handed to an agent is absolute, and the `source_file` it writes back is
absolute, and neither changes. The storage format is a separate decision from the wire
format, and conflating them is what produced the problem: the plan was persisted in the
shape it needed to be transmitted in.

Also, and this is easy to get wrong: **`graphify.extract.collect_files` given a relative
path silently ignores `.graphifyignore`** — no error, everything returned. So anything
handed to graphify must be resolved first. `absolute()` is the only correct thing to pass
across that boundary.

Readers that want the old behaviour call `load_plan()` and get absolute paths, so adopting
the relative format costs them no change.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import config

_MARKER = "repositories/"


def _root() -> Path:
    """The store root, read at call time.

    Deliberately not a module-level constant. `config.ROOT` is settable (KSB_ROOT, or
    `configure()`), and a constant captured at import binds whichever root happened to be
    current when the module was first imported — which is how a test suite silently
    measures the wrong store.
    """
    return config.ROOT


def relative(p: str | Path) -> str:
    """Store-relative form. Idempotent, and tolerant of a foreign absolute root.

    A path from another machine (or from this store's previous location) still names a
    file *inside* the store, so it is relativised at the `repositories/` boundary rather
    than only when it matches the current root. That is what makes an archive written
    before a relocation still readable after one.

    **This must never resolve symlinks, and the first version did.** It began with
    `Path(s).resolve().relative_to(_root())`, which follows links — so converting the corpus
    inventory rewrote a symlink's path into its target's path. That is a change of
    *identity*, not of form:

        <repo>/components/<name>/variables.tf      ->  <repo>/environments/variables.tf
        <repo>/CLAUDE.md                           ->  <repo>/AGENTS.md   (now duplicated)

    Two entries in a corpus of tens of thousands, so nothing looked wrong — and the direction
    of the error is the dangerous one. The graph held dozens of nodes under the *link* path, so the
    converted inventory claimed coverage of a file with no nodes on it and stopped naming
    the path the nodes were actually under. It read as more correct than the truth.

    So: match on the `repositories/` marker first and never call `resolve()`. A store whose
    own root contains a symlinked component is handled by the marker for everything under
    `repositories/`, which is every path this function is asked about in practice.
    """
    s = str(p)
    i = s.find(_MARKER)
    if i >= 0:
        return s[i:]
    if s.startswith("/"):
        # Inside the store but outside `repositories/` — relativise without following
        # links. `relpath` is purely lexical, unlike `resolve`.
        try:
            rel = os.path.relpath(s, _root())
            if not rel.startswith(".."):
                return rel
        except (ValueError, OSError):
            pass
    return s


def absolute(p: str | Path) -> str:
    """Absolute form, for handing to an agent or to graphify."""
    s = str(p)
    return s if s.startswith("/") else str(_root() / s)


def load_plan(path: str | Path | None = None) -> dict[str, list[str]]:
    """The chunk plan with every path absolute, whatever the file holds.

    Callers get what they always got. The on-disk format is free to be relative.
    """
    p = Path(path) if path else _root() / "graphify-out/.graphify_chunk_plan.json"
    plan = json.loads(p.read_text(encoding="utf-8"))
    return {k: [absolute(f) for f in v] for k, v in plan.items()}


def store_relative_plan(plan: dict[str, list[str]]) -> dict[str, list[str]]:
    """The inverse, for writing."""
    return {k: [relative(f) for f in v] for k, v in plan.items()}
