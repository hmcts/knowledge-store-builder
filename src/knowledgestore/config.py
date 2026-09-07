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


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    """A switch that is off unless it is explicitly turned on.

    Anything other than the affirmative spellings below is false, including an
    empty value: a setting that opts into fetching narrative text must not be
    enabled by `KSB_...=` left over in a shell.
    """
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


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
# What the estate is, what it deliberately excludes, and what it does not claim.
# Hand-maintained and optional: none of it can be derived, because a ruling on a
# repository is a decision. Absent means the estate has declared no boundary,
# which the manifest and `status` both say rather than passing over in silence.
BOUNDARY_PATH = ROOT / "config" / "estate-boundary.txt"
# Repositories fetched but never extracted (`fetch` rules). A separate file, not a
# column, so that nothing which reads the estate manifest can mistake one of these
# for part of the estate.
EXTERNAL_CONFIG = ROOT / "config" / "repositories-external.txt"
TOPICS_CONFIG_PATH = ROOT / "config" / "topics.txt"
# Files in a named format `content_set` refuses or excludes - a Terraform state file
# holds resolved secret values - that this estate has decided are safe anyway.
# Hand-maintained and optional, because whether a named file is safe is a ruling
# and no part of the pipeline can derive one. Absent means nothing is declared.
CONTENT_SET_ALLOWED_PATH = ROOT / "config" / "content-set-allowed.txt"
QUESTIONS_PATH = ROOT / "config" / "questions.txt"
# Candidate content cuts for `size-cuts` to measure. Not a cut the pipeline
# applies: a statement of what a store is for, sized before anyone commits to it.
CONTENT_CUTS_PATH = ROOT / "config" / "content-cuts.txt"
# graphify's semantic fan-out reads and writes these. The chunk plan is the only
# map from chunk number to file list, so without it the committed chunk archive
# cannot be read back; it was ad-hoc and machine-specific until the library owned
# it (#144).
DETECT_PATH = ROOT / "graphify-out" / ".graphify_detect.json"
CHUNK_PLAN_PATH = ROOT / "graphify-out" / ".graphify_chunk_plan.json"
UNCACHED_PATH = ROOT / "graphify-out" / ".graphify_uncached.txt"

# --- working directories (regenerable; do not commit) --------------------
REPOSITORIES_DIR = ROOT / "repositories"
# Deliberately NOT under repositories/: the graph extraction pass walks that
# directory, and a fetch-only repository must be unreachable from it.
EXTERNAL_DIR = ROOT / "external"
HISTORY_DIR = ROOT / "knowledge" / "git-history"

