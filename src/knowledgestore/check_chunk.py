"""The fan-out's mechanical gate, shipped once rather than written by every agent (#394).

    knowledgestore check-chunk --batch BATCH.json [BATCH.json ...]
    knowledgestore check-chunk --self-test

The extraction spec asks every agent to check its own output mechanically before it
reports, and nothing shipped the check. So every agent wrote one. Measured on a full
rebuild of one large internal estate, an extraction agent spent 15 of its 24 turns
writing and iterating its own checker and mutation harness - 206 agents, 206
checkers - and that source then sat in the agent's context, re-read on every turn
for the rest of its run. It was the largest avoidable cost per chunk.

**It measures shape, not truth.** A chunk can pass every rule here and describe its
files wrongly. Grounding prose against the corpus stays the agent's own job.

A batch is `{"chunks": [{"n": 42, "out": "<path>", "files": ["<path>", ...]}]}`, the
form a dispatcher hands an agent. Three properties are the point of shipping it:

- **Each chunk is checked against its own file list, never the batch union.** A
  node citing a file from the next chunk in the batch is a containment breach even
  though the batch holds that file: the agent was not given it for this chunk.
- **Every rule contributes to the exit status, and violations print before any
  refusal.** A batch naming zero chunks is refused, because a check whose
  denominator is zero has not run - but what it did find is printed first.
- **Every read is guarded.** An agent killed mid-write leaves truncated JSON, and
  that is a `PARSE` violation naming the file, not a traceback that ends the run
  before the rest of the batch is read.

Two conventions are calibrated against a committed layer, and a first version got
both wrong:

- **A duplicate node id is a defect only within one repository.** Two repositories
  can each hold a `values` or a `readme`, and the merge keeps both because it keys
  on (chunk, id, repository). The repository is the path segment after
  `repositories/` in the node's `source_file`.
- **`contains` is an accepted relation** although the extraction spec's vocabulary
  omits it: real layers carry it, and the merge accepts it.

`--self-test` is the gate checked by breaking it: a synthetic batch, mutations each
asserting their rule fires *and no other*, negative controls that must stay
clean, and a truncated file that must be reported as `PARSE`. A gate that cannot fail
is worse than none, because it is trusted; this is how a user proves this one can.

Ids are compared with graphify's own `normalize_id`, so the stage needs graphify
installed (the `ast` extra carries it) and refuses with that remedy when it is not.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

Normalise = Callable[[str], str]

FILE_TYPES = frozenset({"code", "document", "paper", "image", "rationale", "concept"})
# The spec's vocabulary, plus `contains`: see the module docstring.
RELATIONS = frozenset(
    {
        "calls",
        "implements",
        "references",
        "cites",
        "conceptually_related_to",
        "shares_data_with",
        "semantically_similar_to",
        "rationale_for",
        "contains",
    }
)
INFERRED_SCORES = frozenset({0.95, 0.85, 0.75, 0.65, 0.55})
MIN_HYPEREDGE_ARITY = 3
MAX_HYPEREDGES = 3

# The rule names are the contract: the self-test, the tests and anyone grepping the
# output key on them, so they do not change.
PARSE = "PARSE"
SHAPE = "SHAPE"
FILE_TYPE = "FILE_TYPE"
RELATION = "RELATION"
CONFIDENCE = "CONFIDENCE"
ID_FORM = "ID_FORM"
DUP_NODE = "DUP_NODE"
DUP_HYPEREDGE = "DUP_HYPEREDGE"
DANGLING = "DANGLING"
HYPEREDGE_ARITY = "HYPEREDGE_ARITY"
HYPEREDGE_CAP = "HYPEREDGE_CAP"
SOURCE_FILE = "SOURCE_FILE"
CHUNK_SUFFIX = "CHUNK_SUFFIX"
COVERAGE = "COVERAGE"
RULES = (
    PARSE,
    SHAPE,
    FILE_TYPE,
    RELATION,
    CONFIDENCE,
    ID_FORM,
    DUP_NODE,
    DUP_HYPEREDGE,
    DANGLING,
    HYPEREDGE_ARITY,
    HYPEREDGE_CAP,
    SOURCE_FILE,
    CHUNK_SUFFIX,
    COVERAGE,
)

REPOSITORY = re.compile(r"(?:^|/)repositories/([^/]+)/")


class Violation(NamedTuple):
    rule: str
    where: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule} {self.where}: {self.detail}"


def repository_of(source_file: object) -> str | None:
    found = REPOSITORY.search(source_file) if isinstance(source_file, str) else None
    return found.group(1) if found else None


def own_chunk_suffix(number: int) -> re.Pattern[str]:
    """An id ending in *this* chunk's number, which the spec forbids.

    Compared against the chunk's own number, never a digit pattern: `_c100` is a
    four-digit form code on a real estate and a legitimate identifier, so a
    pattern of trailing digits would fire on correct data - and a gate that fires
    on correct data gets switched off.
    """
    return re.compile(rf"_(?:c{number:04d}|chunk{number})(?:_\d+)?$")


def _path(value: object) -> str | None:
    """A `source_file` as compared: `normpath` folds `./` and doubled slashes, nothing else."""
    return os.path.normpath(value) if isinstance(value, str) else None


def _in(value: object, vocabulary: frozenset) -> bool:
    return isinstance(value, str) and value in vocabulary


# The rubric's scores are two-decimal steps 0.10 apart, so a tolerance far below
# half a step cannot move a score into a neighbouring band, and 1e-9 sits many
# orders above the ~1e-16 a score carries when a writer computed it rather than
# typed it. JSON's `0.85` already parses to the same double as the literal; the
# tolerance is for `0.8500000000000001`, which is the right score.
SCORE_TOLERANCE = 1e-9


def _score_is(score: float, allowed: Iterable[float]) -> bool:
    return any(
        math.isclose(score, value, rel_tol=0.0, abs_tol=SCORE_TOLERANCE) for value in allowed
    )


def confidence_ok(record: dict) -> bool:
    score = record.get("confidence_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return False
    confidence = record.get("confidence")
    if confidence == "EXTRACTED":
        return _score_is(score, (1.0,))
    if confidence == "INFERRED":
        return _score_is(score, INFERRED_SCORES)
    if confidence == "AMBIGUOUS":
        return 0 < score < 0.55
    # Anything else fails. A rubric without this branch passes an unknown band.
    return False


def _read_chunk(out: Path, where: str) -> tuple[dict | None, Violation | None]:
    try:
        payload = json.loads(out.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, Violation(PARSE, where, f"{out} is not on disk")
    except (OSError, ValueError) as error:
        return None, Violation(PARSE, where, f"{out} does not parse: {error}")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("nodes"), list)
        or not isinstance(payload.get("edges"), list)
    ):
        return None, Violation(
            SHAPE, where, "the top level must be an object with `nodes` and `edges` lists"
        )
    return payload, None


# One rule each, shared by nodes, edges and hyperedges so the three report a breach
# of the same rule in the same words.


def _contained(
    record: dict, name: str, where: str, allowed: set[str], found: list[Violation]
) -> str | None:
    """SOURCE_FILE: the record's file as compared, or None when the chunk was not given it."""
    source = _path(record.get("source_file"))
    if source in allowed:
        return source
    found.append(
        Violation(
            SOURCE_FILE,
            where,
            f"{name} cites {record.get('source_file')!r}, not one of this chunk's files",
        )
    )
    return None


