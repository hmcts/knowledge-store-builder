"""Shared pipeline IO helpers - JSON and gzip-JSON reading/writing.

Consolidates the read/write patterns previously re-implemented per stage
(measured in docs/audit-extraction-readiness.md). Named "pipeline_io" (not
"io") so it can never shadow the stdlib io module when scripts run with
sys.path[0] pointing at scripts/.

**The read-path policy**, which every read here is annotated with. Sonar reports
`pythonsecurity:S8707` where a path built from CLI arguments reaches the
filesystem, so whatever assembles those arguments decides what this process
reads. It was reported on `read_json`, and the taint flows the analyser prints
all have one shape - `argparse` in a stage, through `read_json_dict`, to the read
here - so the answer belongs to the class: `_read_json_file` (the read behind
both `read_json` and `load_graph`), `read_gzip_json` and `layer_digests` all
carry it.

The grounds are the ones recorded in `build_community_summaries.merge`, cited
rather than restated so the two cannot drift into two different policies:
reading a path the operator named is the purpose of the flag, and this is an
offline maintainer CLI against a local clone with no privilege boundary to cross.
That module also holds the register of every site under the policy.

`checked_write_target` is deliberately not applied to the reads. It rejects an
upward component because no caller in this library needs to climb out of an
output path it *named*, and that argument does not transfer to a read whose
entire purpose is to open a path the caller chose. Nor are reads confined to the
store root: a stage is documented to accept an explicit path, and
`record-clustering --graph <path>` legitimately points outside the store. Both
confinements were measured on the write side - the store root failed 48 tests, a
configuration-derived allow-list failed 4 - and on the read side the store root
is worse, because far more of the pipeline reads than writes: confining
`read_json` alone fails 106. Confinement belongs at the stage boundary, where the
boundary is known. `tests/test_read_path_policy.py` pins the reads that must keep
working, so the next attempt fails on three named assertions rather than in 106
unrelated places.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Iterable
from io import TextIOWrapper
from pathlib import Path


def checked_write_target(path: Path) -> Path:
    """Raise `ValueError` if any component of the write target climbs upward.

    Called for the raise rather than for its return value. Assigning the result
    and writing through it gave Sonar's taint analysis a second path
    construction from user-controlled data to flag - it reported an S2083
    BLOCKER on the rewritten statement where `main` had only S8707 on the same
    line. Validating in place leaves the write statement unchanged.

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


def _read_json_file(path: Path):
    """Parse a JSON file that exists, gzipped or not. The suffix decides.

    The one implementation of "read this, compressed or not", because there were
    two and they disagreed. This dispatch was added after a stage handed a `.gz`
    path died on the gzip magic byte - `UnicodeDecodeError: 0x8b in position 1` -
    and on one estate that made `record-clustering --graph
    graphify-out/graph.json.gz` impossible, which was the only artefact that
    store ships. It went into `read_json` and not into `load_graph`, which reads
    the same artefact for a different set of stages, so the fix covered part of
    the class and the part it missed was invisible from either call site.

    Private and existence-blind on purpose: the two public readers that call it
    disagree about an absent file, deliberately and in opposite directions, and
    that is the only difference left between them. Folding the existence check in
    here would force one answer on both.
    """
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:  # NOSONAR(S8707) - read policy
            return json.load(handle)
    return json.loads(path.read_text(encoding="utf-8"))  # NOSONAR(S8707) - read policy


def read_json(path: Path, default=None):
    """Parse a JSON file, gzipped or not; return `default` if it does not exist.

    Adding capability, never changing behaviour: a caller passing an uncompressed
    path takes exactly the branch it always took.
    """
    if not path.exists():
        return default
    return _read_json_file(path)


def read_json_dict(path: Path) -> dict:
    """Parse a JSON object, or {} when the file is absent.

    Stages that merge layers into the graph always want a mapping, never None,
    so this is the reader they use.
    """
    value = read_json(path, default={})
    return value if isinstance(value, dict) else {}


