"""Clone or update every configured source repository into repositories/.

Repositories selected by a `fetch` rule are handled too, but into external/ and
never into repositories/. The graph extraction pass walks repositories/, so that
split is what makes "cloned but never extracted" true by construction instead of
by convention. Nothing else about them differs - they are cloned, updated and
recorded the same way.

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


def _sync_external() -> tuple[dict[str, dict], list[tuple[str, str]]]:
    """Sync the `fetch` repositories into external/, away from the extraction pass.

    Absent configuration is normal: most estates have no fetch-only repositories,
    and `discover` only writes the file once a `fetch` rule exists.
    """
    from . import provenance

    if not config.EXTERNAL_CONFIG.is_file():
        return {}, []
    repos = list(read_repository_config(config.EXTERNAL_CONFIG))
    if not repos:
        return {}, []
    config.EXTERNAL_DIR.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    failures: list[tuple[str, str]] = []
    for repo in repos:
        print(f"\nSynchronising {repo.name} (fetch-only, not extracted)")
        try:
            count = sync_repository(repo, config.EXTERNAL_DIR)
        except (subprocess.CalledProcessError, RuntimeError, OSError) as error:
            print(f"{repo.name}: FAILED - {error}", file=sys.stderr)
            failures.append((repo.name, str(error)))
            continue
        print(f"{repo.name}: {count} commits available")
        entries[repo.name] = provenance.head_info(
            config.EXTERNAL_DIR / repo.name, repo.default_branch
        )
    return entries, failures


def main() -> int:
    from . import provenance

    if not config.REPOSITORIES_CONFIG.is_file():
        print(f"Repository configuration not found: {config.REPOSITORIES_CONFIG}", file=sys.stderr)
        return 1

    config.REPOSITORIES_DIR.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    failures: list[tuple[str, str]] = []
    for repo in read_repository_config(config.REPOSITORIES_CONFIG):
        print(f"\nSynchronising {repo.name}")
        # One repository's failure must not cost the estate. Aborting here used
        # to skip every repository after it and, because provenance is written
        # after the loop, discard the record for those that had already
        # succeeded - so one unreachable remote produced nothing at all.
        try:
            count = sync_repository(repo, config.REPOSITORIES_DIR)
        except (subprocess.CalledProcessError, RuntimeError, OSError) as error:
            print(f"{repo.name}: FAILED - {error}", file=sys.stderr)
            failures.append((repo.name, str(error)))
            continue
        print(f"{repo.name}: {count} commits available")
        entries[repo.name] = provenance.head_info(
            config.REPOSITORIES_DIR / repo.name, repo.default_branch
        )
        # Written as we go, not once at the end. A sync that is interrupted -
        # Ctrl-C, a lost connection, or simply run in stages across a session -
        # used to leave no provenance at all rather than a partial record, and
        # `status`, the manifest and the explorer all read it. A truthful partial
        # record is recoverable; nothing is silently wrong. The file is small and
        # this loop is dominated by network and git, so the cost does not signify.
        provenance.write(entries, provenance.read_external())
    external, external_failures = _sync_external()
    failures.extend(external_failures)
    provenance.write(entries, external)
    print(f"\nProvenance recorded for {len(entries)} repositories -> {config.PROVENANCE_PATH}")
    if external:
        print(f"Plus {len(external)} fetched but never extracted -> {config.EXTERNAL_DIR}")
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
