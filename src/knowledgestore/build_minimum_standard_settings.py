"""Snapshot the GitHub settings the build's token can read, for minimum-standard.

Some of the standard's evidence lives only in GitHub: whether the default branch is
protected, whether secret scanning is on, whether the repository is archived. This
stage reads those through the `gh` the build already uses, and writes them with the
time they were read to `knowledge/minimum-standard/github-settings.json`. The
evidence stage reads that file and nothing on the network, so it stays deterministic
and runs offline.

A setting the token cannot read is recorded as `null` and named in `unreadable`,
never as a default. Whether a repository is unprotected and whether the token was
refused look alike from outside, and the layer must not turn the second into the
first.

Run:

    knowledgestore minimum-standard-settings
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import quote

from . import config, minimum_standard
from .build_store_manifest import write_atomically
from .export_git_history import read_repository_config
from .generate_repository_list import run_gh
from .source_urls import github_origin

FIELDS = (
    "archived",
    "branch_protection",
    "default_branch",
    "dependabot_alerts",
    "secret_scanning",
)


def _unread() -> dict:
    return {field: None for field in FIELDS}


def _call(runner, path: str) -> dict | None:
    """The JSON object a `gh api` call returned, `{}` for an empty body (a 204), or
    None when the CLI failed or answered with something else.

    A failure carries no meaning beyond "not read": GitHub answers 404 for a setting
    that is off and for one the token cannot see, so no caller reads a status in it.
    """
    try:
        text = runner(["api", path])
    except (subprocess.CalledProcessError, OSError):
        return None
    try:
        body = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None


def _enabled(analysis: object, key: str) -> str | None:
    """ "enabled" or "disabled" when GitHub reported a status for `key`, else None."""
    entry = analysis.get(key) if isinstance(analysis, dict) else None
    status = entry.get("status") if isinstance(entry, dict) else None
    return status if status in ("enabled", "disabled") else None


def read_repository(runner, org: str, name: str) -> dict:
    """One repository's settings, with what could not be read named in `unreadable`."""
    record = _unread()
    body = _call(runner, f"repos/{org}/{name}")
    if body is not None:
        archived = body.get("archived")
        record["archived"] = archived if isinstance(archived, bool) else None
        branch = body.get("default_branch")
        record["default_branch"] = branch if isinstance(branch, str) and branch else None
        analysis = body.get("security_and_analysis")
        record["secret_scanning"] = _enabled(analysis, "secret_scanning")
    # Dependabot alerts have their own endpoint: 204 (an empty body) when on. A 404 is
    # GitHub's answer both for "off" and for "you cannot see this", so any failure is
    # unreadable and `disabled` is never guessed from it.
    alerts = _call(runner, f"repos/{org}/{name}/vulnerability-alerts")
    if alerts is not None:
        record["dependabot_alerts"] = "enabled"
    # The branches API, not the classic protection endpoint: its `protected` field
    # counts rulesets as well as classic rules, and the classic endpoint answers "not
    # protected" for a branch a ruleset protects. Anything but a boolean is unread.
    branch = record["default_branch"]
    if branch is not None:
        found = _call(runner, f"repos/{org}/{name}/branches/{quote(branch, safe='/')}")
        protected = found.get("protected") if found is not None else None
        if isinstance(protected, bool):
            record["branch_protection"] = "present" if protected else "absent"
    record["unreadable"] = sorted(field for field in FIELDS if record[field] is None)
    return record


def main(argv: list[str] | None = None, runner=run_gh) -> int:
    if not config.REPOSITORIES_CONFIG.is_file():
        print(
            f"{config.REPOSITORIES_CONFIG} does not exist; run `knowledgestore discover` first.",
            file=sys.stderr,
        )
        return 1
    repositories: dict[str, dict] = {}
    for entry in read_repository_config(config.REPOSITORIES_CONFIG):
        origin = github_origin(entry.clone_url)
        if origin is None:
            # Not on github.com: there is nothing to ask, and nothing to assume.
            record = _unread()
            record["unreadable"] = sorted(FIELDS)
        else:
            record = read_repository(runner, *origin)
        repositories[entry.name] = record
    snapshot = {
        "format": minimum_standard.format_stamp(),
        "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repositories": repositories,
    }
    write_atomically(
        config.MINIMUM_STANDARD_SETTINGS_PATH, json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    )
    unreadable = sum(1 for record in repositories.values() if record["unreadable"])
    print(
        f"Read settings for {len(repositories)} repositories; "
        f"{unreadable} had settings the token could not read."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
