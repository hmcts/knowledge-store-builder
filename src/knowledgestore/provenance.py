"""What each repository's clone pointed at when the store was built.

Written by the sync stage, one entry per repository. This is the input every
staleness check needs: without a recorded SHA and commit date, "has the source
moved on?" cannot be answered. Dates are git commit dates, never wall-clock,
so two builds of the same clones produce identical output.
"""

from __future__ import annotations

from pathlib import Path

from . import config, io
from .sync_repositories import run_git

PROVENANCE_PATH = config.PROVENANCE_PATH


def head_info(repo_dir: Path, branch: str, run=run_git) -> dict:
    """The clone's current commit: sha, configured branch, commit date."""
    sha = run(["-C", str(repo_dir), "rev-parse", "HEAD"]).strip()
    committed = run(["-C", str(repo_dir), "log", "-1", "--format=%cI"]).strip()
    return {"sha": sha, "branch": branch, "committed": committed}


def write(entries: dict[str, dict]) -> None:
    io.write_json(
        PROVENANCE_PATH,
        {"repositories": dict(sorted(entries.items()))},
        indent=1,
    )


def read() -> dict[str, dict]:
    """Recorded provenance by repository name, or {} when never recorded."""
    data = io.read_json_dict(PROVENANCE_PATH)
    repos = data.get("repositories", {})
    return repos if isinstance(repos, dict) else {}
