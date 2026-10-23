"""Record evidence against the GOV.UK minimum standard for each repository.

For every clone the store holds, this writes `knowledge/minimum-standard/by-repo/
<repo>.json`: the seven points of the standard for publicly accessible systems, each
`present`, `absent` or `not_determinable`, with the paths or settings it cites. A
`summary.json` beside them holds one row of states per repository, the products a
store declares in `config/products.json`, and what the layer cannot see.

It reads three things and nothing on the network: the clones, the content-file list
and the snapshot `minimum-standard-settings` wrote. So the same inputs give the same
bytes, and a setting nobody could read stays `not_determinable` rather than becoming
a finding against the team.

No secret value is written. The scan reports a rule, a path and a line number, never
the line, so nothing it finds can reach the layer.

Run:

    knowledgestore minimum-standard
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from . import config, minimum_standard, secret_mask
from .build_store_manifest import write_atomically
from .content_set import CORPUS
from .export_git_history import run_git

NOTES = ("archived repositories are not in the store",)

NO_ACCESS = "no access"
NO_SNAPSHOT = "no settings snapshot"

# GitHub's recognised locations, in the order GitHub reads them: the first found is
# the one GitHub uses, so it is the one judged.
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
SECURITY_PATHS = (".github/SECURITY.md", "SECURITY.md", "docs/SECURITY.md")

CONTACT = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|https?://\S+")
THREAT_MODEL_NAMES = ("threat-model", "threat_model", "threatmodel")
DEPENDENCY_CONFIGS = (
    ".github/dependabot.yml",
    ".github/dependabot.yaml",
    "renovate.json",
    ".github/renovate.json",
)
WORKFLOWS = ".github/workflows/"
CODE_SCANNERS = ("codeql-action", "github/codeql")
SECRET_SCANNERS = ("gitleaks", "trufflehog", "detect-secrets")

CLEAN_SCAN = "pattern scan found nothing; a scan cannot show absence"
NO_CONTENT = "no files in the content set"
FOUND_SECRETS = (
    "pattern scan of the current tree found these; values are not recorded "
    "and history is not scanned"
)
NO_THREAT_MODEL = "no threat model found; secure-by-design is not judged from code"
PATCHING = "patching timelines are not recorded in a repository"


class Refusal(Exception):
    """An input the stage will not build from; nothing is written."""


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _names_an_owner(text: str) -> bool:
    """A rule line is a pattern then owners; an owner is `@user`, `@org/team` or an
    email, so any token after the pattern holding `@` names one."""
    for line in text.splitlines():
        tokens = line.split("#", 1)[0].split()
        if len(tokens) > 1 and any("@" in token for token in tokens[1:]):
            return True
    return False


def _owner(clone: Path) -> dict:
    for path in CODEOWNERS_PATHS:
        text = _read(clone / path)
        if text is None:
            continue
        if _names_an_owner(text):
            return minimum_standard.point(
                "owner", "present", [{"path": path, "reason": "names an owner"}]
            )
        return minimum_standard.point(
            "owner",
            "absent",
            [{"path": path, "reason": "no rule names an owner"}],
            "no CODEOWNERS file names an owner",
        )
    return minimum_standard.point(
        "owner",
        "absent",
        [{"path": path, "reason": "not found"} for path in CODEOWNERS_PATHS],
        "no CODEOWNERS file names an owner",
    )


def _security_contact(clone: Path) -> dict:
    for path in SECURITY_PATHS:
        text = _read(clone / path)
        if text is None:
            continue
        if CONTACT.search(text):
            reason = "carries a contact; whether it is monitored is not recorded"
            return minimum_standard.point(
                "security_contact", "present", [{"path": path, "reason": reason}]
            )
        return minimum_standard.point(
            "security_contact",
            "absent",
            [{"path": path, "reason": "no contact"}],
            "no contact in SECURITY.md",
        )
    return minimum_standard.point(
        "security_contact",
        "absent",
        [{"path": path, "reason": "not found"} for path in SECURITY_PATHS],
        "no SECURITY.md",
    )


def _hits(text: str, counts: Counter[str] | None = None) -> tuple[str, Counter[str]]:
    """`text` masked, and the rules that masked something in it.

    `mask` leaves a rule it tried at zero, so only a positive count is a hit.
    """
    tally: Counter[str] = Counter()
    masked = secret_mask.mask(text, tally)
    return masked, Counter({rule: hits for rule, hits in tally.items() if hits > 0})


def _findings(text: str) -> set[tuple[int, str]]:
    """(line number, rule) for every line masking changes. No line is kept.

    The whole file is masked, not each line, because the structural rules need the
    lines around a value: a private-key block, a Kubernetes Secret's `data:`, a
    `password: |` block scalar. Masking keeps every line's number, so a line that
    differs after masking is a line that held a value. Its rule is the one a pass
    over that line alone names; where the line alone is not enough, it is the rules
    the file pass counted beyond what the single lines explain.
    """
    masked, counted = _hits(text)
    if masked == text:
        return set()
    lines = zip(text.split("\n"), masked.split("\n"))
    changed = {
        number: _hits(before)[1]
        for number, (before, after) in enumerate(lines, start=1)
        if before != after
    }
    explained: Counter[str] = Counter()
    for rules in changed.values():
        explained.update(rules)
    structural = sorted(rule for rule in counted if counted[rule] > explained[rule])
    fallback = structural or sorted(counted)
    # A line masked only by a structural rule (its neighbours made it a value) takes the
    # file's counted rules: a rule that masked this file, not necessarily the one on this line.
    return {
        (number, rule) for number, rules in changed.items() for rule in (sorted(rules) or fallback)
    }


def _no_secrets(clone: Path, files: list[str]) -> dict:
    if not files:
        return minimum_standard.point("no_secrets", "not_determinable", reason=NO_CONTENT)
    findings: set[tuple[str, int, str]] = set()
    for path in sorted(set(files)):
        text = _read(clone / path)
        if text is None:
            continue
        findings.update((path, number, rule) for number, rule in _findings(text))
    if not findings:
        return minimum_standard.point("no_secrets", "not_determinable", reason=CLEAN_SCAN)
    evidence = [
        {"line": number, "path": path, "rule": rule} for path, number, rule in sorted(findings)
    ]
    return minimum_standard.point("no_secrets", "absent", evidence, FOUND_SECRETS)


def _secure_by_design(tracked: list[str]) -> dict:
    found = [
        path
        for path in tracked
        if any(name in path.lower() for name in THREAT_MODEL_NAMES)
        or path.startswith("docs/security/")
    ]
    if not found:
        return minimum_standard.point(
            "secure_by_design", "not_determinable", reason=NO_THREAT_MODEL
        )
    return minimum_standard.point(
        "secure_by_design",
        "present",
        [{"path": path, "reason": "threat model"} for path in sorted(found)],
    )


def _workflows_naming(clone: Path, tracked: list[str], needles: tuple[str, ...]) -> list[str]:
    found = []
    for path in tracked:
        if not path.startswith(WORKFLOWS) or not path.endswith((".yml", ".yaml")):
            continue
        text = (_read(clone / path) or "").lower()
        if any(needle in text for needle in needles):
            found.append(path)
    return sorted(found)


def _file_item(item: str, paths: list[str], checked: list[str], found: str, missing: str) -> dict:
    entry: dict = {"checked": checked, "item": item, "paths": paths}
    if paths:
        return {**entry, "reason": found, "state": "present"}
    return {**entry, "reason": missing, "state": "absent"}


def _setting_item(item: str, field: str, settings: dict | None, readings: dict) -> dict:
    """A sub-item read from the snapshot. `readings` maps each value the snapshot can
    hold to a state and reason; anything else, `null` included, was not read."""
    entry: dict = {"item": item, "setting": field}
    if settings is None:
        return {**entry, "reason": NO_SNAPSHOT, "state": "not_determinable"}
    value = settings.get(field)
    # A value of the wrong type was not read either; it must not be looked up.
    value = value if isinstance(value, str) else None
    state, reason = readings.get(value, ("not_determinable", NO_ACCESS))
    return {**entry, "reason": reason, "state": state}


def _dependency_configs(tracked: list[str]) -> list[str]:
    return sorted(
        path
        for path in tracked
        if path in DEPENDENCY_CONFIGS or ("/" not in path and path.startswith(".renovaterc"))
    )


def _hygiene(clone: Path, tracked: list[str], settings: dict | None) -> dict:
    items = [
        _setting_item(
            "branch_protection",
            "branch_protection",
            settings,
            {
                "present": ("present", "the default branch is protected"),
                "absent": ("absent", "the default branch is not protected"),
            },
        ),
        _file_item(
            "code_scanning",
            _workflows_naming(clone, tracked, CODE_SCANNERS),
            [WORKFLOWS],
            "code-scanning workflow found",
            "no code-scanning workflow",
        ),
        _file_item(
            "dependency_updates",
            _dependency_configs(tracked),
            sorted((*DEPENDENCY_CONFIGS, ".renovaterc*")),
            "dependency update config found",
            "no dependency update config",
        ),
        _setting_item(
            "secret_scanning_setting",
            "secret_scanning",
            settings,
            {
                "enabled": ("present", "secret scanning is enabled"),
                "disabled": ("absent", "secret scanning is disabled"),
            },
        ),
        _file_item(
            "secret_scanning_workflow",
            _workflows_naming(clone, tracked, SECRET_SCANNERS),
            [WORKFLOWS],
            "secret-scanning workflow found",
            "no secret-scanning workflow",
        ),
        # Dependabot alerts are vulnerability scanning, not update tooling, so they
        # are their own item. The snapshot records them only as enabled or unread:
        # GitHub answers "off" and "cannot see" alike, so absent is never guessed.
        _setting_item(
            "vulnerability_alerts",
            "dependabot_alerts",
            settings,
            {"enabled": ("present", "Dependabot alerts are enabled")},
        ),
    ]
    states = {item["state"] for item in items}
    if states == {"present"}:
        return minimum_standard.point("hygiene", "present", items)
    if "absent" in states:
        return minimum_standard.point("hygiene", "absent", items)
    return minimum_standard.point(
        "hygiene", "not_determinable", items, "an item could not be determined"
    )


def _unmaintained(as_of: dict, settings: dict | None) -> dict:
    last = {"commit": as_of["commit"], "date": as_of["date"], "reason": "last commit"}
    if settings is None:
        return minimum_standard.point("unmaintained", "not_determinable", [last], NO_SNAPSHOT)
    archived = settings.get("archived")
    if archived is True:
        evidence = [last, {"reason": "archived", "setting": "archived"}]
        return minimum_standard.point("unmaintained", "absent", evidence, "archived")
    if archived is False:
        evidence = [last, {"reason": "not archived", "setting": "archived"}]
        return minimum_standard.point("unmaintained", "present", evidence)
    return minimum_standard.point("unmaintained", "not_determinable", [last], NO_ACCESS)


def evidence_for(clone: Path, files: list[str], settings: dict | None, run=run_git) -> dict:
    """The record for one clone.

    `files` are the clone's content-set paths, repository-relative. `settings` is the
    whole snapshot, or None when there is none; the clone's own entry is looked up by
    its directory name, and a clone the snapshot does not hold is treated as no
    snapshot, because nothing was read for it.
    """
    head = run(clone, "log", "-1", "--format=%H%x1f%cI").strip("\n")
    commit, _, date = head.partition("\x1f")
    as_of = {"commit": commit, "date": date}
    tracked = sorted(path for path in run(clone, "ls-files", "-z").split("\0") if path)
    entries = settings.get("repositories") if isinstance(settings, dict) else None
    entry = entries.get(clone.name) if isinstance(entries, dict) else None
    own = entry if isinstance(entry, dict) else None
    points = {
        "owner": _owner(clone),
        "security_contact": _security_contact(clone),
        "no_secrets": _no_secrets(clone, files),
        "secure_by_design": _secure_by_design(tracked),
        "hygiene": _hygiene(clone, tracked, own),
        "patching": minimum_standard.point("patching", "not_determinable", reason=PATCHING),
        "unmaintained": _unmaintained(as_of, own),
    }
    read_at = settings.get("read_at") if own is not None and settings is not None else None
    return {
        "as_of": as_of,
        "format": minimum_standard.format_stamp(),
        "points": points,
        "repository": clone.name,
        "settings_read_at": read_at,
    }


def _content_by_repository() -> dict[str, list[str]]:
    """The content-file list, grouped by repository and made repository-relative."""
    text = _read(config.CONTENT_FILES_PATH)
    if text is None:
        raise Refusal(
            f"{config.CONTENT_FILES_PATH} does not exist; run `knowledgestore content-set` "
            "first. Without it the secret scan has no files and would read as clean."
        )
    grouped: dict[str, list[str]] = {}
    for line in text.split("\n"):
        parts = line.split("/", 2)
        if len(parts) == 3 and parts[0] == CORPUS and parts[2]:
            grouped.setdefault(parts[1], []).append(parts[2])
    return grouped


def _snapshot() -> dict | None:
    path = config.MINIMUM_STANDARD_SETTINGS_PATH
    if not path.exists():
        return None
    if not path.is_file():
        raise Refusal(f"{path} is not a file; remove it or run `minimum-standard-settings`")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Refusal(f"Cannot read {path}: {error}") from error
    problem = minimum_standard.format_problem(body)
    if problem:
        raise Refusal(f"Cannot read {path}: {problem}")
    return body


def _products(held: set[str]) -> dict[str, list[str]] | None:
    path = config.PRODUCTS_CONFIG
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Refusal(f"Cannot read {path}: {error}") from error
    if not isinstance(body, dict) or not all(
        isinstance(repos, list) and all(isinstance(r, str) for r in repos)
        for repos in body.values()
    ):
        raise Refusal(f"{path} must map each product name to a list of repository names")
    unknown = sorted({repo for repos in body.values() for repo in repos} - held)
    if unknown:
        raise Refusal(f"{path} names repositories the store does not hold: {', '.join(unknown)}")
    return {product: sorted(set(repos)) for product, repos in sorted(body.items())}


def _record(clone: Path, files: list[str], snapshot: dict | None, run) -> dict:
    try:
        return evidence_for(clone, files, snapshot, run)
    except subprocess.CalledProcessError as error:
        raise Refusal(f"git could not read the clone {clone.name} ({clone})") from error


def _dump(body: dict) -> str:
    return json.dumps(body, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None, run=run_git) -> int:
    if not config.REPOSITORIES_DIR.is_dir():
        print(
            f"No clones under {config.REPOSITORIES_DIR} - run `knowledgestore sync` first",
            file=sys.stderr,
        )
        return 1
    clones = sorted(d for d in config.REPOSITORIES_DIR.iterdir() if (d / ".git").is_dir())
    try:
        content = _content_by_repository()
        snapshot = _snapshot()
        products = _products({clone.name for clone in clones})
        records = [_record(clone, content.get(clone.name, []), snapshot, run) for clone in clones]
    except Refusal as error:
        print(f"Nothing written: {error}", file=sys.stderr)
        return 1

    held = {record["repository"] for record in records}
    by_repo = config.MINIMUM_STANDARD_BY_REPO_DIR
    if by_repo.is_dir():
        # A record for a clone the store no longer holds would claim evidence for it.
        for stale in sorted(by_repo.glob("*.json")):
            if stale.stem not in held:
                stale.unlink()
    for record in records:
        write_atomically(by_repo / f"{record['repository']}.json", _dump(record))

    summary: dict = {
        "format": minimum_standard.format_stamp(),
        "notes": sorted(NOTES),
        "repositories": {
            record["repository"]: {
                "as_of": record["as_of"],
                "points": {key: value["state"] for key, value in record["points"].items()},
            }
            for record in records
        },
        "settings_read_at": snapshot.get("read_at") if snapshot is not None else None,
    }
    if products is not None:
        summary["products"] = products
    write_atomically(config.MINIMUM_STANDARD_SUMMARY_PATH, _dump(summary))

    tally = Counter(value["state"] for record in records for value in record["points"].values())
    print(
        f"Recorded the minimum standard for {len(records)} repositories: "
        + ", ".join(f"{tally[state]} {state}" for state in minimum_standard.STATES)
        + f" -> {config.MINIMUM_STANDARD_DIR}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
