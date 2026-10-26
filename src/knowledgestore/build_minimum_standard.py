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
the line, so nothing it finds can reach the layer. A committed key file is found by
its name among the tracked files and is never opened.

Run:

    knowledgestore minimum-standard
"""

from __future__ import annotations

import fnmatch
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

# A mailbox counts wherever it is. A URL counts only on a line about reporting, judged
# with the URLs taken out, so a build badge, or a link that merely spells "security",
# is not taken for an intake route.
# Text is split into words on whitespace and the brackets, quotes and punctuation that
# wrap an address in prose or Markdown, and each word is judged on its own: a single
# pass, where a pattern for an address searched across the text can backtrack.
WORD_BREAKS = re.compile(r"[\s<>()\[\]{},;\"'`*]+")
URL = re.compile(r"https?://\S+")
REPORTING = re.compile(r"report|security|vulnerab|disclos", re.IGNORECASE)
THREAT_MODEL_NAMES = ("threat-model", "threat_model", "threatmodel")
ROOT_PATTERNS = ("*", "/")
NO_ROOT_OWNER = "no rule names an owner for the repository root"
DEPENDENCY_CONFIGS = (
    ".github/dependabot.yml",
    ".github/dependabot.yaml",
    "renovate.json",
    ".github/renovate.json",
)
WORKFLOWS = ".github/workflows/"
CODE_SCANNERS = ("codeql-action", "github/codeql")
SECRET_SCANNERS = ("gitleaks", "trufflehog", "detect-secrets")

# A key file is reported by name alone, so one kept outside the content set, or with an
# extension the content set does not read, is still found. Matched on the file name,
# in any case; a `.pub` file is the public half and is never a finding.
KEY_FILE_RULE = "key-file"
KEY_FILE_NAMES = (
    "*.pem",
    "*.key",
    "*.priv",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_ecdsa*",
    "id_ed25519*",
)

ORG_DEFAULT = "organisation default security policy"
NOT_PUBLIC = "not public, so GitHub applies no default from it"

CLEAN_SCAN = "pattern scan found nothing; a scan cannot show absence"
NAMES_ONLY = "only secret-looking setting names were found; no committed value was confirmed"
NO_CONTENT = "no files in the content set"
FOUND_SECRETS = (
    "pattern scan of the current tree found these; values are not recorded "
    "and history is not scanned"
)
NO_THREAT_MODEL = "no threat model found; secure-by-design is not judged from code"
PATCHING = "patching timelines are not recorded in a repository"
NOT_ARCHIVED = "not archived; maintenance and a patching route are not judged from the repository"


class Refusal(Exception):
    """An input the stage will not build from; nothing is written."""


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _names_a_root_owner(text: str) -> bool:
    """Whether the rule GitHub applies to the repository root names an owner.

    A rule line is a pattern then owners; an owner is `@user`, `@org/team` or an
    email, so a token after the pattern holding `@` names one. Only a `*` or `/`
    pattern covers the root, and the last such rule wins, as it does on GitHub: a
    later root rule with no owner unassigns the root.
    """
    owned = False
    for line in text.splitlines():
        tokens = line.split("#", 1)[0].split()
        if tokens and tokens[0] in ROOT_PATTERNS:
            owned = any("@" in token for token in tokens[1:])
    return owned


def _owner(clone: Path) -> dict:
    for path in CODEOWNERS_PATHS:
        text = _read(clone / path)
        if text is None:
            continue
        if _names_a_root_owner(text):
            reason = "names an owner for the repository root"
            return minimum_standard.point("owner", "present", [{"path": path, "reason": reason}])
        return minimum_standard.point(
            "owner", "absent", [{"path": path, "reason": NO_ROOT_OWNER}], NO_ROOT_OWNER
        )
    return minimum_standard.point(
        "owner",
        "absent",
        [{"path": path, "reason": "not found"} for path in CODEOWNERS_PATHS],
        "no CODEOWNERS file names an owner",
    )


def _is_mailbox(word: str) -> bool:
    """A `mailto:` link, or `local@domain.tld`: a local part, then two or more domain
    labels of letters, digits, `-` or `_`. A handle such as `@team` has no local part."""
    if "mailto:" in word.lower():
        return True
    local, at, domain = word.partition("@")
    labels = domain.rstrip(".:").split(".")
    return (
        bool(at)
        and bool(local)
        and all(c.isalnum() or c in "._+-" for c in local)
        and len(labels) > 1
        and all(label and all(c.isalnum() or c in "-_" for c in label) for label in labels)
    )


def carries_a_contact(text: str) -> bool:
    """Whether a SECURITY.md's text names a contact. The settings stage judges an
    organisation's default policy by this same rule."""
    if any(_is_mailbox(word) for word in WORD_BREAKS.split(text)):
        return True
    return any(
        URL.search(line) and REPORTING.search(URL.sub("", line)) for line in text.splitlines()
    )


