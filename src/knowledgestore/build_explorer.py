"""Build graphify-out/explorer.html - a self-contained, no-install search
and question-answering page over the knowledge graph.

The audience is people with only a browser: no LLM licence, no Python,
no CLI. The page embeds a compact index built from the graph plus the
business-intent layers and offers two modes:

- Search: instant lookup of components, business features, scenarios and
  tickets, with repository, source file, community, strongest
  connections and linked tickets on each result card.
- Ask: question answering without an LLM. Scoring is a JavaScript port of
  graphify's own query ranking; an intent router composes direct answers
  for recognised question shapes (which repositories / where used /
  impact / why / journey / ticket IDs) and falls back to a seeded
  breadth-first evidence cluster otherwise.

The page application lives in the packaged explorer app.js (typed via JSDoc,
`// @ts-check`) and is inlined verbatim at build time. Regression test:
`node tests/explorer/explorer-regression.mjs` after building.

Everything is deterministic and client-side - no server, no LLM, no
external requests. Regenerate after a graph refresh:

    knowledgestore explorer
"""

from __future__ import annotations

import datetime
import gzip
import json
import re
from collections import defaultdict
from importlib import resources


from . import config
from .build_intent_index import truncate
from . import io
from . import kinds


MINIFIED = re.compile(r"^[A-Za-z_$]{1,3}(\(\))?$")
MAX_CONNECTIONS = 5
MAX_TICKETS = 6
# Explorer inclusion policy (the GRAPH stays complete - this only governs
# what the search page indexes). Business kinds are always included; code
# entries must be non-method symbols outside backend test trees with at
# least this many connections. See config.MIN_ENTRY_DEGREE / config.E2E_REPOS.
E2E_REPOS = config.E2E_REPOS


PACKAGE = "knowledgestore"


def latest_synced(recorded: dict[str, dict]) -> str:
    """Return the YYYY-MM-DD date of the chronologically latest committed entry.

    Parses ISO-8601 timestamps with timezone offsets (from git log %cI) to
    compare chronologically, not lexicographically. Skips entries that fail
    to parse. Returns empty string if no valid entries.
    """
    latest_dt = None
    latest_date = ""
    for entry in recorded.values():
        committed = entry.get("committed", "")
        if not committed:
            continue
        try:
            dt = datetime.datetime.fromisoformat(committed)
            if latest_dt is None or dt > latest_dt:
                latest_dt = dt
                latest_date = committed[:10]  # YYYY-MM-DD
        except ValueError:
            # Skip entries with invalid timestamps
            pass
    return latest_date


def app_source() -> str:
    """The explorer page application, shipped as package data."""
    return (resources.files(PACKAGE) / "assets" / "app.js").read_text(encoding="utf-8")


def load_inputs() -> tuple[dict, dict, dict, dict]:
    graph = io.load_graph(config.GRAPH_PATH)
    labels = io.load_labels(config.LABELS_PATH)
    intent = io.read_gzip_json_dict(config.INTENT_INDEX_PATH)
    titles = io.read_gzip_json_dict(config.TICKET_TITLES_PATH)
    return graph, labels, intent, titles


def merge_ticket_evidence(mined: dict, tracker: dict) -> dict:
    """Ticket evidence for the page: what the tracker says, over what commits said.

    `fetch-tickets` wrote an artefact nothing read. On one estate that meant 6,569
    real ticket titles and 5,839 descriptions sat in the repository while the page
    still showed mined commit subjects - a guess where an answer was available.

    Precedence is tracker first, because a title someone wrote about the work beats
    a subject line written while doing it. The mined evidence is kept alongside
    rather than replaced: it is what the commits actually said, it covers ids the
    tracker has never heard of, and the store's honesty rule is that a reader can
    tell which layer answered.

    Comments are excluded deliberately - see config.TICKET_DETAIL_CHARS.

    Keys are terse because this dictionary is embedded in the page: `d` mined
    evidence, `t` tracker title, `x` tracker description extract.
    """
    merged: dict[str, dict] = {ticket: dict(record) for ticket, record in mined.items()}
    for ticket, record in tracker.items():
        if not isinstance(record, dict) or record.get("absent"):
            continue
        entry = merged.setdefault(ticket, {})
        summary = (record.get("summary") or "").strip()
        if summary:
            entry["t"] = summary
        description = (record.get("description") or "").strip()
        if description:
            entry["x"] = truncate(description, config.TICKET_DETAIL_CHARS)
        # Comments carried whole, so the page can search them. They arrive already
        # bounded by KSB_TRACKER_COMMENT_CHARS, so a second cap here would be a
        # second policy to keep in step. This is the layer that answers *why* a
        # change was made, and holding it in the page without searching it would be
        # the worst of both - weight with no reach.
        comments = [c for c in (record.get("comments") or []) if isinstance(c, str) and c.strip()]
        if comments:
            entry["c"] = comments
    return merged


