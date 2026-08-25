"""Report how stale the store's layers are. Never fails: drift is normal.

Cheap by design — this stage must not load the graph. The corpus citation
check reads the small tracked extract, not graph.json, and the partitioner
check reads the recorded `clustering-inputs.json` rather than the clustering
sitting in the graph.

Anything that costs more than that is behind a flag and says so in its help.
`--paths` is the one that reads bulk: it streams every tracked file, a block at
a time, so a gigabyte-scale artefact costs time rather than memory — and it
still never parses the graph.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import gzip
import re
import shutil
import subprocess
import zlib
from collections.abc import Callable
from pathlib import Path

from . import boundary, config, graph_files, io, provenance, record_clustering, store_paths
from .build_topic_briefs import read_topics


# committed layers the page embeds; if any is newer than the page, rebuild.
# Derived from the config paths themselves so a new embedded layer can't be
# added to the page without this list noticing.
EMBEDDED_LAYERS = tuple(
    str(p.relative_to(config.ROOT))
    for p in (
        config.SUMMARIES_PATH,
        config.TOPICS_BRIEFS_PATH,
        config.SYNONYMS_PATH,
        config.TICKET_DESCRIPTIONS_PATH,
        config.TICKET_TITLES_PATH,
        config.DEEPDIVES_PATH,
        config.PROVENANCE_PATH,
    )
)


def run_git(arguments: list[str]) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return completed.stdout


def layer_coverage() -> dict:
    summaries = io.read_json_dict(config.SUMMARIES_PATH)
    digests = io.read_json(config.SUMMARIES_INPUT_PATH, default=[])
    briefs = io.read_json_dict(config.TOPICS_BRIEFS_PATH)
    try:
        topics = read_topics(config.TOPICS_CONFIG_PATH)
    except (OSError, ValueError):
        topics = []
    return {
        "summaries_written": len(summaries),
        "summaries_expected": len(digests) if isinstance(digests, list) else 0,
        "briefs_written": len(briefs),
        "topics_configured": len(topics),
    }


def intent_coverage(recorded: dict) -> dict:
    """How much of the estate the intent index actually covers.

    A layer can cover a fraction of the estate while every other line of this
    report reads green, and nothing said so: an operator saw "914/914 summaries"
    and "361 repositories recorded", and had to open file-tickets.json.gz by hand
    to discover the intent index held 108 of those 361. A store's most dangerous
    output is a confident negative, and "no tickets touched this file" from an
    unmined repository is exactly that - indistinguishable, in the answer, from a
    file no ticket ever touched.
    """
    index = io.read_gzip_json_dict(config.INTENT_INDEX_PATH)
    mined = {repo for repo, files in index.items() if files}
    return {"mined": len(mined), "estate": len(recorded)}


def unsynced(manifest: Path, recorded: dict) -> list[str]:
    """Repositories a manifest declares that provenance does not record.

    A manifest is intent; provenance is what actually reached disk. Nothing
    compared them, so a repository could be declared, committed, and never
    cloned - which is exactly what happened to a fetch-only entry added in the
    same commit as its neighbour. `sync` was not re-run, `sync` had nothing to
    complain about, and every later run reported success over an estate one
    repository short of its own configuration.

    Absent manifest means nothing declared, which is normal for `external`.
    """
    if not manifest.is_file():
        return []
    from .export_git_history import read_repository_config

    try:
        declared = [repo.name for repo in read_repository_config(manifest)]
    except (OSError, ValueError):
        return []
    return sorted(name for name in declared if name not in recorded)


# Content types graphify parses only when an optional extra is installed, and the
# import that proves it is present. Without it the files are read as nothing: the
# build succeeds, the graph is short, and no stage says why. Measured on one
# estate, installing the extras took genuine estate content from ~4,400 nodes to
# ~11,000 - and on another, 320 .tf files contributed exactly 0 nodes to a store
# that had been shipped and queried for weeks.
OPTIONAL_EXTRACTORS = (
    (("tf", "tfvars", "hcl"), "tree_sitter_hcl", "terraform"),
    (("sql",), "tree_sitter_sql", "sql"),
)


def _extractor_installed(module: str) -> bool:
    from importlib.util import find_spec

    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def missing_extractors(corpus: Path) -> list[dict]:
    """Content types present in the corpus that nothing installed can parse."""
    if not corpus.is_dir():
        return []
    missing = []
    for suffixes, module, extra in OPTIONAL_EXTRACTORS:
        if _extractor_installed(module):
            continue
        count = 0
        for suffix in suffixes:
            count += sum(
                1
                for path in corpus.rglob(f"*.{suffix}")
                # Regular files only. A symlink's target is almost always already
                # in the corpus, so counting both reports one file as two - it
                # inflated a real estate's Terraform count from 260 to 320.
                if ".git" not in path.parts and path.is_file() and not path.is_symlink()
            )
        if count:
            missing.append({"files": count, "extra": extra, "suffixes": suffixes})
    return missing


def extractable_suffixes() -> set[str] | None:
    """Suffixes graphify dispatches an extractor for, from its own table.

    Returns None when the table cannot be read - most often because graphify is
    simply not installed, since it is this library's optional dependency rather
    than a required one. The caller must report that as "cannot check" rather
    than "nothing found": a hand-written suffix list is exactly the enumeration
    this codebase has been caught by twice, and silently measuring nothing is
    worse than not measuring.
    """
    try:
        from graphify.extract import _DISPATCH
    except (ImportError, AttributeError):
        return None
    return {str(suffix).lower() for suffix in _DISPATCH}


def _ignore_matcher(repo_root: Path):
    """A predicate saying whether extraction would skip a path under `repo_root`.

    Uses graphify's own `_load_graphifyignore` / `_is_ignored` - the same pair
    `collect_files` uses - so this agrees with the extractor by construction
    rather than by parallel reasoning about pattern syntax. Both names are
    private; when they cannot be imported the caller is told it could not check
    rather than being told nothing is excluded.

    Anchored at the repository root, which is what per-repository extraction
    scans. A `.graphifyignore` above that level is read only when the store root
    is itself the scan root - see the placement table in the guide.
    """
    try:
        from graphify.detect import _is_ignored, _load_graphifyignore
    except (ImportError, AttributeError):
        return None
    patterns = _load_graphifyignore(repo_root)
    if not patterns:
        return lambda path: False
    cache: dict = {}
    return lambda path: bool(_is_ignored(path, repo_root, patterns, _cache=cache))


def _symlink_outcome(path: Path, root: Path, suffixes: set[str]) -> str | None:
    """Which outcome this path produces, or None when it is not a candidate.

    Broken links and targets outside the corpus are named rather than left to
    fall into misattribution by accident: they are different defects, and only
    one of them is about attribution at all.
    """
    if ".git" in path.parts or not path.is_symlink():
        return None
    if path.suffix.lower() not in suffixes:
        return None
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return "broken"
    collected = target.is_relative_to(root) and target.suffix.lower() in suffixes
    return "duplicating" if collected else "misattributing"


def _classify_symlinks(corpus: Path, suffixes: set[str]) -> dict:
    """Tally each symlink by outcome, skipping those already excluded."""
    found: dict = {
        "checked": True,
        "duplicating": {},
        "misattributing": {},
        "broken": {},
        "excluded": 0,
        "targets": 0,
        "exclusion_checked": True,
    }
    if not corpus.is_dir():
        return found
    # Walk from the resolved root so every path is in the same form as the
    # anchors graphify's ignore loader returns. Mixing the two silently matches
    # nothing - on macOS /var against /private/var was enough to report a
    # correctly excluded symlink as exposed.
    root = corpus.resolve()
    matchers: dict[str, Callable[[Path], bool] | None] = {}
    targets: set[Path] = set()
    for path in sorted(root.rglob("*")):
        outcome = _symlink_outcome(path, root, suffixes)
        if not outcome:
            continue
        repo = path.relative_to(root).parts[0]
        if repo not in matchers:
            matchers[repo] = _ignore_matcher(root / repo)
        ignored = matchers[repo]
        if ignored is None:
            found["exclusion_checked"] = False
        elif ignored(path):
            # Already mitigated. Counting it as exposed gives an operator who has
            # done the work the same message as one who has not, and the
            # instruction then reads as outstanding work forever.
            found["excluded"] += 1
            continue
        if outcome != "broken":
            with contextlib.suppress(OSError, RuntimeError):
                targets.add(path.resolve(strict=True))
        found[outcome][repo] = found[outcome].get(repo, 0) + 1
    found["targets"] = len(targets)
    return found


def duplicating_symlinks(corpus: Path, suffixes: set[str] | None = None) -> dict:
    """Symlinked corpus files, split by what extraction will actually do to them.

    Extraction records the path it walked, not the link target, and that has two
    different consequences which a single count conflates:

    - **duplication** - on a *cold* build the target is collected too, so the same
      content becomes two sets of nodes under two paths. It inflates counts and,
      where anything consolidates by label, manufactures a hub that does not exist.
    - **displacement** - on any build with a *warm* cache the two collide, because
      the extraction cache keys on the resolved path, and the link wins. The real
      file disappears from the graph and its content is filed under the link.
      Reproduced in three consecutive runs of the same extraction: run 1 yields
      two distinct `source_file` values, runs 2 and 3 yield one, and it is the
      symlink. So a corpus predicts *which files are at risk*, never which of the
      two outcomes a given build will produce - that is cache state, not corpus.
      Where several links share one target, the other links show nothing at all,
      and a reader concludes those components declare nothing.
    - **misattribution** - the target is not collected, so nothing is duplicated,
      but the graph asserts the content lives at a path that is a link and knows
      nothing about the real file. Quieter, and worse to answer questions from:
      "where is this declared?" returns the link, and asking about the real file
      returns nothing at all.

    On one estate a flat count reported 13 and implied 13 problems, when the
    honest report was 2 nodes duplicated and 57 misattributed - the misattribution
    being the larger effect, and not the one the check is named after.

    This is a **prediction from the corpus, not a measurement of a built graph**,
    because `status` must stay cheap enough to run before a rebuild and so must
    not load the graph. A target can exist on disk and still never reach the
    graph - excluded by a filter, or dropped by an extractor - which would turn a
    predicted duplication into a real misattribution. The exact split needs the
    node counts per `source_file`.
    """
    if suffixes is None:
        suffixes = extractable_suffixes()
    if suffixes is None:
        return {"checked": False}
    found = _classify_symlinks(corpus, suffixes)
    for key in ("duplicating", "misattributing", "broken"):
        found[f"{key}_files"] = sum(found[key].values())
    found["files"] = found["duplicating_files"] + found["misattributing_files"]
    return found


def corpus_citations(root: Path) -> dict:
    corpus = io.read_json_dict(root / "graphify-out" / "graph-knowledge-corpus.json")
    nodes = [n for n in corpus.get("nodes", []) if n.get("source_file")]
    dangling = sorted(n["source_file"] for n in nodes if not (root / n["source_file"]).exists())
    return {"checked": len(nodes), "dangling": dangling}


# --- absolute paths in a store's own tracked files -----------------------
# `store_paths` states the rule - relative at rest, absolute in flight - and
# nothing enforced it or even called it, so a store author had no way to discover
# it existed (#176). This is the executable half: a non-author can run it.
#
# The defect class is the silent kind. Both instances behind that rule passed
# every check a maintainer would naturally run: entry counts reconciled, the JSON
# stayed well-formed, and every path in it existed on disk. Only the next
# relocation showed anything, and by then the rewrite was the cost.

# A slash-led path of at least two segments. Deliberately not "a string starting
# with /": that reads API routes, XPath expressions and URL paths as filesystem
# paths, and a check whose first run is mostly false positives is one nobody runs
# twice. The lookbehind is what keeps `https://host/a/b` out - the character
# before a genuine candidate is never a word character, a dot, a dash or another
# slash - and `[\w.+@~-]` excludes `*`, so a glob like `**/*.py` is not a path.
ABSOLUTE_PATH = re.compile(r"(?<![\w./-])/[\w.+@~-]+(?:/[\w.+@~-]+)+")

# A store's largest tracked artefacts run to gigabytes decompressed, so the scan
# streams. READ_BLOCK is the read size; CARRY is the tail held back between
# blocks so a path straddling the boundary is matched whole. CARRY has to exceed
# any plausible path length, because under-sizing it loses findings and a count
# cannot show you what it failed to see.
READ_BLOCK = 1 << 20
CARRY = 4096


def in_flight_artefacts() -> dict[str, str]:
    """Tracked files that hold absolute paths by contract, and the reason each does.

    The rule has an intended exception, and an operator who learns to ignore one
    line of a report stops reading the whole report - so the exceptions are named
    with their justification rather than left to be recognised.

    graphify's extraction spec requires the FILE_LIST handed to an agent to be
    absolute "verbatim", and `.graphify_uncached.txt` *is* that list, so
    relativising it would break the contract `store_paths` exists to keep.
    `.graphify_detect.json` is graphify's own cache in graphify's format; this
    library prepares its inputs and does not own its shape.

    Derived from `config` rather than hard-coded strings so a store that moves
    either file keeps the exemption.
    """
    return {
        _relative(config.UNCACHED_PATH): (
            "graphify's FILE_LIST, which its extraction spec requires to be absolute verbatim"
        ),
        _relative(config.DETECT_PATH): "graphify's own detection cache, in graphify's format",
    }


def _is_store_path_at_rest(candidate: str) -> bool:
    """Whether `store_paths` would have written this absolute path relative.

    Decided by asking `store_paths.relative` rather than by re-deriving the rule,
    so the check agrees with the utility by construction. The alternative - "any
    absolute path" - is a neighbour of the quantity being claimed rather than the
    quantity itself: it makes `/etc/hosts` and `/api/v1/things` findings, and a
    check that reports those gets switched off the first time it runs.
    """
    return store_paths.relative(candidate) != candidate


def _count_in(text: str, end: int) -> tuple[int, int]:
    """Store paths in `text` finishing by `end`, and the offset to resume from.

    Two numbers because they are one decision. A match running past `end` may be a
    path the block boundary cut in half, so it is not counted here - and what the
    next pass has to start from is that match's *start*, not `end`. Resuming later
    than the match began chops its head off, the lookbehind then refuses what is
    left, and the path is counted by neither pass.

    Measured while building this: 11,997 of 12,000 absolute paths in a 2.4 MB
    artefact, the three lost being the ones that happened to span the hold-back
    point. A count cannot show you what it failed to see, which is why the
    reconciliation against the fixture is the check and the count is not.
    """
    found = 0
    resume = max(0, end)
    for match in ABSOLUTE_PATH.finditer(text):
        if match.end() > end:
            resume = min(resume, match.start())
            break
        if _is_store_path_at_rest(match.group()):
            found += 1
    return found, resume


def _open_text(path: Path):
    """A text stream over a tracked file, transparently gunzipping `.gz`.

    Undecodable bytes are replaced rather than raised: a store commits gzipped
    JSON and archives beside its prose, and one bad byte is not a reason to stop
    reading the file it sits in.
    """
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


def scan_file(path: Path) -> tuple[int, int]:
    """(store paths written absolute, characters read) for one tracked file."""
    found = 0
    read = 0
    carry = ""
    with _open_text(path) as stream:
        while True:
            block = stream.read(READ_BLOCK)
            if not block:
                break
            read += len(block)
            text = carry + block
            counted, resume = _count_in(text, len(text) - CARRY)
            found += counted
            # One character before the resume point, so a path starting exactly
            # there still has the preceding character the lookbehind reads.
            carry = text[max(0, resume - 1) :]
    return found + _count_in(carry, len(carry))[0], read


def absolute_paths_at_rest(run=run_git) -> dict:
    """Store paths written absolute in the store's own tracked files, by file.

    Scoped to what the store *tracks*: untracked build intermediates are
    regenerated on the next run and their shape is nobody's business.

    Reads files, never the graph, so the module docstring's rule holds - but it
    reads every tracked file including the compressed ones, which is why the
    caller opts in.

    `checked` is False when the tracked files could not be listed at all. That is
    "cannot check", not "nothing found", and the two must never print the same.
    """
    try:
        listing = run(["-C", str(config.ROOT), "ls-files", "-z"])
    except (subprocess.CalledProcessError, OSError):
        return {"checked": False}
    tracked = sorted(name for name in listing.split("\0") if name)
    exempt = in_flight_artefacts()
    scan = {
        "checked": True,
        "listed": len(tracked),
        "files": 0,
        "characters": 0,
        "findings": {},
        "in_flight": {},
        "unreadable": [],
    }
    for name in tracked:
        try:
            found, read = scan_file(config.ROOT / name)
        # Three unrelated hierarchies, and each has to be here. `gzip.BadGzipFile`
        # needs no mention because it is an OSError; a *truncated* archive raises
        # EOFError and a corrupt deflate body raises `zlib.error`, and neither is.
        # Measured, not reasoned about: a valid gzip header over random bytes
        # raises `zlib.error`, which would have taken a stage that must never fail
        # down over one bad file.
        except (OSError, EOFError, zlib.error):
            scan["unreadable"].append(name)
            continue
        scan["files"] += 1
        scan["characters"] += read
        if found:
            bucket = "in_flight" if name in exempt else "findings"
            scan[bucket][name] = found
    return scan


def _report_absolute_paths(enabled: bool) -> None:
    """Print the absolute-paths-at-rest section, or nothing when not asked for.

    Opt-in like `--drift` and `--central`, because it reads every tracked file.
    Reports and never refuses: whether a given tracked file may hold absolute
    paths is a judgement about that artefact's contract, and the exemptions this
    stage knows about are the two it can name.
    """
    if not enabled:
        return
    scan = absolute_paths_at_rest()
    if not scan["checked"]:
        print(
            "Absolute paths at rest: could not be checked - `git ls-files` failed, so the "
            "store's own tracked files could not be listed. Not a clean result."
        )
        return
    if not scan["files"]:
        # "0 files read, none found" pairs a measurement of nothing with a clean
        # verdict and reads as a pass - the same shape as the corpus-citation
        # check's "0 checked, none dangling", which was reported and fixed.
        print(
            f"Absolute paths at rest: nothing checked - none of the {scan['listed']} tracked "
            "file(s) could be read."
        )
    elif scan["findings"]:
        print(_findings_line(scan))
    else:
        print(
            f"Absolute paths at rest: none in {scan['files']} tracked file(s) read "
            f"({scan['characters']:,} characters)."
        )
    exempt = in_flight_artefacts()
    for name, count in sorted(scan.get("in_flight", {}).items()):
        print(f"  ({name} holds {count:,}, by contract: {exempt[name]}.)")
    if scan.get("unreadable"):
        shown = ", ".join(scan["unreadable"][:3])
        print(
            f"  {len(scan['unreadable'])} tracked file(s) could not be read, so nothing here "
            f"is claimed about them - {shown}."
        )


def _findings_line(scan: dict) -> str:
    """The finding, with the files named. A count alone cannot be acted on.

    Ordered by count and then by name, never by count alone: a dict ordered only
    by score reorders between processes on ties, and two runs of this stage on one
    store must read identically.
    """
    ranked = sorted(scan["findings"].items(), key=lambda kv: (-kv[1], kv[0]))
    named = ", ".join(f"{name} ({count:,})" for name, count in ranked[:5])
    more = f" and {len(ranked) - 5} more" if len(ranked) > 5 else ""
    return (
        f"Absolute paths at rest: {sum(scan['findings'].values()):,} in {len(ranked)} of "
        f"{scan['files']} tracked file(s) read - {named}{more}. A store's own files should "
        "hold paths relative to the store root (`knowledgestore.store_paths`): an absolute "
        "one records the build machine's directory layout in a published artefact and stops "
        "naming a real file the moment the store moves."
    )


def _committed_at(path: str, run) -> str:
    try:
        return run(["-C", str(config.ROOT), "log", "-1", "--format=%cI", "--", path]).strip()
    except Exception:
        return ""


def _parse_iso(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def artefact_freshness(run=run_git) -> dict:
    explorer = _committed_at(
        str(config.EXPLORER_PATH.relative_to(config.ROOT))
        if config.EXPLORER_PATH.is_relative_to(config.ROOT)
        else str(config.EXPLORER_PATH),
        run,
    )
    if not explorer:
        return {}
    # Compare timestamps chronologically, not lexicographically: ISO-8601
    # strings with different UTC offsets do not sort the same way their
    # instants do (mirrors build_explorer.latest_synced).
    explorer_dt = _parse_iso(explorer)
    newest_layer = ""
    newest_dt = None
    for layer in EMBEDDED_LAYERS:
        committed = _committed_at(layer, run)
        dt = _parse_iso(committed) if committed else None
        if dt is None:
            continue
        if newest_dt is None or dt > newest_dt:
            newest_dt = dt
            newest_layer = committed
    return {
        "explorer_committed": explorer,
        "layers_committed": newest_layer,
        "explorer_stale": bool(newest_dt and explorer_dt and newest_dt > explorer_dt),
    }


def run_gh(arguments: list[str]) -> str:
    """Run a gh CLI command and return its output."""
    completed = subprocess.run(["gh", *arguments], check=True, text=True, stdout=subprocess.PIPE)
    return completed.stdout


def source_drift(runner=run_gh) -> list[dict] | None:
    """Repositories with commits on their branch since the recorded date.

    One API call per repository - the caller opts in. Counts cap at 100
    (one page); "100" therefore means "at least 100". Returns an empty list
    when the check ran cleanly and found no drift. Returns None - not an
    empty list - when the check could not run at all (gh unavailable or
    unauthenticated), printing a diagnostic note; callers must not treat
    None as "no drift".
    """
    drifted = []
    for name, entry in provenance.read().items():
        since = entry.get("committed", "")
        branch = entry.get("branch", "")
        if not since:
            continue
        try:
            raw = runner(
                [
                    "api",
                    f"/repos/{config.GITHUB_ORG}/{name}/commits?sha={branch}&since={since}&per_page=100",
                    "--jq",
                    "length",
                ]
            )
        except (subprocess.CalledProcessError, OSError):
            print("Drift: gh call failed (not authenticated?) - skipped")
            return None
        behind = int(raw.strip() or 0)
        if behind:
            drifted.append({"repo": name, "behind": behind})
    return sorted(drifted, key=lambda d: (-d["behind"], d["repo"]))


# Two simple patterns rather than one spanning both figures. A single pattern
# needs `[\d,]+` next to `\s+`, which is ambiguous enough to backtrack
# super-linearly on input that never matches - and this reads a file whose
# format is graphify's to change.
GRAPH_NODES = re.compile(r"(\d[\d,]*) nodes")
GRAPH_EDGES = re.compile(r"(\d[\d,]*) edges")


def _figure(pattern: re.Pattern, text: str) -> int | None:
    found = pattern.search(text)
    return int(found.group(1).replace(",", "")) if found else None


def graph_report_claims() -> dict:
    """What GRAPH_REPORT.md says it describes, and whether it predates the graph.

    The query skill deliberately carries no counts, so figures cannot rot inside
    it, and routes every "how big is the graph" question to this file. That makes
    the file the single point where the anti-stale-numbers design can fail - and
    it did: one store's report claimed 809,441 nodes beside a graph holding
    779,551, a 29,890-node disagreement in the same directory with nothing
    reporting it. The reader believes they are checking the source, which is what
    makes it worse than a README nobody re-derived.

    Staleness is decided on commit dates rather than by counting the graph,
    because this stage must stay cheap enough to run any time - see the module
    docstring. `--verify-graph` opts into the exact comparison.
    """
    report = config.GRAPH_REPORT_PATH
    if not report.is_file():
        return {"present": False}
    text = report.read_text(encoding="utf-8", errors="replace")[:4000]
    claims = {
        "present": True,
        "nodes": _figure(GRAPH_NODES, text),
        "edges": _figure(GRAPH_EDGES, text),
    }
    graph = config.GRAPH_PATH.with_suffix(".json.gz")
    if not graph.is_file():
        graph = config.GRAPH_PATH
    report_at = _parse_iso(_committed_at(_relative(report), run_git))
    graph_at = _parse_iso(_committed_at(_relative(graph), run_git))
    claims["stale"] = bool(report_at and graph_at and report_at < graph_at)
    return claims


def _relative(path: Path) -> str:
    return str(path.relative_to(config.ROOT)) if path.is_relative_to(config.ROOT) else str(path)


# Keys a node can carry and still be nothing: its identity, and where the merge
# filed it. Anything else - a label, a file, a type - makes it a real node.
IDENTITY_KEYS = frozenset({"id", "local_id", "repo", "_origin", "community", "community_name"})


def contentless_nodes(nodes: list) -> dict:
    """Nodes carrying identity and nothing else, by repository.

    `merge-graphs` composes with networkx, which creates a node for any id an
    edge mentions. A per-repository graph can legitimately hold an edge whose
    endpoint is absent from its own node list, so those endpoints arrive as
    identity and nothing more. One estate carried 94,899 of them, 15% of its
    merged layer, and graphify warns at build time and then builds anyway.

    They defeat the obvious guards: they pass a dangling-endpoint check, because
    after the merge they genuinely are nodes; and `prefix_graph_for_global` sets
    `repo` on everything it touches, so they satisfy a repository filter and
    survive an estate cut. A node with no label and no source file cannot be
    cited, explained or attributed - it is mass in clustering and a blank row
    anywhere it is listed.

    Identity-only, deliberately, rather than "missing a label or a source file".
    The looser rule reads well and is wrong: on the maintainer's own estate
    99,828 nodes lack a `source_file` while carrying a label, a normalised label
    and a file type, and every one of them is legitimate. A check that reported
    those would be dismissed the first time it ran.
    """
    by_repo: dict[str, int] = {}
    for node in nodes:
        carried = {key for key, value in node.items() if value not in (None, "", [], {})}
        if carried and carried <= IDENTITY_KEYS:
            repo = node.get("repo") or "(no repository)"
            by_repo[repo] = by_repo.get(repo, 0) + 1
    return by_repo


def _report_contentless(nodes: list) -> None:
    found = contentless_nodes(nodes)
    total = sum(found.values())
    if not total:
        print(f"Contentless nodes: none of {len(nodes):,}.")
        return
    where = ", ".join(
        f"{repo} ({n})" for repo, n in sorted(found.items(), key=lambda kv: -kv[1])[:4]
    )
    print(
        f"Contentless nodes: {total:,} of {len(nodes):,} ({100 * total / len(nodes):.1f}%) carry "
        f"identity and nothing else - {where}. `merge-graphs` creates a node for any id an edge "
        "mentions, so an edge endpoint missing from its own graph becomes one of these. They "
        "cannot be cited, explained or attributed, and they pass both a dangling-endpoint check "
        "and a repository filter."
    )


def _report_graph_report(verify: bool) -> None:
    claims = graph_report_claims()
    if not claims["present"]:
        return
    if claims["stale"]:
        described = f"describes {claims['nodes']:,} nodes; " if claims["nodes"] else ""
        print(
            f"GRAPH_REPORT.md is older than the graph beside it - it {described}"
            "the query skill treats this file as authoritative for graph size, so a stale "
            "one is quoted as fact. Regenerate it, or do not cite it."
        )
    if not verify or claims["nodes"] is None:
        return
    # Opt-in, like --drift: exact, and it loads the whole graph to be so.
    nodes = io.load_graph(config.GRAPH_PATH).get("nodes", [])
    _report_contentless(nodes)
    actual = len(nodes)
    if actual == claims["nodes"]:
        print(f"GRAPH_REPORT.md agrees with the graph: {actual:,} nodes.")
    else:
        print(
            f"GRAPH_REPORT.md describes {claims['nodes']:,} nodes; the graph has {actual:,} "
            f"- a {abs(actual - claims['nodes']):,} disagreement in the same directory."
        )


def _report_drift(recorded: dict) -> None:
    """Print the --drift section: gh availability, provenance, then results."""
    if not shutil.which("gh"):
        print("Drift: gh CLI not available - skipped")
        return
    if not recorded:
        print("Drift: no provenance recorded - skipped")
        return
    drifted = source_drift()
    if drifted:
        print(f"Source drift ({len(drifted)} repositories moved on):")
        for d in drifted[:15]:
            print(f"  {d['repo']}: {d['behind']}+ commits since the build")
    elif drifted == []:
        print("Source drift: none - every repository is at the build state")
    # drifted is None: the check failed - the diagnostic note above already
    # explained why, so nothing more to print here.


# One reporter per check, rather than one main() that accretes a block per
# check. main() is then the running order, which is the thing worth reading at
# a glance - and each report is reachable from a test without driving the CLI.
def _report_unsynced(recorded: dict) -> None:
    external_recorded = provenance.read_external()
    for manifest, have, label in (
        (config.REPOSITORIES_CONFIG, recorded, "repositories"),
        (config.EXTERNAL_CONFIG, external_recorded, "fetch-only repositories"),
    ):
        pending = unsynced(manifest, have)
        if pending:
            shown = ", ".join(pending[:5]) + (
                f" and {len(pending) - 5} more" if len(pending) > 5 else ""
            )
            print(
                f"Declared but never synced: {len(pending)} of {len(have) + len(pending)} "
                f"{label} - {shown}. Run `knowledgestore sync`."
            )


def _report_boundary(recorded: dict) -> None:
    """What the estate says lies outside it, and where that disagrees with disk.

    A store's most dangerous output is a confident negative, and "no evidence of
    X" is one whenever the reader cannot tell it from "no evidence of X in the
    repositories this store holds". Nothing said which of the two it was. This
    reports the declaration if there is one and its absence if there is not,
    because an undeclared boundary is the case where the reader is most exposed.

    Never raises. `status` never returns non-zero either, so an unparseable
    declaration is reported as the defect it is rather than taking the stage
    down - the manifest build is where it stops a store being published.
    """
    try:
        declared = boundary.read()
    except (OSError, ValueError) as error:
        print(f"Estate boundary: {_relative(config.BOUNDARY_PATH)} cannot be read - {error}")
        return
    if declared is None:
        print(
            "Estate boundary: not declared. Every 'no evidence of X' this store reports "
            "means 'no evidence in the "
            + boundary.plural(len(recorded), "repository", "repositories")
            + " provenance records' - write "
            + _relative(config.BOUNDARY_PATH)
            + " to say which hosts were searched and to rule the repositories left out."
        )
        return
    print(boundary.summary_line(declared))
    disagreements = boundary.reconciliation(declared, set(recorded))
    for key, message in (
        (
            "active_absent",
            "declared active and not held here, so a question about one is answered as "
            "though it did not exist",
        ),
        (
            "ruled_out_held",
            "held here and ruled not-used or decommissioned, so the graph carries them "
            "and answers cite them as current",
        ),
        (
            "alias_absent",
            "named as the estate side of an alias and not held here, so the alias resolves "
            "to nothing and a ruling written under the other name lands nowhere",
        ),
    ):
        names = disagreements[key]
        if names:
            shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
            counted = boundary.plural(len(names), "repository", "repositories")
            print(f"Boundary: {counted} {message} - {shown}.")


def _report_failed_syncs(recorded: dict) -> None:
    """Repositories whose record survives only because their last sync failed.

    Retaining the record keeps the manifest and provenance agreeing on
    membership, which is what lets a reconciliation see the repository at all.
    But a retained record is a stale one, and without this it would be reported
    nowhere: `unsynced` asks who is missing from provenance, and this repository
    is no longer missing. Trading a visible gap for an invisible staleness would
    be the worse bargain.
    """
    stale = sorted(name for name, entry in recorded.items() if entry.get("sync_failed"))
    if not stale:
        return
    shown = ", ".join(stale[:5]) + (f" and {len(stale) - 5} more" if len(stale) > 5 else "")
    print(
        f"Stale provenance: {len(stale)} of {len(recorded)} repositories kept their previous "
        f"commit because the last sync failed - {shown}. Their graph and history describe an "
        "older state of the source. Re-run `knowledgestore sync`."
    )


def _report_intent(recorded: dict) -> None:
    intent = intent_coverage(recorded)
    if intent["estate"]:
        share = 100 * intent["mined"] // intent["estate"]
        line = f"Intent index: {intent['mined']}/{intent['estate']} repositories mined ({share}%)"
        # Partial coverage is normal - history export is expensive and some
        # estates mine a subset deliberately. Silence about it is not: an answer
        # of "no tickets" from an unmined repository looks identical to one from
        # a repository with no tickets.
        print(line if share >= 95 else f"{line} - answers about the rest carry no ticket evidence")
    elif intent["mined"]:
        print(f"Intent index: {intent['mined']} repositories mined")


def _report_missing_extractors() -> None:
    for gap in missing_extractors(config.REPOSITORIES_DIR):
        kinds = ", ".join(f".{s}" for s in gap["suffixes"])
        print(
            f"Extractor missing: {gap['files']} {kinds} file(s) in the corpus and nothing "
            f"installed can parse them - they contribute nothing to the graph. "
            f"Install with `pip install 'graphifyy[{gap['extra']}]'` and rebuild."
        )


def _report_symlink_context(links: dict) -> None:
    """Exclusions already in force, and the limits of the prediction."""
    if links["excluded"] and not links["files"]:
        # Saying nothing here would be worse than it looks: an operator who has
        # mitigated cannot tell a working exclusion from a check that stopped
        # running. The number is the evidence that it is still being enforced.
        print(
            f"Symlinked source files: {links['excluded']} excluded by .graphifyignore, "
            "none exposed."
        )
    elif links["excluded"]:
        print(f"  ({links['excluded']} further symlink(s) already excluded by .graphifyignore.)")
    if not links["exclusion_checked"]:
        print(
            "  Exclusions could not be read, so an already-excluded symlink is counted here "
            "as exposed."
        )
    if links["files"] or links["broken_files"]:
        print(
            "  Predicted from the corpus, not measured from a graph, and which outcome you "
            "get depends on cache state rather than on anything in the corpus."
        )


def _report_symlinks() -> None:
    links = duplicating_symlinks(config.REPOSITORIES_DIR)
    if not links["checked"]:
        print(
            "Symlink check skipped: graphify is not installed, so the set of file "
            "types it would extract - and therefore which symlinks would be "
            "extracted twice - cannot be determined."
        )
        return

    def where(by_repo: dict) -> str:
        # One repository needs no breakdown - "60 in repo-a (60)" repeats itself.
        pairs = sorted(by_repo.items())
        return pairs[0][0] if len(pairs) == 1 else ", ".join(f"{r} ({n})" for r, n in pairs)

    if links["duplicating_files"]:
        # Several links usually share one target, so the count of links overstates
        # the files at risk and understates how badly each is repeated: on one
        # estate 60 links resolved to 12 targets - not 60 files duplicated once,
        # but 12 files appearing six times each.
        spread = f" resolving to {links['targets']} distinct target(s)" if links["targets"] else ""
        print(
            f"Symlinked source files, target inside the corpus: {links['duplicating_files']} "
            f"in {where(links['duplicating'])}{spread}. Extraction records the path it walked, "
            "not the link target, so the real file can be displaced by the link and vanish from "
            "the graph - measured on one estate as 11 of 12 shared files lost on a COLD build, "
            "not only on a warm rebuild. Exclude them before extracting."
        )
    if links["misattributing_files"]:
        print(
            f"Symlinked source files, target outside the corpus: "
            f"{links['misattributing_files']} in {where(links['misattributing'])}. Nothing is "
            "duplicated, but the content is recorded at a path that is a link and the real "
            "file is absent - so asking about it returns nothing."
        )
    if links["broken_files"]:
        print(
            f"Broken symlinks: {links['broken_files']} in {where(links['broken'])}. "
            "They resolve to nothing and contribute nothing."
        )
    _report_symlink_context(links)


def _report_citations() -> None:
    cites = corpus_citations(config.ROOT)
    if cites["dangling"]:
        print(f"Dangling corpus citations ({len(cites['dangling'])}):")
        for path in cites["dangling"][:10]:
            print(f"  - {path}")
    elif cites["checked"]:
        print(f"Corpus citations: {cites['checked']} checked, none dangling")
    else:
        # "0 checked, none dangling" paired a measurement of nothing with a clean
        # verdict, and read as a pass. Nothing checked is not the same as nothing wrong.
        print("Corpus citations: none checked - no committed prose cites the corpus yet")


def recorded_digests() -> dict[str, str]:
    """What the page was built from, from either manifest shape.

    The current shape is a list of `{path, hash}` records; stores built before
    that hold an object keyed by path. Both are read, because the alternative is
    that a store's existing page silently becomes unjudgeable at upgrade - and
    "cannot be judged" is a state this check deliberately distinguishes from
    "agrees", so corrupting it would be worse than a hard failure.

    Accepting both is not permanent. The keyed shape can go once every store has
    rebuilt a page, which `status` will say has happened when no store reports it.
    """
    # Values are NOT coerced to str. `layer_digests` records an absent layer as a
    # non-string sentinel rather than skipping it - so a layer that disappears
    # between builds is a change rather than a silence - and coercing turned every
    # absent layer into a permanent false drift: seven of them, on a fixture with
    # one real layer.
    raw = io.read_json(config.EXPLORER_INPUTS_PATH, default=None)
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        return {
            entry["path"]: entry["hash"]
            for entry in raw
            if isinstance(entry, dict) and "path" in entry and "hash" in entry
        }
    return {}


def embedded_layer_drift() -> list[str]:
    """Layers whose content differs from what the committed page was built from.

    The timestamp check this replaces could not see the ordinary case: a
    regenerated layer and an unrebuilt page committed together have identical
    commit dates, so the page reads as current while embedding the previous
    build. An uncommitted layer edit moves no date at all.

    Returns the layers that changed. An empty list with a recorded manifest means
    the page really was built from what sits beside it; no manifest at all means
    the page predates this check and cannot be judged, which the caller
    distinguishes rather than reporting as agreement.
    """
    recorded = recorded_digests()
    if not recorded:
        return []
    current = io.layer_digests([config.ROOT / layer for layer in EMBEDDED_LAYERS], config.ROOT)
    return sorted(name for name, digest in current.items() if recorded.get(name) != digest)


def _report_freshness() -> None:
    if config.EXPLORER_INPUTS_PATH.is_file():
        drifted = embedded_layer_drift()
        if drifted:
            shown = ", ".join(drifted[:4]) + (
                f" and {len(drifted) - 4} more" if len(drifted) > 4 else ""
            )
            print(
                f"Explorer page was built from different content than {len(drifted)} of the "
                f"layer(s) beside it now hold - {shown}. Run `knowledgestore explorer` and "
                "commit the rebuilt page."
            )
        else:
            print("Explorer page was built from the layers now beside it.")
        return

    fresh = artefact_freshness()
    if fresh.get("explorer_stale"):
        print(
            "Explorer page is OLDER than a layer it embeds - "
            "run `knowledgestore explorer` and commit the rebuilt page"
        )
    elif fresh:
        # Dates only, because no build has recorded content digests yet. This is
        # the weaker claim and says so: it cannot see a layer regenerated and
        # committed alongside a page that was never rebuilt.
        print(
            "Explorer page is not older than any embedded layer by commit date "
            "(rebuild it once to record what it was built from, which is the "
            "question this cannot answer)"
        )


def _seed_clause() -> str:
    """What the record says about hash randomisation when it clustered.

    Three states, because the matching-partitioner message previously read the
    same whether or not the seed was pinned - so a store that clustered unseeded
    was indistinguishable from one that did not. The community count cannot stand
    in for this: a re-cluster can return an identical count with different
    membership, measured on one estate as 4 of 12 unstable communities.
    """
    recorded = io.read_json_dict(config.CLUSTERING_RECORD_PATH)
    if "hash_randomised" not in recorded:
        return (
            "Whether hashes were pinned when it clustered is not recorded, so reproducibility is "
            "unknown - a matching partitioner is necessary and not sufficient, since reproducing "
            "the ids also needs PYTHONHASHSEED=0. Re-run `knowledgestore record-clustering` to "
            "capture it."
        )
    if recorded["hash_randomised"]:
        return (
            "But hash randomisation was ON when it clustered, so those communities are NOT "
            "reproducible even here - re-cluster with PYTHONHASHSEED=0 before relying on the ids."
        )
    if record_clustering.hash_randomisation():
        return (
            "Hashes were pinned when it clustered, but NOT in this process - reproducing the ids "
            "needs PYTHONHASHSEED=0 here too."
        )
    return "Hashes were pinned when it clustered and in this process, so the ids reproduce."


def partitioner_verdict(recorded: str | None, here: str | None, how: str) -> str:
    """What to say about the partitioner, given the record and this environment.

    Four states, and the reason they are four rather than two: an unrecorded
    partitioner is *unknown*, which must never be printed as agreement. Community
    ids key every summary, so a store clustered with Leiden and re-clustered with
    Louvain loses its prose wholesale - and `summaries remap` reports that as
    churn, indistinguishable in its output from a corpus that really did move.

    Every message names the file it consulted. Each silent failure of this kind
    has come from a check that could not say what it read.
    """
    record = _relative(config.CLUSTERING_RECORD_PATH)
    if here is None:
        return (
            f"Clustering partitioner: this environment's is undeterminable - {how}. graphify "
            "catches ImportError and falls back to Louvain; it does not catch this, so it would "
            f"fail here rather than cluster, and nothing can be concluded about {record}."
        )
    mine = record_clustering.PARTITIONER_NAMES[here]
    if recorded is None:
        return (
            f"Clustering partitioner: not recorded - {record} names none, so nothing says which "
            f"partitioner produced the communities in the graph beside it. This environment has "
            f"{mine}; whether it matches is unknown, not agreed. Run "
            "`knowledgestore record-clustering` in the environment that clusters."
        )
    theirs = record_clustering.PARTITIONER_NAMES[recorded]
    if recorded == here:
        return (
            f"Clustering partitioner: {record} records {theirs}, and this environment has the "
            "same partitioner, so a re-cluster here starts from the algorithm that built the "
            f"committed communities. {_seed_clause()}"
        )
    return (
        f"Clustering partitioner: {record} records {theirs}; this environment has {mine}, so a "
        "re-cluster here will NOT reproduce those communities - the ids move, and `summaries "
        "remap` reports retention loss with no cause in the corpus. Match the recorded "
        "partitioner (graspologic installed for Leiden, absent for Louvain), or expect to "
        "re-author the prose the remap drops."
    )


def _report_clustering() -> None:
    """Print the partitioner verdict.

    Reads the recorded artefact rather than the graph, per the module docstring.
    The one cost is the availability probe: on a machine that *has* graspologic,
    importing it takes a second or two. That is the price of deciding by the same
    import graphify decides by, rather than inferring from something cheaper that
    can disagree with it.
    """
    here, how = record_clustering.available_partitioner()
    print(partitioner_verdict(record_clustering.recorded_partitioner(), here, how))


def _report_central(enabled: bool) -> None:
    """What dominates the graph, for a person to judge rather than a rule.

    Opt-in because it streams the whole graph. Reports and never refuses: whether
    a name belongs in an estate is a judgement about provenance, and a stage that
    guessed would exclude an estate's own declarations as readily as a vendored
    bundle.
    """
    if not enabled:
        return
    if not config.GRAPH_PATH.is_file():
        print(f"Most connected: no graph at {config.GRAPH_PATH}")
        return
    ranked = graph_files.most_connected(config.GRAPH_PATH)
    if not ranked:
        print("Most connected: the graph holds no edges")
        return
    print("Most connected nodes (would you name these if asked what the estate is built from?):")
    for node_id, label, degree in ranked:
        print(f"  {degree:>7,}  {label or node_id}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-graph",
        action="store_true",
        help="also load the graph to compare its size against GRAPH_REPORT.md (slow)",
    )
    parser.add_argument(
        "--central",
        action="store_true",
        help="also report the most connected nodes, to show what dominates the graph (slow)",
    )
    parser.add_argument(
        "--drift",
        action="store_true",
        help="also check GitHub for commits since the build (one API call per repository)",
    )
    parser.add_argument(
        "--paths",
        action="store_true",
        help="also report absolute paths in the store's own tracked files (reads every one)",
    )
    arguments = parser.parse_args(argv)

    _report_central(arguments.central)
    recorded = provenance.read()
    print(
        f"Provenance: {len(recorded)} repositories recorded"
        if recorded
        else "Provenance: none recorded - run `knowledgestore sync` to record it"
    )

    cov = layer_coverage()
    print(
        f"Summaries: {cov['summaries_written']}/{cov['summaries_expected']} "
        f"significant communities have prose"
    )
    print(
        f"Topic briefs: {cov['briefs_written']} written, "
        f"{cov['topics_configured']} topics configured"
    )

    # Estate completeness first (what is declared but absent), then what was
    # mined from it, then what cannot be parsed or would be counted twice.
    _report_unsynced(recorded)

    _report_boundary(recorded)

    _report_failed_syncs(recorded)

    _report_intent(recorded)

    _report_missing_extractors()

    _report_symlinks()

    _report_citations()

    _report_absolute_paths(arguments.paths)

    _report_freshness()

    _report_clustering()

    _report_graph_report(arguments.verify_graph)

    if arguments.drift:
        _report_drift(recorded)

    return 0
