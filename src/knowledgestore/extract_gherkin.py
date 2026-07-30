"""Extract Gherkin feature files into the knowledge graph as business-intent
nodes.

Graphify's AST pass covers programming languages only and no semantic (LLM)
pass runs over the E2E repositories, so the ~1,200 Gherkin .feature files -
the estate's business-language description of its own behaviour - never reach
the graph. This script parses them deterministically (no LLM) and enriches
graphify-out/graph.json in place:

- one node per Feature (business capability, in domain language);
- one node per Scenario (a concrete business behaviour);
- one node per Jira ticket referenced in tags, file names or scenario text;
- edges: feature contains scenario, feature references step-definition
  classes (matched via normalised Cucumber expressions), feature/scenario
  references Jira tickets.

Run after the graph has been built/merged:

    knowledgestore gherkin
"""

from __future__ import annotations

import gzip
import json
import re
from collections import defaultdict
from pathlib import Path


from . import config, kinds

GRAPH_PATH = config.GRAPH_PATH
LABELS_PATH = config.LABELS_PATH
REPOSITORIES = config.REPOSITORIES_DIR
FEATURES_DIR = config.FEATURES_DIR
STEP_DEFINITION_LANGUAGES = config.STEP_DEFINITION_LANGUAGES
FORMAT = "gherkin"

TICKET = config.TICKET_PATTERN
STEP_KEYWORD = re.compile(r"^(Given|When|Then|And|But)\s+", re.IGNORECASE)
PLACEHOLDER = "¤"