def node_kind(node: dict) -> str:
    """The explorer's entry kind: business kinds, else code or concept."""
    kind = kinds.node_kind(node)
    if kind in (kinds.FEATURE, kinds.SCENARIO, kinds.TICKET):
        return kind
    return "code" if node.get("file_type") == "code" else "concept"


def include_entry(node: dict, kind: str, degree: int) -> bool:
    """Explorer inclusion policy - see config.MIN_ENTRY_DEGREE comment above."""
    if not node.get("label"):
        return False  # structural nodes (e.g. Java package hierarchy) carry no label
    if (node.get("metadata") or {}).get("kind") in ("deployment", "environment"):
        # Deployment evidence is deliberately sparse in the graph: a service in an
        # environment links to that environment and to the code it deploys, and
        # nothing links back. Degree-gating it would hide the entire layer, which
        # is the same mistake the package exemption below records.
        #
        # Ahead of is_noise, not after it, because environments are named "prd",
        # "dev", "aat" - three letters, exactly what the minified-symbol filter
        # exists to drop. Every environment node on a real estate would go with it.
        return True
    if is_noise(node, kind):
        return False
    if kind in ("feature", "scenario", "ticket"):
        return True
    if node.get("type") == "package":
        # A manifest-declared package is a search target however isolated it is.
        # graphify's manifest ingest deliberately does not invent a stub node for
        # an external dependency, and prunes the dangling `depends_on` edge, so a
        # package node's degree counts only the links that stay inside the corpus
        # - routinely zero. Degree-gating them therefore hides real, named things:
        # on one estate that upstream change took `depends_on` from 8,404 edges to
        # 9 and pushed 21,133 labelled nodes below the bar. Nothing has to link to
        # a package for the package to be the answer to "what is this service?".
        return True
    label = node.get("label", "")
    if label.startswith("."):  # instance methods - not search targets
        return False
    source = node.get("source_file") or ""
    is_test = ".spec." in source or "__tests__" in source or "/test/" in source
    if is_test and node.get("repo") not in E2E_REPOS:
        return False  # backend test scaffolding
    return degree >= config.MIN_ENTRY_DEGREE


def deployment_summary(metadata: dict) -> str:
    """`key=value` pairs for a deployment node, capped and sorted.

    Sorted before capping so two builds of the same graph produce the same page.
    Empty for anything that is not a deployment: the tenth element then costs an
    ordinary entry the four bytes of `, ""`, about 3% of the data block and
    nothing at all once the page is gzipped, which is how it is committed.
    """
    if (metadata or {}).get("kind") != "deployment":
        return ""
    config_map = metadata.get("config") or {}
    pairs = [f"{key}={config_map[key]}" for key in sorted(config_map)]
    return " ".join(pairs[: config.DEPLOY_PAGE_KEYS])


def is_noise(node: dict, kind: str) -> bool:
    """Drop minified/junk symbols that clutter search results."""
    if kind in ("feature", "scenario", "ticket"):
        return False
    return bool(MINIFIED.match(node.get("label", "")))


def node_tickets(node: dict, kind: str, intent: dict) -> list[str]:
    if kind in ("feature", "scenario"):
        return list((node.get("metadata") or {}).get("tickets", []))[:MAX_TICKETS]
    if kind == "ticket":
        return [node["label"]]
    entry = intent.get(node.get("repo", ""), {}).get(node.get("source_file") or "")
    return list(entry["tickets"].keys())[:MAX_TICKETS] if entry else []


