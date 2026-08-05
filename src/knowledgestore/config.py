"""Configuration: where files live, and the values that vary by estate.

Every setting has a default that works for a repository laid out like the
quickstart in the README, and can be overridden by an environment variable
so a pipeline run can be steered without editing code:

    KSB_ROOT=/path/to/store KSB_GITHUB_ORG=myorg knowledgestore discover

The module-level names below are what the stage modules read. `configure()`
rewrites them in place, which is how the CLI applies `--root` and how tests
point a stage at a temporary directory.

Stage modules must read `config.SETTING` where they use it, never copy it to a
module-level name at import. A copy freezes the value before any caller has had
the chance to configure anything, which made `configure()` a silent no-op for
that setting - accepted, since it raises only for an unknown name, and then
ignored. `tests/test_config_and_io.py` fails if a copy reappears.
"""

from __future__ import annotations

import json
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


def _env_pattern_map(name: str, default: dict[str, str]) -> dict[str, str]:
    """A named-regex map, extended by a JSON object in the environment.

    JSON rather than the comma-separated form the other list settings use,
    because a regex contains commas: `[A-Z]{1,2}` would split into nonsense.

    The override is merged over the defaults, so an estate adds its own format
    without restating the shipped ones, and replaces a shipped rule by reusing
    its name. Anything unusable raises: a rule set silently emptied by a stray
    character looks exactly like an estate with nothing to withhold, and the
    whole point of these rules is that nobody discovers the loss later.
    """
    raw = os.environ.get(name)
    if not raw:
        return dict(default)
    try:
        added = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{name} must be a JSON object of rule name -> regex: {error}") from error
    if not isinstance(added, dict) or not all(
        isinstance(rule, str) and isinstance(pattern, str) and pattern
        for rule, pattern in added.items()
    ):
        raise ValueError(f"{name} must map rule names to non-empty regex strings")
    return {**default, **added}


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
# Community membership as it was before a re-cluster, so summaries can be
# remapped onto the new ids afterwards. Written by `summaries snapshot`.
SUMMARIES_SNAPSHOT_PATH = ROOT / "knowledge" / "summaries" / "membership-snapshot.json.gz"
# What the last remap carried and what it displaced, with the displaced prose
# itself - the backfill's raw material for revise-rather-than-rewrite.
REMAP_REPORT_PATH = ROOT / "knowledge" / "summaries" / "remap-report.json"
SYNONYMS_PATH = ROOT / "knowledge" / "semantic" / "token-neighbours.json.gz"
# What each repository's clone pointed at when the store was last built.
# Written by the sync stage; read by status, the manifest and the explorer.
PROVENANCE_PATH = ROOT / "knowledge" / "provenance.json"
TOPICS_INPUT_PATH = ROOT / "knowledge" / "topics" / "topics-input.json"
TOPICS_BRIEFS_PATH = ROOT / "knowledge" / "topics" / "briefs.json"
TOPICS_DOCS_DIR = ROOT / "docs" / "topics"

# --- deep dives: evidence-grounded dossiers on individual repositories ----
DEEPDIVES_INPUT_DIR = ROOT / "knowledge" / "deep-dives"
DEEPDIVES_DOCS_DIR = ROOT / "docs" / "deep-dives"
DEEPDIVES_PATH = ROOT / "knowledge" / "deep-dives" / "dives.json"
# Bundle thresholds (env-overridable where an estate may reasonably differ).
DIVE_TOP_FILES = _env_int("KSB_DIVE_TOP_FILES", 15)
DIVE_MIN_COCHANGE = _env_int("KSB_DIVE_MIN_COCHANGE", 10)
# Tickets touching more files than this are sweeping changes (renames,
# reformat commits) and are excluded from co-change pairing.
DIVE_COCHANGE_MAX_FILES_PER_TICKET = _env_int("KSB_DIVE_COCHANGE_MAX_FILES_PER_TICKET", 40)
# Instability wording in commit-mined ticket descriptions.
REVERT_PATTERN = re.compile(r"\brevert", re.IGNORECASE)
FIX_PATTERN = re.compile(r"\b(fix|defect|bug|hotfix)", re.IGNORECASE)

# --- graph artefacts (produced by graphify, consumed here) ---------------
GRAPH_PATH = ROOT / "graphify-out" / "graph.json"
LABELS_PATH = ROOT / "graphify-out" / ".graphify_labels.json"
# graphify's audit report. The library never writes it, but the gherkin stage
# notes what it added after the report was produced.
GRAPH_REPORT_PATH = ROOT / "graphify-out" / "GRAPH_REPORT.md"
EXPLORER_PATH = ROOT / "graphify-out" / "explorer.html"

# --- pinned dependencies -------------------------------------------------
# A store that pins its dependencies keeps a requirements input and a compiled
# lock. A store that installs the library directly has neither, and
# check-install-docs reports that rather than failing.
REQUIREMENTS_PATH = ROOT / "requirements.txt"
LOCK_PATH = ROOT / "requirements.lock"

