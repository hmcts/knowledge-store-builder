"""Clone or update every configured source repository into repositories/.

Full clones are deliberate: the history export diffs every commit, so all
historical blobs are needed locally anyway. A partial clone
(--filter=blob:none) would lazily re-fetch them one commit at a time during
export, which is far slower overall.

Run:

    knowledgestore sync
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .export_git_history import RepositoryConfig, read_repository_config


from . import config

CONFIG = config.REPOSITORIES_CONFIG
REPOSITORIES = config.REPOSITORIES_DIR


def run_git(arguments: list[str]) -> str:
    """Run git and return stdout. Raises on failure."""
    completed = subprocess.run(["git", *arguments], check=True, text=True, stdout=subprocess.PIPE)
    return completed.stdout


def sync_repository(repo: RepositoryConfig, repositories_dir: Path, run=run_git) -> int:
    """Clone (if absent) then hard-sync one repository to its remote default
    branch. Returns the repository's total commit count."""
    repo_dir = repositories_dir / repo.name

    if not (repo_dir / ".git").is_dir():
        run(["clone", "--origin", "origin", repo.clone_url, str(repo_dir)])

    git = lambda *args: run(["-C", str(repo_dir), *args])  # noqa: E731
    git("remote", "set-url", "origin", repo.clone_url)
    git(
        "fetch",
        "origin",
        "--prune",
        "--prune-tags",
        "--tags",
        "+refs/heads/*:refs/remotes/origin/*",
    )

    remote_ref = f"refs/remotes/origin/{repo.default_branch}"
    try:
        git("show-ref", "--verify", "--quiet", remote_ref)
    except subprocess.CalledProcessError:
        raise RuntimeError(f"Remote branch not found: {repo.name}/{repo.default_branch}") from None

    git("checkout", "-B", repo.default_branch, f"origin/{repo.default_branch}")
    git("reset", "--hard", f"origin/{repo.default_branch}")
    # -e graphify-out: the per-repo graph lives untracked inside the clone;
    # cleaning it forces a full (expensive) re-extraction on every sync.
    git("clean", "-fd", "-e", "graphify-out")
    return int(git("rev-list", "--all", "--count").strip())


def main() -> int:
    from . import provenance

    if not CONFIG.is_file():
        print(f"Repository configuration not found: {CONFIG}", file=sys.stderr)
        return 1

    REPOSITORIES.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    failures: list[tuple[str, str]] = []
    for repo in read_repository_config(CONFIG):
        print(f"\nSynchronising {repo.name}")
        # One repository's failure must not cost the estate. Aborting here used
        # to skip every repository after it and, because provenance is written
        # after the loop, discard the record for those that had already
        # succeeded - so one unreachable remote produced nothing at all.
        try:
            count = sync_repository(repo, REPOSITORIES)
        except (subprocess.CalledProcessError, RuntimeError, OSError) as error:
            print(f"{repo.name}: FAILED - {error}", file=sys.stderr)
            failures.append((repo.name, str(error)))
            continue
        print(f"{repo.name}: {count} commits available")
        entries[repo.name] = provenance.head_info(REPOSITORIES / repo.name, repo.default_branch)
    provenance.write(entries)
    print(f"\nProvenance recorded for {len(entries)} repositories -> {provenance.PROVENANCE_PATH}")
    if failures:
        total = len(entries) + len(failures)
        print(f"\n{len(failures)} of {total} repositories failed to sync:")
        for name, error in failures:
            print(f"  {name}: {error}")
        print(
            "The rest synced and provenance covers them. A failed repository's "
            "graph and history will be missing or stale until it syncs, so this "
            "is an incomplete estate rather than a warning."
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
