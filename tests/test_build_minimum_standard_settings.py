"""The settings stage snapshots what the build's token can read from GitHub.

The stub stands at the `gh` boundary: it answers each `gh api` path with canned
JSON or raises `CalledProcessError` the way the CLI does. Everything downstream is
the real stage, and assertions land on the file it wrote.
"""

from __future__ import annotations

import base64
import json
import re
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
    },
}


def refused(message: str, code: int = 1) -> subprocess.CalledProcessError:
    """What `run_gh` raises for an HTTP error. Real `gh api` exits 1 with the JSON body
    on stdout, which `run_gh` captures as `.output`; its `status` is the HTTP code the
    message names, as a string, and 404 when it names none."""
    found = re.search(r"\(HTTP (\d{3})\)", message)
    status = found.group(1) if found else "404"
    return subprocess.CalledProcessError(
        code, ["gh", "api"], output=json.dumps({"message": message, "status": status})
    )


def contents(text: str) -> dict:
    """What `gh api repos/<org>/.github/contents/<path>` answers for a file: its bytes
    base64-encoded in `content`, wrapped at 60 characters as GitHub wraps them."""
    encoded = base64.b64encode(text.encode()).decode()
    wrapped = "\n".join(encoded[i : i + 60] for i in range(0, len(encoded), 60))
    return {"type": "file", "encoding": "base64", "path": "SECURITY.md", "content": wrapped}


ORG_REPO = "repos/example-org/.github"
ORG_CONTENTS = ORG_REPO + "/contents/"

# What `gh api repos/<org>/.github` answers, in the fields read: a public repository.
PUBLIC_REPO = {"name": ".github", "private": False, "visibility": "public"}


