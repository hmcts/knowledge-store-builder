"""Report how stale the store's layers are. Never fails: drift is normal.

Cheap by design — this stage must not load the graph. The corpus citation
check reads the small tracked extract, not graph.json.
"""

from __future__ import annotations

import argparse
import datetime
import shutil
import subprocess
from pathlib import Path

from . import config, io, provenance
from .build_topic_briefs import read_topics

SUMMARIES_PATH = config.SUMMARIES_PATH
SUMMARIES_INPUT_PATH = config.SUMMARIES_INPUT_PATH
TOPICS_BRIEFS_PATH = config.TOPICS_BRIEFS_PATH
TOPICS_CONFIG_PATH = config.TOPICS_CONFIG_PATH
EXPLORER_PATH = config.EXPLORER_PATH
ROOT = config.ROOT
GITHUB_ORG = config.GITHUB_ORG

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
    summaries = io.read_json_dict(SUMMARIES_PATH)
    digests = io.read_json(SUMMARIES_INPUT_PATH, default=[])
    briefs = io.read_json_dict(TOPICS_BRIEFS_PATH)
    try:
        topics = read_topics(TOPICS_CONFIG_PATH)
    except (OSError, ValueError):
        topics = []
    return {
        "summaries_written": len(summaries),
        "summaries_expected": len(digests) if isinstance(digests, list) else 0,
        "briefs_written": len(briefs),
        "topics_configured": len(topics),
    }


def corpus_citations(root: Path) -> dict:
    corpus = io.read_json_dict(root / "graphify-out" / "graph-knowledge-corpus.json")
    nodes = [n for n in corpus.get("nodes", []) if n.get("source_file")]
    dangling = sorted(n["source_file"] for n in nodes if not (root / n["source_file"]).exists())
    return {"checked": len(nodes), "dangling": dangling}


def _committed_at(path: str, run) -> str:
    try:
        return run(["-C", str(ROOT), "log", "-1", "--format=%cI", "--", path]).strip()
    except Exception:
        return ""


def _parse_iso(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def artefact_freshness(run=run_git) -> dict:
    explorer = _committed_at(
        str(EXPLORER_PATH.relative_to(ROOT))
        if EXPLORER_PATH.is_relative_to(ROOT)
        else str(EXPLORER_PATH),
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
                    f"/repos/{GITHUB_ORG}/{name}/commits?sha={branch}&since={since}&per_page=100",
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

    cites = corpus_citations(ROOT)
    if cites["dangling"]:
        print(f"Dangling corpus citations ({len(cites['dangling'])}):")
        for path in cites["dangling"][:10]:
            print(f"  - {path}")
    else:
        print(f"Corpus citations: {cites['checked']} checked, none dangling")

    fresh = artefact_freshness()
    if fresh.get("explorer_stale"):
        print(
            "Explorer page is OLDER than a layer it embeds - "
            "run `knowledgestore explorer` and commit the rebuilt page"
        )
    elif fresh:
        print("Explorer page is newer than every embedded layer")

    if arguments.drift:
        if not shutil.which("gh"):
            print("Drift: gh CLI not available - skipped")
        elif not recorded:
            print("Drift: no provenance recorded - skipped")
        else:
            drifted = source_drift()
            if drifted:
                print(f"Source drift ({len(drifted)} repositories moved on):")
                for d in drifted[:15]:
                    print(f"  {d['repo']}: {d['behind']}+ commits since the build")
            elif drifted == []:
                print("Source drift: none - every repository is at the build state")
            # drifted is None: the check failed - the diagnostic note above
            # already explained why, so nothing more to print here.

    return 0
