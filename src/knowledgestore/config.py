"""Configuration: where files live, and the values that vary by estate.

Every setting has a default that works for a repository laid out like the
quickstart in the README, and can be overridden by an environment variable
so a pipeline run can be steered without editing code:

    KSB_ROOT=/path/to/store KSB_GITHUB_ORG=myorg knowledgestore discover

The module-level names below are what the stage modules read. `configure()`
rewrites them in place, which is how the CLI applies `--root` and how tests
point a stage at a temporary directory.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_set(name: str, default: set[str]) -> set[str]:
    value = os.environ.get(name)
    return {v.strip() for v in value.split(",") if v.strip()} if value else default


# --- where the knowledge store lives -------------------------------------
# Default: the current working directory, i.e. the store repository you run in.
ROOT = _env_path("KSB_ROOT", Path.cwd())

# --- the estate being described ------------------------------------------
# The GitHub organisation repositories are discovered from. Required: set
# KSB_GITHUB_ORG or pass GITHUB_ORG to configure(). There is no sensible
# default - a library cannot guess whose estate you mean.
GITHUB_ORG = os.environ.get("KSB_GITHUB_ORG", "")

# --- inputs you maintain by hand -----------------------------------------
FILTERS_PATH = ROOT / "config" / "repository-filters.txt"
REPOSITORIES_CONFIG = ROOT / "config" / "repositories.txt"
TOPICS_CONFIG_PATH = ROOT / "config" / "topics.txt"

# --- working directories (regenerable; do not commit) --------------------
REPOSITORIES_DIR = ROOT / "repositories"
HISTORY_DIR = ROOT / "knowledge" / "git-history"

# --- generated datasets (commit these) -----------------------------------
MANIFEST_PATH = ROOT / "knowledge" / "repository-manifest.md"
CONTEXT_PATH = ROOT / "knowledge_context.md"
INTENT_INDEX_PATH = ROOT / "knowledge" / "intent" / "file-tickets.json.gz"
TICKET_DESCRIPTIONS_PATH = ROOT / "knowledge" / "intent" / "ticket-descriptions.json.gz"
TICKET_TITLES_PATH = ROOT / "knowledge" / "intent" / "ticket-titles.json.gz"
SUMMARIES_INPUT_PATH = ROOT / "knowledge" / "summaries" / "communities-input.json"
SUMMARIES_PATH = ROOT / "knowledge" / "summaries" / "communities.json"
SYNONYMS_PATH = ROOT / "knowledge" / "semantic" / "token-neighbours.json.gz"
# What each repository's clone pointed at when the store was last built.
# Written by the sync stage; read by status, the manifest and the explorer.
PROVENANCE_PATH = ROOT / "knowledge" / "provenance.json"
TOPICS_INPUT_PATH = ROOT / "knowledge" / "topics" / "topics-input.json"
TOPICS_BRIEFS_PATH = ROOT / "knowledge" / "topics" / "briefs.json"
TOPICS_DOCS_DIR = ROOT / "docs" / "topics"

# --- graph artefacts (produced by graphify, consumed here) ---------------
GRAPH_PATH = ROOT / "graphify-out" / "graph.json"
LABELS_PATH = ROOT / "graphify-out" / ".graphify_labels.json"
EXPLORER_PATH = ROOT / "graphify-out" / "explorer.html"

# --- issue tracker -------------------------------------------------------
# Ticket references mined from commit subjects, e.g. "PROJ-123".
TICKET_PATTERN = re.compile(
    os.environ.get("KSB_TICKET_PATTERN", r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
)
# Ticket ids in the explorer link here, with the id appended. Empty renders
# them as plain text, which is right until you say where your tracker lives.
TICKET_BROWSE_URL = os.environ.get("KSB_TICKET_BROWSE_URL", "")

# --- explorer page -------------------------------------------------------
EXPLORER_TITLE = os.environ.get("KSB_EXPLORER_TITLE", "Estate Explorer")
# Where "request a topic brief" links point. Empty disables the link.
BRIEF_REQUEST_URL = os.environ.get("KSB_BRIEF_REQUEST_URL", "")
# Minimum connection count for a code entry to be indexed in the explorer.
# Business entries (features, scenarios, tickets) are always indexed.
MIN_ENTRY_DEGREE = _env_int("KSB_MIN_ENTRY_DEGREE", 3)
# Repositories whose test code IS the business documentation (E2E suites),
# so their test files are indexed rather than filtered out as scaffolding.
# Name yours in KSB_E2E_REPOS (comma-separated); by default no repository is
# treated this way.
E2E_REPOS = _env_set("KSB_E2E_REPOS", set())

# --- BDD specifications --------------------------------------------------
# Gherkin (.feature) files are read wherever they appear in a repository. This
# is the directory whose next path segment names the feature's area, used to
# group features into business communities: "features/" makes
# features/payments/refund.feature belong to the "payments" area.
FEATURES_DIR = os.environ.get("KSB_FEATURES_DIR", "features/")

# Step definitions bind a Gherkin step to the code implementing it. Each entry
# is a language: where to look, and how a step pattern is declared there.
#
#   glob       - searched from the repository root
#   annotation - regex whose first group is the step pattern
#   symbol     - optional regex whose first group names the enclosing symbol
#                (a class, typically); the file stem is used when it is absent
#
# Defaults cover Cucumber's three most common hosts. Add or replace entries by
# assigning to this dict via configure(); an estate with an unusual layout only
# needs to narrow the glob.
STEP_DEFINITION_LANGUAGES: dict[str, dict[str, str | None]] = {
    # Cucumber-JVM / Serenity: @Given("...") in Maven's test tree
    "java": {
        "glob": "src/test/java/**/*.java",
        "annotation": r"@(?:Given|When|Then|And|But)\s*\(\s*\"(.*?)\"\s*\)",
        "symbol": r"(?:public\s+)?class\s+(\w+)",
    },
    # behave / pytest-bdd: @given("...") or @given('...')
    "python": {
        "glob": "**/*.py",
        "annotation": (r"@(?:given|when|then|step)\s*\(\s*[\"'](.*?)[\"']\s*[,)]"),
        "symbol": None,
    },
    # cucumber-js: Given("...") or Given('...'), no decorator
    "typescript": {
        "glob": "**/*.ts",
        "annotation": (r"\b(?:Given|When|Then)\s*\(\s*[\"'](.*?)[\"']\s*,"),
        "symbol": None,
    },
}

# --- community summaries -------------------------------------------------
MIN_COMMUNITY_SIZE = _env_int("KSB_MIN_COMMUNITY_SIZE", 25)

# --- semantic index ------------------------------------------------------
EMBEDDING_MODEL = os.environ.get("KSB_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")


def configure(root: Path | str | None = None, **overrides) -> None:
    """Point the pipeline at a different root, or override any setting.

    Call before running a stage. `configure(root=...)` recomputes every
    derived path; other keyword arguments set module names directly:

        configure(root="/tmp/store", GITHUB_ORG="myorg")
    """
    module = globals()
    if root is not None:
        module["ROOT"] = Path(root).expanduser().resolve()
        _recompute_paths()
    for name, value in overrides.items():
        if name not in module:
            raise KeyError(f"unknown setting: {name}")
        module[name] = value


def _recompute_paths() -> None:
    """Re-derive every ROOT-relative path after ROOT changes."""
    module = globals()
    root: Path = module["ROOT"]
    module.update(
        FILTERS_PATH=root / "config" / "repository-filters.txt",
        REPOSITORIES_CONFIG=root / "config" / "repositories.txt",
        TOPICS_CONFIG_PATH=root / "config" / "topics.txt",
        REPOSITORIES_DIR=root / "repositories",
        HISTORY_DIR=root / "knowledge" / "git-history",
        MANIFEST_PATH=root / "knowledge" / "repository-manifest.md",
        CONTEXT_PATH=root / "knowledge_context.md",
        INTENT_INDEX_PATH=root / "knowledge" / "intent" / "file-tickets.json.gz",
        TICKET_DESCRIPTIONS_PATH=root / "knowledge" / "intent" / "ticket-descriptions.json.gz",
        TICKET_TITLES_PATH=root / "knowledge" / "intent" / "ticket-titles.json.gz",
        SUMMARIES_INPUT_PATH=root / "knowledge" / "summaries" / "communities-input.json",
        SUMMARIES_PATH=root / "knowledge" / "summaries" / "communities.json",
        SYNONYMS_PATH=root / "knowledge" / "semantic" / "token-neighbours.json.gz",
        PROVENANCE_PATH=root / "knowledge" / "provenance.json",
        TOPICS_INPUT_PATH=root / "knowledge" / "topics" / "topics-input.json",
        TOPICS_BRIEFS_PATH=root / "knowledge" / "topics" / "briefs.json",
        TOPICS_DOCS_DIR=root / "docs" / "topics",
        GRAPH_PATH=root / "graphify-out" / "graph.json",
        LABELS_PATH=root / "graphify-out" / ".graphify_labels.json",
        EXPLORER_PATH=root / "graphify-out" / "explorer.html",
    )
