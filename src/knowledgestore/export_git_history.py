from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator


FIELD_SEPARATOR = "\x1f"
RECORD_SEPARATOR = "\x1e"
# Separates a commit's metadata fields from its --numstat block, which git
# appends after the pretty format. A commit body can contain anything else.
STAT_SEPARATOR = "\x1d"


@dataclass(frozen=True)
class RepositoryConfig:
    name: str
    clone_url: str
    default_branch: str


def run_git(repo_path: Path, *arguments: str) -> str:
    command = ["git", "-C", str(repo_path), *arguments]

    try:
        completed = subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as error:
        print(
            f"Git command failed in {repo_path}:\n  {' '.join(command)}\n{error.stderr}",
            file=sys.stderr,
        )
        raise

    return completed.stdout


def read_repository_config(path: Path) -> list[RepositoryConfig]:
    repositories: list[RepositoryConfig] = []

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        parts = [part.strip() for part in line.split("|")]

        if len(parts) != 3 or not all(parts):
            raise ValueError(
                f"Invalid repository configuration at {path}:{line_number}: {raw_line}"
            )

        repositories.append(
            RepositoryConfig(
                name=parts[0],
                clone_url=parts[1],
                default_branch=parts[2],
            )
        )

    return repositories


def escape_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").strip()


