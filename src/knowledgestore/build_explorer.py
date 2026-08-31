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
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping
from importlib import resources
from pathlib import Path


from . import config
from .build_intent_index import truncate
from . import graph_files
from . import io
from . import kinds
from . import telemetry


MINIFIED = re.compile(r"^[A-Za-z_$]{1,3}(\(\))?$")
MAX_CONNECTIONS = 5
MAX_TICKETS = 6
# A `__NAME__` slot in the page template. The byte breakdown is keyed on these
# rather than on a hand-kept list, so a slot added to the template without a block
# to fill it is a refusal instead of a silently unattributed layer.
PLACEHOLDER = re.compile(r"__[A-Z][A-Z_]*__")
# How many blocks the pre-write warning names. Three points at a layer and fits on
# a line; the whole breakdown is printed either way.
WARN_TOP_BLOCKS = 3
# A page row is a positional list; this is its ticket column. Named because
# three places count the join through it, and a report that counted a different
# column would be correct code answering a neighbouring question - the shape of
# every wrong measurement this pipeline has shipped.
TICKETS_COLUMN = 7
# Explorer inclusion policy (the GRAPH stays complete - this only governs
# what the search page indexes). Business kinds are always included; code
# entries must be non-method symbols outside backend test trees with at
# least this many connections. See config.MIN_ENTRY_DEGREE / config.E2E_REPOS.
E2E_REPOS = config.E2E_REPOS


PACKAGE = "knowledgestore"


def status_layers() -> tuple[str, ...]:
    """The committed layers this page embeds, as store-relative paths.

    Shared with `status` so the page records digests for exactly what `status`
    later re-hashes; two lists that could drift would make a mismatch mean
    nothing.
    """
    from .status import EMBEDDED_LAYERS

    return EMBEDDED_LAYERS


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
    print(
        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "explorer.html"),
        end="",
        file=sys.stderr,
    )
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


def join_by_layer(kept: list, entries: list, intent: dict) -> dict:
    """Per-layer `(joined, candidates)`, counted only in indexed repositories.

    The restriction is the same both-sides-populated condition the whole-graph
    check has, and without it this cries wolf: a layer whose repository is not
    mined at all joins zero legitimately - 2,115 `meta-arch` nodes on one estate -
    and that is sparsity rather than a key mismatch.
    """
    counts: dict[str, list[int]] = {}
    for (_, node, kind), entry in zip(kept, entries, strict=True):
        if kind in (kinds.FEATURE, kinds.SCENARIO, kinds.TICKET):
            continue
        if not node.get("source_file") or (node.get("repo") or "") not in intent:
            continue
        layer = counts.setdefault(node.get("_origin") or "semantic", [0, 0])
        layer[1] += 1
        if entry[TICKETS_COLUMN]:
            layer[0] += 1
    return {name: (joined, total) for name, (joined, total) in counts.items()}


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
    # id tiebreak: the entry order is the page's own tiebreak - app.js documents
    # DATA as degree-sorted and every client-side ranking of equally-weighted
    # entries falls back to it. Without the id half, equal degrees come out in
    # whatever order the graph file listed its nodes in, which makes the page a
    # property of a peer tool's serialisation rather than of the graph's content.
    # The id, not the label: ids are unique because they key `nodes`, labels are
    # not, so only the id makes this order total.
    kept.sort(key=lambda item: (-degree[item[0]], item[0]))
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
    # Cardinality, not existence. Shape, schema and freshness all pass on a join
    # that matches nothing - only counting the matches says otherwise, and on one
    # store this produced zero across 70,655 nodes with the build still green.
    io.report_join_cardinality(
        joined=sum(1 for entry in entries if entry[TICKETS_COLUMN]),
        candidates=sum(
            1 for _, node, kind in kept if kind not in (kinds.FEATURE, kinds.SCENARIO, kinds.TICKET)
        ),
        index_size=len(intent),
        by_layer=join_by_layer(kept, entries, intent),
    )
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


def block_name(placeholder: str) -> str:
    """`__TICKETINFO__` -> `ticketinfo`: the breakdown's key for a block.

    Derived from the placeholder rather than kept in a second list, which could
    disagree with the template and would then name one block while measuring
    another.
    """
    return placeholder.strip("_").lower()