def _check_confidence(record: dict, name: str, where: str, found: list[Violation]) -> None:
    """CONFIDENCE: the band and its score agree with the rubric."""
    if not confidence_ok(record):
        found.append(
            Violation(
                CONFIDENCE,
                where,
                f"{name} confidence {record.get('confidence')!r} "
                f"score {record.get('confidence_score')!r}",
            )
        )


def _check_dangling(
    ends: Iterable[object], name: str, where: str, ids: set[str], found: list[Violation]
) -> None:
    """DANGLING: every endpoint or member is a node of this chunk."""
    for end in ends:
        if not isinstance(end, str) or end not in ids:
            found.append(Violation(DANGLING, where, f"{name} {end!r} is not a node of this chunk"))


def _check_node_id(
    nid: object,
    source_file: object,
    where: str,
    suffix: re.Pattern[str],
    seen: set[tuple[str, str | None]],
    normalise: Normalise,
    found: list[Violation],
) -> None:
    """ID_FORM, DUP_NODE within one repository, and CHUNK_SUFFIX, for one node id."""
    if not isinstance(nid, str) or not nid or nid != normalise(nid):
        canonical = normalise(nid) if isinstance(nid, str) else ""
        found.append(
            Violation(ID_FORM, where, f"node id {nid!r} is not its canonical form {canonical!r}")
        )
    if not isinstance(nid, str):
        return
    key = (nid, repository_of(source_file))
    if key in seen:
        found.append(Violation(DUP_NODE, where, f"node id {nid!r} twice in repository {key[1]!r}"))
    seen.add(key)
    if suffix.search(nid):
        found.append(Violation(CHUNK_SUFFIX, where, f"node id {nid!r} ends in this chunk's number"))