def build_index(graph: dict, labels: dict, intent: dict) -> tuple[list, list]:
    """Return (entries, edge index pairs) restricted to non-noise nodes."""
    # The join in node_tickets is keyed on `repo`, so without it every lookup
    # misses and the page ships with no ticket evidence at all - which reads as
    # an estate whose files no ticket ever touched.
    io.warn_if_no_repo_attribute(
        graph["nodes"],
        "The file-to-ticket join is keyed on it, so the page will show no ticket "
        "evidence for any file, and every repository column will be blank.",
    )
    nodes = {n["id"]: n for n in graph["nodes"]}
    adjacency: dict[str, set] = defaultdict(set)
    degree: dict[str, int] = defaultdict(int)
    for edge in graph["links"]:
        adjacency[edge["source"]].add(edge["target"])
        adjacency[edge["target"]].add(edge["source"])
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1

    kept = [
        (node_id, node, node_kind(node))
        for node_id, node in nodes.items()
        if include_entry(node, node_kind(node), degree[node_id])
    ]
    kept.sort(key=lambda item: -degree[item[0]])
    index_of = {node_id: i for i, (node_id, _, _) in enumerate(kept)}

    entries = [
        [
            node["label"],
            node.get("repo", ""),
            node.get("source_file") or "",
            labels.get(str(node.get("community")), ""),
            kind,
            degree[node_id],
            entry_connections(node_id, adjacency, degree, nodes),
            node_tickets(node, kind, intent),
            node.get("community", -1),
            deployment_summary(node.get("metadata") or {}),
        ]
        for node_id, node, kind in kept
    ]
    return entries, kept_edges(kept, adjacency, index_of)


def entry_connections(node_id: str, adjacency: dict, degree: dict, nodes: dict) -> list[str]:
    """Labels of the strongest-connected distinct neighbours."""
    connections: list[str] = []
    # name tiebreak: adjacency is a set, so without it equal-degree order
    # follows per-process hash randomisation and builds are unreproducible
    for neighbour in sorted(adjacency.get(node_id, ()), key=lambda n: (-degree[n], n)):
        label = nodes[neighbour].get("label")
        if label and label not in connections:
            connections.append(label)
        if len(connections) == MAX_CONNECTIONS:
            break
    return connections


def kept_edges(kept: list, adjacency: dict, index_of: dict) -> list[int]:
    """Flat [source, target, ...] index pairs among kept nodes only."""
    edges: list[int] = []
    for node_id, _, _ in kept:
        source = index_of[node_id]
        for neighbour in sorted(adjacency.get(node_id, ())):
            target = index_of.get(neighbour)
            if target is not None and source < target:
                edges.extend((source, target))
    return edges


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { --bg:#fff; --fg:#1a1a2e; --muted:#667; --card:#f4f5f8; --line:#dde;
        --accent:#1d70b8; --chip:#e8eef7; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#12121a; --fg:#e8e8f0; --muted:#99a; --card:#1d1d2b;
          --line:#334; --accent:#5da8e8; --chip:#243044; }
}
* { box-sizing:border-box; margin:0; }
body { background:var(--bg); color:var(--fg);
       font:15px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;
       max-width:880px; margin:0 auto; padding:24px 16px 80px; }
h1 { font-size:22px; margin-bottom:2px; }
h2 { font-size:15px; margin:18px 0 8px; color:var(--muted); font-weight:600; }
.sub { color:var(--muted); font-size:13px; margin-bottom:18px; }
.tabs { display:flex; gap:6px; margin-bottom:10px; }
.tabs button { padding:6px 16px; font-size:14px; border:1px solid var(--line);
       border-radius:8px 8px 0 0; background:var(--card); color:var(--muted); cursor:pointer; }
.tabs button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
#q { width:100%; padding:12px 14px; font-size:17px; border:2px solid var(--line);
     border-radius:8px; background:var(--card); color:var(--fg); outline:none; }
#q:focus { border-color:var(--accent); }
.filters { display:flex; gap:8px; flex-wrap:wrap; margin:12px 0 4px; }
.filters select, .filters label { font-size:13px; color:var(--muted); }
.filters select { background:var(--card); color:var(--fg); border:1px solid var(--line);
                  border-radius:6px; padding:4px 8px; }
.filters label { display:flex; align-items:center; gap:4px; cursor:pointer; }
#meta { color:var(--muted); font-size:13px; margin:10px 2px; }
.answer { font-size:17px; line-height:1.45; margin:16px 0 6px; }
.answer b { color:var(--accent); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:12px 14px; margin-bottom:10px; }
.card h3 { font-size:16px; display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }
.kind { font-size:11px; padding:1px 8px; border-radius:10px; background:var(--chip);
        color:var(--accent); text-transform:uppercase; letter-spacing:.4px; }
.path { color:var(--muted); font-size:12.5px; font-family:ui-monospace,Menlo,monospace;
        word-break:break-all; margin:3px 0; }
.row { font-size:13px; margin-top:4px; }
.row b { color:var(--muted); font-weight:600; }
.tickets span, .chips span { display:inline-block; background:var(--chip); border-radius:6px;
                padding:0 7px; margin:2px 4px 0 0; font-size:12px;
                font-family:ui-monospace,Menlo,monospace; }