def page_breakdown(template: str, blocks: Mapping[str, str]) -> dict[str, int]:
    """The bytes each substituted block contributes, plus the template's own.

    Bytes rather than characters, because the page is written as UTF-8 and a
    prose block holding one pound sign costs a byte more than its length.
    Multiplied by occurrences, because `str.replace` fills every slot and
    `__TITLE__` has two of them - a breakdown counting it once is short by the
    title on every build.

    The template's own line is what is left of it after the placeholder tokens
    it no longer contains: the file on disk holds neither `__DATA__` nor the
    template's copy of it, so charging the page for both would overstate the
    total by the length of every slot.

    Raises when the template and the blocks disagree in either direction. A slot
    with no block is a placeholder left in the shipped page and a layer missing
    from the breakdown; a block with no slot is a layer that no longer reaches
    the page at all. Both are silent, and both stop the total meaning anything -
    a breakdown that does not reconcile is worse than none.
    """
    found = Counter(PLACEHOLDER.findall(template))
    unfilled = sorted(set(found) - set(blocks))
    unplaced = sorted(set(blocks) - set(found))
    if unfilled or unplaced:
        raise ValueError(
            "the explorer template and the blocks it is built from disagree: "
            f"{unfilled} in the template with no block to fill them, {unplaced} with no "
            "slot in the template. Every block is measured into the page's byte "
            "breakdown, so an unattributed one would leave the total unable to "
            "reconcile with the file - name the new block in `blocks`, or take its "
            "slot out of the template."
        )
    sizes = {
        block_name(placeholder): occurrences * len(blocks[placeholder].encode("utf-8"))
        for placeholder, occurrences in found.items()
    }
    sizes["template"] = len(template.encode("utf-8")) - sum(
        occurrences * len(placeholder.encode("utf-8")) for placeholder, occurrences in found.items()
    )
    return sizes


def reconcile_breakdown(breakdown: Mapping[str, int], size_bytes: int) -> dict[str, int]:
    """The breakdown with its residual against the file's actual size named.

    The attribution is derived from the template and the blocks; the size is read
    from the file system after the write. Two independent measurements of one
    quantity, so their difference is a real number rather than a formality, and a
    breakdown that reconciles by construction would prove nothing at all.

    Normally zero. `write_text` translates line endings where the platform's
    separator is not "\\n", which would give every page a residual, and a line
    saying so is the honest form of that - the alternative is a difference
    redistributed across the blocks, where nobody can see it.
    """
    return {**breakdown, "unattributed": size_bytes - sum(breakdown.values())}


def ranked(breakdown: Mapping[str, int]) -> list[tuple[str, int]]:
    """Blocks largest first, ties broken by name.

    Two blocks of equal size must not swap places between builds: the report is
    printed for a reader who compares it with the last one, and a listing that
    reorders itself is one nobody can diff.
    """
    return sorted(breakdown.items(), key=lambda item: (-item[1], item[0]))


def breakdown_report(breakdown: Mapping[str, int], size_bytes: int, path: Path) -> str:
    """The attribution as an operator meets it, against the file's own size.

    Printed rather than left in telemetry, because a store facing a page too large
    to push needs to know which layer paid for it at the moment it reads the size,
    not after finding and parsing a committed JSON file.
    """
    lines = [f"Page bytes by block, against {path} ({size_bytes:,} bytes on disk):"]
    lines += [f"  {name:<14}{size:>14,}" for name, size in ranked(breakdown)]
    return "\n".join(lines) + "\n"


def largest_blocks(breakdown: Mapping[str, int]) -> str:
    """The biggest blocks named with their bytes, for a line about the page's size.

    One expression, read by both the warning and the refusal. A second expression for
    the same quantity is how two messages describing one page start disagreeing, and
    they are read side by side: a store meets the warning on one build and the
    refusal on the next.
    """
    return ", ".join(f"{name} {size:,} bytes" for name, size in ranked(breakdown)[:WARN_TOP_BLOCKS])


