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

Each organisation's default security policy is read once, under `organisations`:
GitHub applies a SECURITY.md in an organisation's public `.github` repository to
every repository there that has none of its own, so a repository can have a
security contact that its clone does not show. A private or internal `.github`
repository supplies no default, so its files are not read.

Run:

    knowledgestore minimum-standard-settings
"""

from __future__ import annotations

import base64
import binascii
import json
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import quote

from . import config, minimum_standard
from .build_minimum_standard import SECURITY_PATHS, carries_a_contact
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
    return dict.fromkeys(FIELDS)


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


DEFAULT_POLICY = "default_security_policy"


def _not_found(error: subprocess.CalledProcessError) -> bool:
    """Whether `gh api` failed with a 404. It exits 1 and prints GitHub's JSON error
    on stdout, whose `status` is the HTTP code as a string."""
    try:
        body = json.loads(error.output) if isinstance(error.output, str) else None
    except json.JSONDecodeError:
        return False
    return isinstance(body, dict) and body.get("status") == "404"


def _file_text(text: str) -> str | None:
    """A contents-API answer's file, decoded, or None when it is not a file inline.

    GitHub sends a file's bytes base64-encoded; a directory comes back as a list, and
    a file too large to inline carries an encoding of `none` and no content."""
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict) or body.get("encoding") != "base64":
        return None
    content = body.get("content")
    if not isinstance(content, str) or not content:
        return None
    try:
        raw = base64.b64decode("".join(content.split()), validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw.decode("utf-8", errors="replace")


def _is_public(body: object) -> bool | None:
    """Whether GitHub reports a repository public: False when `visibility` says
    private or internal or `private` is true, True when either says public, and None
    when the body says neither."""
    if not isinstance(body, dict):
        return None
    visibility, private = body.get("visibility"), body.get("private")
    if visibility in ("private", "internal") or private is True:
        return False
    if visibility == "public" or private is False:
        return True
    return None


def _visibility(runner, org: str) -> tuple[bool | None, bool]:
    """Whether `<org>/.github` is public, and whether that was read.

    A 404 counts as read: there is no `.github` repository the token can see, and a
    private one hidden from the token supplies no default either. Any other failure,
    or a body naming no visibility, is not read.
    """
    try:
        text = runner(["api", f"repos/{org}/.github"])
    except subprocess.CalledProcessError as error:
        return None, _not_found(error)
    except OSError:
        return None, False
    try:
        public = _is_public(json.loads(text))
    except json.JSONDecodeError:
        public = None
    return public, public is not None


def read_organisation(runner, org: str) -> dict:
    """The organisation's default security policy, and whether its `.github`
    repository is public.

    GitHub applies default community health files only from a public `.github`, so
    the files of one that is not public are never read: it has no default. For a
    public one, see `_read_default`.
    """
    public, read = _visibility(runner, org)
    if not read:
        return {DEFAULT_POLICY: None, "public": None, "unreadable": [DEFAULT_POLICY]}
    if public is not True:
        return {DEFAULT_POLICY: None, "public": public, "unreadable": []}
    return {**_read_default(runner, org), "public": True}


def _read_default(runner, org: str) -> dict:
    """The default SECURITY.md in a public `.github` repository: the first found, in
    the order the evidence stage reads a repository's own, and whether it carries a
    contact. Only the path and that judgement are kept, never the text.

    A 404 is the one answer that shows a file is not there. Any other failure, before
    a file is found, leaves the default unknown, because the file not read may be the
    one GitHub applies; it is recorded as unreadable, never as "no default".
    """
    unread = {DEFAULT_POLICY: None, "unreadable": [DEFAULT_POLICY]}
    for path in SECURITY_PATHS:
        try:
            text = runner(["api", f"repos/{org}/.github/contents/{path}"])
        except subprocess.CalledProcessError as error:
            if _not_found(error):
                continue
            return unread
        except OSError:
            return unread
        policy = _file_text(text)
        if policy is None:
            return unread
        found = {"contact": carries_a_contact(policy), "path": path}
        return {DEFAULT_POLICY: found, "unreadable": []}
    return {DEFAULT_POLICY: None, "unreadable": []}


def main(runner=run_gh) -> int:
    if not config.REPOSITORIES_CONFIG.is_file():
        print(
            f"{config.REPOSITORIES_CONFIG} does not exist; run `knowledgestore discover` first.",
            file=sys.stderr,
        )
        return 1
    repositories: dict[str, dict] = {}
    organisations: set[str] = set()
    for entry in read_repository_config(config.REPOSITORIES_CONFIG):
        origin = github_origin(entry.clone_url)
        if origin is None:
            # Not on github.com: there is nothing to ask, and nothing to assume.
            record = _unread()
            record["unreadable"] = sorted(FIELDS)
        else:
            record = read_repository(runner, *origin)
            organisations.add(origin[0])
        # `None` off github.com, where no organisation's default can apply.
        record["organisation"] = origin[0] if origin is not None else None
        repositories[entry.name] = record
    snapshot = {
        "format": minimum_standard.format_stamp(),
        "organisations": {org: read_organisation(runner, org) for org in sorted(organisations)},
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