def _check_node_fields(node: dict, nid: object, where: str, found: list[Violation]) -> None:
    """FILE_TYPE from the vocabulary, and a label (a SHAPE breach when absent)."""
    if not _in(node.get("file_type"), FILE_TYPES):
        found.append(
            Violation(FILE_TYPE, where, f"node {nid!r} file_type {node.get('file_type')!r}")
        )
    label = node.get("label")
    if not isinstance(label, str) or not label.strip():
        found.append(Violation(SHAPE, where, f"node {nid!r} has no label"))


def _check_nodes(
    nodes: list,
    number: int,
    where: str,
    allowed: set[str],
    normalise: Normalise,
    found: list[Violation],
) -> tuple[set[str], set[str]]:
    """Check every node; return (the chunk's node ids, the files its nodes cite)."""
    suffix = own_chunk_suffix(number)
    ids: set[str] = set()
    seen: set[tuple[str, str | None]] = set()
    covered: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            found.append(Violation(SHAPE, where, f"a node is not an object: {node!r:.120}"))
            continue
        nid = node.get("id")
        _check_node_id(nid, node.get("source_file"), where, suffix, seen, normalise, found)
        if isinstance(nid, str):
            ids.add(nid)
        _check_node_fields(node, nid, where, found)
        source = _contained(node, f"node {nid!r}", where, allowed, found)
        if source is not None:
            covered.add(source)
    return ids, covered


def _check_edges(
    edges: list, where: str, ids: set[str], allowed: set[str], found: list[Violation]
) -> None:
    for edge in edges:
        if not isinstance(edge, dict):
            found.append(Violation(SHAPE, where, f"an edge is not an object: {edge!r:.120}"))
            continue
        name = f"edge {edge.get('source')}->{edge.get('target')}"
        if not _in(edge.get("relation"), RELATIONS):
            found.append(Violation(RELATION, where, f"{name} relation {edge.get('relation')!r}"))
        _check_confidence(edge, name, where, found)
        _check_dangling(
            (edge.get("source"), edge.get("target")), f"{name} endpoint", where, ids, found
        )
        _contained(edge, name, where, allowed, found)


def _check_hyperedge_id(
    hid: object,
    number: int,
    where: str,
    seen: dict[str, int],
    normalise: Normalise,
    found: list[Violation],
) -> None:
    """A hyperedge id is held to the same canonical form as a node id, and is unique.

    The canonical-form half was added after a real layer carried hyperedge ids
    spelt as repository paths, which a node-only rule passed.
    """
    if not isinstance(hid, str) or not hid:
        found.append(Violation(SHAPE, where, f"a hyperedge id is {hid!r}"))
        return
    if hid != normalise(hid):
        found.append(
            Violation(
                ID_FORM,
                where,
                f"hyperedge id {hid!r} is not its canonical form {normalise(hid)!r}",
            )
        )
    if hid in seen:
        found.append(
            Violation(DUP_HYPEREDGE, where, f"hyperedge id {hid!r} is used by chunk {seen[hid]}")
        )
    else:
        seen[hid] = number