.tickets a, .chips a, .trow a, .answer a { color:var(--accent); text-decoration:none; }
.tickets a:hover, .chips a:hover, .trow a:hover, .answer a:hover { text-decoration:underline; }
.group { border-left:3px solid var(--accent); padding-left:10px; margin:10px 0; }
.group .g-title { font-weight:600; font-size:14px; }
.group .g-items { color:var(--muted); font-size:13px; }
.summary { font-size:14px; line-height:1.55; margin:8px 0; padding:10px 12px;
           background:var(--card); border:1px solid var(--line); border-radius:8px; }
.trow { font-size:13.5px; margin:6px 0; padding-left:10px; border-left:3px solid var(--chip); }
.trow .tid { font-family:ui-monospace,Menlo,monospace; font-size:12px; color:var(--accent); }
.trow .tdates { color:var(--muted); font-size:12px; }
.ev { font-size:13px; margin:4px 0 0 10px; }
.ev .ev-l { color:var(--muted); font-family:ui-monospace,Menlo,monospace; font-size:11.5px; }
.cbody { font-size:13px; line-height:1.5; margin:6px 0 6px 10px; }
.summary b { color:var(--accent); }
table.rt { border-collapse:collapse; width:100%; font-size:13.5px; margin:6px 0 14px; }
table.rt td, table.rt th { text-align:left; padding:5px 10px 5px 0; vertical-align:top;
        border-bottom:1px solid var(--line); }
table.rt th { color:var(--muted); font-weight:600; font-size:12.5px; }
table.rt td.mono { font-family:ui-monospace,Menlo,monospace; font-size:12px;
        word-break:break-all; color:var(--muted); }
.hint { color:var(--muted); font-size:13px; margin-top:30px; }
.brief { background:var(--card); border:1px solid var(--line); border-left:4px solid var(--accent);
         border-radius:8px; padding:6px 16px 12px; margin:12px 0 18px;
         font-size:14px; line-height:1.55; }
.brief h2 { font-size:17px; margin:12px 0 6px; }
.brief h3 { font-size:14.5px; margin:12px 0 4px; }
.brief h4 { font-size:13.5px; margin:10px 0 4px; }
.brief p { margin:6px 0; }
.brief ul { margin:6px 0; padding-left:20px; }
.brief b { color:var(--accent); }
.brief .b-src { color:var(--muted); font-size:12px; margin-top:10px; }
.req-brief { color:var(--muted); font-size:13px; margin:14px 0; }
.req-brief a { color:var(--accent); }
details.tech { margin:16px 0; border-top:1px solid var(--line); padding-top:6px; }
details.tech summary { cursor:pointer; color:var(--muted); font-size:13.5px;
        font-weight:600; padding:4px 0; }
details.tech summary:hover { color:var(--accent); }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="sub">__SUB__ &middot; evidence from the committed knowledge graph &mdash; answers are
computed deterministically in your browser (no AI at question time); pre-written topic briefs
and community summaries were composed with an LLM at build time from graph evidence, then reviewed.
<b>This page answers basic questions.</b> The full power of the knowledge base is querying it
through an agentic LLM harness, which traverses the graph and writes cited narrative answers to
questions nobody anticipated &mdash; see the README.</div>
<div class="tabs">
  <button id="tab-search" class="on">Search</button>
  <button id="tab-ask">Ask a question</button>
</div>
<input id="q" type="search" placeholder="search components, capabilities, tickets&hellip;" autofocus>
<div class="filters" id="search-filters">
  <select id="repo"><option value="">all repositories</option></select>
  <label><input type="checkbox" class="k" value="code" checked> code</label>
  <label><input type="checkbox" class="k" value="concept" checked> concepts</label>
  <label><input type="checkbox" class="k" value="feature" checked> business features</label>
  <label><input type="checkbox" class="k" value="scenario" checked> scenarios</label>
  <label><input type="checkbox" class="k" value="ticket" checked> tickets</label>
