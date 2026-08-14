"""Report how stale the store's layers are. Never fails: drift is normal.

Cheap by design — this stage must not load the graph. The corpus citation
check reads the small tracked extract, not graph.json.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import config, io, provenance
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
            f"in {where(links['duplicating'])}{spread}. On a cold build the content is emitted "
            "twice under two paths; on any rebuild with a warm cache the two collide and the "
            "LINK wins, so the real file vanishes from the graph and its content is filed "
            "under the link. Exclude them either way."
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


def _report_freshness() -> None:
    fresh = artefact_freshness()
    if fresh.get("explorer_stale"):
        print(
            "Explorer page is OLDER than a layer it embeds - "
            "run `knowledgestore explorer` and commit the rebuilt page"
        )
    elif fresh:
        print("Explorer page is newer than every embedded layer")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drift",
        action="store_true",
        help="also check GitHub for commits since the build (one API call per repository)",
    )
    arguments = parser.parse_args(argv)

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

    _report_intent(recorded)

    _report_missing_extractors()

    _report_symlinks()

    _report_citations()

    _report_freshness()

    if arguments.drift:
        _report_drift(recorded)

    return 0
