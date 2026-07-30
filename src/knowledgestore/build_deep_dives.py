"""Deep dives - an evidence-grounded dossier on one repository.

Same shape as topic briefs: a deterministic `extract` gathers evidence, a
person or agent writes the dossier from it, and `merge` validates and renders
before anything enters the store.

    knowledgestore deepdive extract <repo>   # NOTE: loads the full graph
    # write docs/deep-dives/<repo>.md from the bundle, then:
    knowledgestore deepdive merge

Everything in the bundle is derived from committed layers - the graph, the
intent index, ticket descriptions, community summaries - so a dossier's every
claim is checkable against the store itself.
"""

from __future__ import annotations

from collections import Counter

from . import config

GRAPH_PATH = config.GRAPH_PATH
LABELS_PATH = config.LABELS_PATH
INTENT_PATH = config.INTENT_INDEX_PATH
DESCRIPTIONS_PATH = config.TICKET_DESCRIPTIONS_PATH
SUMMARIES_PATH = config.SUMMARIES_PATH
INPUT_DIR = config.DEEPDIVES_INPUT_DIR
DOCS_DIR = config.DEEPDIVES_DOCS_DIR
DIVES_PATH = config.DEEPDIVES_PATH
TOP_FILES = config.DIVE_TOP_FILES
MIN_COCHANGE = config.DIVE_MIN_COCHANGE
COCHANGE_MAX_FILES = config.DIVE_COCHANGE_MAX_FILES_PER_TICKET
REVERT = config.REVERT_PATTERN
FIX = config.FIX_PATTERN

MIN_DIVE_LENGTH = 800


def scale_section(graph: dict, repo: str, labels: dict, summaries: dict) -> dict:
    mine = [n for n in graph["nodes"] if n.get("repo") == repo]
    communities = Counter(n.get("community") for n in mine if n.get("community") is not None)
    top = [
        {
            "id": cid,
            "label": labels.get(str(cid), f"Community {cid}"),
            "size": size,
            "summary": summaries.get(str(cid)),
        }
        for cid, size in sorted(communities.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    ]
    return {
        "nodes": len(mine),
        "share": len(mine) / max(len(graph["nodes"]), 1),
        "communities": len(communities),
        "top_communities": top,
    }


def churn_section(files: dict) -> dict:
    ranked = sorted(files.items(), key=lambda kv: (-len(kv[1].get("tickets", {})), kv[0]))
    return {
        "files_with_history": len(files),
        "top_files": [
            {
                "path": path,
                "tickets": len(info.get("tickets", {})),
                "first": info.get("first", ""),
                "last": info.get("last", ""),
            }
            for path, info in ranked[:TOP_FILES]
        ],
    }


def repo_tickets(files: dict) -> set[str]:
    return {t for info in files.values() for t in info.get("tickets", {})}


def _described(tickets: set[str], descriptions: dict) -> dict[str, str]:
    return {t: " ".join(descriptions[t].get("d", [])) for t in sorted(tickets) if t in descriptions}


def instability_section(tickets: set[str], descriptions: dict) -> dict:
    texts = _described(tickets, descriptions)
    reverts = [t for t, text in texts.items() if REVERT.search(text)]
    fixes = [t for t, text in texts.items() if FIX.search(text)]
    total = max(len(texts), 1)
    sample = lambda ids: [f"{t}: {texts[t][:120]}" for t in ids[:5]]  # noqa: E731
    return {
        "tickets": len(texts),
        "revert_share": len(reverts) / total,
        "fix_share": len(fixes) / total,
        "sample_reverts": sample(reverts),
        "sample_fixes": sample(fixes),
    }


def timeline_section(tickets: set[str], descriptions: dict) -> dict:
    years = Counter(
        str(descriptions[t].get("first", ""))[:4]
        for t in tickets
        if t in descriptions and descriptions[t].get("first")
    )
    return dict(sorted(years.items()))
