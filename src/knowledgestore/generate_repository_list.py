"""Discover the estate's repositories from GitHub into config/repositories.txt.

Discovery is driven by config/repository-filters.txt - reviewable include
prefixes, explicit includes and explicit excludes (no regex, no fuzzy
search). The full organisation repository list is fetched via the GitHub
CLI (`gh`, authenticated) and filtered locally; archived repositories are
always excluded. Output format:

    name|clone-url|default-branch

Run:

    knowledgestore discover
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


from . import config

FILTERS = config.FILTERS_PATH
OUTPUT = config.REPOSITORIES_CONFIG
GITHUB_ORG = config.GITHUB_ORG

HEADER_TEMPLATE = (
    "# Generated from the {org} organisation repository listing\n"
    "# Filtered by config/repository-filters.txt (archived always excluded)\n"
    "# Format: name|clone-url|default-branch\n"
    "\n"
)


@dataclass
class Filters:
    prefixes: list[str] = field(default_factory=list)
    includes: set[str] = field(default_factory=set)
    excludes: set[str] = field(default_factory=set)
    # GitHub team slugs: every non-archived repository the team has is part
    # of the estate. The rule for estates defined by ownership rather than
    # naming - teams without conventions, and infrastructure estates.
    teams: list[str] = field(default_factory=list)

    def matches(self, name: str) -> bool:
        if name in self.excludes:
            return False
        if name in self.includes:
            return True
        return any(name.startswith(prefix) for prefix in self.prefixes)


def read_filters(path: Path) -> Filters:
    filters = Filters()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"Invalid filter at {path}:{line_number}: {raw}")
        kind, value = parts
        if kind == "prefix":
            filters.prefixes.append(value)
        elif kind == "repo":
            filters.includes.add(value)
        elif kind == "exclude":
            filters.excludes.add(value)
        elif kind == "team":
            filters.teams.append(value)
        else:
            raise ValueError(f"Unknown filter kind at {path}:{line_number}: {kind}")
    if not filters.prefixes and not filters.includes and not filters.teams:
        raise ValueError(f"No include rules in {path}")
    return filters


def run_gh(arguments: list[str]) -> str:
    """Run the gh CLI and return stdout. Raises on failure."""
    completed = subprocess.run(["gh", *arguments], check=True, text=True, stdout=subprocess.PIPE)
    return completed.stdout


LISTING_JQ = ".[] | select(.archived==false) | {name, defaultBranch: .default_branch}"


def _parse_listing(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def list_organisation_repositories(runner=run_gh) -> list[dict]:
    """Every non-archived repository in the organisation (paginated)."""
    return _parse_listing(
        runner(
            [
                "api",
                "--paginate",
                f"/orgs/{GITHUB_ORG}/repos?per_page=100&type=all",
                "--jq",
                LISTING_JQ,
            ]
        )
    )


def list_team_repositories(slug: str, runner=run_gh) -> list[dict]:
    """Every non-archived repository a GitHub team has (paginated)."""
    return _parse_listing(
        runner(
            [
                "api",
                "--paginate",
                f"/orgs/{GITHUB_ORG}/teams/{slug}/repos?per_page=100",
                "--jq",
                LISTING_JQ,
            ]
        )
    )


def discover(filters: Filters, runner=run_gh) -> list[dict]:
    """Repositories selected by the filters, sorted by name.

    Name rules (prefix/repo) select from the organisation listing; team rules
    add every repository the team has. Excludes win over both, and a
    repository selected by several rules appears once.
    """
    selected: dict[str, dict] = {
        r["name"]: r for r in list_organisation_repositories(runner) if filters.matches(r["name"])
    }
    for slug in filters.teams:
        for r in list_team_repositories(slug, runner):
            if r["name"] not in filters.excludes:
                selected.setdefault(r["name"], r)
    return sorted(selected.values(), key=lambda r: r["name"])


def render_config(repositories: list[dict]) -> str:
    """Render discovered repositories as the config file content."""
    lines = [
        f"{r['name']}|git@github.com:{GITHUB_ORG}/{r['name']}.git|{r['defaultBranch']}"
        for r in repositories
    ]
    return HEADER_TEMPLATE.format(org=GITHUB_ORG) + "\n".join(lines) + "\n"


def main() -> int:
    if not GITHUB_ORG:
        print(
            "No GitHub organisation configured. Set KSB_GITHUB_ORG to the "
            "organisation whose repositories make up your estate.",
            file=sys.stderr,
        )
        return 1
    if not shutil.which("gh"):
        print("GitHub CLI is required: https://cli.github.com/", file=sys.stderr)
        return 1
    try:
        subprocess.run(
            ["gh", "auth", "status"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        print("GitHub CLI is not authenticated. Run: gh auth login", file=sys.stderr)
        return 1

    filters = read_filters(FILTERS)
    repositories = discover(filters)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render_config(repositories), encoding="utf-8")
    print(f"Generated {OUTPUT}")
    print(
        f"Repositories selected: {len(repositories)} "
        f"({len(filters.prefixes)} prefixes, {len(filters.includes)} explicit, "
        f"{len(filters.excludes)} excluded)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