def size_warning(breakdown: Mapping[str, int], threshold: int) -> str:
    """What to say before writing a page this large, or "" below the threshold.

    Before the write, because the size's only use is deciding differently: after
    the write it describes a file already in the working tree, and after the commit
    it describes a push that will be refused.

    Names the largest blocks, since the size alone is not actionable - a store told
    only how large its page is has the problem it had before. It names no remedy
    this library does not provide: two settings bound part of the page, the prose
    layers have none, and what a store carries in those is the store's decision
    rather than something this stage can trim.
    """
    total = sum(breakdown.values())
    if total <= threshold:
        return ""
    largest = largest_blocks(breakdown)
    return (
        f"WARNING: the explorer page will be {total / 1_048_576:.1f} MB ({total:,} bytes), "
        f"over the {threshold:,}-byte KSB_EXPLORER_WARN_BYTES threshold. GitHub warns above "
        "50 MB per file and refuses a push carrying one above 100 MB, and this page is "
        "committed, so the size is worth having before the file exists rather than after a "
        "push is refused.\n"
        f"Largest blocks: {largest}.\n"
        "Nothing here drops a block: what the page carries is a store's decision. Two "
        "settings bound part of it - KSB_MIN_ENTRY_DEGREE gates which code entries reach "
        "`data` and `edges`, and KSB_TICKET_DETAIL_CHARS caps how much of each tracker "
        "description reaches `ticketinfo`. The prose blocks have no such setting: "
        "`summaries`, `topics` and `dives` are committed layers, so carrying less of them "
        "in the page means holding less of them in the store.\n"
    )


def size_refusal(breakdown: Mapping[str, int], limit: int) -> str:
    """Why this page must not be written at all, or "" at or below the limit.

    GitHub blocks a push carrying a file above 100 MB and the page is committed, so a
    page over that ceiling is not a large artefact but an unshippable one. Warning and
    writing it anyway - which is what this stage did - leaves the store to discover it
    at `git push`, a commit after the run that caused it, with a message naming neither
    the stage, the page's layers, nor anything to change.

    Refused *before* the write rather than failing after it. A page in the working tree
    is one the ordinary workflow commits beside the layers it was built from, `status`
    reads it as the current page, and truncating a pushable page to write an unpushable
    one leaves the next run a worse tree than this one found. Refusing first leaves the
    store exactly where it was, which is a state it can still ship from.

    Measured from the attribution rather than from the file, because the point is that
    there is no file yet; `reconcile_breakdown` is what checks the two agree, on the
    builds that get that far.

    Names the largest blocks for the same reason the warning does - a store told only
    that its page is too large has the problem it had before - and names the setting,
    because a store not pushing this page to GitHub must be able to say so.
    """
    total = sum(breakdown.values())
    if total <= limit:
        return ""
    return (
        f"Refusing to write the explorer page: it would be {total / 1_048_576:.1f} MB "
        f"({total:,} bytes), over the {limit:,}-byte KSB_EXPLORER_MAX_BYTES limit. GitHub "
        "refuses a push carrying a file above 100 MB and this page is committed, so writing "
        "it would hand the store an artefact it cannot ship and a rejection at `git push` "
        "naming none of this. Nothing was written: the page the store already holds, if "
        "any, is untouched.\n"
        f"Largest blocks: {largest_blocks(breakdown)}.\n"
        "Carry less in the page, or raise KSB_EXPLORER_MAX_BYTES where the page is not "
        "pushed to GitHub. Two settings bound part of it - KSB_MIN_ENTRY_DEGREE gates which "
        "code entries reach `data` and `edges`, and KSB_TICKET_DETAIL_CHARS caps how much of "
        "each tracker description reaches `ticketinfo`. The prose blocks have no such "
        "setting: `summaries`, `topics` and `dives` are committed layers, so carrying less "
        "of them in the page means holding less of them in the store.\n"
    )