def normalise_message(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def parse_numstat(value: str) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []

    for line in value.splitlines():
        line = line.strip()

        if not line:
            continue

        parts = line.split("\t", maxsplit=2)

        if len(parts) != 3:
            continue

        additions_raw, deletions_raw, file_path = parts

        additions = int(additions_raw) if additions_raw.isdigit() else None
        deletions = int(deletions_raw) if deletions_raw.isdigit() else None

        files.append(
            {
                "path": file_path,
                "additions": additions,
                "deletions": deletions,
                "binary": additions is None or deletions is None,
            }
        )

    return files


def get_commit_files(repo_path: Path, commit_sha: str) -> list[dict[str, object]]:
    """One commit's file statistics, in its own git process.

    `get_commits` no longer calls this - it reads --numstat from its single log
    pass instead. Retained as the reference implementation that test pins the
    single-pass parsing against, and for callers wanting one commit.
    """
    output = run_git(
        repo_path,
        "show",
        "--numstat",
        "--format=",
        "--find-renames",
        "--find-copies",
        commit_sha,
    )

    return parse_numstat(output)


REACH_DEFAULT = "default"
REACH_TAG = "tag"
REACH_BRANCH = "branch"
_REF_PREFIXES = ("refs/remotes/origin/", "refs/heads/")


def branch_name(source: str) -> str:
    """The branch a `--source` ref names, without its remote or local prefix."""
    for prefix in _REF_PREFIXES:
        if source.startswith(prefix):
            return source[len(prefix) :]
    return source


def _verified(repo_path: Path, ref: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def resolve_default(repo_path: Path, default_branch: str | None) -> tuple[str | None, str | None]:
    """The ref reach is measured against, and a warning when it is not the configured one.

    The configured branch wins over `origin/HEAD`, which in some repositories names
    a ticket or version branch. A stale configuration falls back to `origin/HEAD`
    rather than classing every commit as a branch, and says so.
    """
    if default_branch:
        for ref in (f"refs/remotes/origin/{default_branch}", f"refs/heads/{default_branch}"):
            if _verified(repo_path, ref):
                return ref, None
    configured = default_branch or "(none configured)"
    if _verified(repo_path, "refs/remotes/origin/HEAD"):
        return "refs/remotes/origin/HEAD", (
            f"{repo_path.name}: default branch {configured} not found; reach measured "
            "against origin/HEAD"
        )
    return None, (
        f"{repo_path.name}: default branch {configured} not found and no origin/HEAD; "
        "every commit not reached by a tag is classed as branch"
    )


def reach_sets(repo_path: Path, ref: str | None) -> tuple[frozenset[str], frozenset[str]]:
    """SHAs reachable from the default ref, and SHAs reachable only from tags."""
    if ref is None:
        tagged = run_git(repo_path, "rev-list", "--tags")
        return frozenset(), frozenset(tagged.split())
    landed = run_git(repo_path, "rev-list", ref)
    tagged = run_git(repo_path, "rev-list", "--tags", "--not", ref)
    return frozenset(landed.split()), frozenset(tagged.split())


EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
_NULL_BLOB = "0" * 40


def _raw_writes(output: str) -> Iterator[tuple[str, str, str]]:
    """(commit, path, blob) for each file a commit wrote, from `--raw --format=%x1e%H`.

    Deletions (the null blob) and the empty blob are dropped: a deleted file
    wrote nothing, and every empty file shares one blob, so it matches unrelated
    work.
    """
    for record in output.split(RECORD_SEPARATOR):
        lines = record.strip("\n").splitlines()
        if not lines:
            continue
        sha = lines[0].strip()
        for line in lines[1:]:
            if not line.startswith(":"):
                continue
            meta, _, path = line.partition("\t")
            blob = meta.split()[3]
            if blob not in (_NULL_BLOB, EMPTY_BLOB):
                yield sha, path, blob


_RAW = ("--raw", "--no-abbrev", "--no-renames", f"--format={RECORD_SEPARATOR}%H")


def landed_blobs(
    repo_path: Path, ref: str | None, paths: frozenset[str] | None = None
) -> frozenset[tuple[str, str]]:
    """Every (path, blob) written by a commit reachable from the default ref or a tag.

    `paths` keeps only those paths: on a very large repository the unfiltered
    set runs to hundreds of MB. Paths in rename or copy notation never match,
    which leans towards proposed and is deliberate.
    """
    revisions = [ref, "--tags"] if ref else ["--tags"]
    output = run_git(repo_path, "log", *revisions, *_RAW)
    return frozenset(
        (path, blob) for _, path, blob in _raw_writes(output) if paths is None or path in paths
    )


def branch_blobs(repo_path: Path, ref: str | None) -> dict[str, list[tuple[str, str]]]:
    """For each commit reachable only from a branch, the (path, blob) pairs it wrote.

    Files of branch merge commits and paths in rename or copy notation are never
    reported, so they are never marked landed: that leans towards proposed and
    is deliberate.
    """
    excluded = [ref, "--tags"] if ref else ["--tags"]
    output = run_git(
        repo_path, "log", "--exclude=refs/remotes/*/HEAD", "--all", "--not", *excluded, *_RAW
    )
    written: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for sha, path, blob in _raw_writes(output):
        written[sha].append((path, blob))
    return written


def get_commits(repo_path: Path, default_branch: str | None = None) -> Iterable[dict[str, object]]:
    pretty_format = (
        FIELD_SEPARATOR.join(
            [
                "%H",  # commit SHA
                "%P",  # parent SHAs
                "%aI",  # author date, strict ISO 8601
                "%cI",  # committer date, strict ISO 8601
                "%an",  # author name
                "%ae",  # author email
                "%cn",  # committer name
                "%ce",  # committer email
                "%D",  # refs
                "%S",  # the ref --source reached the commit by
                "%s",  # subject
                "%b",  # body
            ]
        )
        + STAT_SEPARATOR
    )

    # --numstat in this one pass, rather than `git show --numstat` per commit:
    # that cost one process per commit, which on a large estate meant nearly
    # 200,000 of them and a stage that took over an hour while barely using a
    # core. --diff-merges=cc is what `git show` applies to a merge by default,
    # so merge commits keep the file data they had (plain `git log --numstat`
    # silently reports none). Needs git 2.31 or newer.
    output = run_git(
        repo_path,
        "log",
        "--exclude=refs/remotes/*/HEAD",
        "--all",
        "--source",
        "--topo-order",
        "--date-order",
        "--no-show-signature",
        "--numstat",
        "--find-renames",
        "--find-copies",
        "--diff-merges=cc",
        f"--pretty=format:{RECORD_SEPARATOR}{pretty_format}",
    )

    ref, _ = resolve_default(repo_path, default_branch)
    landed, tagged = reach_sets(repo_path, ref)
    written = branch_blobs(repo_path, ref)
    on_default = landed_blobs(
        repo_path, ref, frozenset(path for pairs in written.values() for path, _ in pairs)
    )

    for raw_record in output.split(RECORD_SEPARATOR):
        # Strip only newlines: Python's str.strip() treats \x1f/\x1e as
        # whitespace, which would delete the trailing field separator of
        # commits that have an empty body.
        raw_record = raw_record.strip("\n")

        if not raw_record:
            continue

        metadata, _, numstat = raw_record.partition(STAT_SEPARATOR)
        fields = metadata.split(FIELD_SEPARATOR)

        if len(fields) != 12:
            print(
                f"Skipping malformed Git record in {repo_path}: "
                f"expected 12 fields, found {len(fields)}",
                file=sys.stderr,
            )
            continue

        (
            sha,
            parents,
            author_date,
            committer_date,
            author_name,
            author_email,
            committer_name,
            committer_email,
            refs,
            source,
            subject,
            body,
        ) = fields

        files = parse_numstat(numstat)

        total_additions = sum(
            file["additions"] for file in files if isinstance(file["additions"], int)
        )
        total_deletions = sum(
            file["deletions"] for file in files if isinstance(file["deletions"], int)
        )

        record: dict[str, object] = {
            "sha": sha,
            "short_sha": sha[:12],
            "parents": parents.split() if parents else [],
            "author_date": author_date,
            "committer_date": committer_date,
            "author": {
                "name": author_name,
                "email": author_email,
            },
            "committer": {
                "name": committer_name,
                "email": committer_email,
            },
            "refs": [name.strip() for name in refs.split(",") if name.strip()],
            "subject": subject.strip(),
            "body": normalise_message(body),
            "files": files,
            "file_count": len(files),
            "additions": total_additions,
            "deletions": total_deletions,
            "is_merge": len(parents.split()) > 1,
        }
        if sha in landed:
            record["reach"] = REACH_DEFAULT
        elif sha in tagged:
            record["reach"] = REACH_TAG
        else:
            record["reach"] = REACH_BRANCH
            record["ref"] = branch_name(source.strip())
            landed_paths = {
                path for path, blob in written.get(sha, ()) if (path, blob) in on_default
            }
            for file in files:
                if file["path"] in landed_paths:
                    file["landed"] = True
        yield record


def write_ndjson(
    path: Path,
    repository: RepositoryConfig,
    commits: list[dict[str, object]],
) -> None:
    with path.open("w", encoding="utf-8") as output:
        for commit in commits:
            record = {
                "repository": repository.name,
                "repository_url": repository.clone_url,
                **commit,
            }
            output.write(json.dumps(record, ensure_ascii=False))
            output.write("\n")


def as_list(value: object) -> list:
    """A record field known to hold a list, narrowed for the type checker."""
    return value if isinstance(value, list) else []


def commit_markdown(
    repository: RepositoryConfig,
    commit: dict[str, object],
) -> str:
    author = commit["author"]
    assert isinstance(author, dict)

    files = commit["files"]
    assert isinstance(files, list)

    lines = [
        f"## {commit['short_sha']}: {escape_markdown(str(commit['subject']))}",
        "",
        f"- **Commit:** `{commit['sha']}`",
        f"- **Date:** {commit['author_date']}",
        f"- **Author:** {escape_markdown(str(author['name']))}",
        f"- **Repository:** `{repository.name}`",
        "- **Parents:** "
        + (
            ", ".join(f"`{parent}`" for parent in as_list(commit["parents"]))
            if commit["parents"]
            else "None"
        ),
        f"- **Merge commit:** {'Yes' if commit['is_merge'] else 'No'}",
        f"- **Files changed:** {commit['file_count']}",
        f"- **Lines:** +{commit['additions']} / -{commit['deletions']}",
    ]

    refs = as_list(commit["refs"])

    if refs:
        lines.append(
            "- **References:** " + ", ".join(f"`{escape_markdown(str(ref))}`" for ref in refs)
        )

    body = str(commit["body"]).strip()

    if body:
        lines.extend(
            [
                "",
                "### Commit message",
                "",
                body,
            ]
        )

    if files:
        lines.extend(
            [
                "",
                "### Changed files",
                "",
                "| File | Additions | Deletions |",
                "|---|---:|---:|",
            ]
        )

        for changed_file in files:
            additions = changed_file["additions"]
            deletions = changed_file["deletions"]

            lines.append(
                "| "
                f"`{escape_markdown(str(changed_file['path']))}`"
                " | "
                f"{additions if additions is not None else 'binary'}"
                " | "
                f"{deletions if deletions is not None else 'binary'}"
                " |"
            )

    lines.extend(["", "---", ""])

    return "\n".join(lines)


def write_year_files(
    output_dir: Path,
    repository: RepositoryConfig,
    commits: list[dict[str, object]],
) -> dict[str, int]:
    commits_by_year: dict[str, list[dict[str, object]]] = defaultdict(list)

    for commit in commits:
        timestamp = str(commit["author_date"])
        year = datetime.fromisoformat(timestamp).year
        commits_by_year[str(year)].append(commit)

    counts: dict[str, int] = {}

    for year, year_commits in sorted(
        commits_by_year.items(),
        reverse=True,
    ):
        counts[year] = len(year_commits)
        year_path = output_dir / f"{year}.md"

        with year_path.open("w", encoding="utf-8") as output:
            output.write(f"# {repository.name}: Git history for {year}\n\n")
            output.write(f"Source repository: `{repository.clone_url}`\n\n")
            output.write(
                "This file contains commit metadata and changed-file "
                "summaries. It intentionally excludes complete patches.\n\n"
            )

            for commit in year_commits:
                output.write(commit_markdown(repository, commit))

    return counts


def write_index(
    path: Path,
    repository: RepositoryConfig,
    commits: list[dict[str, object]],
    year_counts: dict[str, int],
    repo_path: Path,
) -> None:
    head_sha = run_git(repo_path, "rev-parse", "HEAD").strip()
    head_date = run_git(
        repo_path,
        "show",
        "-s",
        "--format=%cI",
        "HEAD",
    ).strip()

    authors: dict[str, int] = defaultdict(int)

    for commit in commits:
        author = commit["author"]
        assert isinstance(author, dict)
        authors[str(author["name"])] += 1

    by_reach: dict[str, int] = defaultdict(int)
    branches: set[str] = set()
    for commit in commits:
        by_reach[str(commit.get("reach", REACH_DEFAULT))] += 1
        if commit.get("ref"):
            branches.add(str(commit["ref"]))
    scope = (
        f"default branch `{repository.default_branch}`: {by_reach[REACH_DEFAULT]} commits; "
        f"tags only: {by_reach[REACH_TAG]}; "
        f"branches only: {by_reach[REACH_BRANCH]} across {len(branches)} branches"
    )

    top_authors = sorted(
        authors.items(),
        key=lambda item: (-item[1], item[0].lower()),
    )[:20]

    lines = [
        f"# Repository history: {repository.name}",
        "",
        f"- **Source:** `{repository.clone_url}`",
        f"- **Default branch:** `{repository.default_branch}`",
        f"- **Current HEAD:** `{head_sha}`",
        f"- **Current HEAD date:** {head_date}",
        f"- **Total commits:** {len(commits)}",
        f"- **History scope:** {scope}",
        "",
        "## History files",
        "",
        "| Year | Commits | Dataset |",
        "|---:|---:|---|",
    ]

    for year, count in sorted(year_counts.items(), reverse=True):
        lines.append(f"| {year} | {count} | [{year}.md]({year}.md) |")

    lines.extend(
        [
            "",
            "The complete structured dataset is available in [commits.ndjson](commits.ndjson).",
            "",
            "## Most frequent commit authors",
            "",
            "| Author | Commits |",
            "|---|---:|",
        ]
    )

    for author_name, count in top_authors:
        lines.append(f"| {escape_markdown(author_name)} | {count} |")

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Commit messages and changed-file lists provide historical "
            "context. They should not automatically be treated as current "
            "architectural truth. The checked-out source code is the source "
            "of truth for the present implementation.",
            "",
        ]
    )

    path.write_text("\n".join(lines), encoding="utf-8")