# --- issue tracker -------------------------------------------------------
# Ticket references mined from commit subjects, e.g. "PROJ-123".
TICKET_PATTERN = re.compile(
    os.environ.get("KSB_TICKET_PATTERN", r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
)
# Ticket ids in the explorer link here, with the id appended. Empty renders
# them as plain text, which is right until you say where your tracker lives.
# Automation identities whose commit bodies are not evidence, beyond the GitHub
# App `[bot]` convention that needs no list. Overridable because the default is
# matched as a whole word and several of these are also surnames: measured on
# one estate the list matched 23 identities and every one was a machine, but an
# estate employing someone called Jenkins would lose that person's commit bodies
# with no other recourse. Comma-separated; empty disables the list entirely and
# leaves only the `[bot]` rule.
AUTOMATION_IDENTITIES = [
    name.strip()
    for name in os.environ.get(
        "KSB_AUTOMATION_IDENTITIES",
        "jenkins,renovate,snyk,greenkeeper,devops-team,embedded_devops_sa",
    ).split(",")
    if name.strip()
]

TICKET_BROWSE_URL = os.environ.get("KSB_TICKET_BROWSE_URL", "")

# --- identifiers redacted out of mined commit text ------------------------
# Commit messages are written for colleagues, not for publication, and some of
# them name one specific case or person. A store commits its mined text and its
# browser page embeds it, so anything mined is republished. Every match below is
# replaced in place with `[<rule name> withheld]` and the words around it are
# kept - see `sensitive.py` for what that achieves and what it does not.
#
# Rule names do double duty: a run reports which one fired, and the placeholder
# is derived from the name, so keep names readable as prose. Identifier formats
# differ between estates and no library can know them all, so add yours with
# KSB_SENSITIVE_PATTERNS, a JSON object merged over these:
#
#   KSB_SENSITIVE_PATTERNS='{"listing-reference": "\\bREF/[0-9]{4}\\b"}'
# One default only, and deliberately: an email address has the same shape
# everywhere, so it is the only identifier this library can recognise without
# assuming a jurisdiction or a subject domain. Reference formats for cases,
# claims, patients, accounts or citizens differ per organisation, and national
# identifiers and postal codes differ per country - a library that shipped one
# country's would protect that estate and quietly miss every other one, which is
# worse than shipping none because it reads as coverage. Each estate declares its
# own in KSB_SENSITIVE_PATTERNS, and the run names the rules in force so an
# operator can see what is actually being applied.
DEFAULT_SENSITIVE_PATTERNS: dict[str, str] = {
    "email-address": r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b",
}
SENSITIVE_PATTERNS = _env_pattern_map("KSB_SENSITIVE_PATTERNS", DEFAULT_SENSITIVE_PATTERNS)

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
# Defaults cover Cucumber's three most common hosts. Add or replace entries with
# configure(STEP_DEFINITION_LANGUAGES={...}), before or after importing the
# stage; an estate with an unusual layout only needs to narrow the glob.
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

    `configure(root=...)` recomputes every derived path; other keyword
    arguments set module names directly:

        configure(root="/tmp/store", GITHUB_ORG="myorg")

    Import order does not matter: stages read these names where they use them,
    so a call after a stage module is imported still reaches it. Unknown names
    raise KeyError rather than being accepted and quietly doing nothing.
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
        SUMMARIES_SNAPSHOT_PATH=root / "knowledge" / "summaries" / "membership-snapshot.json.gz",
        REMAP_REPORT_PATH=root / "knowledge" / "summaries" / "remap-report.json",
        SYNONYMS_PATH=root / "knowledge" / "semantic" / "token-neighbours.json.gz",
        PROVENANCE_PATH=root / "knowledge" / "provenance.json",
        TOPICS_INPUT_PATH=root / "knowledge" / "topics" / "topics-input.json",
        TOPICS_BRIEFS_PATH=root / "knowledge" / "topics" / "briefs.json",
        TOPICS_DOCS_DIR=root / "docs" / "topics",
        DEEPDIVES_INPUT_DIR=root / "knowledge" / "deep-dives",
        DEEPDIVES_DOCS_DIR=root / "docs" / "deep-dives",
        DEEPDIVES_PATH=root / "knowledge" / "deep-dives" / "dives.json",
        GRAPH_PATH=root / "graphify-out" / "graph.json",
        LABELS_PATH=root / "graphify-out" / ".graphify_labels.json",
        GRAPH_REPORT_PATH=root / "graphify-out" / "GRAPH_REPORT.md",
        EXPLORER_PATH=root / "graphify-out" / "explorer.html",
        # A store that pins its dependencies keeps these; a store that installs
        # the library directly has neither, and check-install-docs says so.
        REQUIREMENTS_PATH=root / "requirements.txt",
        LOCK_PATH=root / "requirements.lock",
    )