def _members(hyperedge: dict, name: str, where: str, found: list[Violation]) -> list:
    """HYPEREDGE_ARITY: the members, reported when fewer than the minimum."""
    members = hyperedge.get("nodes")
    members = members if isinstance(members, list) else []
    if len(members) < MIN_HYPEREDGE_ARITY:
        found.append(
            Violation(
                HYPEREDGE_ARITY,
                where,
                f"{name} has {len(members)} nodes, needs at least {MIN_HYPEREDGE_ARITY}",
            )
        )
    return members


def _check_hyperedges(
    hyperedges: list,
    number: int,
    where: str,
    ids: set[str],
    allowed: set[str],
    seen: dict[str, int],
    normalise: Normalise,
    found: list[Violation],
) -> None:
    if len(hyperedges) > MAX_HYPEREDGES:
        found.append(
            Violation(
                HYPEREDGE_CAP, where, f"{len(hyperedges)} hyperedges, the cap is {MAX_HYPEREDGES}"
            )
        )
    for hyperedge in hyperedges:
        if not isinstance(hyperedge, dict):
            found.append(Violation(SHAPE, where, "a hyperedge is not an object"))
            continue
        hid = hyperedge.get("id")
        name = f"hyperedge {hid!r}"
        _check_hyperedge_id(hid, number, where, seen, normalise, found)
        members = _members(hyperedge, name, where, found)
        _check_dangling(members, f"{name} member", where, ids, found)
        _check_confidence(hyperedge, name, where, found)
        _contained(hyperedge, name, where, allowed, found)


def check_chunk(
    number: int,
    out: Path,
    files: Sequence[str],
    seen_hyperedges: dict[str, int],
    normalise: Normalise,
) -> tuple[list[Violation], Counter]:
    """Every violation in one chunk, and what was read to find them.

    `seen_hyperedges` is shared across every chunk of every batch in the run, which
    is what makes a hyperedge id reused by another chunk visible at all.
    """
    where = f"chunk {number}"
    counts: Counter = Counter(files=len(files))
    payload, refusal = _read_chunk(out, where)
    if payload is None:
        return [refusal] if refusal else [], counts
    found: list[Violation] = []
    hyperedges = payload.get("hyperedges")
    if hyperedges is None:
        hyperedges = []
    elif not isinstance(hyperedges, list):
        found.append(Violation(SHAPE, where, "`hyperedges` must be a list"))
        hyperedges = []
    counts.update(
        nodes=len(payload["nodes"]), edges=len(payload["edges"]), hyperedges=len(hyperedges)
    )
    allowed = {path for path in map(_path, files) if path is not None}
    ids, covered = _check_nodes(payload["nodes"], number, where, allowed, normalise, found)
    _check_edges(payload["edges"], where, ids, allowed, found)
    _check_hyperedges(hyperedges, number, where, ids, allowed, seen_hyperedges, normalise, found)
    for path in sorted(allowed - covered):
        found.append(Violation(COVERAGE, where, f"no node cites {path}"))
    return found, counts


def _entry(entry: object) -> tuple[int, Path, list[str]] | None:
    """(number, out, files) from one batch entry, or None when it is malformed."""
    if not isinstance(entry, dict):
        return None
    number, out, files = entry.get("n"), entry.get("out"), entry.get("files")
    if isinstance(number, str) and number.isdigit():
        number = int(number)
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or not isinstance(out, str)
        or not isinstance(files, list)
        or not all(isinstance(path, str) for path in files)
    ):
        return None
    return number, Path(out), files


def check_batches(paths: Sequence[Path], normalise: Normalise) -> tuple[list[Violation], Counter]:
    """Every violation across the batches, in the order read, and the run's denominators."""
    found: list[Violation] = []
    totals: Counter = Counter()
    seen_hyperedges: dict[str, int] = {}
    for path in paths:
        where = f"batch {path}"
        try:
            # NOSONAR(S8707) - reading a path the operator named is the purpose of
            # --batch. Grounds are stated once in `build_community_summaries.merge`;
            # this site cites them rather than restating them, so the two cannot drift.
            batch = json.loads(Path(path).read_text(encoding="utf-8"))  # NOSONAR(S8707)
        except (OSError, ValueError) as error:
            found.append(Violation(PARSE, where, f"unreadable: {error}"))
            continue
        chunks = batch.get("chunks") if isinstance(batch, dict) else None
        if not isinstance(chunks, list):
            found.append(Violation(PARSE, where, "holds no `chunks` list"))
            continue
        for index, entry in enumerate(chunks):
            parsed = _entry(entry)
            if parsed is None:
                found.append(
                    Violation(
                        SHAPE,
                        where,
                        f"entry {index} needs `n` (a chunk number), `out` (a path) "
                        "and `files` (a list of paths)",
                    )
                )
                continue
            number, out, files = parsed
            violations, counts = check_chunk(number, out, files, seen_hyperedges, normalise)
            found.extend(violations)
            totals["chunks"] += 1
            totals.update(counts)
    return found, totals


