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

  3. knowledgestore summaries merge <file.json ...>
       -> knowledge/summaries/communities.json  (committed)
       Validates ids and length bounds, merges over any existing file.

The explorer embeds the merged summaries; Ask answers then include
pre-written prose selected deterministically - no query-time AI.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


from . import config
from . import io
from . import kinds

GRAPH_PATH = config.GRAPH_PATH
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


def community_digest(community: int, nodes: list[dict], labels: dict,
                     intent: dict, degree: dict) -> dict:
    """The raw material one community summary is written from."""
    nodes.sort(key=lambda n: -degree[n["id"]])
    repos = Counter(n.get("repo", "") for n in nodes)
    features = [
        n["label"] for n in nodes
        if kinds.is_kind(n, kinds.FEATURE)
    ]
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
            for n in nodes[:TOP_NODES * 2]
            if n.get("label")
        ][:TOP_NODES],
        "business_features": features[:TOP_FEATURES],
        "tickets": [t for t, _ in tickets.most_common(TOP_TICKETS)],
    }


def extract() -> int:
    graph = io.load_graph(GRAPH_PATH)
    labels = io.load_labels(LABELS_PATH)
    intent = io.read_gzip_json(INTENT_PATH, default={})

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
    known_ids = {
        str(d["id"])
        for d in json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    }
    merged: dict[str, str] = (
        json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        if OUTPUT_PATH.exists()
        else {}
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
        json.dumps(dict(sorted(merged.items(), key=lambda kv: int(kv[0]))),
                   indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    for r in rejected:
        print(f"rejected - {r}")
    print(f"{added} summaries merged ({len(merged)} total) -> {OUTPUT_PATH}")
    missing = len(known_ids) - len(merged)
    if missing:
        print(f"{missing} significant communities still lack a summary")
    return 1 if rejected else 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "extract":
        return extract()
    if len(sys.argv) >= 3 and sys.argv[1] == "merge":
        return merge(sys.argv[2:])
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