# The one reserved top-level key in a committed prose artefact - the merged
# community summaries, the topic briefs and the deep dives. Everything else in
# such a file is `<entry id>: <authored content>`; this key holds the digests
# that say the prose is still what the stage wrote, and, for summaries, the
# content digest the write is gated on and the per-community coverage block.
# Community ids are the string forms of integers, so a leading underscore cannot
# collide with one; a topic slug or repository name called `_metadata` could, and
# nothing else in either configuration would make sense either.
#
# The name keeps `SUMMARIES_` because it is the name three stages, five tests and
# a mutation-gate entry already refer to. Renaming it would be a change with no
# behaviour in it.
SUMMARIES_METADATA_KEY = "_metadata"

# The recorded prose digests inside that block: one per entry, sorted, and
# deliberately not keyed to the entry id. `prose_digest` says why.
PROSE_DIGESTS_KEY = "prose_digests"


def summaries_body(document: dict) -> dict:
    """A merged summaries document with its metadata block removed."""
    return {key: value for key, value in document.items() if key != SUMMARIES_METADATA_KEY}


def read_summaries(path: Path) -> dict:
    """The prose in a merged summaries artefact, without its metadata block.

    Every stage that embeds, counts, re-keys or verifies summaries wants the
    prose alone. A stage reading the raw document instead treats the metadata
    block as a community: it reaches the explorer page, the semantic index's
    vocabulary, the count `status` prints and a remap's displaced prose, and each
    of those looks like ordinary output. One reader, so there is one copy of that
    knowledge rather than six chances to forget it.
    """
    return summaries_body(read_json_dict(path))


def read_prose_layer(path: Path) -> dict:
    """The entries in a committed prose artefact, without its metadata block.

    `read_summaries`, for the two prose layers that gained a metadata block later
    (#316): the topic briefs and the deep dives. Its docstring is the whole
    argument for this one existing - a consumer reading the document raw treats
    the block as an entry, which is a brief in the explorer's topic list and one
    more in the coverage count `status` prints, and both look like ordinary
    output. The two artefacts are shaped `<entry id>: {...}` rather than
    `<community id>: prose`, so nothing here can assume the value is a string.
    """
    return summaries_body(read_json_dict(path))


def rendered_prose(document: dict) -> dict[str, str]:
    """The authored prose in a briefs or dives artefact, one string per entry.

    The rendered `html`, because that is the LLM-authored half. The other fields
    of an entry - title, keywords, source path, the short sha - are derived from
    configuration or from provenance on every run, so a stage rewrites them
    whatever anybody did to the file and a digest over them would report a
    configuration change as an edit to the prose.
    """
    return {
        key: str(entry.get("html", ""))
        for key, entry in summaries_body(document).items()
        if isinstance(entry, dict)
    }


def prose_digest(prose: str) -> str:
    """A hash of one piece of authored prose, and of nothing else.

    Not of the id it is keyed to, deliberately. Community ids are positional and
    `summaries remap` re-keys prose onto new ones by design, so a digest over
    `{id: prose}` differs after every re-clustering - an integrity check resting
    on one would fire on the operation this library exists to perform, and be
    switched off within a week. Hashing the prose alone makes a legitimate re-key
    invisible to the check and an edit to the words visible to it.
    """
    return hashlib.sha256(prose.encode("utf-8")).hexdigest()


def prose_digests(prose: Iterable[str]) -> list[str]:
    """What a prose artefact records about itself: one digest per entry, sorted.

    A sorted list rather than a mapping, for the reason in `prose_digest`: the
    entry ids are not in it at all, so prose carried onto a new id produces the
    same list. Sorted because two runs on the same inputs must be byte-identical
    and the order entries happened to be built in is not a contract.
    """
    return sorted(prose_digest(text) for text in prose)


def prose_metadata(prose: Iterable[str]) -> dict:
    """The reserved block for a prose artefact that records nothing but digests."""
    return {PROSE_DIGESTS_KEY: prose_digests(prose)}