def export_repository(
    root_dir: Path,
    output_root: Path,
    repository: RepositoryConfig,
) -> None:
    repo_path = root_dir / "repositories" / repository.name

    if not (repo_path / ".git").is_dir():
        raise FileNotFoundError(f"Repository has not been cloned: {repo_path}")

    print(f"Exporting history for {repository.name}")

    output_dir = output_root / repository.name
    output_dir.mkdir(parents=True, exist_ok=True)

    _, warning = resolve_default(repo_path, repository.default_branch)
    if warning:
        print(f"  {warning}", file=sys.stderr)
    commits = list(get_commits(repo_path, repository.default_branch))

    write_ndjson(
        output_dir / "commits.ndjson",
        repository,
        commits,
    )

    year_counts = write_year_files(
        output_dir,
        repository,
        commits,
    )

    write_index(
        output_dir / "index.md",
        repository,
        commits,
        year_counts,
        repo_path,
    )

    print(f"Exported {len(commits)} commits for {repository.name}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Export Git histories as Markdown and NDJSON for Graphify.")
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Root directory of the knowledge store (default: the configured store root).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Repository configuration file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output directory for Git-history datasets.",
    )

    arguments = parser.parse_args()

    # Late import so `knowledgestore --root <store> export-history` is
    # honoured: the CLI applies --root via config.configure() before the
    # stage module loads.
    from . import config

    root_dir = arguments.root.resolve() if arguments.root else config.ROOT
    config_path = (
        arguments.config.resolve() if arguments.config else root_dir / "config" / "repositories.txt"
    )
    output_root = (
        arguments.output.resolve() if arguments.output else root_dir / "knowledge" / "git-history"
    )

    output_root.mkdir(parents=True, exist_ok=True)

    try:
        repositories = read_repository_config(config_path)

        for repository in repositories:
            export_repository(
                root_dir,
                output_root,
                repository,
            )

    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"History export failed: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