# --- generated datasets (commit these) -----------------------------------
MANIFEST_PATH = ROOT / "knowledge" / "repository-manifest.md"
CONTEXT_PATH = ROOT / "knowledge_context.md"
INTENT_INDEX_PATH = ROOT / "knowledge" / "intent" / "file-tickets.json.gz"
TICKET_DESCRIPTIONS_PATH = ROOT / "knowledge" / "intent" / "ticket-descriptions.json.gz"
TICKET_TITLES_PATH = ROOT / "knowledge" / "intent" / "ticket-titles.json.gz"
# What the issue tracker said about each discovered ticket, one request per
# ticket ever. Written by `fetch-tickets`; a build without tracker credentials
# reads the committed file rather than degrading.
TICKET_TRACKER_PATH = ROOT / "knowledge" / "intent" / "ticket-tracker.json.gz"
# Ticket prefixes nobody has decided about yet: not in the allowlist, not in the
# deny list, so not requested and not silently dropped either.
TRACKER_UNDECIDED_PATH = ROOT / "knowledge" / "intent" / "tracker-undecided.json"
SUMMARIES_INPUT_PATH = ROOT / "knowledge" / "summaries" / "communities-input.json"
SUMMARIES_PATH = ROOT / "knowledge" / "summaries" / "communities.json"
# Community membership as it was before a re-cluster, so summaries can be
# remapped onto the new ids afterwards. Written by `summaries snapshot`.
SUMMARIES_SNAPSHOT_PATH = ROOT / "knowledge" / "summaries" / "membership-snapshot.json.gz"
# The same communities keyed by `(repository, source_file)` instead of node id,
# written beside the membership snapshot by the same `summaries snapshot` run.
# `remap`'s fallback route needs the OLD side's file sets, and the old node ids
# cannot supply them: the whole reason the fallback exists is that those ids are
# absent from the rebuilt graph, so nothing in the new graph can say which files
# they came from (#302).
#
# A second file rather than a second key inside the membership snapshot. That
# snapshot's value is a list of node ids and an older library reads it as one, so
# widening it to a dict would leave a pinned release intersecting the strings
# "nodes" and "files" with the graph's node ids - a clean, silent, total drift.
# An extra file is ignored by every reader that does not know about it.
SUMMARIES_FILE_SNAPSHOT_PATH = ROOT / "knowledge" / "summaries" / "membership-files.json.gz"
# What the last remap carried and what it displaced, with the displaced prose
# itself - the backfill's raw material for revise-rather-than-rewrite.
REMAP_REPORT_PATH = ROOT / "knowledge" / "summaries" / "remap-report.json"
# Prose the last remap would not re-key, in the same shape as `communities.json`
# so it can be read, revised and merged straight back. The remap report holds the
# same prose with its reason and its near-miss target; this file is the half a
# backfill consumes, and it is rewritten on every remap so it never describes an
# earlier run (#296).
SUMMARIES_WITHDRAWN_PATH = ROOT / "knowledge" / "summaries" / "communities-withdrawn.json"
SYNONYMS_PATH = ROOT / "knowledge" / "semantic" / "token-neighbours.json.gz"
# The content set: which corpus files the pipeline decided were content, as a
# path list a search can consume directly, plus the measurement that says why
# searching the raw tree instead is a bad idea. Written by `content-set` (#213).
# A path list rather than only a JSON manifest because the consumer is `grep`,
# and a consumer that has to parse JSON first re-derives the set badly instead.
CONTENT_FILES_PATH = ROOT / "knowledge" / "corpus" / "content-files.txt"
CONTENT_SET_PATH = ROOT / "knowledge" / "corpus" / "content-set.json"
# What each repository's clone pointed at when the store was last built.
# Written by the sync stage; read by status, the manifest and the explorer.
PROVENANCE_PATH = ROOT / "knowledge" / "provenance.json"
# What the last build measured, so the next one can say what moved. Written by
# the stages that compute the counts (intent, merge-layers, explorer) and read
# back by each of them before it overwrites its own; `status` prints it.
# Committed on purpose: the diff is the record of what a refresh changed.
TELEMETRY_PATH = ROOT / "knowledge" / "telemetry.json"
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
# What the committed page was actually built from, by content. Commit dates
# cannot answer that: the ordinary workflow commits a changed layer and the page
# together, so their dates match whether or not the page was rebuilt.
EXPLORER_INPUTS_PATH = ROOT / "graphify-out" / "explorer-inputs.json"
# Which partitioner produced the communities in the graph. graphify chooses it
# from the environment - Leiden where graspologic imports, Louvain where it does
# not - and community ids key every authored summary, so the choice is a silent
# input to the whole summary layer. Small on purpose: `status` reads it and must
# never load the graph.
CLUSTERING_RECORD_PATH = ROOT / "graphify-out" / "clustering-inputs.json"

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


def _browse_url(value: str) -> str:
    """A tracker browse URL a ticket id can be appended to.

    Both consumers - the explorer page's embedded config and the brief renderer -
    build a link by concatenating the id onto this. A URL without a trailing
    separator therefore produced `https://tracker/browseCCT-890`: a broken link,
    silently, in every brief and every search result. Normalising here rather than
    at each call site means the page's embedded value is already usable, so the
    page application needs no matching change.

    A URL already ending in a separator is trusted as-is, because not every
    tracker puts the id in a path segment: `https://tracker/issue?key=` wants the
    id appended directly and a slash would break it.
    """
    value = value.strip()
    return value + "/" if value and value[-1] not in "/=?&#" else value


TICKET_BROWSE_URL = _browse_url(os.environ.get("KSB_TICKET_BROWSE_URL", ""))

