"""Shared pipeline IO helpers - JSON and gzip-JSON reading/writing.

Consolidates the read/write patterns previously re-implemented per stage
(measured in docs/audit-extraction-readiness.md). Named "pipeline_io" (not
"io") so it can never shadow the stdlib io module when scripts run with
sys.path[0] pointing at scripts/.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import sys
from io import TextIOWrapper
from pathlib import Path


def read_json(path: Path, default=None):
    """Parse a JSON file; return `default` if it does not exist."""
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_dict(path: Path) -> dict:
    """Parse a JSON object, or {} when the file is absent.

    Stages that merge layers into the graph always want a mapping, never None,
    so this is the reader they use.
    """
    value = read_json(path, default={})
    return value if isinstance(value, dict) else {}


def write_json(path: Path, data, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")


def read_gzip_json(path: Path, default=None):
    """Parse a gzip-compressed JSON file; return `default` if absent."""
    if not path.exists():
        return default
    with gzip.open(path, "rt", encoding="utf-8") as source:
        return json.load(source)


def read_gzip_json_dict(path: Path) -> dict:
    """Parse a gzip-compressed JSON object, or {} when the file is absent."""
    value = read_gzip_json(path, default={})
    return value if isinstance(value, dict) else {}


@contextlib.contextmanager
def gzip_text(path: Path, compresslevel: int = 9):
    """Deterministic gzip text writer: fixed compression level, no timestamp
    and no filename in the header — identical content produces identical bytes,
    the behaviour of `gzip -9 -n`.

    Python's default writer embeds the current time and the output filename,
    so every rebuild rewrote committed artefacts whose content had not
    changed, quietly defeating the byte-identical guarantee and dirtying
    version control on every run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with (
        open(path, "wb") as raw,
        # filename="" explicitly: GzipFile otherwise lifts raw.name into the
        # header's FNAME field, which is the other source of byte churn.
        gzip.GzipFile(
            filename="", fileobj=raw, mode="wb", compresslevel=compresslevel, mtime=0
        ) as binary,
    ):
        text = TextIOWrapper(binary, encoding="utf-8")
        try:
            yield text
        finally:
            text.flush()
            text.detach()


def write_gzip_json(path: Path, data) -> None:
    with gzip_text(path) as out:
        json.dump(data, out, ensure_ascii=False)


def load_graph(path: Path) -> dict:
    """The estate graph (node-link JSON). Raises if absent - callers treat a
    missing graph as a hard error with their own message."""
    return json.loads(path.read_text(encoding="utf-8"))


def warn_if_no_repo_attribute(nodes: list, consequence: str) -> bool:
    """Warn, once and loudly, when no node carries `repo`. True when none does.

    Nine modules read this attribute, almost all as `.get("repo", "")`, so a
    graph built without it degrades silently rather than failing: digests get one
    repository called "", per-repository bundles come out empty, and the
    file-to-ticket join matches nothing. Every one of those looks like a thin
    estate rather than a broken precondition.

    It goes missing on a real route, not a hypothetical one. `merge-graphs`
    stamps the attribute; a store that extracts per repository and concatenates
    instead has to reimplement that, and one such store set `repository` on all
    70,655 of its nodes and `repo` on none. The bypass is the same root as the
    node-id collisions in issue #115 - whatever skips `merge-graphs` inherits
    responsibility for what `merge-graphs` did, and reimplementation drifts.

    `consequence` says what this particular caller will produce anyway, because
    "attribute missing" alone does not tell an operator what they are about to
    ship.
    """
    if not nodes or any(node.get("repo") for node in nodes):
        return False
    print(
        f"WARNING: no node carries a `repo` attribute. {consequence} "
        "A graph built by concatenating per-repository extractions must stamp it "
        "the way `merge-graphs` does.",
        file=sys.stderr,
    )
    return True


def warn_if_join_is_empty(joined: int, candidates: int, index_size: int) -> bool:
    """Warn when a join that had both sides to work with matched nothing.

    Shape, schema and freshness checks all pass on a join that matches nothing:
    the graph is valid, the index is valid, and every count is healthy. Only the
    cardinality of the join itself says otherwise, and nothing measured it. On
    one store the file-to-ticket join produced **zero** matches across 70,655
    nodes and 108 repositories of mined tickets, and the build was green.

    The cause is that the two documented build routes disagree about
    `source_file`. The index is keyed `{repo: {repo-relative path: ...}}`, which
    is what the per-repository route plus `merge-graphs` produces; the
    single-root route produces `repositories/<repo>/<path>` instead. Nothing
    rewrites it - `prefix_graph_for_global` sets `repo` and `local_id` and does
    not touch `source_file` - so the join is not degraded, it is dead.

    Zero is the only safe floor to assert generically, and the reason is stronger
    than caution. A non-zero floor would be a guess about estate shape; a guess
    that fires wrongly gets suppressed, and a suppressed check is worse than an
    absent one because somebody has explicitly decided to ignore it. Both sides
    populated with an empty intersection is the only condition that is
    unambiguously a defect rather than a judgement, so it is the only one that
    survives contact with an annoyed maintainer.

    The evidence shape is what the message reports, not just the count: two
    populated sides that share no keys are in different key spaces. That names a
    class - which includes joins nobody has written yet - where naming one
    likely prefix only names an instance.
    """
    if not index_size or not candidates or joined:
        return False
    print(
        f"WARNING: the file-to-ticket join matched nothing. Both sides are populated - "
        f"{candidates} candidate node(s) against an index covering {index_size} "
        "repositories - and the intersection is empty, so the two sides are keyed in "
        "different spaces rather than the estate being sparse. Every answer will report no "
        "ticket evidence for any file, which is indistinguishable from an estate no ticket "
        "ever touched. Here the usual cause is `source_file` carrying a "
        "`repositories/<repo>/` prefix the index is not keyed on.",
        file=sys.stderr,
    )
    return True


def load_labels(path: Path) -> dict:
    """Community labels, or {} when not yet generated."""
    return read_json_dict(path)
