"""The evidence stage records, per repository, what the clone and the snapshot show.

Every clone here is a real git repository built in a temporary directory, the secret
scan is the real `secret_mask`, and the settings snapshot is a written file in the
settings stage's shape. Assertions land on the layer the stage wrote.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_minimum_standard as stage  # noqa: E402
from knowledgestore import cli, config, minimum_standard  # noqa: E402

# A fixed committer date, so `as_of` and the last-commit evidence are known by hand.
# Off UTC, because git prints a UTC date as `Z` rather than `+00:00`.
COMMIT_DATE = "2026-01-02T03:04:05+01:00"

# Planted values. Neither may appear in any file the stage writes.
PLANTED_KEY = "AKIAIOSFODNN7EXAMPLE"
PLANTED_PASSWORD = "s3cr3t-value-123"

PLANTED_PEM_FIRST = "MIIEpAIBAAKCAQEAplantedPemFirstLineQWERTY1234"
PLANTED_PEM_SECOND = "ZZZZplantedPemSecondLineABCDEF987654"
PLANTED_K8S = "cGxhbnRlZC1rOHMtdmFsdWU="
PLANTED_BLOCK = "plantedBlockValue123456"

# Split so this file does not itself read as a private key to a secret scanner.
PEM_KIND = "RSA PRIVATE" + " KEY"

FULL_ACCESS = {
    "archived": False,
    "default_branch": "main",
    "branch_protection": "present",
    "secret_scanning": "enabled",
    "dependabot_alerts": "enabled",
    "organisation": "example-org",
    "unreadable": [],
}

NO_ACCESS = {
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
}

DEPENDENCY_CONFIGS_CHECKED = [
    ".github/dependabot.yaml",
    ".github/dependabot.yml",
    ".github/renovate.json",
    ".renovaterc*",
    "renovate.json",
]

CODEOWNERS_PATHS = [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]
SECURITY_PATHS = [".github/SECURITY.md", "SECURITY.md", "docs/SECURITY.md"]

# An organisation whose `.github` repository holds no SECURITY.md, as the settings
# stage records one when every path answers 404.
NO_DEFAULT = {"default_security_policy": None, "public": True, "unreadable": []}

# Fake key material, split so this file does not read as a key to a secret scanner.
PLANTED_KEY_FILE = "plantedKeyFileMarker" + "Q9x7Zk2"
FAKE_PEM_BODY = "fixture-not-a-secret"
KEY_FILE_TEXT = "-----BEGIN OPENSSH PRIVATE" + " KEY-----\n" + PLANTED_KEY_FILE + "\n"


def _git(repo: Path, *arguments: str) -> str:
    environment = {**os.environ, "GIT_COMMITTER_DATE": COMMIT_DATE, "GIT_AUTHOR_DATE": COMMIT_DATE}
    completed = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    return completed.stdout.strip()


class TheEvidenceStage(SettingsIsolated):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        config.configure(self.root)
        self.content: list[str] = []

    # -- fixtures ---------------------------------------------------------------

    def clone(self, name: str, files: dict[str, str], *, in_content_set: bool = True) -> str:
        """A real clone with `files` committed; returns its commit sha. Its files join
        the content-file list unless `in_content_set` is false."""
        repo = config.REPOSITORIES_DIR / name
        repo.mkdir(parents=True)
        _git(repo, "init", "-q")
        for path, text in files.items():
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "initial")
        if in_content_set:
            self.content.extend(f"repositories/{name}/{path}" for path in files)
        return _git(repo, "rev-parse", "HEAD")

    def snapshot(
        self,
        repositories: dict[str, dict],
        read_at: str = "2026-10-22T10:00:00+00:00",
        organisations: dict[str, dict] | None = None,
    ):
        body = {
            "format": minimum_standard.format_stamp(),
            "organisations": {"example-org": NO_DEFAULT}
            if organisations is None
            else organisations,
            "read_at": read_at,
            "repositories": repositories,
        }
        config.MINIMUM_STANDARD_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.MINIMUM_STANDARD_SETTINGS_PATH.write_text(json.dumps(body), encoding="utf-8")

    def products(self, declared: object) -> None:
        config.PRODUCTS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        config.PRODUCTS_CONFIG.write_text(json.dumps(declared), encoding="utf-8")

    def write_content(self) -> None:
        config.CONTENT_FILES_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Reversed, so a stage that trusts the list's order is caught by the byte check.
        config.CONTENT_FILES_PATH.write_text(
            "".join(f"{path}\n" for path in reversed(self.content)), encoding="utf-8"
        )

    def run_stage(self, expect: int = 0, *, content: bool = True) -> tuple[int, str, str]:
        if content:
            self.write_content()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = stage.main()
        self.assertEqual(code, expect, err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def record(self, name: str) -> dict:
        path = config.MINIMUM_STANDARD_BY_REPO_DIR / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def summary(self) -> dict:
        return json.loads(config.MINIMUM_STANDARD_SUMMARY_PATH.read_text(encoding="utf-8"))

    def assert_nothing_written(self) -> None:
        self.assertFalse(config.MINIMUM_STANDARD_BY_REPO_DIR.exists())
        self.assertFalse(config.MINIMUM_STANDARD_SUMMARY_PATH.exists())

    def hygiene_item(self, record: dict, item: str) -> dict:
        found = [e for e in record["points"]["hygiene"]["evidence"] if e["item"] == item]
        self.assertEqual(len(found), 1, record["points"]["hygiene"]["evidence"])
        return found[0]

    # -- owner and security contact ---------------------------------------------

    def test_codeowners_and_security_md_with_a_contact_are_present(self):
        """Breaks if a recognised CODEOWNERS naming an owner, or a SECURITY.md carrying
        a contact, is not read as present, or is cited at the wrong path."""
        self.clone(
            "repo-a",
            {
                ".github/CODEOWNERS": "# owners\n* @example-org/team-a\n",
                "SECURITY.md": "Report a vulnerability to security@example.com.\n",
            },
        )
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        points = self.record("repo-a")["points"]
        self.assertEqual(points["owner"]["state"], "present")
        self.assertEqual(
            points["owner"]["evidence"],
            [{"path": ".github/CODEOWNERS", "reason": "names an owner for the repository root"}],
        )
        self.assertEqual(points["security_contact"]["state"], "present")
        self.assertEqual(
            points["security_contact"]["evidence"],
            [
                {
                    "path": "SECURITY.md",
                    "reason": "carries a contact; whether it is monitored is not recorded",
                }
            ],
        )

    def test_missing_files_are_absent_and_name_the_paths_checked(self):
        """Breaks if a clone without CODEOWNERS or SECURITY.md reads as anything but
        absent, or if the record stops saying where it looked."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        points = self.record("repo-a")["points"]
        self.assertEqual(points["owner"]["state"], "absent")
        self.assertEqual(
            points["owner"]["evidence"],
            [{"path": path, "reason": "not found"} for path in CODEOWNERS_PATHS],
        )
        self.assertEqual(points["security_contact"]["state"], "absent")
        self.assertEqual(
            points["security_contact"]["evidence"],
            [{"path": path, "reason": "not found"} for path in SECURITY_PATHS]
            + [
                {"path": "example-org/.github", "reason": "no organisation default security policy"}
            ],
        )

    def test_a_codeowners_naming_no_owner_is_absent(self):
        """Breaks if a CODEOWNERS of comments, or of patterns with no owner, reads as
        present: the file exists and names nobody."""
        self.clone("repo-a", {"CODEOWNERS": "# nobody yet\n*.md\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        owner = self.record("repo-a")["points"]["owner"]
        self.assertEqual(owner["state"], "absent")
        self.assertEqual(
            owner["evidence"],
            [{"path": "CODEOWNERS", "reason": "no rule names an owner for the repository root"}],
        )

    def test_a_codeowners_with_only_subdirectory_rules_is_absent(self):
        """Breaks if a rule for `docs/` is taken as an owner for the repository: the
        standard asks for a named owner of the system, which is the root rule."""
        self.clone("repo-a", {"CODEOWNERS": "docs/ @example-org/docs-team\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        owner = self.record("repo-a")["points"]["owner"]
        self.assertEqual(owner["state"], "absent")
        self.assertEqual(owner["reason"], "no rule names an owner for the repository root")

    def test_a_root_rule_spelled_slash_names_an_owner(self):
        """Breaks if only `*` is read as the repository root: `/` names it too."""
        self.clone("repo-a", {"CODEOWNERS": "docs/ @example-org/docs-team\n/ @example-org/a\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        self.assertEqual(self.record("repo-a")["points"]["owner"]["state"], "present")

    def test_a_contact_counts_only_as_a_mailbox_or_a_reporting_url(self):
        """Breaks if any URL in SECURITY.md reads as a contact - a build badge is not
        an intake route - or if a mailto, a bare address or a URL on a line about
        reporting stops counting."""
        cases = {
            "a badge": (
                "# Security policy\n"
                "[![Build](https://img.shields.io/badge/build-passing-green)]"
                "(https://github.com/example/repo/actions)\n",
                "absent",
            ),
            "a URL that only spells security": (
                "See https://example.com/security for more.\n",
                "absent",
            ),
            "a reporting URL": (
                "Report a vulnerability at https://example.com/intake\n",
                "present",
            ),
            "a disclosure URL": ("Disclose issues via https://example.com/form\n", "present"),
            "a mailto": ("Write to <mailto:team@example.com>\n", "present"),
            "a bare address": ("Contact: team@example.com\n", "present"),
            "an address in a Markdown link": ("Email [the team](team@example.com).\n", "present"),
            "a handle": ("Follow @example-team for news.\n", "absent"),
            "a domain with no local part": ("Find us at @example.org.\n", "absent"),
            "a host with no domain": ("Contact: team@localhost\n", "absent"),
        }
        for name in cases:
            self.clone(name.replace(" ", "-"), {"SECURITY.md": cases[name][0]})
        self.snapshot({})
        self.run_stage()
        for name, (_, expected) in cases.items():
            with self.subTest(case=name):
                contact = self.record(name.replace(" ", "-"))["points"]["security_contact"]
                self.assertEqual(contact["state"], expected)

    def test_a_security_md_without_a_contact_is_absent(self):
        """Breaks if a SECURITY.md with no email or URL reads as a security contact."""
        self.clone("repo-a", {"docs/SECURITY.md": "Please tell us about problems.\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        contact = self.record("repo-a")["points"]["security_contact"]
        self.assertEqual(contact["state"], "absent")
        self.assertEqual(contact["reason"], "no contact in SECURITY.md")
        self.assertEqual(
            contact["evidence"], [{"path": "docs/SECURITY.md", "reason": "no contact"}]
        )

    # -- the organisation's default security policy -----------------------------

    def contact_with_default(
        self, default: dict, files: dict[str, str] | None = None, name: str = "repo-a"
    ) -> dict:
        """A clone's security_contact, under an example-org whose record is `default`."""
        self.clone(name, files or {"README.md": f"# {name}\n"})
        self.snapshot({name: FULL_ACCESS}, organisations={"example-org": default})
        self.run_stage()
        return self.record(name)["points"]["security_contact"]

    def test_an_organisation_default_with_a_contact_is_present_and_cited(self):
        """Breaks if a repository with no SECURITY.md of its own reads as absent when its
        organisation's `.github` repository supplies one with a contact: GitHub shows
        that policy on the repository, so the contact is there."""
        default = {
            "default_security_policy": {"contact": True, "path": "SECURITY.md"},
            "public": True,
            "unreadable": [],
        }
        contact = self.contact_with_default(default)
        self.assertEqual(contact["state"], "present")
        self.assertEqual(
            contact["evidence"],
            [
                {
                    "path": "example-org/.github:SECURITY.md",
                    "reason": "organisation default security policy",
                }
            ],
        )

    def test_a_github_repository_that_is_not_public_gives_no_default(self):
        """Breaks if a SECURITY.md in a private or internal `.github` repository counts
        as the organisation's default - a false present, because GitHub applies only a
        public one's files. The record cites the repository as not public."""
        default = {
            "default_security_policy": {"contact": True, "path": "SECURITY.md"},
            "public": False,
            "unreadable": [],
        }
        contact = self.contact_with_default(default)
        self.assertEqual(contact["state"], "absent")
        self.assertEqual(contact["reason"], "no SECURITY.md")
        self.assertEqual(
            contact["evidence"],
            [{"path": path, "reason": "not found"} for path in SECURITY_PATHS]
            + [
                {
                    "path": "example-org/.github",
                    "reason": "not public, so GitHub applies no default from it",
                }
            ],
        )

    def test_a_default_whose_repository_visibility_was_not_read_is_not_present(self):
        """Breaks if a default with a contact counts when the snapshot does not say its
        `.github` repository is public - a false present for a private one."""
        for name, public in (("repo-a", None), ("repo-b", "true")):
            with self.subTest(public=public):
                default = {
                    "default_security_policy": {"contact": True, "path": "SECURITY.md"},
                    "public": public,
                    "unreadable": [],
                }
                contact = self.contact_with_default(default, name=name)
                self.assertEqual(contact["state"], "not_determinable")
                self.assertEqual(contact["reason"], "no access")

    def test_the_repositorys_own_security_md_wins_over_the_organisation_default(self):
        """Breaks if the organisation's default is consulted for a repository that has
        its own SECURITY.md: GitHub shows the repository's own, so its own is judged,
        and its lack of a contact is not covered by the organisation's."""
        default = {
            "default_security_policy": {"contact": True, "path": "SECURITY.md"},
            "public": True,
            "unreadable": [],
        }
        contact = self.contact_with_default(
            default, {"SECURITY.md": "Please tell us about problems.\n"}
        )
        self.assertEqual(contact["state"], "absent")
        self.assertEqual(contact["evidence"], [{"path": "SECURITY.md", "reason": "no contact"}])

    def test_an_organisation_default_without_a_contact_is_not_present(self):
        """Breaks if a default policy counts merely by existing: it is judged by the
        same contact rule as a repository's own."""
        default = {
            "default_security_policy": {"contact": False, "path": "docs/SECURITY.md"},
            "public": True,
            "unreadable": [],
        }
        contact = self.contact_with_default(default)
        self.assertEqual(contact["state"], "absent")
        self.assertIn(
            {
                "path": "example-org/.github:docs/SECURITY.md",
                "reason": "organisation default security policy has no contact",
            },
            contact["evidence"],
        )

    def test_no_organisation_default_is_absent_and_says_where_it_looked(self):
        """Breaks if an organisation with no default (every path a 404) stops reading
        as absent, or the record stops naming the `.github` repository it checked."""
        contact = self.contact_with_default(NO_DEFAULT)
        self.assertEqual(contact["state"], "absent")
        self.assertEqual(contact["reason"], "no SECURITY.md")
        self.assertEqual(
            contact["evidence"],
            [{"path": path, "reason": "not found"} for path in SECURITY_PATHS]
            + [
                {"path": "example-org/.github", "reason": "no organisation default security policy"}
            ],
        )

    def test_an_unreadable_organisation_default_is_not_determinable_with_no_access(self):
        """Breaks if an organisation default the token could not read is taken for no
        default, which would report every repository relying on it as having no
        security contact, from no access."""
        unread = {"default_security_policy": None, "unreadable": ["default_security_policy"]}
        contact = self.contact_with_default(unread)
        self.assertEqual(contact["state"], "not_determinable")
        self.assertEqual(contact["reason"], "no access")

    def test_no_snapshot_leaves_a_repository_without_security_md_not_determinable(self):
        """Breaks if a repository with no SECURITY.md reads as absent when nothing was
        read about its organisation's default, with no snapshot or none for this
        repository or its organisation."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.clone("repo-b", {"README.md": "# repo-b\n"})
        cases = {
            "no snapshot": None,
            "missing from the snapshot": ({"repo-b": FULL_ACCESS}, None),
            "organisation not read": ({"repo-a": FULL_ACCESS}, {}),
        }
        for case, given in cases.items():
            with self.subTest(case=case):
                config.MINIMUM_STANDARD_SETTINGS_PATH.unlink(missing_ok=True)
                if given is not None:
                    self.snapshot(given[0], organisations=given[1])
                self.run_stage()
                contact = self.record("repo-a")["points"]["security_contact"]
                self.assertEqual(contact["state"], "not_determinable")
                self.assertEqual(contact["reason"], "no settings snapshot")

    def test_a_repository_off_github_has_no_organisation_default(self):
        """Breaks if a repository not on github.com, which no organisation's default can
        reach, stops reading as absent for want of a SECURITY.md."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": {**NO_ACCESS, "organisation": None}}, organisations={})
        self.run_stage()
        contact = self.record("repo-a")["points"]["security_contact"]
        self.assertEqual(contact["state"], "absent")
        self.assertEqual(contact["reason"], "no SECURITY.md")

    # -- no secrets ---------------------------------------------------------------

    def test_a_planted_secret_is_reported_by_location_and_its_value_appears_nowhere(self):
        """Breaks if a finding carries the line, an excerpt or the value, into any file
        the stage writes or onto its output (Review Focus 1); or if a hit is not
        reported by rule, path and line."""
        self.clone(
            "repo-a",
            {
                "config/app.env": f"# settings\naws_id = {PLANTED_KEY}\n",
                "src/db.py": f'host = "db"\npassword = "{PLANTED_PASSWORD}"\n',
            },
        )
        self.snapshot({"repo-a": FULL_ACCESS})
        _, out, err = self.run_stage()
        no_secrets = self.record("repo-a")["points"]["no_secrets"]
        self.assertEqual(no_secrets["state"], "absent")
        self.assertEqual(
            no_secrets["evidence"],
            [
                {"line": 2, "path": "config/app.env", "rule": "aws-access-key-id"},
                {"line": 2, "path": "src/db.py", "rule": "key-name"},
            ],
        )
        written = [p for p in (self.root / "knowledge").rglob("*") if p.is_file()]
        self.assertIn(config.MINIMUM_STANDARD_SUMMARY_PATH, written)
        for path in written:
            body = path.read_bytes()
            for planted in (PLANTED_KEY, PLANTED_PASSWORD):
                with self.subTest(path=path.name, planted=planted):
                    self.assertNotIn(planted.encode(), body)
        for planted in (PLANTED_KEY, PLANTED_PASSWORD):
            self.assertNotIn(planted, out + err)

    def test_multi_line_secrets_are_found_by_location_and_their_values_appear_nowhere(self):
        """Breaks if the scan reads one line at a time, which cannot see a private-key
        block, a Kubernetes Secret's `data:` or a `password: |` block scalar; or if any
        of their values reaches a file the stage writes."""
        self.clone(
            "repo-a",
            {
                "deploy/key.pem": (
                    f"# deploy key\n-----BEGIN {PEM_KIND}-----\n"
                    f"{PLANTED_PEM_FIRST}\n{PLANTED_PEM_SECOND}\n"
                    f"-----END {PEM_KIND}-----\n"
                ),
                "k8s/secret.yaml": (
                    "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\ndata:\n"
                    f"  anything: {PLANTED_K8S}\n"
                ),
                "values.yaml": f"db:\n  password: |\n    {PLANTED_BLOCK}\n",
            },
        )
        self.snapshot({"repo-a": FULL_ACCESS})
        _, out, err = self.run_stage()
        no_secrets = self.record("repo-a")["points"]["no_secrets"]
        self.assertEqual(no_secrets["state"], "absent")
        self.assertEqual(
            no_secrets["evidence"],
            [
                {"path": "deploy/key.pem", "rule": "key-file"},
                {"line": 3, "path": "deploy/key.pem", "rule": "pem-private-key"},
                {"line": 4, "path": "deploy/key.pem", "rule": "pem-private-key"},
                {"line": 6, "path": "k8s/secret.yaml", "rule": "key-name"},
                {"line": 3, "path": "values.yaml", "rule": "key-name"},
            ],
        )
        planted = (PLANTED_PEM_FIRST, PLANTED_PEM_SECOND, PLANTED_K8S, PLANTED_BLOCK)
        for path in (p for p in (self.root / "knowledge").rglob("*") if p.is_file()):
            body = path.read_bytes()
            for value in planted:
                with self.subTest(path=path.name, planted=value):
                    self.assertNotIn(value.encode(), body)
        for value in planted:
            self.assertNotIn(value, out + err)

    def test_a_tracked_key_file_is_found_by_name_and_never_read(self):
        """Breaks if a committed key file outside the content set goes unreported, which
        is how a `.priv` beside the code was missed; or if its content is read into the
        layer or onto the output. The planted marker is inside the file."""
        self.clone(
            "repo-a",
            {"deploy/signing.priv": KEY_FILE_TEXT, "README.md": "# a\n"},
            in_content_set=False,
        )
        self.content.append("repositories/repo-a/README.md")
        self.snapshot({"repo-a": FULL_ACCESS})
        _, out, err = self.run_stage()
        no_secrets = self.record("repo-a")["points"]["no_secrets"]
        self.assertEqual(no_secrets["state"], "absent")
        self.assertEqual(
            no_secrets["evidence"], [{"path": "deploy/signing.priv", "rule": "key-file"}]
        )
        for path in (p for p in (self.root / "knowledge").rglob("*") if p.is_file()):
            with self.subTest(path=path.name):
                self.assertNotIn(PLANTED_KEY_FILE.encode(), path.read_bytes())
        self.assertNotIn(PLANTED_KEY_FILE, out + err)

    def test_a_key_file_is_found_when_the_content_set_is_empty(self):
        """Breaks if a repository the content set excluded skips the key-file walk: the
        walk reads the tracked names, not the content set."""
        self.clone("repo-a", {"certs/server.key": KEY_FILE_TEXT}, in_content_set=False)
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        no_secrets = self.record("repo-a")["points"]["no_secrets"]
        self.assertEqual(no_secrets["state"], "absent")
        self.assertEqual(no_secrets["evidence"], [{"path": "certs/server.key", "rule": "key-file"}])

    def test_a_public_key_and_an_untracked_key_file_are_not_found(self):
        """Breaks if a `.pub` file is taken for a private key, or if the walk reads the
        working directory rather than what git tracks: an untracked file is not
        committed."""
        self.clone("repo-a", {".ssh/id_rsa.pub": "ssh-rsa AAAA example\n", "README.md": "# a\n"})
        (config.REPOSITORIES_DIR / "repo-a" / "id_ed25519").write_text(
            KEY_FILE_TEXT, encoding="utf-8"
        )
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        no_secrets = self.record("repo-a")["points"]["no_secrets"]
        self.assertEqual(no_secrets["state"], "not_determinable")
        self.assertEqual(no_secrets["evidence"], [])

    def test_key_file_names_are_recognised_by_their_documented_patterns(self):
        """Breaks if a documented key-file name stops matching, or a near miss starts."""
        found = [
            "a.pem",
            "b.KEY",
            "c.priv",
            "d.p12",
            "e.pfx",
            "f.jks",
            "g.keystore",
            "id_rsa",
            "id_ecdsa_deploy",
            "id_ed25519",
            "dir/id_rsa.old",
        ]
        missed = ["id_rsa.pub", "key.pem.pub", "keys.md", "monkey.py", "pemfile", "src/id.py"]
        for path in found:
            with self.subTest(path=path):
                self.assertTrue(stage.is_key_file(path))
        for path in missed:
            with self.subTest(path=path):
                self.assertFalse(stage.is_key_file(path))

    def pem(self, *labels: str) -> str:
        """A PEM-looking file: real boundary lines around a body that is not a secret."""
        return "".join(
            f"-----BEGIN {label}-----\n{FAKE_PEM_BODY}\n-----END {label}-----\n" for label in labels
        )

    def no_secrets_for(self, files: dict[str, str]) -> dict:
        """The `no_secrets` point for a fresh `repo-a` holding `files`, outside the
        content set; callable more than once in a test."""
        shutil.rmtree(config.REPOSITORIES_DIR / "repo-a", ignore_errors=True)
        self.clone("repo-a", files, in_content_set=False)
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        return self.record("repo-a")["points"]["no_secrets"]

    def test_a_certificate_only_pem_is_not_a_key_file(self):
        """Breaks if a CA bundle, a leaf certificate or a public key is reported as a
        committed key because of its extension: it holds no secret, and the point
        would read `absent` for a repository that committed none."""
        for name, text in {
            "certs/ca-bundle.pem": self.pem("CERTIFICATE", "CERTIFICATE"),
            "certs/leaf.crt": self.pem("CERTIFICATE"),
            "certs/server.cer": self.pem("CERTIFICATE"),
            "certs/public.key": self.pem("PUBLIC KEY"),
            "certs/mixed.pem": self.pem("CERTIFICATE", "PUBLIC KEY"),
        }.items():
            with self.subTest(path=name):
                no_secrets = self.no_secrets_for({name: text})
                self.assertEqual(no_secrets["state"], "not_determinable")
                self.assertEqual(no_secrets["evidence"], [])

    def test_a_pem_holding_a_private_key_block_is_a_key_file_whatever_else_it_holds(self):
        """Breaks if the boundary labels are not read, or only the first is: every
        private-key label counts, and a bundle that ends in one is a key file."""
        for kind in (
            "PRIVATE KEY",
            "RSA PRIVATE KEY",
            "EC PRIVATE KEY",
            "OPENSSH PRIVATE KEY",
            "ENCRYPTED PRIVATE KEY",
        ):
            for name in ("deploy/key.pem", "deploy/key.key", "deploy/cert.crt", "deploy/c.cer"):
                with self.subTest(kind=kind, path=name):
                    no_secrets = self.no_secrets_for({name: self.pem("CERTIFICATE", kind)})
                    self.assertEqual(no_secrets["state"], "absent")
                    self.assertEqual(no_secrets["evidence"], [{"path": name, "rule": "key-file"}])

    def test_a_pem_with_no_boundary_stays_a_key_file(self):
        """Breaks if the conservative choice flips: a `.pem` or `.key` whose boundary
        cannot be read is still reported, because a missed key is the costlier error.
        A `.crt` with no boundary was never reported and still is not."""
        for name in ("certs/raw.pem", "certs/raw.key"):
            with self.subTest(path=name):
                no_secrets = self.no_secrets_for({name: FAKE_PEM_BODY + "\n"})
                self.assertEqual(no_secrets["state"], "absent")
                self.assertEqual(no_secrets["evidence"], [{"path": name, "rule": "key-file"}])
        self.assertEqual(
            self.no_secrets_for({"certs/raw.crt": FAKE_PEM_BODY + "\n"})["state"],
            "not_determinable",
        )

    def test_a_trust_store_leaves_the_point_undetermined_and_a_keystore_stays_a_finding(self):
        """Breaks if a binary keystore named for a trust store is reported as a key
        (the file cannot be judged without opening it), or if the exemption spreads to
        a keystore that is not named for one."""
        trust = self.no_secrets_for({"config/truststore.jks": "binary"})
        self.assertEqual(trust["state"], "not_determinable")
        self.assertEqual(
            trust["evidence"], [{"path": "config/truststore.jks", "rule": "trust-store"}]
        )
        self.assertIn("trust store", trust["reason"])
        for name in ("config/cacerts.jks", "config/CACERTS.p12", "ssl/my-TrustStore.pfx"):
            with self.subTest(path=name):
                self.assertEqual(self.no_secrets_for({name: "binary"})["state"], "not_determinable")
        for name in ("config/keystore.p12", "config/app.jks", "config/store.keystore"):
            with self.subTest(path=name):
                no_secrets = self.no_secrets_for({name: "binary"})
                self.assertEqual(no_secrets["state"], "absent")
                self.assertEqual(no_secrets["evidence"], [{"path": name, "rule": "key-file"}])

    def test_a_trust_store_beside_a_real_key_does_not_hide_it(self):
        """Breaks if the trust-store exemption swallows a genuine finding in the same
        repository: the key keeps the point `absent`."""
        no_secrets = self.no_secrets_for(
            {"config/truststore.jks": "binary", "deploy/key.pem": self.pem("PRIVATE KEY")}
        )
        self.assertEqual(no_secrets["state"], "absent")
        self.assertEqual(no_secrets["evidence"], [{"path": "deploy/key.pem", "rule": "key-file"}])

    def test_no_pem_content_reaches_a_finding_or_the_output(self):
        """Breaks if reading the boundary labels lets a body line, or the label itself
        beyond the rule name, into the layer or the printed output."""
        self.clone(
            "repo-a",
            {
                "deploy/key.pem": self.pem("PRIVATE KEY"),
                "certs/ca.pem": self.pem("CERTIFICATE"),
            },
            in_content_set=False,
        )
        self.snapshot({"repo-a": FULL_ACCESS})
        _, out, err = self.run_stage()
        for path in (p for p in (self.root / "knowledge").rglob("*") if p.is_file()):
            with self.subTest(path=path.name):
                self.assertNotIn(FAKE_PEM_BODY.encode(), path.read_bytes())
        self.assertNotIn(FAKE_PEM_BODY, out + err)

    def test_is_key_file_reads_a_clone_only_when_given_one(self):
        """Breaks if the name-only form, used where no clone is at hand, changes: it
        still answers by name alone."""
        self.assertTrue(stage.is_key_file("certs/ca.pem"))
        self.assertFalse(stage.is_key_file("certs/a.crt"))
        self.assertFalse(stage.is_key_file("config/truststore.jks"))
        self.assertTrue(stage.is_trust_store("config/Truststore.JKS"))
        self.assertFalse(stage.is_trust_store("config/truststore.txt"))

    def test_setting_names_alone_are_not_determinable_and_keep_their_findings(self):
        """Breaks if a `key-name` hit alone makes the point absent: a setting named for
        a secret shows no committed value, and in most repositories those hits are
        yaml and terraform setting names. The findings stay listed as evidence."""
        self.clone("repo-a", {"values.yaml": "db:\n  password: hunter2value\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        no_secrets = self.record("repo-a")["points"]["no_secrets"]
        self.assertEqual(no_secrets["state"], "not_determinable")
        self.assertEqual(
            no_secrets["reason"],
            "only secret-looking setting names were found; no committed value was confirmed",
        )
        self.assertEqual(
            no_secrets["evidence"], [{"line": 2, "path": "values.yaml", "rule": "key-name"}]
        )

    def test_a_setting_name_beside_a_value_shaped_finding_is_absent(self):
        """Breaks if a value-shaped finding stops making the point absent when a
        `key-name` hit is beside it."""
        self.clone(
            "repo-a",
            {
                "values.yaml": "db:\n  password: hunter2value\n",
                "notes.txt": f"-----BEGIN {PEM_KIND}-----\n{PLANTED_PEM_FIRST}\n"
                f"-----END {PEM_KIND}-----\n",
            },
        )
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        no_secrets = self.record("repo-a")["points"]["no_secrets"]
        self.assertEqual(no_secrets["state"], "absent")
        self.assertEqual(
            {e["rule"] for e in no_secrets["evidence"]}, {"key-name", "pem-private-key"}
        )

    def test_the_value_shaped_rules_are_every_rule_but_key_name(self):
        """Breaks if a rule that finds a value drops out of the set that makes
        `no_secrets` absent, or `key-name` joins it. Named by hand: the default value
        shapes, the built-in query-parameter rule and the key-file walk."""
        self.assertEqual(
            stage.value_shaped_rules(),
            {
                "authorization-header",
                "aws-access-key-id",
                "cli-secret-flag",
                "connection-string-credential",
                "connection-string-key",
                "feature-flag-sdk-key",
                "github-token",
                "jwt",
                "key-file",
                "pem-private-key",
                "sas-signature",
                "sk-key",
                "slack-webhook",
                "sonarqube-token",
                "sops-encrypted",
                "sql-credential",
                "url-credential",
                "url-query-secret",
            },
        )

    def test_a_clean_scan_is_not_determinable_never_present(self):
        """Breaks if a scan that finds nothing reads as present or absent: a pattern
        scan cannot show that nothing is committed."""
        self.clone("repo-a", {"src/app.py": "def main():\n    return 0\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        no_secrets = self.record("repo-a")["points"]["no_secrets"]
        self.assertEqual(no_secrets["state"], "not_determinable")
        self.assertEqual(
            no_secrets["reason"], "pattern scan found nothing; a scan cannot show absence"
        )
        self.assertEqual(no_secrets["evidence"], [])

    def test_a_repository_with_no_content_files_is_not_determinable(self):
        """Breaks if a clone the content set excluded reads as scanned clean, or worse
        (Review Focus 5). A secret outside the content set is not scanned, by design."""
        self.clone(
            "repo-a", {"src/db.py": f'password = "{PLANTED_PASSWORD}"\n'}, in_content_set=False
        )
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        no_secrets = self.record("repo-a")["points"]["no_secrets"]
        self.assertEqual(no_secrets["state"], "not_determinable")
        self.assertEqual(no_secrets["reason"], "no files in the content set")

    # -- secure by design and patching ------------------------------------------

    def test_a_threat_model_is_present_and_its_absence_is_not_determinable(self):
        """Breaks if a threat model by a recognised name is missed, or if its absence
        reads as `absent`: secure-by-design is not judged from code."""
        self.clone("repo-a", {"docs/Threat_Model.md": "# Threats\n"})
        self.clone("repo-b", {"README.md": "# repo-b\n"})
        self.snapshot({"repo-a": FULL_ACCESS, "repo-b": FULL_ACCESS})
        self.run_stage()
        found = self.record("repo-a")["points"]["secure_by_design"]
        self.assertEqual(found["state"], "present")
        self.assertEqual(
            found["evidence"], [{"path": "docs/Threat_Model.md", "reason": "threat model"}]
        )
        missing = self.record("repo-b")["points"]["secure_by_design"]
        self.assertEqual(missing["state"], "not_determinable")
        self.assertEqual(
            missing["reason"], "no threat model found; secure-by-design is not judged from code"
        )

    def test_a_threat_model_is_known_by_its_file_name_or_its_docs_directory(self):
        """Breaks if a path merely containing the words counts - a parser named for
        threat models is code, not a threat model - or if anything under
        `docs/security/` stands in for one; or if a file named for one, in any case
        and with any extension, or a file under `docs/threat-model*/`, is missed."""
        self.clone(
            "repo-a",
            {
                "src/threat_model_parser.py": "def parse():\n    return 0\n",
                "docs/security/policy.md": "# Policy\n",
                "threat-models-notes/readme.md": "# Notes\n",
            },
        )
        self.clone(
            "repo-b",
            {
                "THREATMODEL": "# Threats\n",
                "design/threat-model.v2.pdf": "%PDF\n",
                "docs/threat-models/overview.md": "# Overview\n",
                "src/app.py": "x = 1\n",
            },
        )
        self.snapshot({})
        self.run_stage()
        unrelated = self.record("repo-a")["points"]["secure_by_design"]
        self.assertEqual(unrelated["state"], "not_determinable")
        self.assertEqual(unrelated["evidence"], [])
        found = self.record("repo-b")["points"]["secure_by_design"]
        self.assertEqual(found["state"], "present")
        self.assertEqual(
            [entry["path"] for entry in found["evidence"]],
            ["THREATMODEL", "design/threat-model.v2.pdf", "docs/threat-models/overview.md"],
        )

    def test_patching_is_always_not_determinable(self):
        """Breaks if patching is ever judged: timelines are not recorded in a repository,
        however much else the repository evidences."""
        self.clone(
            "repo-a",
            {
                "CODEOWNERS": "* @example-org/team-a\n",
                "SECURITY.md": "https://example.com/security\n",
                ".github/dependabot.yml": "version: 2\n",
            },
        )
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        patching = self.record("repo-a")["points"]["patching"]
        self.assertEqual(
            patching,
            {
                "state": "not_determinable",
                "evidence": [],
                "reason": "patching timelines are not recorded in a repository",
            },
        )

    # -- hygiene and the snapshot -------------------------------------------------

    def test_every_hygiene_item_present_makes_the_point_present(self):
        """Breaks if a hygiene item is not recognised from its file or setting, or if the
        point does not combine its items."""
        self.clone(
            "repo-a",
            {
                ".github/dependabot.yml": "version: 2\n",
                ".github/workflows/codeql.yml": "steps:\n  - uses: github/codeql-action/init@v3\n",
                ".github/workflows/secrets.yaml": "steps:\n  - run: gitleaks detect\n",
            },
        )
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        record = self.record("repo-a")
        hygiene = record["points"]["hygiene"]
        self.assertEqual(hygiene["state"], "present")
        self.assertEqual(
            hygiene["evidence"],
            [
                {
                    "item": "branch_protection",
                    "reason": "the default branch is protected",
                    "setting": "branch_protection",
                    "state": "present",
                },
                {
                    "checked": [".github/workflows/"],
                    "item": "code_scanning_workflow",
                    "paths": [".github/workflows/codeql.yml"],
                    "reason": "code-scanning workflow found",
                    "state": "present",
                },
                {
                    "checked": DEPENDENCY_CONFIGS_CHECKED,
                    "item": "dependency_updates",
                    "paths": [".github/dependabot.yml"],
                    "reason": "dependency update config found",
                    "state": "present",
                },
                {
                    "item": "secret_scanning_setting",
                    "reason": "secret scanning is enabled",
                    "setting": "secret_scanning",
                    "state": "present",
                },
                {
                    "checked": [".github/workflows/"],
                    "item": "secret_scanning_workflow",
                    "paths": [".github/workflows/secrets.yaml"],
                    "reason": "secret-scanning workflow found",
                    "state": "present",
                },
                {
                    "item": "vulnerability_alerts",
                    "reason": "Dependabot alerts are enabled",
                    "setting": "dependabot_alerts",
                    "state": "present",
                },
            ],
        )

    def test_one_absent_hygiene_item_makes_the_point_absent(self):
        """Breaks if an unprotected default branch or disabled secret scanning does not
        read as absent, or if one absent item does not make the point absent."""
        self.clone("repo-a", {"renovate.json": "{}\n"})
        self.snapshot(
            {
                "repo-a": {
                    **FULL_ACCESS,
                    "branch_protection": "absent",
                    "secret_scanning": "disabled",
                }
            }
        )
        self.run_stage()
        record = self.record("repo-a")
        self.assertEqual(record["points"]["hygiene"]["state"], "absent")
        self.assertEqual(self.hygiene_item(record, "branch_protection")["state"], "absent")
        self.assertEqual(self.hygiene_item(record, "secret_scanning_setting")["state"], "absent")
        code = self.hygiene_item(record, "code_scanning_workflow")
        self.assertEqual(code["state"], "not_determinable")
        dependency = self.hygiene_item(record, "dependency_updates")
        self.assertEqual(dependency["state"], "present")
        self.assertEqual(dependency["paths"], ["renovate.json"])

    def test_no_code_scanning_workflow_is_not_determinable_never_absent(self):
        """Breaks if a clone without a CodeQL workflow reads as having no code scanning:
        GitHub's default setup runs without a workflow and the build cannot see it.
        With every other requirement met, the point is then not determinable."""
        self.clone(
            "repo-a",
            {
                ".github/dependabot.yml": "version: 2\n",
                ".github/workflows/secrets.yaml": "steps:\n  - run: gitleaks detect\n",
            },
        )
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        record = self.record("repo-a")
        code = self.hygiene_item(record, "code_scanning_workflow")
        self.assertEqual(code["state"], "not_determinable")
        self.assertEqual(code["reason"], "no code-scanning workflow; default setup is not visible")
        self.assertEqual(record["points"]["hygiene"]["state"], "not_determinable")

    def test_secret_scanning_is_one_requirement_met_by_either_the_setting_or_a_workflow(self):
        """Breaks if the setting and the workflow are each required: either one scans
        for secrets. Also breaks if the pair reads absent while the setting was not
        read, or present when neither half is. Every other requirement is met, so the
        point's state is the pair's."""
        workflow = {".github/workflows/secrets.yaml": "steps:\n  - run: gitleaks detect\n"}
        cases = {
            "enabled, no workflow": ("enabled", {}, "present"),
            "disabled, a workflow": ("disabled", workflow, "present"),
            "unread, a workflow": (None, workflow, "present"),
            "disabled, no workflow": ("disabled", {}, "absent"),
            "unread, no workflow": (None, {}, "not_determinable"),
        }
        settings = {}
        for name, (setting, files, _) in cases.items():
            repo = name.replace(", ", "-").replace(" ", "-")
            self.clone(
                repo,
                {
                    ".github/dependabot.yml": "version: 2\n",
                    ".github/workflows/codeql.yml": "uses: github/codeql-action/init@v3\n",
                    **files,
                },
            )
            settings[repo] = {**FULL_ACCESS, "secret_scanning": setting}
        self.snapshot(settings)
        self.run_stage()
        for name, (_, _, expected) in cases.items():
            with self.subTest(case=name):
                record = self.record(name.replace(", ", "-").replace(" ", "-"))
                self.assertEqual(record["points"]["hygiene"]["state"], expected)

    def test_an_absent_requirement_outranks_an_undetermined_one(self):
        """Breaks if a point with one requirement definitely missing reads as merely
        not determinable because another could not be read."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": {**FULL_ACCESS, "branch_protection": None}})
        self.run_stage()
        record = self.record("repo-a")
        self.assertEqual(
            self.hygiene_item(record, "branch_protection")["state"], "not_determinable"
        )
        self.assertEqual(self.hygiene_item(record, "dependency_updates")["state"], "absent")
        self.assertEqual(record["points"]["hygiene"]["state"], "absent")

    def test_no_update_config_is_absent_whatever_the_alerts_say(self):
        """Breaks if Dependabot alerts stand in for update tooling: alerts are
        vulnerability scanning, a separate item, and a clone with no Dependabot or
        Renovate config has no dependency updates evidenced. Also breaks if the paths
        checked are not listed."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        record = self.record("repo-a")
        dependency = self.hygiene_item(record, "dependency_updates")
        self.assertEqual(dependency["state"], "absent")
        self.assertEqual(dependency["paths"], [])
        self.assertEqual(dependency["checked"], DEPENDENCY_CONFIGS_CHECKED)
        self.assertEqual(self.hygiene_item(record, "vulnerability_alerts")["state"], "present")

    def test_a_snapshot_value_of_the_wrong_type_is_not_determinable(self):
        """Breaks if a list or object where the snapshot holds a string crashes the
        stage or is read as a setting: it was not read, so it is no access."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        odd = {**FULL_ACCESS, "branch_protection": ["present"], "secret_scanning": {"a": 1}}
        odd["dependabot_alerts"] = 1
        self.snapshot({"repo-a": odd})
        self.run_stage()
        record = self.record("repo-a")
        for item in ("branch_protection", "secret_scanning_setting", "vulnerability_alerts"):
            with self.subTest(item=item):
                entry = self.hygiene_item(record, item)
                self.assertEqual(entry["state"], "not_determinable")
                self.assertEqual(entry["reason"], "no access")

    def test_unreadable_settings_are_not_determinable_with_no_access(self):
        """Breaks if a setting the token could not read becomes absent: no access is not
        a missing control. Also the partial case, where `archived` was readable and
        branch protection was not."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.clone("repo-b", {"README.md": "# repo-b\n"})
        partial = {**NO_ACCESS, "archived": False, "default_branch": "main"}
        self.snapshot({"repo-a": NO_ACCESS, "repo-b": partial})
        self.run_stage()
        record = self.record("repo-a")
        for item in ("branch_protection", "secret_scanning_setting", "vulnerability_alerts"):
            with self.subTest(item=item):
                entry = self.hygiene_item(record, item)
                self.assertEqual(entry["state"], "not_determinable")
                self.assertEqual(entry["reason"], "no access")
        unmaintained = record["points"]["unmaintained"]
        self.assertEqual(unmaintained["state"], "not_determinable")
        self.assertEqual(unmaintained["reason"], "no access")

        other = self.record("repo-b")
        self.assertEqual(other["points"]["unmaintained"]["state"], "not_determinable")
        protection = self.hygiene_item(other, "branch_protection")
        self.assertEqual(protection["state"], "not_determinable")
        self.assertEqual(protection["reason"], "no access")

    def test_no_snapshot_makes_github_items_not_determinable(self):
        """Breaks if a repository the snapshot does not hold, or a store with no snapshot
        at all, reads its GitHub-derived items as absent (Review Focus 3)."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.clone("repo-b", {"README.md": "# repo-b\n"})
        for case in ("missing from the snapshot", "no snapshot"):
            with self.subTest(case=case):
                if case == "no snapshot":
                    config.MINIMUM_STANDARD_SETTINGS_PATH.unlink()
                else:
                    self.snapshot({"repo-a": FULL_ACCESS})
                self.run_stage()
                record = self.record("repo-b")
                for item in (
                    "branch_protection",
                    "secret_scanning_setting",
                    "vulnerability_alerts",
                ):
                    entry = self.hygiene_item(record, item)
                    self.assertEqual(entry["state"], "not_determinable", item)
                    self.assertEqual(entry["reason"], "no settings snapshot", item)
                unmaintained = record["points"]["unmaintained"]
                self.assertEqual(unmaintained["state"], "not_determinable")
                self.assertEqual(unmaintained["reason"], "no settings snapshot")
                self.assertIsNone(record["settings_read_at"])

    # -- unmaintained -------------------------------------------------------------

    def test_archived_in_snapshot_is_present(self):
        """Breaks if an archived repository is not read as the safe posture the point
        asks for: archived code is marked as unmaintained."""
        sha = self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": {**FULL_ACCESS, "archived": True}})
        self.run_stage()
        unmaintained = self.record("repo-a")["points"]["unmaintained"]
        self.assertEqual(
            unmaintained,
            {
                "state": "present",
                "evidence": [
                    {"commit": sha, "date": COMMIT_DATE, "reason": "last commit"},
                    {"reason": "archived", "setting": "archived"},
                ],
            },
        )

    def test_a_live_repository_is_not_determinable_with_its_last_commit_and_dated(self):
        """Breaks if a repository that is not archived reads as safely maintained: the
        point asks for an owner and a patching route, which a repository cannot show.
        Also breaks if the last-commit date or the clone's `as_of` is dropped or
        misread, or if the snapshot's read time is not carried onto the record."""
        sha = self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": FULL_ACCESS}, read_at="2026-10-22T10:00:00+00:00")
        self.run_stage()
        record = self.record("repo-a")
        self.assertEqual(record["format"], minimum_standard.format_stamp())
        self.assertEqual(record["repository"], "repo-a")
        self.assertEqual(record["as_of"], {"commit": sha, "date": COMMIT_DATE})
        self.assertEqual(record["settings_read_at"], "2026-10-22T10:00:00+00:00")
        self.assertEqual(list(record["points"]), sorted(minimum_standard.POINTS))
        unmaintained = record["points"]["unmaintained"]
        self.assertEqual(
            unmaintained,
            {
                "state": "not_determinable",
                "evidence": [
                    {"commit": sha, "date": COMMIT_DATE, "reason": "last commit"},
                    {"reason": "not archived", "setting": "archived"},
                ],
                "reason": (
                    "not archived; maintenance and a patching route are not judged "
                    "from the repository"
                ),
            },
        )

    # -- products, the summary and the run --------------------------------------

    def test_products_naming_an_unknown_repository_exit_1_and_write_nothing(self):
        """Breaks if a product declaration naming a repository the store does not hold
        is accepted, or if anything is written before it is refused (Review Focus 4)."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        self.products({"product-x": ["repo-a", "repo-z"]})
        _, _, err = self.run_stage(expect=1)
        self.assertIn("repo-z", err)
        self.assertFalse(config.MINIMUM_STANDARD_BY_REPO_DIR.exists())
        self.assertFalse(config.MINIMUM_STANDARD_SUMMARY_PATH.exists())

    def test_a_malformed_products_declaration_exits_1_and_writes_nothing(self):
        """Breaks if a products file that is not JSON, or not a map of names to lists
        of names, is published or half-read."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        for body in ("{not json", json.dumps(["repo-a"]), json.dumps({"product-x": "repo-a"})):
            with self.subTest(body=body):
                config.PRODUCTS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
                config.PRODUCTS_CONFIG.write_text(body, encoding="utf-8")
                _, _, err = self.run_stage(expect=1)
                self.assertIn("products.json", err)
                self.assert_nothing_written()

    def test_a_missing_content_file_list_exits_1_and_writes_nothing(self):
        """Breaks if a store that never ran `content-set` gets a layer whose secret
        scan read no files, which would read as every repository scanned."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        _, _, err = self.run_stage(expect=1, content=False)
        self.assertIn("content-set", err)
        self.assert_nothing_written()

    def test_an_unstamped_or_foreign_snapshot_exits_1_and_writes_nothing(self):
        """Breaks if a snapshot without this layer's stamp, or with another major, is
        read as settings: a store's own file of another shape would be taken for one."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        cases = {
            "no stamp": {"read_at": "x", "repositories": {"repo-a": FULL_ACCESS}},
            "another major": {
                "format": {"layer": "minimum_standard", "major": 2, "minor": 0},
                "read_at": "x",
                "repositories": {"repo-a": FULL_ACCESS},
            },
        }
        for case, body in cases.items():
            with self.subTest(case=case):
                config.MINIMUM_STANDARD_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
                config.MINIMUM_STANDARD_SETTINGS_PATH.write_text(json.dumps(body), encoding="utf-8")
                _, _, err = self.run_stage(expect=1)
                self.assertIn("github-settings.json", err)
                self.assert_nothing_written()

    def test_a_directory_at_the_snapshot_path_exits_1_and_writes_nothing(self):
        """Breaks if something other than a file where the snapshot belongs is read as
        no snapshot: that would quietly mark every GitHub item unread."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        config.MINIMUM_STANDARD_SETTINGS_PATH.mkdir(parents=True)
        _, _, err = self.run_stage(expect=1)
        self.assertIn("is not a file", err)
        self.assert_nothing_written()

    def test_a_clone_git_cannot_read_exits_1_naming_it_and_writes_nothing(self):
        """Breaks if a clone with no commit - which `git log` refuses - crashes the stage,
        is skipped silently, or is refused without saying which clone it was."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        empty = config.REPOSITORIES_DIR / "repo-empty"
        empty.mkdir()
        _git(empty, "init", "-q")
        self.snapshot({"repo-a": FULL_ACCESS})
        _, _, err = self.run_stage(expect=1)
        self.assertIn("could not read the clone repo-empty", err)
        self.assert_nothing_written()

    def test_declared_products_are_published_in_the_summary(self):
        """Breaks if a valid product declaration is dropped from the layer."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.clone("repo-b", {"README.md": "# repo-b\n"})
        self.snapshot({"repo-a": FULL_ACCESS, "repo-b": FULL_ACCESS})
        self.products({"product-x": ["repo-b", "repo-a"]})
        self.run_stage()
        self.assertEqual(self.summary()["products"], {"product-x": ["repo-a", "repo-b"]})

    def test_the_summary_says_archived_repositories_are_not_in_the_store(self):
        """Breaks if the summary loses the note that bounds point 7, its stamp, or its
        row of states for each repository."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        summary = self.summary()
        self.assertEqual(summary["format"], minimum_standard.format_stamp())
        self.assertIn("archived repositories are not in the store", summary["notes"])
        self.assertEqual(
            summary["repositories"]["repo-a"]["points"]["patching"], "not_determinable"
        )
        self.assertEqual(summary["repositories"]["repo-a"]["points"]["owner"], "absent")
        self.assertNotIn("products", summary)

    def test_the_summary_carries_no_verdict_field(self):
        """Breaks if the summary grows a field beyond its evidence - a `compliant`, a
        `score`, a pass - at the top level or in a repository's row. The layer reports
        evidence; people make judgements. Products appear only when declared."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        top = {"format", "notes", "repositories", "settings_read_at"}
        for declared in (False, True):
            with self.subTest(products=declared):
                if declared:
                    self.products({"product-x": ["repo-a"]})
                self.run_stage()
                summary = self.summary()
                self.assertEqual(set(summary), top | ({"products"} if declared else set()))
                for name, row in summary["repositories"].items():
                    self.assertEqual(set(row), {"as_of", "points"}, name)
                    self.assertEqual(set(row["points"]), set(minimum_standard.POINTS), name)

    def test_two_runs_write_identical_bytes(self):
        """Breaks if any list or key in the layer follows input order or iteration
        order rather than a sort (CLAUDE.md 1.3)."""
        self.clone(
            "repo-b",
            {
                "z.env": f"aws_id = {PLANTED_KEY}\n",
                "a.py": f'password = "{PLANTED_PASSWORD}"\n',
                ".github/workflows/b.yml": "uses: github/codeql-action/init@v3\n",
                ".github/workflows/a.yml": "uses: github/codeql-action/analyze@v3\n",
            },
        )
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-b": FULL_ACCESS, "repo-a": NO_ACCESS})
        self.products({"product-y": ["repo-b", "repo-a"], "product-x": ["repo-a"]})

        def layer() -> dict[str, bytes]:
            return {
                str(p.relative_to(self.root)): p.read_bytes()
                for p in sorted(config.MINIMUM_STANDARD_DIR.rglob("*.json"))
            }

        self.run_stage()
        first = layer()
        self.content.reverse()
        self.run_stage()
        self.assertEqual(layer(), first)
        record = json.loads(first["knowledge/minimum-standard/by-repo/repo-b.json"])
        self.assertEqual(
            [(e["path"], e["line"]) for e in record["points"]["no_secrets"]["evidence"]],
            [("a.py", 1), ("z.env", 1)],
        )

    def test_a_repository_no_longer_held_loses_its_record(self):
        """Breaks if a record for a clone the store no longer holds survives a run: it
        would claim evidence for a repository that is not in the store."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        stale = config.MINIMUM_STANDARD_BY_REPO_DIR / "repo-gone.json"
        stale.parent.mkdir(parents=True)
        stale.write_text("{}", encoding="utf-8")
        self.run_stage()
        self.assertFalse(stale.exists())
        self.assertTrue((config.MINIMUM_STANDARD_BY_REPO_DIR / "repo-a.json").is_file())

    def test_the_stage_is_registered_after_relationships_and_before_index(self):
        """Breaks if the stage is not reachable as `knowledgestore minimum-standard`,
        runs before the clones' layers or after the index that reads them, or escapes
        the git floor check although it runs git on the clones."""
        module, help_ = cli.STAGES["minimum-standard"]
        self.assertEqual(module, "build_minimum_standard")
        self.assertEqual(
            help_, "record evidence against the GOV.UK minimum standard for each repository"
        )
        self.assertIn("minimum-standard", cli.GIT_STAGES)
        order = list(cli.STAGES)
        self.assertLess(order.index("relationships"), order.index("minimum-standard"))
        self.assertLess(order.index("minimum-standard"), order.index("index"))


if __name__ == "__main__":
    unittest.main()