# Settings whose value is normalised however it arrives.
_NORMALISERS = {"TICKET_BROWSE_URL": _browse_url}

# --- asking the tracker what a ticket is (the `fetch-tickets` stage) ------
# Every setting here is empty or off by default, because the stage is opt-in:
# the pipeline is complete without it, and a store with no tracker credentials
# reads whatever the last credentialled run committed.
#
# The tracker's API root, e.g. https://tracker.example/jira - the part before
# /rest/api/2. Empty means the stage is not configured and does nothing.
TRACKER_BASE_URL = os.environ.get("KSB_TRACKER_BASE_URL", "")
# A personal access token, sent as `Authorization: Bearer <token>`. Never
# written to an artefact, a log line or an error message - see fetch_tickets.py.
TRACKER_TOKEN = os.environ.get("KSB_TRACKER_TOKEN", "")
# The ticket prefixes this store may read, comma-separated (`AAA,BBB`). Empty
# means none, which is not the same as "all": prefixes that are in neither list
# are reported for a person to decide about, and never requested.
TRACKER_PROJECTS = _env_set("KSB_TRACKER_PROJECTS", set())
# Prefixes that must never be requested, whatever the allowlist says. A deny
# entry wins, so an allowlist edit cannot re-enable a project somebody withdrew.
TRACKER_DENY = _env_set("KSB_TRACKER_DENY", set())
# Narrative text is not requested unless asked for. A response that never
# carried a description is a stronger guarantee than one that carried it and had
# it discarded locally, so these two settings change the request, not the store.
TRACKER_FETCH_DESCRIPTION = _env_bool("KSB_TRACKER_FETCH_DESCRIPTION", False)
TRACKER_FETCH_COMMENTS = _env_bool("KSB_TRACKER_FETCH_COMMENTS", False)
# Tickets per search request. One request per ticket turns a large estate into
# tens of thousands of calls; batching turns the same work into hundreds.
# Longest comment kept, in characters, cut at a word boundary. Measured on one
# estate's first live fetch: 38,681 comments held 14.4 M characters, and 778 of
# them - 2% - held 37% of that, being stack traces and log dumps pasted into a
# ticket. Capping at 2,000 removed a quarter of the payload while leaving 93% of
# tickets with every comment intact. There is deliberately no limit on the NUMBER
# of comments: keeping only the first few was measured, saved a further 1.7 MB
# gzipped, and cost 37% of tickets their threads - and a ticket with eighteen
# comments is one where something went wrong and got argued about, so the
# resolution is at the end, not the beginning.
TRACKER_COMMENT_CHARS = _env_int("KSB_TRACKER_COMMENT_CHARS", 2000)
# Comments matching this are dropped rather than shortened: a truncated log dump is
# still a log dump. Override per estate - another tracker's automation says
# different things, and dropping content is not a judgement to hard-code. The
# stack-frame branch's package-root list is a starting point, not a closed set:
# it covers common Java/Scala roots, but an estate with other vendor or in-house
# roots inherits a filter with a hole in it, and a miss here is silent - the
# frame just reads as narrative. Extend the list for your estate.
TRACKER_COMMENT_NOISE = os.environ.get(
    "KSB_TRACKER_COMMENT_NOISE",
    r"(?i)\b(jenkins|build (succeeded|failed|#)|pipeline|bitbucket|pull request"
    r"|auto-?generated|sonarqube|renovate|stack ?trace|caused by:|at (uk|com|net|io|java|org)\.)",
)

TRACKER_PAGE_SIZE = _env_int("KSB_TRACKER_PAGE_SIZE", 100)
# Pause between pages. One request is in flight at a time regardless; this is
# what keeps a first run from reading as traffic worth alerting on.
TRACKER_DELAY_SECONDS = _env_float("KSB_TRACKER_DELAY_SECONDS", 1.0)

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
#   KSB_SENSITIVE_PATTERNS='{"record-reference": "\\bREC/[0-9]{4}\\b"}'
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
# How much of a tracker description the page carries, in characters. The page is
# the artefact people download and open offline, so it takes an opening extract,
# not the whole text - the same split the intent index makes between a body's
# description and its evidence. The full description and every comment stay in
# knowledge/intent/ticket-tracker.json.gz for an agent to read: on one estate that
# is 10.1 M characters of comments, which has no business inline in a 49 MB page.
TICKET_DETAIL_CHARS = _env_int("KSB_TICKET_DETAIL_CHARS", 300)

