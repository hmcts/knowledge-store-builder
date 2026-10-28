"""Make the fixture's minimum-standard records with knowledge-store-builder's own
writer, and check them against the literals `build.py` holds (DESIGN.md 11).

    uv run python tests/fixture_store/generate_minimum_standard.py <library src>

`<library src>` is the `src/` directory of a knowledge-store-builder checkout that
has the minimum-standard stage. The library is not a dependency of this repository
(see `tests/library_store/generate.py` for why), so this is run by hand and its
output transcribed; CI never runs it. It writes three synthetic clones to a
temporary directory, runs `build_minimum_standard.evidence_for` over each, and
supplies git's two answers - the last commit, and the tracked files - through the
writer's own `run=` seam, so each `as_of` is the commit DESIGN.md 1 pins.

The settings snapshot the writer reads is made the same way: the settings stage's
own `read_repository` and `read_organisation` run over canned `gh api` answers
(`GITHUB`), through the `runner` seam. Only `read_at` is taken from `build.py`,
because the stage stamps it from the clock. The script exits 0 when the records
and the snapshot equal `build.py`'s, and otherwise prints what differs so it can
be transcribed.

The clones' files and the `gh api` answers are the only hand-written parts. The
secret-scan finding comes from a made-up value in `config/application.yml`; the
writer records its rule, path and line, and the value reaches no file of the
fixture.
"""

import importlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fixture_store.build import (  # noqa: E402
    MINIMUM_STANDARD_RECORDS,
    MINIMUM_STANDARD_SETTINGS,
    ORG,
    REPOSITORIES,
)

# The repositories the snapshot holds; reporting-worker is left out, so the writer
# has no snapshot entry for it (DESIGN.md 11).
SNAPSHOT_REPOSITORIES = ("ledger-service", "payments-api")

# `gh api <path>` -> (HTTP status, body): 200 with a JSON body, 204 with none, and
# 404 as `gh` reports it. A path not here is a 404 too, which is how GitHub answers
# a token that cannot see ledger-service: every one of its settings is unreadable.
# The organisation's `.github` repository is public and holds no SECURITY.md, so
# GitHub applies no default contact from it.
GITHUB: dict[str, tuple[int, dict | None]] = {
    f"repos/{ORG}/payments-api": (
        200,
        {
            "archived": False,
            "default_branch": "main",
            "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
        },
    ),
    f"repos/{ORG}/payments-api/vulnerability-alerts": (204, None),
    f"repos/{ORG}/payments-api/branches/main": (200, {"name": "main", "protected": True}),
    f"repos/{ORG}/.github": (200, {"private": False, "visibility": "public"}),
}

# name -> (every tracked file and its text, the content-set paths the scan reads).
CLONES: dict[str, tuple[dict[str, str], list[str]]] = {
    "payments-api": (
        {
            ".github/CODEOWNERS": "* @example-org/payments-team\n",
            "SECURITY.md": "# Security\n\nReport a vulnerability to security@example.org.\n",
            "config/application.yml": (
                "server:\n  port: 8080\ndatabase:\n  password: fixture-not-a-secret\n"
            ),
            "docs/threat-model.md": "# Threat model\n",
            ".github/workflows/codeql.yml": "steps:\n  - uses: github/codeql-action/init@v3\n",
            ".github/workflows/secrets.yml": "steps:\n  - uses: gitleaks/gitleaks-action@v2\n",
            ".github/dependabot.yml": "version: 2\n",
            "src/main/java/com/example/payments/RefundHandler.java": "class RefundHandler {}\n",
        },
        ["config/application.yml", "src/main/java/com/example/payments/RefundHandler.java"],
    ),
    "ledger-service": (
        {
            "CODEOWNERS": "# owners to be agreed\n*\n",
            "src/main/java/com/example/ledger/LedgerClient.java": "class LedgerClient {}\n",
        },
        ["src/main/java/com/example/ledger/LedgerClient.java"],
    ),
    "reporting-worker": (
        {
            ".github/CODEOWNERS": "/src/ @example-org/reporting-team\n",
            "SECURITY.md": "# Security\n\nAsk the reporting team.\n",
            ".github/workflows/build.yml": (
                "steps:\n  - uses: github/codeql-action/analyze@v3\n"
                "  - uses: gitleaks/gitleaks-action@v2\n"
            ),
            "renovate.json": "{}\n",
            # Named for a private key and empty: the writer reads it for BEGIN
            # labels, finds no boundary, and keeps a `.pem` with none as a key file.
            "deploy/signing.pem": "",
        },
        [],
    ),
}


def gh(arguments: list[str]) -> str:
    """`gh api <path>` as the settings stage's `runner` calls it."""
    if len(arguments) != 2 or arguments[0] != "api":
        raise AssertionError(f"gh {arguments} is not a call the stage was expected to make")
    status, body = GITHUB.get(arguments[1], (404, None))
    if status == 404:
        error = json.dumps({"message": "Not Found", "status": "404"})
        raise subprocess.CalledProcessError(1, ["gh", *arguments], output=error)
    return "" if body is None else json.dumps(body)


def settings(stage) -> dict:
    """The snapshot as the settings stage's `main` composes it, less its stamp."""
    repositories = {}
    for name in SNAPSHOT_REPOSITORIES:
        record = stage.read_repository(gh, ORG, name)
        record["organisation"] = ORG
        repositories[name] = record
    return {
        "organisations": {ORG: stage.read_organisation(gh, ORG)},
        "read_at": MINIMUM_STANDARD_SETTINGS["read_at"],
        "repositories": repositories,
    }


def records(work: Path, snapshot: dict) -> dict[str, dict]:
    writer = importlib.import_module("knowledgestore.build_minimum_standard")
    contract = importlib.import_module("knowledgestore.minimum_standard")
    snapshot = {**snapshot, "format": contract.format_stamp()}
    made = {}
    for name, (files, content) in sorted(CLONES.items()):
        clone = work / name
        for rel, text in files.items():
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            (clone / rel).write_text(text, encoding="utf-8")
        sha, date = REPOSITORIES[name]
        tracked = sorted(files)

        def run(_clone, *args, sha=sha, date=date, tracked=tracked) -> str:
            if args[0] == "log":
                return f"{sha}\x1f{date}\n"
            if args[0] == "ls-files":
                return "\0".join(tracked) + "\0"
            raise AssertionError(f"git {args[0]} is not one the writer was expected to run")

        record = writer.evidence_for(clone, content, snapshot, run)
        made[name] = {k: v for k, v in record.items() if k != "format"}
    return made


def main() -> int:
    library_src = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(library_src))
    snapshot = settings(importlib.import_module("knowledgestore.build_minimum_standard_settings"))
    with tempfile.TemporaryDirectory() as tmp:
        made = records(Path(tmp), snapshot)
    status = 0
    if snapshot != MINIMUM_STANDARD_SETTINGS:
        print(json.dumps({"MINIMUM_STANDARD_SETTINGS": snapshot}, indent=2, sort_keys=True))
        print("build.py's settings snapshot differs from the stage's", file=sys.stderr)
        status = 1
    if made != MINIMUM_STANDARD_RECORDS:
        print(json.dumps({"MINIMUM_STANDARD_RECORDS": made}, indent=2, sort_keys=True))
        print("build.py's records differ from the writer's", file=sys.stderr)
        status = 1
    if status:
        print("transcribe the above into build.py", file=sys.stderr)
    else:
        print(
            f"build.py's settings snapshot and {len(made)} minimum-standard records "
            "are the library's own"
        )
    return status


if __name__ == "__main__":
    sys.exit(main())
