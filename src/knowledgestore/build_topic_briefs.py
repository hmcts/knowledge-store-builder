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


def _degrees(graph: dict) -> dict:
    """Connection count per node id, one pass over the edge list."""
    degree: dict = {}
    for link in graph.get("links", []):
        for end in (link.get("source"), link.get("target")):
            if end is not None:
                degree[end] = degree.get(end, 0) + 1
    return degree


def topic_dossier(topic: Topic, graph: dict, summaries: dict, descriptions: dict) -> dict:
    """Deterministic evidence pack a brief is written from.

    Matching nodes are ranked by connectivity before the per-repository cap is
    applied. The cap used to keep the first matches in node-iteration order,
    which handed the brief's author arbitrary matching evidence rather than
    the strongest: at estate scale a keyword can match hundreds of nodes per
    repository, and the twelve that survived were whichever the file happened
    to list first.
    """
    degree = _degrees(graph)
    by_repo_nodes: dict[str, list] = {}
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
            by_repo_nodes.setdefault(node.get("repo", ""), []).append(node)
    by_repo: dict[str, list] = {}
    for repo, nodes in by_repo_nodes.items():
        nodes.sort(key=lambda n: (-degree.get(n.get("id"), 0), n["label"]))
        by_repo[repo] = [
            f"{n['label']} ({n.get('source_file') or '?'})" for n in nodes[:MAX_NODES_PER_REPO]
        ]
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


# A ticket id as this estate's commit messages carry them: a project prefix and a
# number. Bounded so it cannot match inside a longer word or a URL path segment.
_TICKET = re.compile(r"(?<![A-Za-z0-9/-])([A-Z][A-Z0-9]{1,9}-\d{1,6})(?![A-Za-z0-9-])")


def _repositories() -> frozenset[str]:
    """The estate's own repository names, for linking prose to source.

    Read from the repository list rather than matched by shape. Names like
    `app-commons` and `app-admin` look exactly like repositories while being module
    directories inside one; linking anything hyphenated would send readers to 404s.
    """
    path = config.REPOSITORIES_CONFIG
    if not path.exists():
        return frozenset()
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Entries carry the clone target and branch: `name.git|main`, sometimes
        # `org/name.git|main`. Parsing only on "/" left the suffix attached, so
        # every name failed to match and the linking silently did nothing - the
        # unit tests used bare names and passed anyway.
        entry = line.split("|")[0].strip().split("/")[-1]
        names.add(entry[:-4] if entry.endswith(".git") else entry)
    return frozenset(names)


def _outside_tags(markup: str, transform) -> str:
    """Apply `transform` to the text between tags, never inside one.

    Linking has to run after the code-span and bold passes, so by this point the
    string holds markup. Rewriting blindly would corrupt an `href` this function
    itself inserted moments earlier - turning a tracker URL into one pointing at
    another link - so tag interiors are skipped.
    """
    return "".join(
        part if part.startswith("<") and part.endswith(">") else transform(part)
        for part in re.split(r"(<[^>]*>)", markup)
    )


def _link_identifiers(markup: str) -> str:
    """Turn ticket ids and repository names in rendered prose into links.

    Briefs and deep dives are the answers people read, and their identifiers were
    unclickable: one estate's Welsh-language brief cited 16 ticket ids and rendered
    no anchors at all. The constrained markdown subset excludes link syntax on
    purpose - an author should not be hand-writing URLs into prose - so this is
    the renderer's job, where the tracker URL and the organisation are already
    configuration. Both are opt-in: unset, the prose stays as it was.
    """
    if config.TICKET_BROWSE_URL:
        base = html.escape(config.TICKET_BROWSE_URL, quote=True)
        markup = _outside_tags(
            markup,
            lambda s: _TICKET.sub(
                lambda m: (
                    f'<a href="{base}{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>'
                ),
                s,
            ),
        )
    repositories = _repositories()
    if config.GITHUB_ORG and repositories:
        org = html.escape(config.GITHUB_ORG, quote=True)
        # Longest first, so a repository whose name contains another's is not
        # half-linked from the inside out.
        pattern = re.compile(
            r"(?<![A-Za-z0-9/-])("
            + "|".join(re.escape(name) for name in sorted(repositories, key=len, reverse=True))
            + r")(?![A-Za-z0-9-])"
        )
        markup = _outside_tags(
            markup,
            lambda s: pattern.sub(
                lambda m: (
                    f'<a href="https://github.com/{org}/{m.group(1)}" target="_blank"'
                    f' rel="noopener">{m.group(1)}</a>'
                ),
                s,
            ),
        )
    return markup


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return _link_identifiers(escaped)


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


def _heading_block(lines: list[str], i: int) -> tuple[str, int]:
    stripped = lines[i].strip()
    level = min(len(stripped) - len(stripped.lstrip("#")), 4)
    return f"<h{level + 1}>{_inline(stripped.lstrip('#').strip())}</h{level + 1}>", i + 1


def _table_block(lines: list[str], i: int) -> tuple[str, int]:
    rows = []
    while i < len(lines) and lines[i].strip().startswith("|"):
        rows.append(lines[i])
        i += 1
    return _table(rows), i


def _list_block(lines: list[str], i: int) -> tuple[str, int]:
    items = []
    while i < len(lines) and lines[i].strip().startswith("- "):
        items.append(f"<li>{_inline(lines[i].strip()[2:])}</li>")
        i += 1
    return "<ul>" + "".join(items) + "</ul>", i


def _paragraph_block(lines: list[str], i: int) -> tuple[str, int]:
    paragraph = [lines[i].strip()]
    i += 1
    while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||- )", lines[i].strip()):
        paragraph.append(lines[i].strip())
        i += 1
    return f"<p>{_inline(' '.join(paragraph))}</p>", i


def markdown_to_html(markdown: str) -> str:
    """Render the constrained markdown subset briefs are written in.

    One block consumer per shape, each returning its html and the line it stopped
    at, so this stays a dispatch loop rather than four interleaved scanners.
    """
    blocks: list[str] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("#"):
            html, i = _heading_block(lines, i)
        elif stripped.startswith("|"):
            html, i = _table_block(lines, i)
        elif stripped.startswith("- "):
            html, i = _list_block(lines, i)
        else:
            html, i = _paragraph_block(lines, i)
        blocks.append(html)
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