MIN_ENTRY_DEGREE = _env_int("KSB_MIN_ENTRY_DEGREE", 3)
# Repositories whose test code IS the business documentation (E2E suites),
# so their test files are indexed rather than filtered out as scaffolding.
# Name yours in KSB_E2E_REPOS (comma-separated); by default no repository is
# treated this way.
E2E_REPOS = _env_set("KSB_E2E_REPOS", set())
# The page size above which the explorer stage says so before writing the file.
# GitHub warns above 50 MB per file and rejects a push carrying one above 100 MB,
# and the page is committed, so a store that learns its size after the commit has
# an artefact it cannot ship and a rebuild it cannot repeat cheaply. Deliberately
# under GitHub's own warning: the point is room to decide, not a second copy of
# the same alarm. Raise it where a store has settled that its page is this large.
EXPLORER_WARN_BYTES = _env_int("KSB_EXPLORER_WARN_BYTES", 40 * 1_048_576)
# The page size the explorer stage refuses to write at all. GitHub blocks a push
# carrying a file above 100 MB, so a page over that is not a large artefact but an
# unshippable one - and the failure arrives at `git push`, a commit after the stage
# that produced it and naming neither the stage nor the layer that grew. Refused
# before the write rather than after: a page already in the working tree is one the
# ordinary workflow commits beside the layers it was built from. Raise it where a
# store ships its page somewhere GitHub's ceiling does not apply.
EXPLORER_MAX_BYTES = _env_int("KSB_EXPLORER_MAX_BYTES", 100 * 1_048_576)

# --- deployment evidence (the `deployments` stage) ------------------------
# Off until an estate names the repository that holds its deployment config,
# because most estates have no such repository and the stage would find
# nothing. The glob is relative to that clone; the path segment between the
# glob root and the file names the environment, so
# `ansible/group_vars/prd/foo_values.yaml.j2` is service `foo` in `prd`, and a
# file directly under `group_vars/` is the environment-independent base layer.
DEPLOY_REPOS = _env_set("KSB_DEPLOY_REPOS", set())
DEPLOY_VALUES_GLOB = os.environ.get(
    "KSB_DEPLOY_VALUES_GLOB", "ansible/group_vars/**/*_values.yaml.j2"
)
DEPLOY_BASE_ENV = os.environ.get("KSB_DEPLOY_BASE_ENV", "_base")
# A values file runs to hundreds of keys; the page and the prose want the shape,
# not the whole file, which stays readable in the clone.
DEPLOY_MAX_KEYS = _env_int("KSB_DEPLOY_MAX_KEYS", 60)
DEPLOY_VALUE_CHARS = _env_int("KSB_DEPLOY_VALUE_CHARS", 200)
# What reaches the browser. The graph keeps the whole configuration for an agent
# to read; the page carries a capped summary, the same trade the ticket detail
# makes - a values file runs to hundreds of keys and the page is a download.
DEPLOY_PAGE_KEYS = _env_int("KSB_DEPLOY_PAGE_KEYS", 12)

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

# --- ingestion candidates (`gaps`) ---------------------------------------
# Tokens that mark a consumed artefact as framework plumbing rather than domain
# knowledge. `gaps` reports domain namespaces first whatever their weight,
# because most reference weight is plumbing and a reference to a test utility
# says the estate writes tests, not how its business works.
#
# Matched as whole tokens, never as substrings, so `attestation-service` is not
# read as `test`. `common` is deliberately absent: a shared domain library is
# routinely named that, and demoting it hides the rows worth reading.
#
# KSB_FRAMEWORK_MARKERS replaces this list rather than extending it. An estate
# whose plumbing is named differently needs its own vocabulary, and inheriting
# these would leave it classifying its own domain artefacts as framework.
FRAMEWORK_MARKERS = _env_set(
    "KSB_FRAMEWORK_MARKERS",
    {
        "framework",
        "starter",
        "parent",
        "bom",
        "plugin",
        "archetype",
        "test",
        "tests",
        "testing",
        "mock",
        "mocks",
        "fixture",
        "fixtures",
        "util",
        "utils",
        "lint",
        "checkstyle",
        "codestyle",
    },
)

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
        # A setting with a normaliser gets it here too. Environment and
        # `configure()` are both supported entry points, so a rule applied only
        # where the environment is read would hold for a real estate and not for
        # a consumer configuring the library programmatically - the harder case
        # to notice, because it fails silently in whatever the caller builds.
        module[name] = _NORMALISERS.get(name, lambda given: given)(value)


