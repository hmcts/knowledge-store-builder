"""Discover the estate's repositories from GitHub into config/repositories.txt.

Discovery is driven by config/repository-filters.txt - reviewable include
prefixes, explicit includes, explicit excludes and fetch-only rules (no regex,
no fuzzy search). The full organisation repository list is fetched via the GitHub
CLI (`gh`, authenticated) and filtered locally; archived repositories are
always excluded. Output format:

    name|clone-url|default-branch

A `fetch` rule means **clone it, never extract it**. Those repositories are
written to config/repositories-external.txt instead, and the sync stage puts them
under external/ rather than repositories/. That separation is the whole mechanism:
the graph extraction pass walks repositories/, so a fetch-only repository is
unreachable from it by construction rather than by anyone remembering.

It exists for sources a store needs on disk but must not ingest wholesale - one
whose content a bespoke script takes selectively, or one held back behind a
review. Before this rule the only options were to ingest a repository or to have
no supported way to obtain it, and estates were working around that with manual
clones that drifted and that a newcomer had no way to know about.

Run:

    knowledgestore discover
"""

from __future__ import annotations

import json
import shutil
import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


from . import config


HEADER_TEMPLATE = (
    "# Generated from the {org} organisation repository listing\n"
    "# Filtered by config/repository-filters.txt (archived always excluded)\n"
    "# Format: name|clone-url|default-branch\n"
    "\n"
)

EXTERNAL_HEADER_TEMPLATE = (
    "# Generated from the {org} organisation repository listing\n"
    "# `fetch` rules in config/repository-filters.txt: cloned, NEVER extracted.\n"
    "#\n"
    "# These are not part of the estate. They are synced to external/ instead of\n"
    "# repositories/ so the graph extraction pass cannot reach them. Do not move an\n"
    "# entry into config/repositories.txt to \"fix\" a script that cannot find it -\n"
    "# that ingests the repository, which is what the rule exists to prevent.\n"
    "# Format: name|clone-url|default-branch\n"
    "\n"
)


@dataclass
class Filters:
    prefixes: list[str] = field(default_factory=list)
    includes: set[str] = field(default_factory=set)
    excludes: set[str] = field(default_factory=set)
    # Fetched, never extracted. Selected from the org listing exactly like an
    # explicit include - the clone URL and default branch are needed - but routed
    # to a separate manifest and a separate directory.
    fetch_only: set[str] = field(default_factory=set)
    # GitHub team slugs: every non-archived repository the team has is part
    # of the estate. The rule for estates defined by ownership rather than
    # naming - teams without conventions, and infrastructure estates.
    teams: list[str] = field(default_factory=list)
    # Where each selecting rule was written: (line number, "kind value"). Kept
    # so a rule that selects nothing can be named back to the operator.
    # Excludes are not tracked: one legitimately outlives the repository it
    # excluded, so an unmatched exclude is not a defect.
    origins: list[tuple[int, str]] = field(default_factory=list)

    def matches(self, name: str) -> bool:
        if name in self.excludes:
            return False
        if name in self.includes or name in self.fetch_only:
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
            filters.origins.append((line_number, f"prefix {value}"))
        elif kind == "repo":
            filters.includes.add(value)
            filters.origins.append((line_number, f"repo {value}"))
        elif kind == "fetch":
            filters.fetch_only.add(value)
            filters.origins.append((line_number, f"fetch {value}"))
        elif kind == "exclude":
            filters.excludes.add(value)
        elif kind == "team":
            filters.teams.append(value)
            filters.origins.append((line_number, f"team {value}"))
        else:
            raise ValueError(f"Unknown filter kind at {path}:{line_number}: {kind}")
    overlap = filters.fetch_only & filters.includes
    if overlap:
        raise ValueError(
            f"{path}: {', '.join(sorted(overlap))} is both `repo` and `fetch`. "
            "A repository is either part of the estate or fetched without being "
            "extracted; it cannot be both."
        )
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
                f"/orgs/{config.GITHUB_ORG}/repos?per_page=100&type=all",
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
                f"/orgs/{config.GITHUB_ORG}/teams/{slug}/repos?per_page=100",
                "--jq",
                LISTING_JQ,
            ]
        )
    )