def norm_id(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", value.lower())).strip("_")


def normalise_step(text: str) -> str:
    """Normalise a feature step or annotation pattern to a comparable form."""
    text = text.strip().strip("^$")
    # Cucumber expressions ({int}, {string}) and behave/parse typed
    # parameters ({amount:d}) -> placeholder, so the same business step
    # matches whichever language declared it
    text = re.sub(r"\{[a-zA-Z_]*(?::[^}]*)?\}", PLACEHOLDER, text)
    text = re.sub(r"\((?:\?:)?[^)]*\)", PLACEHOLDER, text)
    # Quoted values and <outline-params> -> placeholder
    text = re.sub(r'"[^"]*"', PLACEHOLDER, text)
    text = re.sub(r"<[^>]*>", PLACEHOLDER, text)
    text = re.sub(r"\b\d+\b", PLACEHOLDER, text)
    return re.sub(r"\s+", " ", text).lower().strip()


def parse_step_definitions(repo_dir: Path) -> dict[str, tuple[str, str]]:
    """Map normalised step pattern -> (symbol name, repo-relative file).

    Every language in config.STEP_DEFINITION_LANGUAGES is searched, so an
    estate can mix Java, Python and TypeScript step definitions.
    """
    patterns: dict[str, tuple[str, str]] = {}
    for language in STEP_DEFINITION_LANGUAGES.values():
        patterns.update(_language_step_definitions(repo_dir, language))
    return patterns


def _language_step_definitions(repo_dir: Path,
                               language: dict) -> dict[str, tuple[str, str]]:
    """Step patterns declared in one language's files."""
    annotation = re.compile(language["annotation"], re.DOTALL)
    symbol = re.compile(language["symbol"]) if language.get("symbol") else None
    found: dict[str, tuple[str, str]] = {}
    for path in sorted(repo_dir.glob(language["glob"])):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        declared = annotation.findall(content)
        if not declared:
            continue
        match = symbol.search(content) if symbol else None
        name = match.group(1) if match else path.stem
        rel = str(path.relative_to(repo_dir))
        for pattern in declared:
            found[normalise_step(pattern)] = (name, rel)
    return found


def parse_feature_line(line: str, feature: dict[str, object]) -> None:
    """Fold one Gherkin line into the accumulating feature dict."""
    if line.startswith("@"):
        feature["tags"].update(tag.lstrip("@") for tag in line.split())
        feature["tickets"].update(TICKET.findall(line))
    elif line.startswith("Feature:"):
        feature["name"] = line.split(":", 1)[1].strip() or None
    elif line.startswith(("Scenario:", "Scenario Outline:")):
        name = line.split(":", 1)[1].strip()
        if name:
            feature["scenarios"].append(name)
        feature["tickets"].update(TICKET.findall(line))
    elif STEP_KEYWORD.match(line):
        feature["steps"].add(normalise_step(STEP_KEYWORD.sub("", line)))


def parse_feature(path: Path, repo_dir: Path) -> dict[str, object] | None:
    feature: dict[str, object] = {
        "rel": str(path.relative_to(repo_dir)),
        "name": None,
        "scenarios": [],
        "steps": set(),
        "tags": set(),
        "tickets": set(TICKET.findall(path.stem)),
    }
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parse_feature_line(raw.strip(), feature)
    if not feature["name"]:
        return None
    feature["name"] = feature["name"] or path.stem
    feature["tags"] = sorted(feature["tags"])
    feature["tickets"] = sorted(feature["tickets"])
    return feature


def feature_area(rel: str) -> str:
    """First directory under features/, used to group business communities."""
    if FEATURES_DIR in rel:
        tail = rel.split(FEATURES_DIR, 1)[1]
        if "/" in tail:
            return tail.split("/")[0]
    return "(root)"


class GraphEnricher:
    """Accumulates Gherkin-derived nodes and edges onto an existing graph."""

    def __init__(self, graph: dict, labels: dict[str, str]) -> None:
        self.graph = graph
        self.labels = labels
        self.existing_ids = {n["id"] for n in graph["nodes"]}
        self.max_community = max(
            (n.get("community", 0) for n in graph["nodes"]), default=0
        )
        self.community_by_area: dict[str, int] = {}
        self.ticket_ids: dict[str, str] = {}
        self.stats: dict[str, int] = defaultdict(int)

    def community_for(self, area: str) -> int:
        if area not in self.community_by_area:
            self.max_community += 1
            self.community_by_area[area] = self.max_community
            self.labels[str(self.max_community)] = f"Business Features: {area}"
        return self.community_by_area[area]

    def add_node(self, node_id: str, label: str, repo: str, rel: str,
                 community: int, metadata: dict,
                 source_location: str | None = None) -> bool:
        if node_id in self.existing_ids:
            return False
        self.existing_ids.add(node_id)
        self.graph["nodes"].append(
            {
                "id": node_id,
                "label": label,
                "norm_label": label.lower(),
                "file_type": "concept",
                "source_file": rel,
                "source_location": source_location,
                "repo": repo,
                "_origin": "gherkin",
                "community": community,
                "local_id": node_id.split("::", 1)[-1],
                "metadata": metadata,
            }
        )
        return True

    def add_edge(self, source: str, target: str, relation: str, rel: str) -> None:
        self.graph["links"].append(
            {
                "source": source,
                "target": target,
                "relation": relation,
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": rel,
                "source_location": None,
                "weight": 1.0,
            }
        )

    def add_scenarios(self, feature: dict, feature_id: str, repo: str,
                      stem: str, community: int) -> None:
        rel = feature["rel"]
        for index, scenario in enumerate(feature["scenarios"], start=1):
            scenario_id = f"{repo}::{stem}_scenario_{index}"
            if self.add_node(scenario_id, scenario[:120], repo, rel, community,
                             {"kind": kinds.SCENARIO, "format": FORMAT}):
                self.add_edge(feature_id, scenario_id, "contains", rel)
                self.stats["scenarios"] += 1

    def add_stepdef_edges(self, feature: dict, feature_id: str,
                          step_index: dict[str, tuple[str, str]],
                          class_node_by_file: dict[str, str]) -> None:
        matched_files = {
            step_index[step][1] for step in feature["steps"] if step in step_index
        }
        for class_file in sorted(matched_files):
            target = class_node_by_file.get(class_file)
            if target:
                self.add_edge(feature_id, target, "references", feature["rel"])
                self.stats["stepdef_edges"] += 1

    def add_ticket_edges(self, feature: dict, feature_id: str, repo: str,
                         community: int) -> None:
        rel = feature["rel"]
        for ticket in feature["tickets"]:
            ticket_id = self.ticket_ids.setdefault(ticket, f"jira::{norm_id(ticket)}")
            if self.add_node(ticket_id, ticket, repo, rel, community,
                             {"kind": kinds.TICKET}):
                self.stats["tickets"] += 1
            self.add_edge(feature_id, ticket_id, "references", rel)
            self.stats["ticket_edges"] += 1

    def add_feature(self, feature: dict, repo: str,
                    step_index: dict[str, tuple[str, str]],
                    class_node_by_file: dict[str, str]) -> None:
        rel = feature["rel"]
        community = self.community_for(feature_area(rel))
        stem = norm_id(str(Path(rel).with_suffix("")))
        feature_id = f"{repo}::{stem}_feature"
        created = self.add_node(
            feature_id, feature["name"], repo, rel, community,
            {
                "kind": kinds.FEATURE,
                "format": FORMAT,
                "tags": feature["tags"],
                "tickets": feature["tickets"],
                "scenario_count": len(feature["scenarios"]),
            },
            source_location="L1",
        )
        if not created:
            self.stats["duplicate"] += 1
            return
        self.stats["features"] += 1
        self.add_scenarios(feature, feature_id, repo, stem, community)
        self.add_stepdef_edges(feature, feature_id, step_index, class_node_by_file)
        self.add_ticket_edges(feature, feature_id, repo, community)


def stepdef_class_nodes(graph: dict, repo: str) -> dict[str, str]:
    """Existing java class nodes keyed by repo-relative source file."""
    return {
        n.get("source_file"): n["id"]
        for n in graph["nodes"]
        if n.get("repo") == repo
        and n.get("source_file", "").endswith(".java")
        and n["label"] == Path(n.get("source_file", "")).stem
    }


def enrich_repository(repo_dir: Path, enricher: GraphEnricher) -> None:
    features = [
        f for f in sorted(repo_dir.rglob("*.feature"))
        if "node_modules" not in f.parts
    ]
    if not features:
        return
    repo = repo_dir.name
    step_index = parse_step_definitions(repo_dir)
    class_node_by_file = stepdef_class_nodes(enricher.graph, repo)
    for path in features:
        feature = parse_feature(path, repo_dir)
        if feature is None:
            enricher.stats["unparsed"] += 1
        else:
            enricher.add_feature(feature, repo, step_index, class_node_by_file)


def write_outputs(graph: dict, labels: dict[str, str]) -> None:
    # Sonar S2083 (path injection) misfires here: both targets are module
    # constants derived from this script's own location, and this is offline
    # build tooling operating on a local clone, not a service handling
    # untrusted input.
    serialised = json.dumps(graph, ensure_ascii=False)
    GRAPH_PATH.write_text(serialised, encoding="utf-8")  # NOSONAR(S2083)
    LABELS_PATH.write_text(  # NOSONAR(S2083)
        json.dumps(labels, ensure_ascii=False), encoding="utf-8"
    )
    with gzip.open(
        GRAPH_PATH.with_suffix(".json.gz"), "wt", encoding="utf-8", compresslevel=9
    ) as out:
        out.write(serialised)


def main() -> int:
    if not GRAPH_PATH.exists():
        print(f"Graph not found: {GRAPH_PATH} (gunzip -k graph.json.gz first)")
        return 1

    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    labels = (
        json.loads(LABELS_PATH.read_text(encoding="utf-8"))
        if LABELS_PATH.exists()
        else {}
    )

    enricher = GraphEnricher(graph, labels)
    for repo_dir in sorted(REPOSITORIES.iterdir()):
        if (repo_dir / ".git").is_dir():
            enrich_repository(repo_dir, enricher)

    write_outputs(graph, labels)

    stats = enricher.stats
    print(
        f"Features: {stats['features']}, scenarios: {stats['scenarios']}, "
        f"ticket nodes: {stats['tickets']}, "
        f"step-definition edges: {stats['stepdef_edges']}, "
        f"ticket edges: {stats['ticket_edges']}, "
        f"unparsed: {stats['unparsed']}"
    )
    print(
        f"Graph now: {len(graph['nodes'])} nodes, {len(graph['links'])} edges "
        f"({len(enricher.community_by_area)} new business-feature communities)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