def page_measurements(
    graph: dict, entries: list, edges: list, size_bytes: int, breakdown: Mapping[str, int]
) -> dict[str, int]:
    """What this page is, as counts a later build can compare itself against.

    `rows_with_tickets` and `rows_indexed` are the file-to-ticket join at the
    surface a reader actually meets, and they are recorded as two counts rather
    than as the rate between them so that the rate can be recomputed for the
    previous build too. `report_join_cardinality` already refuses a join that
    matched nothing; a join that has quietly halved is not zero, is not a
    threshold question either, and is exactly what a predecessor makes visible.

    Counted through `TICKETS_COLUMN` - the same column the join report counts -
    because a second expression for the same quantity is how two numbers
    describing one thing start disagreeing.

    `page_bytes` keeps its meaning - the size of the file on disk - and the
    per-block metrics account for it. One number can say the page grew and not
    which layer did it, which leaves a store with the growth and no lever but
    guessing; these are what let the next build name the block that moved.
    """
    return {
        "explorer.graph_nodes": len(graph.get("nodes", [])),
        "explorer.rows_indexed": len(entries),
        "explorer.rows_with_tickets": sum(1 for entry in entries if entry[TICKETS_COLUMN]),
        # Halved, because `edges` is the flattened pair list the page embeds and
        # the printed line above reports edges, not endpoints.
        "explorer.edges": len(edges) // 2,
        "explorer.page_bytes": size_bytes,
        **{f"explorer.bytes_{name}": size for name, size in sorted(breakdown.items())},
    }


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
    # One mapping rather than a chain of `.replace` calls, so each block's bytes can
    # be counted before any of them is substituted. Insertion order is the order the
    # chain used, and the substitution below keeps it: a block whose own text
    # contained another block's placeholder would otherwise be filled differently.
    blocks = {
        "__TITLE__": config.EXPLORER_TITLE,
        "__SUB__": sub,
        "__DATA__": json.dumps(entries, ensure_ascii=False).replace("</", "<\\/"),
        "__EDGES__": json.dumps(edges),
        "__TITLES__": json.dumps(titles, ensure_ascii=False).replace("</", "<\\/"),
        "__SUMMARIES__": json.dumps(summaries, ensure_ascii=False).replace("</", "<\\/"),
        "__SYNONYMS__": json.dumps(synonyms, ensure_ascii=False).replace("</", "<\\/"),
        "__TICKETINFO__": json.dumps(ticket_info, ensure_ascii=False).replace("</", "<\\/"),
        "__CONFIG__": json.dumps(page_config, ensure_ascii=False),
        "__TOPICS__": json.dumps(topics, ensure_ascii=False).replace("</", "<\\/"),
        "__DIVES__": json.dumps(divesdata, ensure_ascii=False).replace("</", "<\\/"),
        "__APP_JS__": app_js,
    }
    breakdown = page_breakdown(TEMPLATE, blocks)
    # Before the warning as well as before the write: a page over the hard limit is
    # over the warning threshold too, and two messages about one page invite reading
    # the softer one. The refusal supersedes it.
    refusal = size_refusal(breakdown, config.EXPLORER_MAX_BYTES)
    if refusal:
        print(refusal, end="", file=sys.stderr)
        return 1
    warning = size_warning(breakdown, config.EXPLORER_WARN_BYTES)
    if warning:
        print(warning, end="", file=sys.stderr)
    html = TEMPLATE
    for placeholder, block in blocks.items():
        html = html.replace(placeholder, block)
    # Sonar S2083 misfires here: config.EXPLORER_PATH is a module constant derived from
    # configuration, not untrusted input; this is offline build tooling.
    config.EXPLORER_PATH.write_text(html, encoding="utf-8")  # NOSONAR(S2083)
    # Record what this page was built from, by content. Commit dates cannot
    # answer that question - the ordinary workflow commits a regenerated layer
    # and the page together, so their dates match whether or not the page was
    # rebuilt - and `status` compares these digests instead.
    # A list of records, not an object keyed by path. Keyed by path put
    # `knowledge/semantic/token-neighbours.json.gz` immediately beside a hex
    # digest, and a key containing "token" adjacent to a hex string reads as a
    # hard-coded secret: SonarCloud raises `json:S6418` as a BLOCKER, so one
    # store could not commit this artefact at all. The information is identical;
    # only the adjacency changes. Sorted, so a committed file diffs readably.
    digests = io.layer_digests([config.ROOT / layer for layer in status_layers()], config.ROOT)
    io.write_json(
        config.EXPLORER_INPUTS_PATH,
        [{"path": path, "hash": digest} for path, digest in sorted(digests.items())],
    )
    # Bytes as well as megabytes: a change to the page's own code or to what it
    # embeds moves the size by kilobytes, which one decimal place of a megabyte
    # cannot show, and the growth is what a store's clone cost is measured in.
    size_bytes = config.EXPLORER_PATH.stat().st_size
    breakdown = reconcile_breakdown(breakdown, size_bytes)
    print(
        f"{len(entries):,} entries, {len(edges) // 2:,} edges -> {config.EXPLORER_PATH} "
        f"({size_bytes / 1_048_576:.1f} MB, {size_bytes:,} bytes)"
    )
    print(breakdown_report(breakdown, size_bytes, config.EXPLORER_PATH), end="")
    telemetry.record(page_measurements(graph, entries, edges, size_bytes, breakdown))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
