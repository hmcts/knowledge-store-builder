"""Report how stale the store's layers are. Never fails: drift is normal.

Cheap by design — this stage must not load the graph. The corpus citation
check reads the small tracked extract, not graph.json.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import config, io, provenance
from .build_topic_briefs import read_topics

SUMMARIES_PATH = config.SUMMARIES_PATH
SUMMARIES_INPUT_PATH = config.SUMMARIES_INPUT_PATH
TOPICS_BRIEFS_PATH = config.TOPICS_BRIEFS_PATH
TOPICS_CONFIG_PATH = config.TOPICS_CONFIG_PATH
EXPLORER_PATH = config.EXPLORER_PATH
ROOT = config.ROOT

# committed layers the page embeds; if any is newer than the page, rebuild
EMBEDDED_LAYERS = (
    "knowledge/summaries/communities.json",
    "knowledge/topics/briefs.json",
    "knowledge/semantic/token-neighbours.json.gz",
    "knowledge/intent/ticket-descriptions.json.gz",
)


def run_git(arguments: list[str]) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return completed.stdout


def layer_coverage() -> dict:
    summaries = io.read_json_dict(SUMMARIES_PATH)
    digests = io.read_json(SUMMARIES_INPUT_PATH, default=[])
    briefs = io.read_json_dict(TOPICS_BRIEFS_PATH)
    try:
        topics = read_topics(TOPICS_CONFIG_PATH)
    except (OSError, ValueError):
        topics = []
    return {
        "summaries_written": len(summaries),
        "summaries_expected": len(digests) if isinstance(digests, list) else 0,
        "briefs_written": len(briefs),
        "topics_configured": len(topics),
    }


def corpus_citations(root: Path) -> dict:
    corpus = io.read_json_dict(root / "graphify-out" / "graph-knowledge-corpus.json")
    nodes = [n for n in corpus.get("nodes", []) if n.get("source_file")]
    dangling = sorted(n["source_file"] for n in nodes if not (root / n["source_file"]).exists())
    return {"checked": len(nodes), "dangling": dangling}


def _committed_at(path: str, run) -> str:
    try:
        return run(["-C", str(ROOT), "log", "-1", "--format=%cI", "--", path]).strip()
    except Exception:
        return ""


def artefact_freshness(run=run_git) -> dict:
    explorer = _committed_at(
        str(EXPLORER_PATH.relative_to(ROOT))
        if EXPLORER_PATH.is_relative_to(ROOT)
        else str(EXPLORER_PATH),
        run,
    )
    if not explorer:
        return {}
    layers = [d for layer in EMBEDDED_LAYERS if (d := _committed_at(layer, run))]
    newest_layer = max(layers, default="")
    return {
        "explorer_committed": explorer,
        "layers_committed": newest_layer,
        "explorer_stale": bool(newest_layer) and newest_layer > explorer,
    }


def main() -> int:
    recorded = provenance.read()
    print(
        f"Provenance: {len(recorded)} repositories recorded"
        if recorded
        else "Provenance: none recorded - run `knowledgestore sync` to record it"
    )

    cov = layer_coverage()
    print(
        f"Summaries: {cov['summaries_written']}/{cov['summaries_expected']} "
        f"significant communities have prose"
    )
    print(
        f"Topic briefs: {cov['briefs_written']} written, "
        f"{cov['topics_configured']} topics configured"
    )

    cites = corpus_citations(ROOT)
    if cites["dangling"]:
        print(f"Dangling corpus citations ({len(cites['dangling'])}):")
        for path in cites["dangling"][:10]:
            print(f"  - {path}")
    else:
        print(f"Corpus citations: {cites['checked']} checked, none dangling")

    fresh = artefact_freshness()
    if fresh.get("explorer_stale"):
        print(
            "Explorer page is OLDER than a layer it embeds - "
            "run `knowledgestore explorer` and commit the rebuilt page"
        )
    elif fresh:
        print("Explorer page is newer than every embedded layer")

    return 0