</div>
<div id="meta"></div>
<div id="out"></div>
<div class="hint">Narrative write-ups live in <code>docs/</code>. For open-ended questions
this page can't answer, query the knowledge base with an agentic harness (clone the repository
and ask your coding agent) or run <code>graphify query</code> from the terminal &mdash; see the
README.</div>
<script id="data" type="application/json">__DATA__</script>
<script id="edges" type="application/json">__EDGES__</script>
<script id="titles" type="application/json">__TITLES__</script>
<script id="summaries" type="application/json">__SUMMARIES__</script>
<script id="synonyms" type="application/json">__SYNONYMS__</script>
<script id="tickets" type="application/json">__TICKETINFO__</script>
<script id="config" type="application/json">__CONFIG__</script>
<script id="topics" type="application/json">__TOPICS__</script>
<script id="dives" type="application/json">__DIVES__</script>
<script>
__APP_JS__
</script>
</body>
</html>
"""


def main() -> int:
    if not config.GRAPH_PATH.exists():
        print(f"Graph not found: {config.GRAPH_PATH} (gunzip -k graph.json.gz first)")
        return 1

    graph, labels, intent, titles = load_inputs()
    entries, edges = build_index(graph, labels, intent)
    summaries = (
        json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))
        if config.SUMMARIES_PATH.exists()
        else {}
    )
    synonyms = (
        json.load(gzip.open(config.SYNONYMS_PATH, "rt", encoding="utf-8"))
        if config.SYNONYMS_PATH.exists()
        else {}
    )
    mined_tickets = (
        json.load(gzip.open(config.TICKET_DESCRIPTIONS_PATH, "rt", encoding="utf-8"))
        if config.TICKET_DESCRIPTIONS_PATH.exists()
        else {}
    )
    tracker_tickets = (
        json.load(gzip.open(config.TICKET_TRACKER_PATH, "rt", encoding="utf-8"))
        if config.TICKET_TRACKER_PATH.exists()
        else {}
    )
    ticket_info = merge_ticket_evidence(mined_tickets, tracker_tickets)
    # Topic briefs (GraphRAG phase 3): pre-written narratives composed at
    # build time from knowledge/topics evidence; served without any LLM.
    topics = io.read_json_dict(config.TOPICS_BRIEFS_PATH)
    # Deep dives (Task 7): pre-written, provenance-stamped dossiers on
    # individual repositories, served without any LLM.
    divesdata = io.read_json_dict(config.DEEPDIVES_PATH)

    # Page configuration read by app.js at startup. Set these in config
    # (KSB_TICKET_BROWSE_URL, KSB_BRIEF_REQUEST_URL) per estate.
    page_config = {
        "jiraBrowseUrl": config.TICKET_BROWSE_URL,
        # Where "request a topic brief" links point (pre-filled issue).
        # Empty string hides the link.
        "briefRequestUrl": config.BRIEF_REQUEST_URL,
    }

    app_js = app_source()
    if "</script" in app_js.lower():
        raise ValueError("app.js must not contain a literal </script> sequence")

    sub = (
        f"{len(graph['nodes']):,} graph nodes &middot; {len(entries):,} indexed "
        f"here (business features, tickets and connected code) &middot; the "
        f"full graph is queryable via the graphify CLI"
    )
    # the {"repositories": {...}} shape is owned by provenance.py
    recorded = io.read_json_dict(config.PROVENANCE_PATH).get("repositories", {})
    synced = latest_synced(recorded)
    if synced:
        sub += f" &middot; sources synced to {synced}"
    html = (
        TEMPLATE.replace("__TITLE__", config.EXPLORER_TITLE)
        .replace("__SUB__", sub)
        .replace("__DATA__", json.dumps(entries, ensure_ascii=False).replace("</", "<\\/"))
        .replace("__EDGES__", json.dumps(edges))
        .replace("__TITLES__", json.dumps(titles, ensure_ascii=False).replace("</", "<\\/"))
        .replace("__SUMMARIES__", json.dumps(summaries, ensure_ascii=False).replace("</", "<\\/"))
        .replace("__SYNONYMS__", json.dumps(synonyms, ensure_ascii=False).replace("</", "<\\/"))
        .replace(
            "__TICKETINFO__", json.dumps(ticket_info, ensure_ascii=False).replace("</", "<\\/")
        )
        .replace("__CONFIG__", json.dumps(page_config, ensure_ascii=False))
        .replace("__TOPICS__", json.dumps(topics, ensure_ascii=False).replace("</", "<\\/"))
        .replace("__DIVES__", json.dumps(divesdata, ensure_ascii=False).replace("</", "<\\/"))
        .replace("__APP_JS__", app_js)
    )
    # Sonar S2083 misfires here: config.EXPLORER_PATH is a module constant derived from
    # configuration, not untrusted input; this is offline build tooling.
    config.EXPLORER_PATH.write_text(html, encoding="utf-8")  # NOSONAR(S2083)
    # Bytes as well as megabytes: a change to the page's own code or to what it
    # embeds moves the size by kilobytes, which one decimal place of a megabyte
    # cannot show, and the growth is what a store's clone cost is measured in.
    size_bytes = config.EXPLORER_PATH.stat().st_size
    print(
        f"{len(entries):,} entries, {len(edges) // 2:,} edges -> {config.EXPLORER_PATH} "
        f"({size_bytes / 1_048_576:.1f} MB, {size_bytes:,} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