def prose_drift(path: Path, document: dict, prose: dict[str, str]) -> list[str]:
    """Report lines for entries carrying prose no digest in the file accounts for.

    `document` is the committed artefact as it stands and `prose` is the prose in
    that same document - this is a self-consistency check, not a comparison
    against what the stage is about to write. A hand edit changes the prose and
    leaves the recorded digests alone, so the two stop agreeing; the stage that
    wrote them last left them agreeing.

    Reports, and does not refuse: no caller changes its exit code on the strength
    of this. Whether post-hoc editing of generated prose happens at all is
    unmeasured (#316), and a stage that refused before anyone knew how common a
    legitimate hand edit was would be a stage nobody adopted.

    Names the entries rather than announcing that something moved, because "the
    digest changed" is not something a reader can act on. Matching is by multiset,
    so two entries holding identical prose need two recorded digests.

    Deletion is not reported. A digest per entry says an entry's prose is not one
    the stage wrote; it cannot say an entry the stage wrote has gone, because the
    recorded list is not tied to the ids. Chaining each entry's hash onto the
    previous one would make a removal break its successor - the idea #316 records
    and defers, being worth its own change rather than a corner of this one.

    An artefact with no recorded digests reports nothing: it predates this, and
    calling that tampering would flag every store's first run.

    How long the report persists differs by layer, and neither behaviour is this
    function's to choose. `topics merge` and `deepdive merge` re-render from the
    markdown an author writes, so the run that reports an edit also replaces it and
    the report fires once. `summaries merge` has no such source, and its write gate
    skips a run whose merged prose matches the digest recorded in the file - so a
    batch re-supplying the original prose leaves the edit on disk and the report
    repeats every run until somebody acts on it. For a stage that reports rather
    than refuses, persisting is the safer of the two: it does not silently revert
    an edit somebody may have meant, and it does not fall silent either.
    """
    recorded = (document.get(SUMMARIES_METADATA_KEY) or {}).get(PROSE_DIGESTS_KEY)
    if not isinstance(recorded, list) or not recorded:
        return []
    outstanding = Counter(str(digest) for digest in recorded)
    edited = []
    for entry in sorted(prose):
        digest = prose_digest(prose[entry])
        if outstanding[digest]:
            outstanding[digest] -= 1
        else:
            edited.append(entry)
    if not edited:
        return []
    return [
        f"{len(edited)} of {len(prose)} entries in {path} no longer carry the prose "
        "recorded there - reported, not refused:",
        *(f"  {entry}: prose differs from the digest recorded beside it" for entry in edited),
    ]


def write_json(path: Path, data, indent: int | None = None) -> None:
    checked_write_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(  # NOSONAR(S2083, S8707) - validated by checked_write_target above
        json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8"
    )


def read_gzip_json(path: Path, default=None):
    """Parse a gzip-compressed JSON file; return `default` if absent."""
    if not path.exists():
        return default
    with gzip.open(path, "rt", encoding="utf-8") as source:  # NOSONAR(S8707) - read policy
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
    checked_write_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (
        open(path, "wb") as raw,  # NOSONAR(S2083, S8707) - validated above
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
    """The estate graph (node-link JSON), gzipped or not. Raises if absent -
    callers treat a missing graph as a hard error with their own message.

    Every store gitignores the uncompressed graph and commits the archive beside
    it, so the compressed file is the one a fresh checkout has. This read had no
    suffix dispatch while `read_json` did, and the stages reading through it -
    the explorer, deep dives, topic briefs, community summaries and
    `status --verify-graph` - could therefore open only the file that is usually
    absent. Nothing said so at the call site; the archive fallback simply stopped
    at the boundary of a function that could not read an archive.

    Shares `_read_json_file` rather than repeating the dispatch, which is the
    whole point: one implementation cannot disagree with itself. Delegating to
    `read_json` instead would be the shorter change and the wrong one, because it
    answers an absent file with a default and this must raise - a default would
    turn a missing graph into an empty estate that every downstream count then
    reports as real.
    """
    return _read_json_file(path)


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
            # Read policy as above; the path comes from a caller that built it
            # from `--root`, so the same grounds apply.
            digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]  # NOSONAR(S8707)
        else:
            digests[name] = None
    return digests


def load_labels(path: Path) -> dict:
    """Community labels, or {} when not yet generated."""
    return read_json_dict(path)
