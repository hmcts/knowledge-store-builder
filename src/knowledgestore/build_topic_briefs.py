"""Topic briefs - GraphRAG phase 3, computed at build time.

A topic brief is a pre-written, committed narrative answering an
estate-level question ("How does Welsh language work?") in the format a
human analyst would produce: headline verdict, mechanisms, evidence,
limits. Composition needs an LLM, so - as with community summaries - the
LLM runs at BUILD time in Claude Code and only its reviewed output ships.

Deterministic halves provided here (the generation step sits between):

  1. knowledgestore topics extract
       -> knowledge/topics/topics-input.json
       One evidence dossier per topic in config/topics.txt: matching graph
       nodes by repository, community summaries, business features, and
       Jira tickets with their commit-mined descriptions.

  2. In Claude Code: write one markdown brief per dossier to
     docs/topics/<slug>.md (headline verdict first; mechanisms with file
     citations; a "What this is NOT" section; only dossier-evidenced
     claims).

  3. knowledgestore topics merge
       -> knowledge/topics/briefs.json  (committed)
       Validates the docs/topics/ briefs against the configured topics,
       renders the constrained-markdown subset to HTML, and emits the
       embeddable briefs file consumed by build_explorer.py.

Topic matching keywords live with the topic in config/topics.txt so the
whole surface is reviewable configuration - no code changes to add topics.
"""

from __future__ import annotations

import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config
from . import io
from . import kinds


MAX_NODES_PER_REPO = 12
MAX_SUMMARIES = 20
MAX_FEATURES = 25
MAX_TICKETS = 40
MIN_BRIEF_LENGTH = 800


@dataclass
class Topic:
    slug: str
    title: str
    keywords: list[str]


def read_topics(path: Path) -> list[Topic]:
    topics: list[Topic] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 3 or not all(parts):
            raise ValueError(f"Invalid topic at {path}:{line_number}: {raw}")
        keywords = [k.strip().lower() for k in parts[2].split(",") if k.strip()]
        topics.append(Topic(slug=parts[0], title=parts[1], keywords=keywords))
    if not topics:
        raise ValueError(f"No topics configured in {path}")
    return topics


def node_matches(node: dict, keywords: list[str]) -> bool:
    haystack = (node.get("label", "") + " " + (node.get("source_file") or "")).lower()
    return any(k in haystack for k in keywords)


def linked_tickets(graph: dict, matched_ids: set) -> set[str]:
    """Jira tickets reachable in one hop from any matched node. Ticket labels
    ("DD-100") never contain topic keywords - relevance comes from edges."""
    ticket_label = {
        node["id"]: node["label"] for node in graph["nodes"] if kinds.is_kind(node, kinds.TICKET)
    }
    tickets: set[str] = set()
    for link in graph.get("links", graph.get("edges", [])):
        source, target = link.get("source"), link.get("target")
        if source in matched_ids and target in ticket_label:
            tickets.add(ticket_label[target])
        elif target in matched_ids and source in ticket_label:
            tickets.add(ticket_label[source])
    return tickets


def topic_dossier(topic: Topic, graph: dict, summaries: dict, descriptions: dict) -> dict:
    """Deterministic evidence pack a brief is written from."""
    by_repo: dict[str, list] = {}
    features: list[str] = []
    matched_ids: set = set()
    for node in graph["nodes"]:
        if not node_matches(node, topic.keywords):
            continue
        matched_ids.add(node.get("id"))
        kind = kinds.node_kind(node)
        if kind == kinds.FEATURE:
            features.append(node["label"])
        elif kind != kinds.TICKET:
            entries = by_repo.setdefault(node.get("repo", ""), [])
            if len(entries) < MAX_NODES_PER_REPO:
                entries.append(f"{node['label']} ({node.get('source_file') or '?'})")
    tickets = linked_tickets(graph, matched_ids)

    matched_summaries = [
        {"community": cid, "summary": text}
        for cid, text in summaries.items()
        if any(k in text.lower() for k in topic.keywords)
    ][:MAX_SUMMARIES]

    described = []
    for ticket, info in descriptions.items():
        haystack = " ".join(info.get("d", [])).lower()
        if any(k in haystack for k in topic.keywords):
            described.append(
                {
                    "ticket": ticket,
                    "descriptions": info.get("d", []),
                    "first": info.get("first"),
                    "last": info.get("last"),
                    "repos": info.get("repos", []),
                }
            )
    described.sort(key=lambda t: -(len(t["repos"])))

    return {
        "slug": topic.slug,
        "title": topic.title,
        "keywords": topic.keywords,
        "nodes_by_repo": dict(sorted(by_repo.items(), key=lambda kv: -len(kv[1]))),
        "business_features": sorted(set(features))[:MAX_FEATURES],
        "ticket_nodes": sorted(tickets)[:MAX_TICKETS],
        "described_tickets": described[:MAX_TICKETS],
        "matched_summaries": matched_summaries,
    }