# --------------------------------------------------------------------- self-test


def _node(nid: str, source_file: str) -> dict:
    return {
        "id": nid,
        "label": f"node {nid}",
        "file_type": "code",
        "source_file": source_file,
        "source_location": None,
        "source_url": None,
        "captured_at": None,
        "author": None,
        "contributor": None,
    }


def _edge(source: str, target: str, source_file: str) -> dict:
    return {
        "source": source,
        "target": target,
        "relation": "references",
        "confidence": "EXTRACTED",
        "confidence_score": 1.0,
        "source_file": source_file,
        "source_location": None,
        "weight": 1.0,
    }


def _hyperedge(hid: str, members: list[str], source_file: str) -> dict:
    return {
        "id": hid,
        "label": f"hyperedge {hid}",
        "nodes": members,
        "relation": "form",
        "confidence": "INFERRED",
        "confidence_score": 0.85,
        "source_file": source_file,
    }


def _fixture(root: Path) -> tuple[dict[int, list[str]], dict[int, dict]]:
    """A clean two-chunk batch: (files per chunk, payload per chunk).

    Chunk 42 spans two repositories so the cross-repository control has somewhere
    to stand; chunk 43 exists so a hyperedge id can be reused across chunks.
    """
    a, b = (str(root / "repositories" / "alpha-repo" / name) for name in ("a.yaml", "b.tf"))
    c = str(root / "repositories" / "beta-repo" / "c.md")
    d = str(root / "repositories" / "beta-repo" / "d.md")
    files = {42: [a, b, c], 43: [d]}
    payloads = {
        42: {
            "nodes": [_node("alpha", a), _node("beta", b), _node("gamma", a), _node("delta", c)],
            "edges": [_edge("alpha", "beta", a)],
            "hyperedges": [_hyperedge("trio", ["alpha", "beta", "gamma"], a)],
        },
        43: {
            "nodes": [_node(nid, d) for nid in ("one", "two", "three")],
            "edges": [_edge("one", "two", d)],
            "hyperedges": [_hyperedge("three_together", ["one", "two", "three"], d)],
        },
    }
    return files, payloads


def _rename(payload: dict, old: str, new: str) -> dict:
    """Rename a node id everywhere it appears, so only the rule under test can fire."""
    for node in payload["nodes"]:
        if node["id"] == old:
            node["id"] = new
    for edge in payload["edges"]:
        for end in ("source", "target"):
            if edge[end] == old:
                edge[end] = new
    for hyperedge in payload["hyperedges"]:
        hyperedge["nodes"] = [new if member == old else member for member in hyperedge["nodes"]]
    return payload


Mutation = Callable[[dict[int, dict], dict[int, list[str]]], "dict[int, dict] | str"]


def _set(payloads: dict[int, dict], kind: str, index: int, **fields) -> dict[int, dict]:
    payloads[42][kind][index].update(fields)
    return payloads


def _without_beta(payloads: dict[int, dict]) -> dict[int, dict]:
    chunk = payloads[42]
    chunk["nodes"] = [node for node in chunk["nodes"] if node["id"] != "beta"]
    chunk["edges"], chunk["hyperedges"] = [], []
    return payloads


def _over_cap(payloads: dict[int, dict]) -> dict[int, dict]:
    extra = payloads[42]["hyperedges"][0]
    payloads[42]["hyperedges"] += [dict(extra, id=f"trio_{i}") for i in (1, 2, 3)]
    return payloads


