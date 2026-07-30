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
        else:
            raise ValueError(f"Unknown filter kind at {path}:{line_number}: {kind}")
    if not filters.prefixes and not filters.includes:
        raise ValueError(f"No include rules in {path}")
    return filters


def run_gh(arguments: list[str]) -> str:
    """Run the gh CLI and return stdout. Raises on failure."""
    completed = subprocess.run(
        ["gh", *arguments], check=True, text=True, stdout=subprocess.PIPE
    )
    return completed.stdout


def list_organisation_repositories(runner=run_gh) -> list[dict]:
    """Every non-archived repository in the organisation (paginated)."""
    output = runner([
        "api", "--paginate", f"/orgs/{GITHUB_ORG}/repos?per_page=100&type=all",
        "--jq", '.[] | select(.archived==false) | {name, defaultBranch: .default_branch}',
    ])
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def discover(filters: Filters, runner=run_gh) -> list[dict]:
    """Repositories selected by the filters, sorted by name."""
    repositories = [
        r for r in list_organisation_repositories(runner) if filters.matches(r["name"])
    ]
    return sorted(repositories, key=lambda r: r["name"])


def render_config(repositories: list[dict]) -> str:
    """Render discovered repositories as the config file content."""
    lines = [
        f"{r['name']}|git@github.com:{GITHUB_ORG}/{r['name']}.git|{r['defaultBranch']}"
        for r in repositories
    ]
    return HEADER_TEMPLATE.format(org=GITHUB_ORG) + "\n".join(lines) + "\n"


def main() -> int:
    if not shutil.which("gh"):
        print("GitHub CLI is required: https://cli.github.com/", file=sys.stderr)
        return 1
    try:
        subprocess.run(["gh", "auth", "status"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        print("GitHub CLI is not authenticated. Run: gh auth login", file=sys.stderr)
        return 1

    filters = read_filters(FILTERS)
    repositories = discover(filters)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render_config(repositories), encoding="utf-8")
    print(f"Generated {OUTPUT}")
    print(f"Repositories selected: {len(repositories)} "
          f"({len(filters.prefixes)} prefixes, {len(filters.includes)} explicit, "
          f"{len(filters.excludes)} excluded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