def extract() -> int:
    topics = read_topics(config.TOPICS_CONFIG_PATH)
    graph = io.load_graph(config.GRAPH_PATH)
    summaries = io.read_json_dict(config.SUMMARIES_PATH)
    descriptions = io.read_gzip_json_dict(config.TICKET_DESCRIPTIONS_PATH)

    dossiers = [topic_dossier(t, graph, summaries, descriptions) for t in topics]
    io.write_json(config.TOPICS_INPUT_PATH, dossiers, indent=1)
    for dossier in dossiers:
        repos = len(dossier["nodes_by_repo"])
        print(
            f"{dossier['slug']}: {repos} repos, "
            f"{len(dossier['business_features'])} features, "
            f"{len(dossier['described_tickets'])} described tickets"
        )
    print(f"{len(dossiers)} dossiers -> {config.TOPICS_INPUT_PATH}")
    return 0


# --- constrained markdown -> HTML (headings, bold, code, lists, tables,
# paragraphs). Deliberately tiny: briefs are written to this subset. -------


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)


def _table(rows: list[str]) -> str:
    out = ["<table class='rt'>"]
    for index, row in enumerate(rows):
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
            continue
        tag = "th" if index == 0 else "td"
        out.append("<tr>" + "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def markdown_to_html(markdown: str) -> str:
    """Render the constrained markdown subset briefs are written in."""
    blocks: list[str] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("#"):
            level = min(len(stripped) - len(stripped.lstrip("#")), 4)
            blocks.append(f"<h{level + 1}>{_inline(stripped.lstrip('#').strip())}</h{level + 1}>")
            i += 1
        elif stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            blocks.append(_table(rows))
        elif stripped.startswith("- "):
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(f"<li>{_inline(lines[i].strip()[2:])}</li>")
                i += 1
            blocks.append("<ul>" + "".join(items) + "</ul>")
        else:
            paragraph = [stripped]
            i += 1
            while (
                i < len(lines)
                and lines[i].strip()
                and not re.match(r"^(#|\||- )", lines[i].strip())
            ):
                paragraph.append(lines[i].strip())
                i += 1
            blocks.append(f"<p>{_inline(' '.join(paragraph))}</p>")
    return "".join(blocks)


def merge() -> int:
    topics = read_topics(config.TOPICS_CONFIG_PATH)
    briefs: dict[str, dict] = {}
    problems: list[str] = []
    for topic in topics:
        source = config.TOPICS_DOCS_DIR / f"{topic.slug}.md"
        if not source.exists():
            problems.append(f"{topic.slug}: missing {source}")
            continue
        markdown = source.read_text(encoding="utf-8")
        if len(markdown) < MIN_BRIEF_LENGTH:
            problems.append(f"{topic.slug}: brief shorter than {MIN_BRIEF_LENGTH} chars")
            continue
        briefs[topic.slug] = {
            "title": topic.title,
            "keywords": topic.keywords,
            "html": markdown_to_html(markdown),
            "source": f"docs/topics/{topic.slug}.md",
        }
    io.write_json(config.TOPICS_BRIEFS_PATH, briefs, indent=1)
    for problem in problems:
        print(f"skipped - {problem}")
    print(f"{len(briefs)} briefs -> {config.TOPICS_BRIEFS_PATH}")
    return 1 if problems else 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "extract":
        return extract()
    if len(sys.argv) >= 2 and sys.argv[1] == "merge":
        return merge()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