def unmatched_rules(filters: Filters, selected: list[dict], runner=run_gh) -> list[tuple[int, str]]:
    """Selecting rules that contributed no repository, as (line number, rule).

    Rules take the whole rest of their line, so a trailing comment produces a
    rule that can never match; the same silence covers a renamed repository and
    a mistyped team slug. Excludes are deliberately not checked - a stale
    exclude is normal, not a defect.

    `runner` is the seam `discover` takes. A team rule needs the team's listing
    to tell "owns nothing we selected" from "does not exist", costing one extra
    API call per team rule; a failed call reports the rule as unmatched, which is
    the honest reading of an unreachable team.
    """
    names = {r["name"] for r in selected}

    def team_contributed(slug: str) -> bool:
        try:
            owned = list_team_repositories(slug, runner)
        except (subprocess.CalledProcessError, OSError):
            return False
        return any(r["name"] in names for r in owned)

    problems: list[tuple[int, str]] = []
    for line_number, rule in filters.origins:
        kind, _, value = rule.partition(" ")
        if kind == "prefix":
            matched = any(n.startswith(value) for n in names)
        elif kind == "repo":
            matched = value in names
        elif kind == "team":
            matched = team_contributed(value)
        else:
            continue
        if not matched:
            problems.append((line_number, rule))
    return problems


def _report_unmatched(problems: list[tuple[int, str]]) -> None:
    """Name every rule that selected nothing, with where it was written.

    Printed even when the exit code stays 0: a rule matching nothing is usually
    a typo or a rename, and the selected-count line gives no hint of it.
    """
    for line_number, rule in problems:
        print(
            f"[warn] {config.FILTERS_PATH}:{line_number}: rule `{rule}` matched no repository",
            file=sys.stderr,
        )
    if problems:
        print(
            f"[warn] {len(problems)} rule(s) selected nothing. A trailing "
            "'# comment' becomes part of the value; a renamed repository or a "
            "mistyped team slug does the same.",
            file=sys.stderr,
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


def render_config(repositories: list[dict], header: str = HEADER_TEMPLATE) -> str:
    """Render discovered repositories as the config file content."""
    lines = [
        f"{r['name']}|git@github.com:{config.GITHUB_ORG}/{r['name']}.git|{r['defaultBranch']}"
        for r in repositories
    ]
    return header.format(org=config.GITHUB_ORG) + "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, runner=run_gh) -> int:
    parser = argparse.ArgumentParser(prog="knowledgestore discover", add_help=False)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("-h", "--help", action="help")
    options = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not config.GITHUB_ORG:
        print(
            "No GitHub organisation configured. Set KSB_GITHUB_ORG to the "
            "organisation whose repositories make up your estate.",
            file=sys.stderr,
        )
        return 1
    # The gh preflight exists to give a clear error when the real CLI is absent
    # or unauthenticated. A caller that injected its own runner is not using the
    # real CLI, so checking for it would fail for the wrong reason - which is
    # exactly what happened to this function's own test in CI, where no
    # authenticated gh exists.
    if runner is run_gh:
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

    filters = read_filters(config.FILTERS_PATH)
    repositories = discover(filters, runner=runner)
    problems = unmatched_rules(filters, repositories, runner=runner)
    # A `fetch` rule wins over a prefix that would otherwise have included the
    # repository, for the same reason `exclude` does: the specific rule is the
    # deliberate one, and the cost of getting this backwards is a wholesale
    # ingestion nobody asked for.
    estate = [r for r in repositories if r["name"] not in filters.fetch_only]
    external = [r for r in repositories if r["name"] in filters.fetch_only]

    config.REPOSITORIES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    config.REPOSITORIES_CONFIG.write_text(render_config(estate), encoding="utf-8")
    print(f"Generated {config.REPOSITORIES_CONFIG}")
    # Written even when empty, so that removing the last `fetch` rule clears the
    # file rather than leaving a stale one for `sync` to act on.
    config.EXTERNAL_CONFIG.write_text(
        render_config(external, EXTERNAL_HEADER_TEMPLATE), encoding="utf-8")
    if external:
        print(f"Generated {config.EXTERNAL_CONFIG} "
              f"({len(external)} fetched but never extracted)")
    _report_unmatched(problems)
    print(
        f"Repositories selected: {len(estate)} "
        f"({len(filters.prefixes)} prefixes, {len(filters.includes)} explicit, "
        f"{len(filters.excludes)} excluded, {len(external)} fetch-only)"
    )
    # --strict is for CI, where a rule selecting nothing is a config defect.
    # Interactively it stays a warning, so a half-edited filter file still
    # produces a usable estate.
    return 1 if (options.strict and problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
