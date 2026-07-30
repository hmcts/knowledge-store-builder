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

import sys
from collections import Counter, defaultdict
from itertools import combinations

from . import config, io, kinds, provenance
from .build_topic_briefs import markdown_to_html

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


def _is_test_pair(a: str, b: str) -> bool:
    sa = a.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    sb = b.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return sa + "Test" == sb or sb + "Test" == sa


def cochange_section(files: dict) -> list[dict]:
    by_ticket: dict[str, list[str]] = defaultdict(list)
    for path, info in files.items():
        for t in info.get("tickets", {}):
            by_ticket[t].append(path)
    pairs: Counter = Counter()
    for paths in by_ticket.values():
        if 2 <= len(paths) <= COCHANGE_MAX_FILES:
            for a, b in combinations(sorted(paths), 2):
                pairs[(a, b)] += 1
    kept = [
        {"a": a, "b": b, "n": n}
        for (a, b), n in pairs.items()
        if n >= MIN_COCHANGE and not _is_test_pair(a, b)
    ]
    return sorted(kept, key=lambda p: (-p["n"], p["a"], p["b"]))[:25]


def hotspot_section(files: dict, graph: dict, repo: str) -> list[dict]:
    degree: Counter = Counter()
    ids = {}
    for n in graph["nodes"]:
        if n.get("repo") == repo and n.get("source_file"):
            ids[n["id"]] = n["source_file"]
    for e in graph["links"]:
        for end in (e["source"], e["target"]):
            if end in ids:
                degree[ids[end]] += 1
    if not degree:
        return []
    quartile = sorted(degree.values())[int(len(degree) * 0.75)]
    churn_top = {f["path"]: f["tickets"] for f in churn_section(files)["top_files"]}
    hot = [
        {"path": p, "tickets": t, "degree": degree[p]}
        for p, t in churn_top.items()
        if degree.get(p, 0) >= quartile
    ]
    return sorted(hot, key=lambda h: (-h["tickets"], -h["degree"], h["path"]))


def coupling_surface(graph: dict, repo: str) -> list[dict]:
    mine = {
        n["label"]
        for n in graph["nodes"]
        if n.get("repo") == repo and str(n.get("label", "")).endswith(".json")
    }
    elsewhere: dict[str, set] = defaultdict(set)
    for n in graph["nodes"]:
        if n.get("label") in mine and n.get("repo") not in (repo, "", None):
            elsewhere[n["label"]].add(n["repo"])
    surface = [{"label": label, "other_repos": sorted(repos)} for label, repos in elsewhere.items()]
    return sorted(surface, key=lambda s: (-len(s["other_repos"]), s["label"]))[:20]


def feature_section(graph: dict, tickets: set[str]) -> list[dict]:
    linked = []
    for n in graph["nodes"]:
        if not kinds.is_kind(n, kinds.FEATURE):
            continue
        shared = sorted(set((n.get("metadata") or {}).get("tickets", [])) & tickets)
        if shared:
            linked.append({"label": n.get("label", ""), "tickets": shared})
    return sorted(linked, key=lambda f: (-len(f["tickets"]), f["label"]))[:15]


def summary_coverage(graph: dict, repo: str, summaries: dict) -> dict:
    comms = {
        n.get("community")
        for n in graph["nodes"]
        if n.get("repo") == repo and n.get("community") is not None
    }
    covered = sum(1 for c in comms if str(c) in summaries)
    return {"with": covered, "without": len(comms) - covered}


def extract(repo: str) -> int:
    graph = io.load_graph(GRAPH_PATH)  # NOTE: the full graph
    if not any(n.get("repo") == repo for n in graph["nodes"]):
        print(
            f"No nodes for repository '{repo}' - is it in the estate, "
            f"and is the graph decompressed?",
            file=sys.stderr,
        )
        return 1
    labels = io.read_json_dict(LABELS_PATH)
    summaries = io.read_json_dict(SUMMARIES_PATH)
    intent = io.read_gzip_json_dict(INTENT_PATH)
    descriptions = io.read_gzip_json_dict(DESCRIPTIONS_PATH)
    files = intent.get(repo, {})
    tickets = repo_tickets(files)
    bundle = {
        "repo": repo,
        "provenance": provenance.read().get(repo),
        "scale": scale_section(graph, repo, labels, summaries),
        "churn": churn_section(files),
        "instability": instability_section(tickets, descriptions),
        "timeline": timeline_section(tickets, descriptions),
        "cochange": cochange_section(files),
        "hotspots": hotspot_section(files, graph, repo),
        "coupling_surface": coupling_surface(graph, repo),
        "features": feature_section(graph, tickets),
        "summary_coverage": summary_coverage(graph, repo, summaries),
    }
    io.write_json(INPUT_DIR / f"{repo}-input.json", bundle, indent=1)
    print(f"{repo}: bundle -> {INPUT_DIR / (repo + '-input.json')}")
    return 0


def merge() -> int:
    dives_out: dict[str, dict] = {}
    problems: list[str] = []
    bundles = sorted(INPUT_DIR.glob("*-input.json"))
    if not bundles:
        print(
            f"No bundles in {INPUT_DIR} - run `knowledgestore deepdive extract <repo>` first",
            file=sys.stderr,
        )
        return 1
    for bundle_path in bundles:
        bundle = io.read_json_dict(bundle_path)
        repo = str(bundle.get("repo", ""))
        doc = DOCS_DIR / f"{repo}.md"
        sha = str((bundle.get("provenance") or {}).get("sha", ""))[:8]
        if not doc.exists():
            problems.append(f"{repo}: missing {doc}")
            continue
        markdown = doc.read_text(encoding="utf-8")
        if len(markdown) < MIN_DIVE_LENGTH:
            problems.append(f"{repo}: dossier shorter than {MIN_DIVE_LENGTH}")
            continue
        if sha and sha not in markdown:
            problems.append(
                f"{repo}: dossier does not state the build it measured "
                f"(expected the short sha `{sha}`)"
            )
            continue
        dives_out[repo] = {
            "title": f"Deep dive: {repo}",
            "html": markdown_to_html(markdown),
            "source": f"docs/deep-dives/{repo}.md",
            "sha": sha,
        }
    io.write_json(DIVES_PATH, dict(sorted(dives_out.items())), indent=1)
    for problem in problems:
        print(f"skipped - {problem}")
    print(f"{len(dives_out)} deep dives -> {DIVES_PATH}")
    return 1 if problems else 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "extract":
        return extract(sys.argv[2])
    if len(sys.argv) >= 2 and sys.argv[1] == "merge":
        return merge()  # Task 7
    print(__doc__)
    return 1