class Gh:
    """A `gh` that answers by API path. A value that is an exception is raised."""

    def __init__(self, answers: dict[str, object]):
        self.answers = answers
        self.paths: list[str] = []

    def __call__(self, arguments: list[str]) -> str:
        path = arguments[1]
        self.paths.append(path)
        # An organisation's `.github` repository a test does not mention is public and
        # has no SECURITY.md: GitHub's answer for a missing file is a 404.
        default: object = None
        if path == ORG_REPO:
            default = PUBLIC_REPO
        elif path.startswith(ORG_CONTENTS):
            default = refused("Not Found")
        answer = self.answers[path] if default is None else self.answers.get(path, default)
        if isinstance(answer, BaseException):
            raise answer
        # A string is the raw body: `vulnerability-alerts` answers 204 with none.
        return answer if isinstance(answer, str) else json.dumps(answer)


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
        self.assertEqual(stage.main(runner=gh), 0)
        return json.loads(config.MINIMUM_STANDARD_SETTINGS_PATH.read_text(encoding="utf-8"))

    def test_full_access_records_every_setting(self):
        """Breaks if a readable setting is dropped, misread or recorded under another
        repository: the evidence stage cites these values."""
        self.declare("repo-a")
        gh = Gh(
            {
                "repos/example-org/repo-a": REPO_BODY,
                "repos/example-org/repo-a/vulnerability-alerts": "",
                "repos/example-org/repo-a/branches/main": {"name": "main", "protected": True},
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
                    "dependabot_alerts": "enabled",
                    "organisation": "example-org",
                    "unreadable": [],
                }
            },
        )

    def branch(self, *, protected: object = None, omit: bool = False, error=None) -> Gh:
        """A `gh` for repo-a whose default branch answers as given. The real response
        (checked against `gh api repos/<org>/<repo>/branches/<branch>`) is the branch
        object, with `protected` beside `name`, `commit` and `protection`."""
        answer: object = error or {"name": "main", "commit": {"sha": "0" * 40}}
        if isinstance(answer, dict) and not omit:
            answer["protected"] = protected
        return Gh(
            {
                "repos/example-org/repo-a": REPO_BODY,
                "repos/example-org/repo-a/vulnerability-alerts": "",
                "repos/example-org/repo-a/branches/main": answer,
            }
        )

    def test_a_ruleset_protected_branch_is_present(self):
        """Breaks if protection is read from anywhere that ignores rulesets: the classic
        endpoint answers 'Branch not protected' for a ruleset-protected branch, and the
        layer would report a protected branch as unprotected."""
        self.declare("repo-a")
        record = self.run_stage(self.branch(protected=True))["repositories"]["repo-a"]
        self.assertEqual(record["branch_protection"], "present")
        self.assertEqual(record["unreadable"], [])

    def test_an_unprotected_branch_is_absent(self):
        """Breaks if `protected: false` reads as present or unreadable: it is the one
        answer that shows protection is missing."""
        self.declare("repo-a")
        record = self.run_stage(self.branch(protected=False))["repositories"]["repo-a"]
        self.assertEqual(record["branch_protection"], "absent")
        self.assertEqual(record["unreadable"], [])

    def test_a_missing_protected_field_is_unreadable_not_absent(self):
        """Breaks if a branch object with no boolean `protected` (absent, null or a
        string) reads as `absent`: a missing key is not a negative answer."""
        self.declare("repo-a")
        for kwargs in ({"omit": True}, {"protected": None}, {"protected": "false"}):
            with self.subTest(kwargs=kwargs):
                record = self.run_stage(self.branch(**kwargs))["repositories"]["repo-a"]
                self.assertIsNone(record["branch_protection"])
                self.assertEqual(record["unreadable"], ["branch_protection"])

    def test_a_refused_branch_call_is_unreadable_not_absent(self):
        """Breaks if a 403 or 404 on the branch call reads as `absent` (Review Focus 2):
        a token that cannot see the branch would report every repository unprotected.
        `archived` must still be recorded."""
        self.declare("repo-a")
        for message in (
            "Resource not accessible by integration (HTTP 403)",
            "Branch not found (HTTP 404)",
        ):
            with self.subTest(message=message):
                record = self.run_stage(self.branch(error=refused(message)))["repositories"][
                    "repo-a"
                ]
                self.assertIs(record["archived"], False)
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
                "repos/example-org/repo-a/vulnerability-alerts": "",
                "repos/example-org/repo-a/branches/trunk": {"name": "trunk", "protected": True},
            }
        )
        record = self.run_stage(gh)["repositories"]["repo-a"]
        self.assertIs(record["archived"], True)
        self.assertIsNone(record["secret_scanning"])
        self.assertEqual(record["dependabot_alerts"], "enabled")
        self.assertEqual(record["unreadable"], ["secret_scanning"])
        self.assertEqual(record["branch_protection"], "present")

    def test_secret_scanning_disabled_is_recorded_as_disabled(self):
        """Breaks if GitHub's explicit `disabled` is dropped to None or mapped to `enabled`:
        the snapshot would then report a repository with scanning off as unread, and the
        evidence stage could never call the setting absent."""
        self.declare("repo-a")
        body = {
            "archived": False,
            "default_branch": "main",
            "security_and_analysis": {"secret_scanning": {"status": "disabled"}},
        }
        gh = Gh(
            {
                "repos/example-org/repo-a": body,
                "repos/example-org/repo-a/vulnerability-alerts": "",
                "repos/example-org/repo-a/branches/main": {"name": "main", "protected": True},
            }
        )
        record = self.run_stage(gh)["repositories"]["repo-a"]
        self.assertEqual(record["secret_scanning"], "disabled")
        self.assertEqual(record["unreadable"], [])

    def test_an_unreadable_repository_records_nothing_it_did_not_read(self):
        """Breaks if a failed repository call leaves guessed values, or is followed by
        a protection call for a branch nobody named."""
        self.declare("repo-a")
        gh = Gh(
            {
                "repos/example-org/repo-a": refused("Not Found (HTTP 404)"),
                "repos/example-org/repo-a/vulnerability-alerts": refused("Not Found (HTTP 404)"),
            }
        )
        record = self.run_stage(gh)["repositories"]["repo-a"]
        self.assertEqual(
            record,
            {
                "archived": None,
                "default_branch": None,
                "branch_protection": None,
                "secret_scanning": None,
                "dependabot_alerts": None,
                "organisation": "example-org",
                "unreadable": [
                    "archived",
                    "branch_protection",
                    "default_branch",
                    "dependabot_alerts",
                    "secret_scanning",
                ],
            },
        )
        # No branch is known, so no protection call; the alerts call needs only the name.
        self.assertEqual(
            [path for path in gh.paths if not path.startswith(ORG_REPO)],
            ["repos/example-org/repo-a", "repos/example-org/repo-a/vulnerability-alerts"],
        )

    def test_output_is_sorted_and_stamped(self):
        """Breaks if the stamp is missing or the repositories come out in declaration
        order: a re-run must differ only in `read_at` (CLAUDE.md 1.3)."""
        self.declare("repo-b", "repo-a")
        answers: dict[str, object] = {}
        for name in ("repo-a", "repo-b"):
            answers[f"repos/example-org/{name}"] = REPO_BODY
            answers[f"repos/example-org/{name}/vulnerability-alerts"] = ""
            answers[f"repos/example-org/{name}/branches/main"] = {"name": "main", "protected": True}
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
        self.assertEqual(stage.main(runner=Gh({})), 1)
        self.assertFalse(config.MINIMUM_STANDARD_SETTINGS_PATH.exists())

    def test_a_refused_vulnerability_alerts_call_is_unreadable_never_disabled(self):
        """Breaks if a 404 or 403 on `vulnerability-alerts` reads as `disabled`: GitHub
        answers 404 both for 'off' and for 'you cannot see this', so only the 204
        shows the setting, and the layer never guesses the other."""
        for message in (
            "Not Found (HTTP 404)",
            "Resource not accessible by integration (HTTP 403)",
        ):
            with self.subTest(message=message):
                self.declare("repo-a")
                gh = Gh(
                    {
                        "repos/example-org/repo-a": REPO_BODY,
                        "repos/example-org/repo-a/vulnerability-alerts": refused(message),
                        "repos/example-org/repo-a/branches/main": {
                            "name": "main",
                            "protected": True,
                        },
                    }
                )
                record = self.run_stage(gh)["repositories"]["repo-a"]
                self.assertIsNone(record["dependabot_alerts"])
                self.assertEqual(record["unreadable"], ["dependabot_alerts"])
                self.assertEqual(record["secret_scanning"], "enabled")

    def test_an_empty_204_from_vulnerability_alerts_is_enabled(self):
        """Breaks if the empty body of a 204 is read as a failure: `gh` prints nothing
        for it, and that nothing is the whole of the 'enabled' answer."""
        self.declare("repo-a")
        gh = Gh(
            {
                "repos/example-org/repo-a": REPO_BODY,
                "repos/example-org/repo-a/vulnerability-alerts": "",
                "repos/example-org/repo-a/branches/main": {"name": "main", "protected": True},
            }
        )
        self.assertEqual(
            self.run_stage(gh)["repositories"]["repo-a"]["dependabot_alerts"], "enabled"
        )

    # -- the organisation's default security policy -----------------------------

    def organisation(self, answers: dict[str, object], repo: object = PUBLIC_REPO) -> dict:
        """The snapshot's record for example-org, with `.github` answering as given:
        `repo` for the repository itself, `answers` for its files."""
        self.declare("repo-a")
        gh = Gh(
            {
                "repos/example-org/repo-a": REPO_BODY,
                "repos/example-org/repo-a/vulnerability-alerts": "",
                "repos/example-org/repo-a/branches/main": {"name": "main", "protected": True},
                ORG_REPO: repo,
                **{ORG_CONTENTS + path: answer for path, answer in answers.items()},
            }
        )
        self.gh = gh
        body = self.run_stage(gh)
        self.assertEqual(body["repositories"]["repo-a"]["organisation"], "example-org")
        self.assertEqual(list(body["organisations"]), ["example-org"])
        return body["organisations"]["example-org"]

    def test_an_organisation_default_with_a_contact_is_recorded_with_its_path(self):
        """Breaks if the `.github` repository's SECURITY.md is not read, is read from
        the wrong place, or its base64 content is judged undecoded: GitHub applies it to
        every repository in the organisation that has none of its own, and without it
        those repositories read as having no security contact."""
        found = self.organisation(
            {"SECURITY.md": contents("Report a vulnerability to security@example.com.\n")}
        )
        self.assertEqual(
            found,
            {
                "default_security_policy": {"contact": True, "path": "SECURITY.md"},
                "public": True,
                "unreadable": [],
            },
        )

    def test_an_organisation_default_without_a_contact_records_no_contact(self):
        """Breaks if a default SECURITY.md is taken for a contact merely by existing:
        it is judged by the same rule as a repository's own."""
        found = self.organisation(
            {"docs/SECURITY.md": contents("Please tell us about problems.\n")}
        )
        self.assertEqual(
            found["default_security_policy"], {"contact": False, "path": "docs/SECURITY.md"}
        )
        self.assertEqual(found["unreadable"], [])

    def test_a_404_at_every_path_means_the_organisation_has_no_default(self):
        """Breaks if a 404, GitHub's answer for a file that is not there, reads as
        unreadable: every repository in an organisation without a default would then
        lose its `absent` to `no access`."""
        found = self.organisation({})
        self.assertEqual(found, {"default_security_policy": None, "public": True, "unreadable": []})

    def test_a_refused_or_undecodable_default_is_unreadable(self):
        """Breaks if a failure other than a 404 - a 403, a body that is not a file - is
        read as 'no default', which would report every repository relying on the
        organisation's policy as having no contact, from no access."""
        for name, answer in (
            ("a 403", refused("Resource not accessible by integration (HTTP 403)")),
            ("a directory listing", "[]"),
            ("content that is not base64", {"encoding": "base64", "content": "!!!"}),
            ("a file too large to inline", {"encoding": "none", "content": ""}),
        ):
            with self.subTest(case=name):
                found = self.organisation({".github/SECURITY.md": answer})
                self.assertEqual(
                    found,
                    {
                        "default_security_policy": None,
                        "public": True,
                        "unreadable": ["default_security_policy"],
                    },
                )

    def test_a_private_or_internal_github_repository_gives_no_default(self):
        """Breaks if the SECURITY.md of a `.github` repository that is not public is
        recorded as the organisation's default - a false present, because GitHub
        applies default community health files only from a public `.github`. Its
        files are not read at all."""
        policy = {"SECURITY.md": contents("Report a vulnerability to security@example.com.\n")}
        for name, repo in (
            ("private", {"private": True, "visibility": "private"}),
            ("internal", {"private": True, "visibility": "internal"}),
            ("internal, private unset", {"private": False, "visibility": "internal"}),
            ("private, no visibility", {"private": True}),
        ):
            with self.subTest(case=name):
                found = self.organisation(policy, repo=repo)
                self.assertEqual(
                    found, {"default_security_policy": None, "public": False, "unreadable": []}
                )
                self.assertEqual(
                    [path for path in self.gh.paths if path.startswith(ORG_CONTENTS)], []
                )

    def test_a_github_repository_that_does_not_exist_gives_no_default(self):
        """Breaks if a 404 on the `.github` repository - none exists, or the token
        cannot see a private one - reads as unreadable, or is followed by reads of
        files that cannot be there."""
        found = self.organisation({}, repo=refused("Not Found (HTTP 404)"))
        self.assertEqual(found, {"default_security_policy": None, "public": None, "unreadable": []})
        self.assertEqual([path for path in self.gh.paths if path.startswith(ORG_CONTENTS)], [])

    def test_unread_github_repository_visibility_is_unreadable(self):
        """Breaks if a `.github` repository whose visibility was not read - a refused
        call, or a body naming neither `private` nor `visibility` - has its
        SECURITY.md taken as the default: a false present when the repository is
        private."""
        policy = {"SECURITY.md": contents("Report a vulnerability to security@example.com.\n")}
        for name, repo in (
            ("a 403", refused("Resource not accessible by integration (HTTP 403)")),
            ("no visibility fields", {"name": ".github"}),
            ("a body that is not an object", "[]"),
        ):
            with self.subTest(case=name):
                found = self.organisation(policy, repo=repo)
                self.assertEqual(
                    found,
                    {
                        "default_security_policy": None,
                        "public": None,
                        "unreadable": ["default_security_policy"],
                    },
                )

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
