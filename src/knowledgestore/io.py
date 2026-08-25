"""Shared pipeline IO helpers - JSON and gzip-JSON reading/writing.

Consolidates the read/write patterns previously re-implemented per stage
(measured in docs/audit-extraction-readiness.md). Named "pipeline_io" (not
"io") so it can never shadow the stdlib io module when scripts run with
sys.path[0] pointing at scripts/.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import sys
from io import TextIOWrapper
from pathlib import Path


def checked_write_target(path: Path) -> Path:
    """The write target, or `ValueError` if any component climbs upward.

    These writers are reached with paths built from `--root` and other CLI
    arguments, so whatever constructs those arguments - an operator, a script, or
    an agent - decides where the process writes. A `..` component is the one thing
    that is never legitimate here: every caller in this library names an output
    path directly, and none needs to climb out of it.

    Two wider guards were tried first and both were wrong. Confining writes to the
    store root failed 48 tests, because `configure()` sets each output path
    independently and an output directory outside the root is supported. Widening
    the allow-list to every directory the configuration declares still failed 4,
    because this module's own unit tests write to bare temporary directories -
    which is the tell that the check sat in the wrong place. A low-level writer
    consulting global configuration would surprise any consumer using it as one.

    So this checks the property that holds wherever the store lives. Confining
    writes to a declared boundary is a real improvement on it, but it belongs at
    the stage or CLI boundary where that boundary is known, and it is an interface
    change rather than a fix.

    Checked lexically, before any resolution: `realpath` collapses `..`, so
    resolving first would launder exactly what this rejects.
    """
    if any(part == ".." for part in Path(path).parts):
        raise ValueError(
            f"refusing to write to a path that traverses upward: {path}. "
            "Name the output path directly."
        )
    return Path(path)


def read_json(path: Path, default=None):
    """Parse a JSON file, gzipped or not; return `default` if it does not exist.

    The suffix decides. Without this, a stage handed a `.gz` path died on the gzip
    magic byte - `UnicodeDecodeError: 0x8b in position 1` - and on one estate that
    made `record-clustering --graph graphify-out/graph.json.gz` impossible, which
    was the only artefact that store ships. The dispatch is here rather than in
    that stage because three other call sites read `GRAPH_PATH` the same way, so
    fixing it once fixes the class.

    Adding capability, never changing behaviour: a caller passing an uncompressed
    path takes exactly the branch it always took.
    """
    if not path.exists():
        return default
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_dict(path: Path) -> dict:
    """Parse a JSON object, or {} when the file is absent.

    Stages that merge layers into the graph always want a mapping, never None,
    so this is the reader they use.
    """
    value = read_json(path, default={})
    return value if isinstance(value, dict) else {}


def write_json(path: Path, data, indent: int | None = None) -> None:
    target = checked_write_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")


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
    target = checked_write_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        open(target, "wb") as raw,
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


def report_join_cardinality(
    joined: int, candidates: int, index_size: int, by_layer: dict | None = None
) -> bool:
    """Report how much of the file-to-ticket join actually matched.

    Two outcomes, deliberately different in kind. A join that matched **nothing**
    while both sides were populated is a defect and goes to stderr as a warning.
    Any other rate is a **measurement** and goes to stdout, because the threshold
    between "sparse" and "broken" is a judgement this library cannot make for an
    estate - see below for why guessing it would be worse than not.

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

    Zero is the only floor safe to assert generically, and the reason is stronger
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

    `by_layer` maps each layer to `(joined, candidates)` and applies the same zero
    floor one level down - still a floor, still no threshold. It exists because
    the composite cannot see a **half**-dead join: one estate converted its AST
    layer and left the semantic layer skipping every record, giving 5,692 of
    72,370, which is never zero and reads as a working join on a sparse estate.
    Per layer it was 0 of 46,602 against 5,692 - a zero the composite structurally
    could not produce.

    Counted only over repositories the index covers, which is the same
    both-sides-populated condition the composite has. Without that it cries wolf:
    on the maintainer's own estate a `meta-arch` layer of 2,115 nodes joins zero
    because its repository is not mined at all, and that is sparsity, not a key
    mismatch. Per-*repository* granularity was measured and rejected - in all 108
    ticket-covered repositories of the reporting estate both layers were present,
    so the working layer made every repository non-zero and the floor was masked
    exactly where it mattered.
    """
    if not index_size or not candidates:
        return False
    for layer, (layer_joined, layer_candidates) in sorted((by_layer or {}).items()):
        if layer_candidates and not layer_joined:
            print(
                f"WARNING: the {layer} layer's file-to-ticket join matched nothing - "
                f"0 of {layer_candidates:,} candidate node(s) in repositories the index "
                "covers, while other layers joined. One layer keyed differently from the "
                "rest is the half-dead case a whole-graph count cannot show.",
                file=sys.stderr,
            )
    if joined:
        # Reported as a measurement, not a verdict. Zero is the only floor safe
        # to assert (above), but a *partial* join is the quieter failure - one
        # estate fixed the AST half and left the semantic half skipping every
        # record, and 5,692 of 72,370 reads as a working join on a sparse
        # estate. Printing the rate every build makes that visible across
        # refreshes without anyone having to guess what "enough" is.
        print(
            f"File-to-ticket join: {joined:,} of {candidates:,} candidate nodes "
            f"({100 * joined / candidates:.1f}%) carry ticket evidence."
        )
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


def layer_digests(paths: "list[Path]", root: Path) -> dict:
    """A content digest per embedded layer, for artefacts that are built from them.

    Timestamps cannot answer "was this page built from these layers": the
    ordinary workflow commits a regenerated layer and the page in one commit, so
    their commit dates are identical whether or not the page was rebuilt, and an
    uncommitted layer edit moves no date at all. Content is the only evidence
    that survives both.

    Missing layers are recorded as absent rather than skipped, so a layer that
    disappears between builds is a change rather than a silence.

    `root` is passed rather than read from config: this module is deliberately
    below configuration and importing it here would invert that.
    """
    digests = {}
    for path in paths:
        name = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        if path.is_file():
            digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        else:
            digests[name] = None
    return digests


def load_labels(path: Path) -> dict:
    """Community labels, or {} when not yet generated."""
    return read_json_dict(path)
