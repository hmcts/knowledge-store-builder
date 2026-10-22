"""The settings stage snapshots what the build's token can read from GitHub.

The stub stands at the `gh` boundary: it answers each `gh api` path with canned
JSON or raises `CalledProcessError` the way the CLI does. Everything downstream is
the real stage, and assertions land on the file it wrote.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_minimum_standard_settings as stage  # noqa: E402
from knowledgestore import cli, config, minimum_standard  # noqa: E402

REPO_BODY = {
    "archived": False,
    "default_branch": "main",
    "security_and_analysis": {
        "secret_scanning": {"status": "enabled"},
        "dependabot_alerts": {"status": "disabled"},
    },
}


def refused(message: str, code: int = 1) -> subprocess.CalledProcessError:
    """What `gh api` raises for an HTTP error: the body on stdout, the line on stderr."""
    return subprocess.CalledProcessError(
        code, ["gh", "api"], output=json.dumps({"message": message}), stderr=f"gh: {message}"
    )


class Gh:
    """A `gh` that answers by API path. A value that is an exception is raised."""

    def __init__(self, answers: dict[str, object]):
        self.answers = answers
        self.paths: list[str] = []

    def __call__(self, arguments: list[str]) -> str:
        path = arguments[1]
        self.paths.append(path)
        answer = self.answers[path]
        if isinstance(answer, BaseException):
            raise answer
        return json.dumps(answer)


class TheSettingsSnapshot(SettingsIsolated):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        config.configure(self.root)

    def declare(self, *names: str) -> None:
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        lines = [f"{name}|git@github.com:example-org/{name}.git|main" for name in names]
        config.REPOSITORIES_CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_stage(self, gh: Gh) -> dict:
        self.assertEqual(stage.main([], runner=gh), 0)
        return json.loads(config.MINIMUM_STANDARD_SETTINGS_PATH.read_text(encoding="utf-8"))

    def test_full_access_records_every_setting(self):
        """Breaks if a readable setting is dropped, misread or recorded under another
        repository: the evidence stage cites these values."""
        self.declare("repo-a")
        gh = Gh(
            {
                "repos/example-org/repo-a": REPO_BODY,
                "repos/example-org/repo-a/branches/main/protection": {"url": "x"},
            }
        )
        body = self.run_stage(gh)
        self.assertEqual(
            body["repositories"],
            {
                "repo-a": {
                    "archived": False,
                    "default_branch": "main",
                    "branch_protection": "present",
                    "secret_scanning": "enabled",
                    "dependabot_alerts": "disabled",
                    "unreadable": [],
                }
            },
        )

    def test_a_forbidden_protection_call_is_unreadable_not_absent(self):
        """Breaks if a 403 on branch protection reads as `absent`: a token without
        admin would then report every repository as unprotected (Review Focus 2).
        `archived` must still be recorded."""
        self.declare("repo-a")
        gh = Gh(
            {
                "repos/example-org/repo-a": REPO_BODY,
                "repos/example-org/repo-a/branches/main/protection": refused(
                    "Resource not accessible by integration (HTTP 403)"
                ),
            }
        )
        record = self.run_stage(gh)["repositories"]["repo-a"]
        self.assertIs(record["archived"], False)
        self.assertIsNone(record["branch_protection"])
        self.assertEqual(record["unreadable"], ["branch_protection"])

    def test_an_unprotected_branch_is_absent(self):
        """Breaks if GitHub's 'Branch not protected' 404 reads as unreadable: that is
        the one answer that does show protection is missing."""
        self.declare("repo-a")
        gh = Gh(
            {
                "repos/example-org/repo-a": REPO_BODY,
                "repos/example-org/repo-a/branches/main/protection": refused(
                    "Branch not protected (HTTP 404)"
                ),
            }
        )
        record = self.run_stage(gh)["repositories"]["repo-a"]
        self.assertEqual(record["branch_protection"], "absent")
        self.assertEqual(record["unreadable"], [])

    def test_any_other_404_on_protection_is_unreadable(self):
        """Breaks if every 404 reads as `absent`: a 404 also answers a token that
        cannot see the repository at all, and that says nothing about protection."""
        self.declare("repo-a")
        gh = Gh(
            {
                "repos/example-org/repo-a": REPO_BODY,
                "repos/example-org/repo-a/branches/main/protection": refused(
                    "Not Found (HTTP 404)"
                ),
            }
        )
        record = self.run_stage(gh)["repositories"]["repo-a"]
        self.assertIsNone(record["branch_protection"])
        self.assertEqual(record["unreadable"], ["branch_protection"])

    def test_secret_scanning_missing_from_security_and_analysis_is_unreadable(self):
        """Breaks if a repository body that omits `security_and_analysis` (which GitHub
        does for a token without admin) reads as scanning `disabled`."""
        self.declare("repo-a")
        body = {"archived": True, "default_branch": "trunk"}
        gh = Gh(
            {
                "repos/example-org/repo-a": body,
                "repos/example-org/repo-a/branches/trunk/protection": {"url": "x"},
            }
        )
        record = self.run_stage(gh)["repositories"]["repo-a"]
        self.assertIs(record["archived"], True)
        self.assertIsNone(record["secret_scanning"])
        self.assertIsNone(record["dependabot_alerts"])
        self.assertEqual(record["unreadable"], ["dependabot_alerts", "secret_scanning"])
        self.assertEqual(record["branch_protection"], "present")

    def test_an_unreadable_repository_records_nothing_it_did_not_read(self):
        """Breaks if a failed repository call leaves guessed values, or is followed by
        a protection call for a branch nobody named."""
        self.declare("repo-a")
        gh = Gh({"repos/example-org/repo-a": refused("Not Found (HTTP 404)")})
        record = self.run_stage(gh)["repositories"]["repo-a"]
        self.assertEqual(
            record,
            {
                "archived": None,
                "default_branch": None,
                "branch_protection": None,
                "secret_scanning": None,
                "dependabot_alerts": None,
                "unreadable": [
                    "archived",
                    "branch_protection",
                    "default_branch",
                    "dependabot_alerts",
                    "secret_scanning",
                ],
            },
        )
        self.assertEqual(gh.paths, ["repos/example-org/repo-a"])

    def test_output_is_sorted_and_stamped(self):
        """Breaks if the stamp is missing or the repositories come out in declaration
        order: a re-run must differ only in `read_at` (CLAUDE.md 1.3)."""
        self.declare("repo-b", "repo-a")
        answers: dict[str, object] = {}
        for name in ("repo-a", "repo-b"):
            answers[f"repos/example-org/{name}"] = REPO_BODY
            answers[f"repos/example-org/{name}/branches/main/protection"] = {"url": "x"}
        self.run_stage(Gh(answers))
        text = config.MINIMUM_STANDARD_SETTINGS_PATH.read_text(encoding="utf-8")
        body = json.loads(text)
        self.assertEqual(minimum_standard.format_problem(body), "")
        self.assertEqual(list(body["repositories"]), ["repo-a", "repo-b"])
        self.assertRegex(body["read_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertEqual(text, json.dumps(body, indent=2, sort_keys=True) + "\n")
        self.run_stage(Gh(answers))
        again = json.loads(config.MINIMUM_STANDARD_SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            {**again, "read_at": None},
            {**body, "read_at": None},
        )

    def test_a_missing_repository_list_exits_1_and_writes_nothing(self):
        """Breaks if the stage writes an empty snapshot for a store with no list: the
        evidence stage would read that as 'every repository was checked'."""
        self.assertEqual(stage.main([], runner=Gh({})), 1)
        self.assertFalse(config.MINIMUM_STANDARD_SETTINGS_PATH.exists())

    def test_the_stage_is_registered(self):
        """Breaks if the stage is not reachable as `knowledgestore minimum-standard-settings`."""
        module, help_ = cli.STAGES["minimum-standard-settings"]
        self.assertEqual(module, "build_minimum_standard_settings")
        self.assertEqual(
            help_,
            "snapshot the GitHub settings the build's token can read, for minimum-standard",
        )


if __name__ == "__main__":
    unittest.main()
