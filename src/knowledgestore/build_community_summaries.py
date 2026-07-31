"""Community summaries - the GraphRAG indexing step, done at build time.

GraphRAG's core technique is LLM-written summaries of each graph community
at *index* time, so query time needs no LLM at all. This script provides
the deterministic halves of that step; the generation itself runs in
Claude Code (maintainers have a licence; consumers never need one):

  1. knowledgestore summaries extract
       -> knowledge/summaries/communities-input.json
       One digest per significant community (label, size, repositories,
       top nodes, business features, Jira tickets) - the raw material.

  2. In Claude Code: generate 2-4 sentence business summaries for each
     digest, as JSON files of {"<community id>": "<summary>", ...}.

  3. knowledgestore summaries snapshot
       Records community membership before a re-cluster moves the ids.

  4. knowledgestore summaries remap [--bar 0.6] [--floor 10]
       After re-clustering, carries summaries onto the new ids by membership
       overlap and reports retention.

  5. knowledgestore summaries merge <file.json ...>
       -> knowledge/summaries/communities.json  (committed)
       Validates ids and length bounds, merges over any existing file.

The explorer embeds the merged summaries; Ask answers then include
pre-written prose selected deterministically - no query-time AI.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


from . import config
from . import io
from . import kinds

GRAPH_PATH = config.GRAPH_PATH
SNAPSHOT_PATH = config.SUMMARIES_SNAPSHOT_PATH
LABELS_PATH = config.LABELS_PATH
INTENT_PATH = config.INTENT_INDEX_PATH
INPUT_PATH = config.SUMMARIES_INPUT_PATH
OUTPUT_PATH = config.SUMMARIES_PATH

MIN_COMMUNITY_SIZE = config.MIN_COMMUNITY_SIZE
TOP_NODES = 12
TOP_FEATURES = 5
TOP_TICKETS = 8
MIN_SUMMARY_LEN = 60
MAX_SUMMARY_LEN = 700


def community_digest(
    community: int, nodes: list[dict], labels: dict, intent: dict, degree: dict
) -> dict:
    """The raw material one community summary is written from."""
    nodes.sort(key=lambda n: -degree[n["id"]])
    repos = Counter(n.get("repo", "") for n in nodes)
    features = [n["label"] for n in nodes if kinds.is_kind(n, kinds.FEATURE)]
    tickets: Counter = Counter()
    for n in nodes[:30]:
        tickets.update((n.get("metadata") or {}).get("tickets") or [])
        entry = intent.get(n.get("repo", ""), {}).get(n.get("source_file") or "")
        if entry:
            tickets.update(dict(list(entry["tickets"].items())[:3]))
    return {
        "id": community,
        "label": labels.get(str(community), f"Community {community}"),
        "size": len(nodes),
        "repositories": [r for r, _ in repos.most_common(4) if r],
        "top_nodes": [
            # label-less structural nodes (Java package hierarchy) are skipped
            f"{n['label']} ({n.get('source_file') or '?'})"
            for n in nodes[: TOP_NODES * 2]
            if n.get("label")
        ][:TOP_NODES],
        "business_features": features[:TOP_FEATURES],
        "tickets": [t for t, _ in tickets.most_common(TOP_TICKETS)],
    }


def extract() -> int:
    graph = io.load_graph(GRAPH_PATH)
    labels = io.load_labels(LABELS_PATH)
    intent = io.read_gzip_json_dict(INTENT_PATH)

    degree: dict[str, int] = defaultdict(int)
    for edge in graph["links"]:
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1

    members: dict[int, list[dict]] = defaultdict(list)
    for node in graph["nodes"]:
        members[node.get("community", -1)].append(node)

    digests = [
        community_digest(community, nodes, labels, intent, degree)
        for community, nodes in sorted(members.items(), key=lambda kv: -len(kv[1]))
        if len(nodes) >= MIN_COMMUNITY_SIZE
    ]

    INPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Sonar S2083 misfires here: INPUT_PATH is a module constant derived from
    # configuration, not untrusted input; this is offline build tooling.
    INPUT_PATH.write_text(  # NOSONAR(S2083)
        json.dumps(digests, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print(f"{len(digests)} community digests -> {INPUT_PATH}")
    return 0


def merge(paths: list[str]) -> int:
    known_ids = {str(d["id"]) for d in json.loads(INPUT_PATH.read_text(encoding="utf-8"))}
    merged: dict[str, str] = (
        json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else {}
    )
    added, rejected = 0, []
    for path in paths:
        # Sonar S8707: reading a caller-supplied path is this maintainer CLI's
        # purpose; it runs offline against a local clone with no privilege
        # boundary to cross.
        batch = json.loads(Path(path).read_text(encoding="utf-8"))  # NOSONAR(S8707)
        for community_id, summary in batch.items():
            summary = " ".join(str(summary).split())
            if str(community_id) not in known_ids:
                rejected.append(f"{community_id}: unknown community id")
            elif not MIN_SUMMARY_LEN <= len(summary) <= MAX_SUMMARY_LEN:
                rejected.append(f"{community_id}: length {len(summary)} outside bounds")
            else:
                merged[str(community_id)] = summary
                added += 1

    OUTPUT_PATH.write_text(
        json.dumps(
            dict(sorted(merged.items(), key=lambda kv: int(kv[0]))), indent=1, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    for r in rejected:
        print(f"rejected - {r}")
    print(f"{added} summaries merged ({len(merged)} total) -> {OUTPUT_PATH}")
    missing = len(known_ids) - len(merged)
    if missing:
        print(f"{missing} significant communities still lack a summary")
    return 1 if rejected else 0


# The share of an old cluster's members that must land in one new cluster before
# its summary is carried across. Below this the summary is dropped: prose on the
# wrong cluster reads as authoritative and is worse than no prose. 0.6 has been
# used across several estate refreshes and behaved sensibly.
DEFAULT_BAR = 0.6
# Refuse to remap when fewer summaries than this are loaded. A mis-specified path
# reads as "almost nothing to do" rather than failing, and the run then writes an
# empty file over a good one.
DEFAULT_FLOOR = 10


def _membership(graph: dict) -> dict[str, list[str]]:
    members: dict[str, list[str]] = {}
    for node in graph.get("nodes", []):
        community = node.get("community")
        if community is None:
            continue
        members.setdefault(str(community), []).append(node["id"])
    return members


def snapshot() -> int:
    """Record community membership before a re-cluster moves the ids."""
    graph = io.read_json_dict(GRAPH_PATH)
    members = _membership(graph)
    if not members:
        print(
            f"No communities in {GRAPH_PATH}. Cluster the graph before snapshotting - "
            "an empty snapshot makes every later remap drop everything.",
            file=sys.stderr,
        )
        return 1
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(SNAPSHOT_PATH, "wt", encoding="utf-8") as handle:
        json.dump(members, handle)
    print(
        f"Snapshotted {len(members)} communities "
        f"({sum(len(v) for v in members.values())} member nodes) -> {SNAPSHOT_PATH}"
    )
    return 0


def remap(bar: float = DEFAULT_BAR, floor: int = DEFAULT_FLOOR) -> int:
    """Carry committed summaries onto new community ids after a re-cluster.

    For each summary, find the new cluster holding the largest share of its old
    members and carry it there when that share meets `bar`. Drop it otherwise,
    and report retention so the cost of the re-cluster is a measured number.
    """
    if not SNAPSHOT_PATH.exists():
        print(
            f"No membership snapshot at {SNAPSHOT_PATH}. Run `summaries snapshot` "
            "before re-clustering.",
            file=sys.stderr,
        )
        return 1
    with gzip.open(SNAPSHOT_PATH, "rt", encoding="utf-8") as handle:
        old_members: dict[str, list[str]] = json.load(handle)
    summaries = io.read_json_dict(OUTPUT_PATH)
    if len(summaries) < floor:
        print(
            f"Refusing to remap: only {len(summaries)} summaries loaded from "
            f"{OUTPUT_PATH} (floor {floor}). A mis-specified path looks like this. "
            "Pass --floor to lower it for a genuinely small store.",
            file=sys.stderr,
        )
        return 1
    new_community = {
        node["id"]: str(node["community"])
        for node in io.read_json_dict(GRAPH_PATH).get("nodes", [])
        if node.get("community") is not None
    }
    snapshot_ids = {node for ids in old_members.values() for node in ids}
    if snapshot_ids and not (snapshot_ids & set(new_community)):
        print(
            "Refusing to remap: the snapshot and the graph share no node ids, so "
            "this is the wrong snapshot. Proceeding would drop every summary and "
            "report it as legitimate 0% retention.",
            file=sys.stderr,
        )
        return 1

    remapped: dict[str, str] = {}
    below_bar: list[str] = []
    members_gone: list[str] = []
    collisions: list[str] = []
    # Sorted so a collision resolves to the lowest old id every run, rather than
    # to whichever happened to be seen first.
    for old_id in sorted(summaries, key=lambda k: (len(k), k)):
        members = old_members.get(str(old_id))
        if not members:
            members_gone.append(old_id)
            continue
        landed = Counter(new_community[m] for m in members if m in new_community)
        if not landed:
            members_gone.append(old_id)
            continue
        target, count = landed.most_common(1)[0]
        if count / len(members) < bar:
            below_bar.append(old_id)
            continue
        if target in remapped:
            collisions.append(old_id)
            continue
        remapped[target] = summaries[old_id]

    OUTPUT_PATH.write_text(
        json.dumps(dict(sorted(remapped.items(), key=lambda kv: int(kv[0]))), indent=1),
        encoding="utf-8",
    )
    total = len(summaries)
    share = (100 * len(remapped) // total) if total else 0
    print(f"Retained {len(remapped)} of {total} summaries ({share}%) -> {OUTPUT_PATH}")
    print(
        f"Dropped: {len(below_bar)} below {int(bar * 100)}% overlap, "
        f"{len(members_gone)} whose members are gone, "
        f"{len(collisions)} merged-cluster collisions"
    )
    return 0


# A token is treated as a claim about code only if it is shaped like one. This is
# structural rather than a blocklist, because ordinary prose is full of
# capitalised words - "Welsh", "Angular", "Common Platform" - that are not claims
# about code, and a blocklist of them would never be complete. Requiring an
# internal case change, a separator, a file extension or a ticket shape excludes
# English capitalisation and all-caps acronyms without naming any of them.
_CAMEL = re.compile(r"\b[A-Za-z][a-z0-9]*[a-z0-9][A-Z][A-Za-z0-9]*\b")
_SNAKE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b")
_DASHED = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+){2,}\b")
_FILE = re.compile(
    r"\b[A-Za-z0-9_.-]+\.(?:java|ts|js|py|json|yaml|yml|xml|raml|csv|feature|sql|html|tsx)\b"
)
_TICKET = re.compile(r"\b[A-Z]{2,}-\d+\b")
# Words that mark a claim the author had no evidence for. A factual description
# layer should contain almost none.
_SPECULATION = re.compile(
    r"\b(probably|likely|presumably|possibly|perhaps|appears to|seems to|"
    r"suggests that|may be|might be|could be)\b",
    re.I,
)


def prose_identifiers(text: str) -> set[str]:
    """Tokens in `text` shaped like a claim about code."""
    found: set[str] = set()
    for pattern in (_FILE, _TICKET, _CAMEL, _SNAKE, _DASHED):
        found.update(pattern.findall(text))
    return found


def _digest_identifiers(digest: dict) -> set[str]:
    """Every identifier the evidence contains, including path components."""
    evidence: set[str] = set()
    for repo in digest.get("repositories", []):
        evidence.add(repo)
    for field in ("label",):
        if digest.get(field):
            evidence.add(str(digest[field]))
    for node in digest.get("top_nodes", []):
        if node.get("label"):
            evidence.add(str(node["label"]))
        source = str(node.get("source_file") or "")
        if source:
            evidence.add(source)
            evidence.update(part for part in re.split(r"[/\\]", source) if part)
    for feature in digest.get("business_features", []):
        label = feature.get("label") if isinstance(feature, dict) else feature
        if label:
            evidence.add(str(label))
    evidence.update(str(t) for t in digest.get("tickets", []))
    # a prose mention of Foo.java is grounded by evidence naming Foo, and vice
    # versa, so index the stem alongside the filename
    for item in list(evidence):
        stem = item.rsplit(".", 1)[0]
        if stem and stem != item:
            evidence.add(stem)
        evidence.add(f"{item}.java")
    return evidence


def verify(sample: int | None = None, strict: bool = False) -> int:
    """Check authored summaries cite only what their digests contain.

    Coverage checks confirm every digest got prose; this confirms the prose is
    grounded. Reports rather than fails, so it can be run on a whole store
    without blocking; `strict` is for CI.
    """
    loaded = io.read_json(INPUT_PATH, default=[]) or []
    digests = {str(d["id"]): d for d in loaded if isinstance(d, dict) and "id" in d}
    prose = io.read_json_dict(OUTPUT_PATH)
    if not digests or not prose:
        print(
            f"Nothing to verify: {len(digests)} digests in {INPUT_PATH}, "
            f"{len(prose)} summaries in {OUTPUT_PATH}. Run `summaries extract` first.",
            file=sys.stderr,
        )
        return 1

    ids = sorted(prose, key=lambda k: (len(k), k))
    if sample and sample < len(ids):
        # deterministic sample: reproducible between runs, so a finding can be
        # re-examined without it disappearing
        step = len(ids) / sample
        ids = [ids[int(i * step)] for i in range(sample)]

    unsupported: list[tuple[str, set[str]]] = []
    speculative: list[tuple[str, list[str]]] = []
    orphaned: list[str] = []
    for cid in ids:
        text = prose[cid]
        digest = digests.get(cid)
        if digest is None:
            orphaned.append(cid)
            continue
        extra = prose_identifiers(text) - _digest_identifiers(digest)
        if extra:
            unsupported.append((cid, extra))
        hedges = _SPECULATION.findall(text)
        if hedges:
            speculative.append((cid, hedges))

    print(f"Verified {len(ids)} of {len(prose)} summaries against their digests.")
    for cid, extra in unsupported:
        print(f"  [unsupported] community {cid} cites: {', '.join(sorted(extra))}")
    for cid, hedges in speculative:
        print(
            f"  [speculation] community {cid}: {', '.join(sorted(set(h.lower() for h in hedges)))}"
        )
    for cid in orphaned:
        print(f"  [no digest] community {cid} has prose but no evidence to check it against")
    if not (unsupported or speculative or orphaned):
        print("  nothing unsupported.")
    else:
        print(
            f"  {len(unsupported)} unsupported, {len(speculative)} speculative, "
            f"{len(orphaned)} without a digest."
        )
    return 1 if (strict and (unsupported or orphaned)) else 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["extract"]:
        return extract()
    if arguments[:1] == ["merge"] and len(arguments) >= 2:
        return merge(arguments[1:])
    if arguments[:1] == ["snapshot"]:
        return snapshot()
    if arguments[:1] == ["verify"]:
        parser = argparse.ArgumentParser(prog="knowledgestore summaries verify")
        parser.add_argument("--sample", type=int, default=None)
        parser.add_argument("--strict", action="store_true")
        options = parser.parse_args(arguments[1:])
        return verify(sample=options.sample, strict=options.strict)
    if arguments[:1] == ["remap"]:
        parser = argparse.ArgumentParser(prog="knowledgestore summaries remap")
        parser.add_argument("--bar", type=float, default=DEFAULT_BAR)
        parser.add_argument("--floor", type=int, default=DEFAULT_FLOOR)
        options = parser.parse_args(arguments[1:])
        return remap(bar=options.bar, floor=options.floor)
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