def _security_contact(clone: Path, own: dict | None, settings: dict | None) -> dict:
    for path in SECURITY_PATHS:
        text = _read(clone / path)
        if text is None:
            continue
        if carries_a_contact(text):
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
    checked = [{"path": path, "reason": "not found"} for path in SECURITY_PATHS]
    return _organisation_default(checked, own, settings)


def _organisation_default(checked: list[dict], own: dict | None, settings: dict | None) -> dict:
    """The contact point for a repository with no SECURITY.md of its own.

    GitHub shows such a repository its organisation's default, from the `.github`
    repository, so that default is judged in its place. A repository off github.com
    has no organisation to inherit from. Where nothing was read about the default,
    the point is not determinable rather than absent.
    """
    if own is None or settings is None or "organisation" not in own:
        return minimum_standard.point("security_contact", "not_determinable", checked, NO_SNAPSHOT)
    org = own["organisation"]
    if not isinstance(org, str):
        return minimum_standard.point("security_contact", "absent", checked, "no SECURITY.md")
    entries = settings.get("organisations")
    entry = entries.get(org) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return minimum_standard.point("security_contact", "not_determinable", checked, NO_SNAPSHOT)
    return _judge_default(org, entry, checked)


def _judge_default(org: str, entry: dict, checked: list[dict]) -> dict:
    """The contact point from the snapshot's record of one organisation's default.

    A default counts only from a `.github` repository the snapshot reads as public,
    because GitHub applies no other: one read as not public gives no default, and
    one whose visibility is unknown leaves the point undetermined.
    """
    if entry.get("public") is False:
        evidence = [*checked, {"path": f"{org}/.github", "reason": NOT_PUBLIC}]
        return minimum_standard.point("security_contact", "absent", evidence, "no SECURITY.md")
    policy = entry.get("default_security_policy")
    if isinstance(policy, dict) and isinstance(policy.get("path"), str):
        cited = f"{org}/.github:{policy['path']}"
        if policy.get("contact") is True and entry.get("public") is True:
            evidence = [{"path": cited, "reason": ORG_DEFAULT}]
            return minimum_standard.point("security_contact", "present", evidence)
        if policy.get("contact") is False:
            evidence = [*checked, {"path": cited, "reason": f"{ORG_DEFAULT} has no contact"}]
            return minimum_standard.point(
                "security_contact",
                "absent",
                evidence,
                "no contact in the organisation's default SECURITY.md",
            )
    if policy is None and entry.get("unreadable") == []:
        evidence = [*checked, {"path": f"{org}/.github", "reason": f"no {ORG_DEFAULT}"}]
        return minimum_standard.point("security_contact", "absent", evidence, "no SECURITY.md")
    return minimum_standard.point("security_contact", "not_determinable", checked, NO_ACCESS)


def _hits(text: str) -> tuple[str, Counter[str]]:
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


def is_key_file(path: str) -> bool:
    """Whether a tracked path is named as a private key or a keystore is."""
    name = path.rsplit("/", 1)[-1].lower()
    return not name.endswith(".pub") and any(
        fnmatch.fnmatchcase(name, pattern) for pattern in KEY_FILE_NAMES
    )


def value_shaped_rules() -> frozenset[str]:
    """The rules whose finding is a committed value, not merely a name.

    Every value shape in force (an estate's own included), the URL query-parameter
    shape built into the masker, and a key file. The one rule left out is
    `secret_mask.KEY_NAME_RULE`: it fires on a setting named for a secret, and a
    name shows no value was committed. A rule not named here is treated as a name.
    """
    shapes = {rule for rule, _ in secret_mask.rules()}
    return frozenset({*shapes, secret_mask.URL_QUERY_RULE, KEY_FILE_RULE})


def _no_secrets(clone: Path, files: list[str], tracked: list[str]) -> dict:
    # Key files are named, never opened, so none of their content can reach the layer.
    evidence: list[dict] = [
        {"path": path, "rule": KEY_FILE_RULE} for path in tracked if is_key_file(path)
    ]
    if not files and not evidence:
        return minimum_standard.point("no_secrets", "not_determinable", reason=NO_CONTENT)
    findings: set[tuple[str, int, str]] = set()
    for path in sorted(set(files)):
        text = _read(clone / path)
        if text is None:
            continue
        findings.update((path, number, rule) for number, rule in _findings(text))
    evidence.extend({"line": number, "path": path, "rule": rule} for path, number, rule in findings)
    if not evidence:
        return minimum_standard.point("no_secrets", "not_determinable", reason=CLEAN_SCAN)
    evidence.sort(key=lambda found: (found["path"], found.get("line", 0), found["rule"]))
    # A setting named for a secret is not a committed value; only a value shape is.
    if any(found["rule"] in value_shaped_rules() for found in evidence):
        return minimum_standard.point("no_secrets", "absent", evidence, FOUND_SECRETS)
    return minimum_standard.point("no_secrets", "not_determinable", evidence, NAMES_ONLY)