def _truncated(payloads: dict[int, dict]) -> str:
    text = json.dumps(payloads[42])
    return text[: len(text) // 2]


def _cross_repository(payloads: dict[int, dict], files: dict[int, list[str]]) -> dict[int, dict]:
    payloads[42]["nodes"].append(_node("alpha", files[42][2]))
    return payloads


# (rule that must fire alone, what the mutation does, the mutation). A mutation returns
# chunk 42's replacement text as a string when the defect is in the bytes themselves.
MUTATIONS: tuple[tuple[str, str, Mutation], ...] = (
    (PARSE, "chunk file truncated mid-write", lambda p, f: _truncated(p)),
    (SHAPE, "`nodes` is an object, not a list", lambda p, f: {**p, 42: {"nodes": {}, "edges": []}}),
    (
        FILE_TYPE,
        "file_type outside the vocabulary",
        lambda p, f: _set(p, "nodes", 0, file_type="yaml"),
    ),
    (
        RELATION,
        "relation outside the vocabulary",
        lambda p, f: _set(p, "edges", 0, relation="depends_on"),
    ),
    (
        CONFIDENCE,
        "INFERRED with a score off the rubric",
        lambda p, f: _set(p, "edges", 0, confidence="INFERRED", confidence_score=0.9),
    ),
    (
        ID_FORM,
        "a node id that is not its canonical form",
        lambda p, f: {**p, 42: _rename(p[42], "alpha", "Alpha")},
    ),
    (
        ID_FORM,
        "a hyperedge id that is not its canonical form",
        lambda p, f: _set(p, "hyperedges", 0, id="alpha-repo/trio"),
    ),
    (
        DUP_NODE,
        "one id twice in one repository",
        lambda p, f: {**p, 42: {**p[42], "nodes": [*p[42]["nodes"], dict(p[42]["nodes"][0])]}},
    ),
    (
        DUP_HYPEREDGE,
        "a hyperedge id reused by another chunk",
        lambda p, f: {**p, 43: {**p[43], "hyperedges": [dict(p[43]["hyperedges"][0], id="trio")]}},
    ),
    (
        DANGLING,
        "an edge to a node the chunk lacks",
        lambda p, f: _set(p, "edges", 0, target="nobody"),
    ),
    (
        HYPEREDGE_ARITY,
        "a hyperedge of two nodes",
        lambda p, f: _set(p, "hyperedges", 0, nodes=["alpha", "beta"]),
    ),
    (HYPEREDGE_CAP, "four hyperedges in one chunk", lambda p, f: _over_cap(p)),
    (
        SOURCE_FILE,
        "a node citing another chunk's file",
        lambda p, f: _set(p, "nodes", 0, source_file=f[43][0]),
    ),
    (
        CHUNK_SUFFIX,
        "an id ending in this chunk's number",
        lambda p, f: {**p, 42: _rename(p[42], "gamma", "gamma_c0042")},
    ),
    (COVERAGE, "a file no node cites", lambda p, f: _without_beta(p)),
)

# Each must leave the batch entirely clean. The first five are ids ending in digits
# that are not this chunk's number - a form code, a year, a port, the neighbouring
# chunk's number; the last two are the calibrated conventions.
CONTROLS: tuple[tuple[str, Mutation], ...] = (
    ("id `form_c21`", lambda p, f: {**p, 42: _rename(p[42], "alpha", "form_c21")}),
    ("id `form_c100`", lambda p, f: {**p, 42: _rename(p[42], "alpha", "form_c100")}),
    ("id `year_2026`", lambda p, f: {**p, 42: _rename(p[42], "alpha", "year_2026")}),
    (
        "id `host_localhost_3000`",
        lambda p, f: {**p, 42: _rename(p[42], "alpha", "host_localhost_3000")},
    ),
    (
        "id `other_c0043` (another chunk's number)",
        lambda p, f: {**p, 42: _rename(p[42], "alpha", "other_c0043")},
    ),
    ("one id in two repositories", _cross_repository),
    ("relation `contains`", lambda p, f: _set(p, "edges", 0, relation="contains")),
)


def _run_mutant(root: Path, label: str, mutation: Mutation, normalise: Normalise) -> set[str]:
    """The rules that fire on the fixture after `mutation`, in a directory of its own."""
    directory = root / label
    directory.mkdir()
    files, payloads = _fixture(directory)
    mutated = mutation(payloads, files)
    entries = []
    for number in (42, 43):
        out = directory / f".graphify_chunk_{number:04d}.json"
        if isinstance(mutated, str) and number == 42:
            out.write_text(mutated, encoding="utf-8")
        else:
            out.write_text(
                json.dumps(mutated[number] if isinstance(mutated, dict) else payloads[number]),
                encoding="utf-8",
            )
        entries.append({"n": number, "out": str(out), "files": files[number]})
    batch = directory / "batch.json"
    batch.write_text(json.dumps({"chunks": entries}), encoding="utf-8")
    violations, _ = check_batches([batch], normalise)
    return {violation.rule for violation in violations}


def self_test(normalise: Normalise) -> int:
    """Prove each rule can fire, alone, and that correct data stays clean."""
    failures = []
    with tempfile.TemporaryDirectory(prefix="check-chunk-self-test-") as tmp:
        root = Path(tmp)
        baseline = _run_mutant(root, "baseline", lambda p, f: p, normalise)
        if baseline:
            print(f"FAIL  the unmutated fixture is not clean: {sorted(baseline)}")
            return 1
        for index, (rule, description, mutation) in enumerate(MUTATIONS):
            fired = _run_mutant(root, f"mutant-{index}", mutation, normalise)
            if fired == {rule}:
                print(f"caught  {rule:<16} {description}")
            else:
                failures.append(rule)
                print(f"FAIL    {rule:<16} {description}: fired {sorted(fired) or 'nothing'}")
        for index, (description, control) in enumerate(CONTROLS):
            fired = _run_mutant(root, f"control-{index}", control, normalise)
            if fired:
                failures.append(description)
                print(f"FAIL    control  {description}: fired {sorted(fired)}")
            else:
                print(f"clean   control  {description}")
    untested = sorted(set(RULES) - {rule for rule, _, _ in MUTATIONS})
    for rule in untested:
        failures.append(rule)
        print(f"FAIL    {rule:<16} no mutation exercises this rule")
    print(
        f"self-test: {len(MUTATIONS)} mutations over {len(RULES)} rules, "
        f"{len(CONTROLS)} negative controls; failures={len(failures)}"
    )
    return 1 if failures else 0


# --------------------------------------------------------------------- entry point


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore check-chunk",
        description="Check each chunk of a fan-out batch against its own file list.",
    )
    parser.add_argument(
        "--batch",
        type=Path,
        nargs="+",
        default=[],
        help='batch file(s): {"chunks": [{"n", "out", "files"}]}; hyperedge ids are '
        "compared across every batch named",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="break every rule on a synthetic batch and require each to fire, alone",
    )
    arguments = parser.parse_args(argv)
    if not arguments.self_test and not arguments.batch:
        parser.error("give --batch BATCH.json, or --self-test")
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        from graphify.ids import normalize_id
    except ImportError:
        print(
            "check-chunk compares every id with graphify's own normalize_id, and graphify is "
            "not installed. Install it with `pip install 'hmcts-knowledge-store-builder[ast]'` "
            "and re-run.",
            file=sys.stderr,
        )
        return 1
    if arguments.self_test:
        return self_test(normalize_id)

    violations, totals = check_batches(arguments.batch, normalize_id)
    for violation in violations:
        print(violation)
    print(
        f"checked: {totals['chunks']:,} chunks, {totals['nodes']:,} nodes, "
        f"{totals['edges']:,} edges, {totals['hyperedges']:,} hyperedges over "
        f"{totals['files']:,} files from {len(arguments.batch)} batch file(s); "
        f"violations={len(violations):,}"
    )
    if violations:
        by_rule = Counter(violation.rule for violation in violations)
        print("by rule: " + ", ".join(f"{rule} {by_rule[rule]:,}" for rule in sorted(by_rule)))
    if totals["chunks"] == 0:
        print("REFUSED: zero chunks checked, so nothing above is evidence of a clean batch")
        return 1
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
