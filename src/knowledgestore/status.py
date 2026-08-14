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

    _report_unsynced(recorded)

    _report_intent(recorded)

    _report_citations()

    _report_freshness()

    if arguments.drift:
        _report_drift(recorded)

    return 0