def _is_threat_model(path: str) -> bool:
    """A file named for a threat model, in any case and with any extension, or any
    file under `docs/threat-model*/`. A name that merely contains the words, such as
    a parser for threat models, is code rather than a threat model."""
    parts = path.lower().split("/")
    if parts[-1].split(".", 1)[0] in THREAT_MODEL_NAMES:
        return True
    return len(parts) > 2 and parts[0] == "docs" and parts[1].startswith("threat-model")


def _secure_by_design(tracked: list[str]) -> dict:
    found = [path for path in tracked if _is_threat_model(path)]
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


def _file_item(
    item: str,
    paths: list[str],
    checked: list[str],
    found: str,
    missing: str,
    when_missing: str = "absent",
) -> dict:
    """A sub-item read from the clone. `when_missing` is the state when no file is
    found: `absent` where the file is the only way to have the control, and
    `not_determinable` where GitHub can provide it without one."""
    entry: dict = {"checked": checked, "item": item, "paths": paths}
    if paths:
        return {**entry, "reason": found, "state": "present"}
    return {**entry, "reason": missing, "state": when_missing}


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


def _either(setting: dict, workflow: dict) -> str:
    """The state of a requirement two items can each meet: present if either is,
    absent only if both definitely are not, otherwise not determinable."""
    halves = (setting["state"], workflow["state"])
    if "present" in halves:
        return "present"
    if halves == ("absent", "absent"):
        return "absent"
    return "not_determinable"


def _hygiene(clone: Path, tracked: list[str], settings: dict | None) -> dict:
    protection = _setting_item(
        "branch_protection",
        "branch_protection",
        settings,
        {
            "present": ("present", "the default branch is protected"),
            "absent": ("absent", "the default branch is not protected"),
        },
    )
    # GitHub's default code-scanning setup runs without a workflow and the build cannot
    # see it, so a missing workflow does not show that code scanning is off.
    code_scanning = _file_item(
        "code_scanning_workflow",
        _workflows_naming(clone, tracked, CODE_SCANNERS),
        [WORKFLOWS],
        "code-scanning workflow found",
        "no code-scanning workflow; default setup is not visible",
        when_missing="not_determinable",
    )
    updates = _file_item(
        "dependency_updates",
        _dependency_configs(tracked),
        sorted((*DEPENDENCY_CONFIGS, ".renovaterc*")),
        "dependency update config found",
        "no dependency update config",
    )
    secret_setting = _setting_item(
        "secret_scanning_setting",
        "secret_scanning",
        settings,
        {
            "enabled": ("present", "secret scanning is enabled"),
            "disabled": ("absent", "secret scanning is disabled"),
        },
    )
    secret_workflow = _file_item(
        "secret_scanning_workflow",
        _workflows_naming(clone, tracked, SECRET_SCANNERS),
        [WORKFLOWS],
        "secret-scanning workflow found",
        "no secret-scanning workflow",
    )
    # Dependabot alerts are vulnerability scanning, not update tooling, so they are
    # their own item. The snapshot records them only as enabled or unread: GitHub
    # answers "off" and "cannot see" alike, so absent is never guessed.
    alerts = _setting_item(
        "vulnerability_alerts",
        "dependabot_alerts",
        settings,
        {"enabled": ("present", "Dependabot alerts are enabled")},
    )
    items = [protection, code_scanning, updates, secret_setting, secret_workflow, alerts]
    # Each item is reported on its own, but the setting and the workflow both scan for
    # secrets, so either one meets that requirement.
    requirements = {
        protection["state"],
        code_scanning["state"],
        updates["state"],
        _either(secret_setting, secret_workflow),
        alerts["state"],
    }
    if requirements == {"present"}:
        return minimum_standard.point("hygiene", "present", items)
    if "absent" in requirements:
        return minimum_standard.point("hygiene", "absent", items)
    return minimum_standard.point(
        "hygiene", "not_determinable", items, "a requirement could not be determined"
    )


def _unmaintained(as_of: dict, settings: dict | None) -> dict:
    last = {"commit": as_of["commit"], "date": as_of["date"], "reason": "last commit"}
    if settings is None:
        return minimum_standard.point("unmaintained", "not_determinable", [last], NO_SNAPSHOT)
    archived = settings.get("archived")
    # Archived is the safe posture the point asks for. A live repository needs an owner
    # and a patching route, which nothing in a repository shows, so it is not judged.
    if archived is True:
        evidence = [last, {"reason": "archived", "setting": "archived"}]
        return minimum_standard.point("unmaintained", "present", evidence)
    if archived is False:
        evidence = [last, {"reason": "not archived", "setting": "archived"}]
        return minimum_standard.point("unmaintained", "not_determinable", evidence, NOT_ARCHIVED)
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
        "security_contact": _security_contact(clone, own, settings),
        "no_secrets": _no_secrets(clone, files, tracked),
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


def main(run=run_git) -> int:
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
