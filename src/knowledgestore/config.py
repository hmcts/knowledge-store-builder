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

**A store commits its settings in `config/knowledgestore.toml`**, as string
values in a `[settings]` table, read when this module loads and again whenever
`configure(root=...)` moves the root. A variable already in the environment
wins, so CI and a one-off override still work. A setting that lives only in a
shell is lost by the next shell, and a rebuild without it succeeds and writes a
poorer layer (#483); the file is how the layer is reproduced from the repository.
TOML rather than a `.env` file, because secret guards, scanners and common
`.gitignore` templates all treat `*.env` as a secret - so a store's own agent
could not read it, and a store could silently not commit it. It refuses rather
than guesses: an unknown key, a key or value that looks like a secret,
`KSB_ROOT` (which decides where the file is), any other table and any value that
is not a string all stop the start-up, naming the key and never printing a value.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import overload

import tomllib


class SettingsFileError(ValueError):
    """The store's settings file holds something the library will not start with."""


# Where a store commits its settings, relative to the root.
# A string, not a Path: every Path constant here is one `configure(root=...)` repoints.
_SETTINGS_FILE = "config/knowledgestore.toml"
# A committed file is the wrong place for a credential, whatever the library reads.
_SECRET_KEY_ENDINGS = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD")
_SETTING_KEY = re.compile(r"KSB_[A-Z0-9_]+\Z")


def _setting_readers(source: str) -> dict[str, frozenset[str]]:
    """Each module-level setting assigned in `source`, with the `KSB_*` keys it reads.

    Derived from the code rather than listed by hand, so a setting added here is a
    key the file accepts without anyone remembering a second list. A key is read
    when it is the first argument of a call inside the assignment - `_env_int(...)`
    or `_lookup(...)` alike.
    """
    readers: dict[str, frozenset[str]] = {}
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        keys = frozenset(
            node.args[0].value
            for node in ast.walk(value)
            if isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and _SETTING_KEY.match(node.args[0].value)
        )
        if keys:
            readers[target.id] = keys
    return readers


_READERS = _setting_readers(Path(__file__).read_text(encoding="utf-8"))


def settings_keys() -> frozenset[str]:
    """Every `KSB_*` key this module reads, which is every key the file may name."""
    return frozenset().union(*_READERS.values())


def _refusal(path: Path, key: str, value: object) -> str | None:
    """Why one `[settings]` entry cannot be used, or None.

    Never quotes the value: a refused value may be the credential it is refused for.
    """
    if not key.startswith("KSB_"):
        return f"{path}: {key} is not a setting this library reads; only KSB_* settings belong here"
    if key.endswith(_SECRET_KEY_ENDINGS):
        return f"{path}: {key} looks like a secret; keep it in the environment, never in a committed file"
    if key == "KSB_ROOT":
        return f"{path}: KSB_ROOT decides where this file is read from, so it cannot be set here; use KSB_ROOT or --root"
    if key not in settings_keys():
        return f"{path}: {key} is not a setting this library reads"
    if not isinstance(value, str):
        return f"{path}: {key} must be a string, not a TOML {type(value).__name__}; quote it"
    return None


def _read_settings_file(path: Path) -> dict[str, str] | None:
    """The `[settings]` table `path` declares, or None when there is no file.

    Raises SettingsFileError for anything it refuses: a file TOML cannot read, any
    table or key outside `[settings]`, and any entry `_refusal` names. Values are
    the strings TOML reads, used as they are, so `chart-{name}` needs no escaping.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise SettingsFileError(f"{path}: cannot be read: {error}") from error
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise SettingsFileError(f"{path}: not valid TOML: {error}") from error
    for name in sorted(document):
        if name != "settings":
            raise SettingsFileError(
                f"{path}: {name} is outside [settings], the only table this file holds"
            )
    table = document.get("settings", {})
    if not isinstance(table, dict):
        raise SettingsFileError(f"{path}: settings must be the [settings] table")
    for key in sorted(table):
        problem = _refusal(path, key, table[key])
        if problem:
            raise SettingsFileError(problem)
    return dict(table)


def _check_values(path: Path, values: dict[str, str]) -> None:
    """Refuse a value the worker masking would mask: a credential under an
    innocent key is still a credential in a committed file.

    Imported here, not at the top, because `secret_mask` reads this module, and
    only when a file holds values, so a store without one pays nothing.
    """
    if not values:
        return
    from . import secret_mask

    for key in sorted(values):
        if secret_mask.mask(values[key]) != values[key]:
            raise SettingsFileError(
                f"{path}: {key} holds a value shaped like a secret; keep it in the "
                "environment, never in a committed file"
            )


# What the file supplied: every value it holds, whether one was read, and which of
# its keys the environment overrode. Upper-case so test isolation restores them.
_FILE_VALUES: dict[str, str] = {}
_FILE_READ = False
_FILE_OVERRIDDEN: tuple[str, ...] = ()


@overload
def _lookup(name: str) -> str | None: ...
@overload
def _lookup(name: str, default: str) -> str: ...
def _lookup(name: str, default: str | None = None) -> str | None:
    """A setting's raw value: the environment, then the store's file, then `default`."""
    if name in os.environ:
        return os.environ[name]
    return _FILE_VALUES.get(name, default)


def _env_path(name: str, default: Path) -> Path:
    value = _lookup(name)
    return Path(value).expanduser().resolve() if value else default


def _env_int(name: str, default: int) -> int:
    value = _lookup(name)
    return default if value is None else int(value)


def _env_float(name: str, default: float) -> float:
    value = _lookup(name)
    return default if value is None else float(value)


def _env_bool(name: str, default: bool) -> bool:
    """A switch that is off unless it is explicitly turned on.

    Anything other than the affirmative spellings below is false, including an
    empty value: a setting that opts into fetching narrative text must not be
    enabled by `KSB_...=` left over in a shell.
    """
    value = _lookup(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_set(name: str, default: set[str]) -> set[str]:
    value = _lookup(name)
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
    raw = _lookup(name)
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
# `_GIVEN_ROOT` is set only on the copy `configure(root=...)` loads to read another
# store's file; see `_settings_at`.
ROOT: Path = globals().get("_GIVEN_ROOT") or _env_path("KSB_ROOT", Path.cwd())

# The store's committed settings, read before any setting below so each can come
# from it. Only KSB_ROOT is read before this, which is why the file cannot set it.
SETTINGS_FILE_PATH = ROOT / _SETTINGS_FILE


def _load(path: Path) -> tuple[dict[str, str], bool, tuple[str, ...]]:
    """The file's values, whether there was a file, and which keys the environment overrides."""
    values = _read_settings_file(path)
    if values is None:
        return {}, False, ()
    return values, True, tuple(sorted(key for key in values if key in os.environ))


_FILE_VALUES, _FILE_READ, _FILE_OVERRIDDEN = _load(SETTINGS_FILE_PATH)

# --- the estate being described ------------------------------------------
# The GitHub organisation repositories are discovered from. Required: set
# KSB_GITHUB_ORG or pass GITHUB_ORG to configure(). There is no sensible
# default - a library cannot guess whose estate you mean.
GITHUB_ORG = _lookup("KSB_GITHUB_ORG", "")

# The store's name in its manifest, which a reader holding several stores uses to
# tell them apart. Optional: when unset, the manifest takes the repository name
# of the store's `origin` remote.
STORE_ID = _lookup("KSB_STORE_ID", "").strip()

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
# Exclusions every clone's `.graphifyignore` must carry. `sync` deletes those files,
# so `apply-ignores` writes these back and `extract-ast` refuses a clone missing one.
# Hand-maintained and optional: absent means nothing is declared and nothing changes.
GRAPHIFY_IGNORE_PATH = ROOT / "config" / "graphify-ignore.txt"
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
# A store's own manifest layers, declared by hand. Optional.
STORE_EXTRA_LAYERS_PATH = ROOT / "config" / "store-extra-layers.json"
# What the store contains, written last by `knowledgestore manifest`. Committed:
# a reader on another machine opens it before any layer.
STORE_MANIFEST_PATH = ROOT / "knowledge" / "store-manifest.json"
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
TICKET_PATTERN = re.compile(_lookup("KSB_TICKET_PATTERN", r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b"))


def _prefix_list(given: str | Iterable[str]) -> list[str]:
    """Ticket-key prefixes from a comma-separated string or a list, upper-cased."""
    names = given.split(",") if isinstance(given, str) else given
    return [name.strip().upper() for name in names if name.strip()]


# Identifier schemes with a ticket's shape that are not a tracker: the pattern
# above reads `CVE-2024-12345` as ticket `CVE-2024`, and advisory ids fill
# dependency-bump messages. A match whose prefix is listed is not a ticket.
# Comma-separated; empty restores every match, for a store whose tracker
# genuinely uses one of these keys.
TICKET_PREFIX_DENY = _prefix_list(_lookup("KSB_TICKET_PREFIX_DENY", "CVE,GHSA"))


def ticket_ids(text: str) -> list[str]:
    """Every ticket id text names, in order, less those whose prefix is denied.

    The one place a ticket is read from text, so commit messages and Gherkin
    tags agree on what a ticket is. Filtered after matching, on the prefix,
    rather than written into the pattern, so a store whose tracker genuinely uses
    a denied key restores it with `KSB_TICKET_PREFIX_DENY` instead of rewriting
    `KSB_TICKET_PATTERN`. The prefix is upper-cased before the comparison, as the
    list is, so a case-insensitive pattern still denies `cve-2024-1234`. Reads
    both settings when called, so `configure()` reaches it.
    """
    denied = frozenset(TICKET_PREFIX_DENY)
    return [
        ticket
        for ticket in TICKET_PATTERN.findall(text)
        if ticket.split("-", 1)[0].upper() not in denied
    ]


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
    for name in _lookup(
        "KSB_AUTOMATION_IDENTITIES",
        "jenkins,renovate,snyk,greenkeeper,devops-team,embedded_devops_sa",
    ).split(",")
    if name.strip()
]

# Whether the intent index publishes links from commits found only on a branch,
# as `proposed`. `omit` leaves proposals out, for a store that wants none; branch
# work whose content landed on the default branch is still indexed. History keeps
# it either way, so changing this setting needs no re-export.
PROPOSED_LINKS = _lookup("KSB_PROPOSED_LINKS", "write").strip().lower() or "write"


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


TICKET_BROWSE_URL = _browse_url(_lookup("KSB_TICKET_BROWSE_URL", ""))

# Settings whose value is normalised however it arrives.
_NORMALISERS = {
    "TICKET_BROWSE_URL": _browse_url,
    "PROPOSED_LINKS": lambda given: str(given).strip().lower() or "write",
    "TICKET_PREFIX_DENY": _prefix_list,
    "INDEX_WARN_MB": int,
}

# --- asking the tracker what a ticket is (the `fetch-tickets` stage) ------
# Every setting here is empty or off by default, because the stage is opt-in:
# the pipeline is complete without it, and a store with no tracker credentials
# reads whatever the last credentialled run committed.
#
# How the stage reaches the tracker. `rest` is Jira Data Center or Server over
# HTTP, with the base URL and token below. `twg` is Jira Cloud through the
# Atlassian TWG CLI, which holds its own sign-in, so neither setting is needed.
# Only the transport changes: every decision about what to ask and what to keep
# is the same on both. See twg_transport.py.
TRACKER_TRANSPORT = _lookup("KSB_TRACKER_TRANSPORT", "rest").strip().lower()
# The tracker's API root, e.g. https://tracker.example/jira - the part before
# /rest/api/2. Empty means the stage is not configured and does nothing.
TRACKER_BASE_URL = _lookup("KSB_TRACKER_BASE_URL", "")
# A personal access token, sent as `Authorization: Bearer <token>`. Never
# written to an artefact, a log line or an error message - see fetch_tickets.py.
TRACKER_TOKEN = _lookup("KSB_TRACKER_TOKEN", "")
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
TRACKER_COMMENT_NOISE = _lookup(
    "KSB_TRACKER_COMMENT_NOISE",
    r"(?i)\b(jenkins|build (succeeded|failed|#)|pipeline|bitbucket|pull request"
    r"|auto-?generated|sonarqube|renovate|stack ?trace|caused by:|at (uk|com|net|io|java|org)\.)",
)

TRACKER_PAGE_SIZE = _env_int("KSB_TRACKER_PAGE_SIZE", 100)
# Pause between pages. One request is in flight at a time regardless; this is
# what keeps a first run from reading as traffic worth alerting on.
TRACKER_DELAY_SECONDS = _env_float("KSB_TRACKER_DELAY_SECONDS", 1.0)
# Days before an unresolved ticket's record is asked about again. A resolved
# ticket is history and is never re-requested; an open one is still changing, so
# its status and text would otherwise freeze at the first fetch. A week keeps a
# daily build from re-requesting every open ticket every day. Zero (or less)
# turns refresh off, which is how the stage behaved before it existed.
TRACKER_REFRESH_DAYS = _env_int("KSB_TRACKER_REFRESH_DAYS", 7)
# Most open tickets one run re-requests, oldest first. A store upgrading from a
# release without refresh has every open record due at once; the cap spreads that
# backlog over several runs so no single refresh pays for all of it. At the
# default page size, 200 is two requests.
TRACKER_REFRESH_MAX = _env_int("KSB_TRACKER_REFRESH_MAX", 200)
# Most follow-up calls the twg transport makes in one run. `bulk-get` answers a
# page of keys in one call, but each key it does not return costs a `get` to
# learn whether the ticket moved, and each comment thread it returns only part of
# costs a `get --comments` to complete. Past this many, a key is left for the
# next run and a thread is stored as returned and marked truncated, rather than
# turning one page into a hundred calls. The REST transport makes none.
TRACKER_FOLLOWUP_MAX = _env_int("KSB_TRACKER_FOLLOWUP_MAX", 100)

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
# Two defaults, and deliberately only two. An email address has the same shape
# everywhere, and so does a pasted mention - `[@Display Name|<profile link>]`,
# which a tracker or chat tool writes into a comment: the name and the link both
# identify a person, so the whole span goes. The pattern lets the name hold a `|`
# and lets text sit before an email placeholder (the email rule runs first), and
# leaves `[@attr=value]` code fragments, which carry no link, alone. These are the
# identifiers this library can recognise without assuming a jurisdiction or a
# subject domain. Reference formats for cases,
# claims, patients, accounts or citizens differ per organisation, and national
# identifiers and postal codes differ per country - a library that shipped one
# country's would protect that estate and quietly miss every other one, which is
# worse than shipping none because it reads as coverage. Each estate declares its
# own in KSB_SENSITIVE_PATTERNS, and the run names the rules in force so an
# operator can see what is actually being applied.
DEFAULT_SENSITIVE_PATTERNS: dict[str, str] = {
    "email-address": r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b",
    "pasted-mention": r"\[@[^\]\n]{1,80}?\|(?:mailto:[^\]\s\[]*\[[a-z ]+ withheld\]|[^\]\s]*)\]",
}
SENSITIVE_PATTERNS = _env_pattern_map("KSB_SENSITIVE_PATTERNS", DEFAULT_SENSITIVE_PATTERNS)

# --- secret values masked out of files a headless worker reads -------------
# `workers extract` gives each worker a masked copy of its chunk's text files, so
# a literal credential committed in source never reaches the model or the session
# transcript - see `secret_mask.py` for what that achieves and what it does not.
# These are the value-shape rules; the key-name rule (`password: ...`) is code.
# A rule with capturing groups masks the first group that took part in the match
# and keeps the rest as context; one without masks the whole match. A group named
# `guarded` is masked only when its value looks like a credential. Rule names are what the
# run counts under. Add your estate's shapes with KSB_SECRET_PATTERNS, a JSON
# object merged over these:
#
#   KSB_SECRET_PATTERNS='{"estate-pin": "PIN-([0-9]{4})"}'
DEFAULT_SECRET_PATTERNS: dict[str, str] = {
    "authorization-header": (
        r"(?i)\bauthorization[\"']?[ \t]*[:=][ \t]*[\"']?(?:bearer|basic)[ \t]+"
        r"([A-Za-z0-9._~+/-]+=*)"
    ),
    "aws-access-key-id": r"\bAKIA[0-9A-Z]{16}\b",
    # Long flags with `=` or a space; `-p` only attached to its value and only
    # after a mysql client, because `-p` elsewhere is a port or `mkdir -p`.
    "cli-secret-flag": (
        r"(?:(?<![\w-])--(?:password|passwd|token|api-key|client-secret|secret)(?![\w-])"
        r"(?:=|[ \t]+)|\bmysql(?:dump|admin)?\b[^\n]*?[ \t]-p)"
        r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"'-][^\s\"']*)"
    ),
    # An instrumentation key's field holds a word as often as a key, so its group is
    # `guarded`: masked only when the value looks like a credential.
    "connection-string-key": (
        r"(?i)\b(?:(?:AccountKey|SharedAccessKey)=([^;\s\"']+)"
        r"|InstrumentationKey=(?P<guarded>[^;\s\"']+))"
    ),
    # A `Password=` pair inside an ADO.NET or JDBC-style connection string, which
    # a `;` or the string's opening quote comes before. The key naming the string
    # is often `DefaultConnection`, which names no secret.
    "connection-string-credential": (
        r"(?i)(?<=[;\"'])[ \t]*(?:password|pwd)[ \t]*=[ \t]*([^;\"'\r\n]+)"
    ),
    "feature-flag-sdk-key": (
        r"(?i)\bsdk-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    ),
    "github-token": r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
    "jwt": r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    "pem-private-key": (
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----([\s\S]*?)"
        r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    ),
    # After `?` or `&`, or the `;` of an HTML-escaped `&amp;`; the value stops at the
    # next parameter, escaped or not.
    "sas-signature": r"[?&;]sig=([^&;\s\"'<>]+)",
    # A digit is required: real keys carry them, and `sk-learn-...` does not.
    "sk-key": r"\b[sr]k(?:-|_live_|_test_)(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{12,}",
    "slack-webhook": r"hooks\.slack\.com/services/([A-Za-z0-9/_-]+)",
    "sonarqube-token": r"\bsq[apu]_[0-9a-f]{40}\b",
    "sops-encrypted": r"ENC\[[^\]\n]*\]",
    "sql-credential": r"(?i)\b(?:identified[ \t]+by|password)[ \t]+'([^'\n]*)'",
    # The user may be empty, as in `redis://:password@host`.
    "url-credential": r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@'\"]*:([^/\s@'\"]+)@",
}
SECRET_PATTERNS = _env_pattern_map("KSB_SECRET_PATTERNS", DEFAULT_SECRET_PATTERNS)

# --- explorer page -------------------------------------------------------
EXPLORER_TITLE = _lookup("KSB_EXPLORER_TITLE", "Estate Explorer")
# Where "request a topic brief" links point. Empty disables the link.
BRIEF_REQUEST_URL = _lookup("KSB_BRIEF_REQUEST_URL", "")
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
DEPLOY_VALUES_GLOB = _lookup("KSB_DEPLOY_VALUES_GLOB", "ansible/group_vars/**/*_values.yaml.j2")
DEPLOY_BASE_ENV = _lookup("KSB_DEPLOY_BASE_ENV", "_base")
# A values file runs to hundreds of keys; the page and the prose want the shape,
# not the whole file, which stays readable in the clone.
DEPLOY_MAX_KEYS = _env_int("KSB_DEPLOY_MAX_KEYS", 60)
DEPLOY_VALUE_CHARS = _env_int("KSB_DEPLOY_VALUE_CHARS", 200)
# What reaches the browser. The graph keeps the whole configuration for an agent
# to read; the page carries a capped summary, the same trade the ticket detail
# makes - a values file runs to hundreds of keys and the page is a download.
DEPLOY_PAGE_KEYS = _env_int("KSB_DEPLOY_PAGE_KEYS", 12)

# --- repository relationships (the `relationships` stage) ------------------
# Which repositories reference which others, parsed from their own declarations.
RELATIONSHIPS_DIR = ROOT / "knowledge" / "relationships"
RELATIONSHIPS_BY_REPO_DIR = RELATIONSHIPS_DIR / "by-repo"
REPOSITORY_DEPENDENCIES_PATH = RELATIONSHIPS_DIR / "repository-dependencies.json"
RELATIONSHIP_KINDS_PATH = RELATIONSHIPS_DIR / "kinds.json"
# A store's own joined data for `by-repo/<repo>.json`'s `extra`, one file per
# repository. An input the stage copies, never one it edits.
RELATIONSHIPS_EXTRA_DIR = RELATIONSHIPS_DIR / "extra"
RELATIONSHIP_KINDS_CONFIG_PATH = ROOT / "config" / "relationship-kinds.json"

# --- minimum-standard evidence (the `minimum-standard` stages) -------------
# Dated, cited evidence per repository against the seven points of the GOV.UK
# minimum standard. `github-settings.json` is written by its own stage and read by
# the evidence stage, which never calls GitHub.
MINIMUM_STANDARD_DIR = ROOT / "knowledge" / "minimum-standard"
MINIMUM_STANDARD_BY_REPO_DIR = MINIMUM_STANDARD_DIR / "by-repo"
MINIMUM_STANDARD_SUMMARY_PATH = MINIMUM_STANDARD_DIR / "summary.json"
MINIMUM_STANDARD_SETTINGS_PATH = MINIMUM_STANDARD_DIR / "github-settings.json"
# Optional: `{"<product>": ["<repo>", ...]}`, the store's own grouping.
PRODUCTS_CONFIG = ROOT / "config" / "products.json"
# The repository a chart lives in, by naming convention: `chart-{name}` makes
# chart `web` the clone `chart-web`. Empty by default, because a convention is an
# estate's and not this library's; resolution then starts at the clone whose
# `Chart.yaml` names the chart.
CHART_REPO_PATTERN = _lookup("KSB_CHART_REPO_PATTERN", "")
# Repositories that bundle other repositories' charts. A chart they define resolves
# to them only when no other clone defines it.
UMBRELLA_REPOS = _env_set("KSB_UMBRELLA_REPOS", set())
# Repositories that copy another repository and publish its Maven coordinates or
# npm package names. A coordinate they define resolves to them only when no other
# clone defines it.
FORK_REPOS = _env_set("KSB_FORK_REPOS", set())
# Example lines carried per edge. `references` is never capped by it.
RELATIONSHIP_EXAMPLES = _env_int("KSB_RELATIONSHIP_EXAMPLES", 3)

# --- BDD specifications --------------------------------------------------
# Gherkin (.feature) files are read wherever they appear in a repository. This
# is the directory whose next path segment names the feature's area, used to
# group features into business communities: "features/" makes
# features/payments/refund.feature belong to the "payments" area.
FEATURES_DIR = _lookup("KSB_FEATURES_DIR", "features/")

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

# --- query index (the `index` stage) ---------------------------------------
# The archive size, in MB, above which the stage warns that GitHub will refuse the
# commit. GitHub rejects a push carrying a file over 100 MB, and the archive is the
# file a store commits, so a store that learns its size at `git push` has an
# artefact it cannot ship. Under the limit on purpose: the point is room to decide
# between committing the archive and gitignoring it, not a second copy of the alarm.
INDEX_WARN_MB = _env_int("KSB_INDEX_WARN_MB", 90)
INDEX_PATH = ROOT / "knowledge" / "index" / "store.sqlite"

# --- semantic index ------------------------------------------------------
EMBEDDING_MODEL = _lookup("KSB_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")


def configure(root: Path | str | None = None, **overrides) -> None:
    """Point the pipeline at a different root, or override any setting.

    `configure(root=...)` recomputes every derived path and reads the new root's
    settings file, then other keyword arguments set module names directly:

        configure(root="/tmp/store", GITHUB_ORG="myorg")

    Import order does not matter: stages read these names where they use them,
    so a call after a stage module is imported still reaches it. Unknown names
    raise KeyError rather than being accepted and quietly doing nothing. A
    settings file the new root holds and the library refuses raises
    SettingsFileError before anything changes.
    """
    module = globals()
    if root is not None:
        at_root = _settings_at(Path(root).expanduser().resolve())
        module["ROOT"] = Path(root).expanduser().resolve()
        _recompute_paths()
        _adopt(at_root)
    for name, value in overrides.items():
        if name not in module:
            raise KeyError(f"unknown setting: {name}")
        # A setting with a normaliser gets it here too. Environment and
        # `configure()` are both supported entry points, so a rule applied only
        # where the environment is read would hold for a real estate and not for
        # a consumer configuring the library programmatically - the harder case
        # to notice, because it fails silently in whatever the caller builds.
        module[name] = _NORMALISERS.get(name, lambda given: given)(value)


def _settings_at(root: Path) -> ModuleType | None:
    """This module as it would load at `root`, or None when no file is involved.

    The settings the file reaches are computed by the same lines that compute them
    at import, by loading a second copy of this module pointed at `root` - so there
    is one definition of every setting, not a second one kept for re-rooting. None
    when neither the current root nor the new one has a file: then nothing the file
    could reach changes, and every caller that never writes one pays nothing.
    """
    if not _FILE_READ and not (root / _SETTINGS_FILE).exists():
        return None
    spec = importlib.util.spec_from_file_location(f"{__name__}_at_root", __file__)
    if spec is None or spec.loader is None:  # pragma: no cover - this file exists
        raise SettingsFileError(f"cannot load the settings for {root}")
    at_root = importlib.util.module_from_spec(spec)
    at_root.__dict__["_GIVEN_ROOT"] = root
    try:
        spec.loader.exec_module(at_root)
    except ValueError as error:
        # The copy raises its own class; callers catch this module's.
        raise SettingsFileError(str(error)) from error
    return at_root


def _adopt(at_root: ModuleType | None) -> None:
    """Take every setting the old file or the new one reaches from `at_root`.

    Only those: a setting neither file names keeps whatever a caller configured.
    A key the old file set and the new one does not falls back to the environment
    or its default, so one store's settings never carry into the next.
    """
    module = globals()
    if at_root is None:
        module.update(_FILE_VALUES={}, _FILE_READ=False, _FILE_OVERRIDDEN=())
        return
    reached = set(module["_FILE_VALUES"]) | set(at_root._FILE_VALUES)
    for name, keys in _READERS.items():
        if keys & reached:
            module[name] = getattr(at_root, name)
    module.update(
        _FILE_VALUES=at_root._FILE_VALUES,
        _FILE_READ=at_root._FILE_READ,
        _FILE_OVERRIDDEN=at_root._FILE_OVERRIDDEN,
    )


def describe_settings_file() -> str:
    """Which settings file was read and which keys it set - never a value."""
    if not _FILE_READ:
        return f"none ({SETTINGS_FILE_PATH} does not exist)"
    applied = [key for key in sorted(_FILE_VALUES) if key not in _FILE_OVERRIDDEN]
    text = f"{SETTINGS_FILE_PATH} set {', '.join(applied) or 'nothing'}"
    if _FILE_OVERRIDDEN:
        text += f"; the environment overrides {', '.join(_FILE_OVERRIDDEN)}"
    return text


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
        GRAPHIFY_IGNORE_PATH=root / "config" / "graphify-ignore.txt",
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
        STORE_EXTRA_LAYERS_PATH=root / "config" / "store-extra-layers.json",
        STORE_MANIFEST_PATH=root / "knowledge" / "store-manifest.json",
        TOPICS_INPUT_PATH=root / "knowledge" / "topics" / "topics-input.json",
        TOPICS_BRIEFS_PATH=root / "knowledge" / "topics" / "briefs.json",
        TOPICS_DOCS_DIR=root / "docs" / "topics",
        DEEPDIVES_INPUT_DIR=root / "knowledge" / "deep-dives",
        DEEPDIVES_DOCS_DIR=root / "docs" / "deep-dives",
        DEEPDIVES_PATH=root / "knowledge" / "deep-dives" / "dives.json",
        GRAPH_PATH=root / "graphify-out" / "graph.json",
        INDEX_PATH=root / "knowledge" / "index" / "store.sqlite",
        LABELS_PATH=root / "graphify-out" / ".graphify_labels.json",
        GRAPH_REPORT_PATH=root / "graphify-out" / "GRAPH_REPORT.md",
        EXPLORER_PATH=root / "graphify-out" / "explorer.html",
        EXPLORER_INPUTS_PATH=root / "graphify-out" / "explorer-inputs.json",
        CLUSTERING_RECORD_PATH=root / "graphify-out" / "clustering-inputs.json",
        RELATIONSHIPS_DIR=root / "knowledge" / "relationships",
        RELATIONSHIPS_BY_REPO_DIR=root / "knowledge" / "relationships" / "by-repo",
        REPOSITORY_DEPENDENCIES_PATH=root
        / "knowledge"
        / "relationships"
        / "repository-dependencies.json",
        RELATIONSHIP_KINDS_PATH=root / "knowledge" / "relationships" / "kinds.json",
        RELATIONSHIPS_EXTRA_DIR=root / "knowledge" / "relationships" / "extra",
        RELATIONSHIP_KINDS_CONFIG_PATH=root / "config" / "relationship-kinds.json",
        MINIMUM_STANDARD_DIR=root / "knowledge" / "minimum-standard",
        MINIMUM_STANDARD_BY_REPO_DIR=root / "knowledge" / "minimum-standard" / "by-repo",
        MINIMUM_STANDARD_SUMMARY_PATH=root / "knowledge" / "minimum-standard" / "summary.json",
        MINIMUM_STANDARD_SETTINGS_PATH=root
        / "knowledge"
        / "minimum-standard"
        / "github-settings.json",
        PRODUCTS_CONFIG=root / "config" / "products.json",
        # A store that pins its dependencies keeps these; a store that installs
        # the library directly has neither, and check-install-docs says so.
        REQUIREMENTS_PATH=root / "requirements.txt",
        LOCK_PATH=root / "requirements.lock",
        SETTINGS_FILE_PATH=root / _SETTINGS_FILE,
    )


# Last, because the check imports a module that reads this one.
_check_values(SETTINGS_FILE_PATH, _FILE_VALUES)