def _recompute_paths() -> None:
    """Re-derive every ROOT-relative path after ROOT changes."""
    module = globals()
    root: Path = module["ROOT"]
    module.update(
        FILTERS_PATH=root / "config" / "repository-filters.txt",
        REPOSITORIES_CONFIG=root / "config" / "repositories.txt",
        BOUNDARY_PATH=root / "config" / "estate-boundary.txt",
        EXTERNAL_CONFIG=root / "config" / "repositories-external.txt",
        TOPICS_CONFIG_PATH=root / "config" / "topics.txt",
        CONTENT_SET_ALLOWED_PATH=root / "config" / "content-set-allowed.txt",
        QUESTIONS_PATH=root / "config" / "questions.txt",
        CONTENT_CUTS_PATH=root / "config" / "content-cuts.txt",
        DETECT_PATH=root / "graphify-out" / ".graphify_detect.json",
        CHUNK_PLAN_PATH=root / "graphify-out" / ".graphify_chunk_plan.json",
        UNCACHED_PATH=root / "graphify-out" / ".graphify_uncached.txt",
        REPOSITORIES_DIR=root / "repositories",
        EXTERNAL_DIR=root / "external",
        HISTORY_DIR=root / "knowledge" / "git-history",
        MANIFEST_PATH=root / "knowledge" / "repository-manifest.md",
        CONTEXT_PATH=root / "knowledge_context.md",
        INTENT_INDEX_PATH=root / "knowledge" / "intent" / "file-tickets.json.gz",
        TICKET_DESCRIPTIONS_PATH=root / "knowledge" / "intent" / "ticket-descriptions.json.gz",
        TICKET_TITLES_PATH=root / "knowledge" / "intent" / "ticket-titles.json.gz",
        TICKET_TRACKER_PATH=root / "knowledge" / "intent" / "ticket-tracker.json.gz",
        TRACKER_UNDECIDED_PATH=root / "knowledge" / "intent" / "tracker-undecided.json",
        SUMMARIES_INPUT_PATH=root / "knowledge" / "summaries" / "communities-input.json",
        SUMMARIES_PATH=root / "knowledge" / "summaries" / "communities.json",
        SUMMARIES_SNAPSHOT_PATH=root / "knowledge" / "summaries" / "membership-snapshot.json.gz",
        SUMMARIES_FILE_SNAPSHOT_PATH=root / "knowledge" / "summaries" / "membership-files.json.gz",
        REMAP_REPORT_PATH=root / "knowledge" / "summaries" / "remap-report.json",
        SUMMARIES_WITHDRAWN_PATH=root / "knowledge" / "summaries" / "communities-withdrawn.json",
        SYNONYMS_PATH=root / "knowledge" / "semantic" / "token-neighbours.json.gz",
        CONTENT_FILES_PATH=root / "knowledge" / "corpus" / "content-files.txt",
        CONTENT_SET_PATH=root / "knowledge" / "corpus" / "content-set.json",
        PROVENANCE_PATH=root / "knowledge" / "provenance.json",
        TELEMETRY_PATH=root / "knowledge" / "telemetry.json",
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
        EXPLORER_INPUTS_PATH=root / "graphify-out" / "explorer-inputs.json",
        CLUSTERING_RECORD_PATH=root / "graphify-out" / "clustering-inputs.json",
        # A store that pins its dependencies keeps these; a store that installs
        # the library directly has neither, and check-install-docs says so.
        REQUIREMENTS_PATH=root / "requirements.txt",
        LOCK_PATH=root / "requirements.lock",
    )
