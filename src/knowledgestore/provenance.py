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


def head_info(repo_dir: Path, branch: str, run=run_git) -> dict:
    """The clone's current commit: sha, configured branch, commit date."""
    sha = run(["-C", str(repo_dir), "rev-parse", "HEAD"]).strip()
    committed = run(["-C", str(repo_dir), "log", "-1", "--format=%cI"]).strip()
    return {"sha": sha, "branch": branch, "committed": committed}


def write(entries: dict[str, dict], external: dict[str, dict] | None = None) -> None:
    """Record what each clone pointed at.

    Fetch-only repositories are recorded under a separate key, never merged into
    `repositories`: they are not part of the estate, and every count taken from
    that key - "163 repositories recorded" - would silently include sources the
    graph does not contain. They still need a recorded SHA, because a finding that
    cites one has to be able to say which commit it read.
    """
    document: dict[str, dict] = {"repositories": dict(sorted(entries.items()))}
    if external:
        document["external"] = dict(sorted(external.items()))
    io.write_json(config.PROVENANCE_PATH, document, indent=1)


def read_external() -> dict[str, dict]:
    """Recorded provenance for fetch-only repositories, or {} when there are none."""
    data = io.read_json_dict(config.PROVENANCE_PATH)
    external = data.get("external", {})
    return external if isinstance(external, dict) else {}


def read() -> dict[str, dict]:
    """Recorded provenance by repository name, or {} when never recorded."""
    data = io.read_json_dict(config.PROVENANCE_PATH)
    repos = data.get("repositories", {})
    return repos if isinstance(repos, dict) else {}
