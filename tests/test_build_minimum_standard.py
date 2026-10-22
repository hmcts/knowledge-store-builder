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

FULL_ACCESS = {
    "archived": False,
    "default_branch": "main",
    "branch_protection": "present",
    "secret_scanning": "enabled",
    "dependabot_alerts": "enabled",
    "unreadable": [],
}

NO_ACCESS = {
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
}

CODEOWNERS_PATHS = [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]
SECURITY_PATHS = [".github/SECURITY.md", "SECURITY.md", "docs/SECURITY.md"]


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

    def snapshot(self, repositories: dict[str, dict], read_at: str = "2026-10-22T10:00:00+00:00"):
        body = {
            "format": minimum_standard.format_stamp(),
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

    def run_stage(self, expect: int = 0) -> tuple[int, str, str]:
        self.write_content()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = stage.main([])
        self.assertEqual(code, expect, err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def record(self, name: str) -> dict:
        path = config.MINIMUM_STANDARD_BY_REPO_DIR / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def summary(self) -> dict:
        return json.loads(config.MINIMUM_STANDARD_SUMMARY_PATH.read_text(encoding="utf-8"))

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
            [{"path": ".github/CODEOWNERS", "reason": "names an owner"}],
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
            [{"path": path, "reason": "not found"} for path in SECURITY_PATHS],
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
            owner["evidence"], [{"path": "CODEOWNERS", "reason": "no rule names an owner"}]
        )

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
                    "item": "code_scanning",
                    "paths": [".github/workflows/codeql.yml"],
                    "reason": "code-scanning workflow found",
                    "state": "present",
                },
                {
                    "item": "dependency_updates",
                    "paths": [".github/dependabot.yml"],
                    "reason": "dependency update config found",
                    "setting": "dependabot_alerts",
                    "state": "present",
                },
                {
                    "item": "secret_scanning_setting",
                    "reason": "secret scanning is enabled",
                    "setting": "secret_scanning",
                    "state": "present",
                },
                {
                    "item": "secret_scanning_workflow",
                    "paths": [".github/workflows/secrets.yaml"],
                    "reason": "secret-scanning workflow found",
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
        self.assertEqual(self.hygiene_item(record, "code_scanning")["state"], "absent")
        dependency = self.hygiene_item(record, "dependency_updates")
        self.assertEqual(dependency["state"], "present")
        self.assertEqual(dependency["paths"], ["renovate.json"])

    def test_alerts_enabled_without_a_config_make_dependency_updates_present(self):
        """Breaks if Dependabot alerts read from the snapshot do not count when the clone
        holds no update config."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": FULL_ACCESS})
        self.run_stage()
        dependency = self.hygiene_item(self.record("repo-a"), "dependency_updates")
        self.assertEqual(dependency["state"], "present")
        self.assertEqual(dependency["paths"], [])
        self.assertEqual(dependency["reason"], "Dependabot alerts are enabled")

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
        for item in ("branch_protection", "secret_scanning_setting", "dependency_updates"):
            with self.subTest(item=item):
                entry = self.hygiene_item(record, item)
                self.assertEqual(entry["state"], "not_determinable")
                self.assertEqual(entry["reason"], "no access")
        unmaintained = record["points"]["unmaintained"]
        self.assertEqual(unmaintained["state"], "not_determinable")
        self.assertEqual(unmaintained["reason"], "no access")

        other = self.record("repo-b")
        self.assertEqual(other["points"]["unmaintained"]["state"], "present")
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
                for item in ("branch_protection", "secret_scanning_setting", "dependency_updates"):
                    entry = self.hygiene_item(record, item)
                    self.assertEqual(entry["state"], "not_determinable", item)
                    self.assertEqual(entry["reason"], "no settings snapshot", item)
                unmaintained = record["points"]["unmaintained"]
                self.assertEqual(unmaintained["state"], "not_determinable")
                self.assertEqual(unmaintained["reason"], "no settings snapshot")
                self.assertIsNone(record["settings_read_at"])

    # -- unmaintained -------------------------------------------------------------

    def test_archived_in_snapshot_is_absent(self):
        """Breaks if an archived repository reads as maintained."""
        self.clone("repo-a", {"README.md": "# repo-a\n"})
        self.snapshot({"repo-a": {**FULL_ACCESS, "archived": True}})
        self.run_stage()
        unmaintained = self.record("repo-a")["points"]["unmaintained"]
        self.assertEqual(unmaintained["state"], "absent")
        self.assertEqual(unmaintained["reason"], "archived")

    def test_a_live_repository_is_present_with_its_last_commit_and_dated(self):
        """Breaks if the last-commit date or the clone's `as_of` is dropped or misread,
        or if the snapshot's read time is not carried onto the record."""
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
        self.assertEqual(unmaintained["state"], "present")
        self.assertIn(
            {"commit": sha, "date": COMMIT_DATE, "reason": "last commit"}, unmaintained["evidence"]
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
        """Breaks if the stage is not reachable as `knowledgestore minimum-standard`, or
        runs before the clones' layers or after the index that reads them."""
        module, help_ = cli.STAGES["minimum-standard"]
        self.assertEqual(module, "build_minimum_standard")
        self.assertEqual(
            help_, "record evidence against the GOV.UK minimum standard for each repository"
        )
        order = list(cli.STAGES)
        self.assertLess(order.index("relationships"), order.index("minimum-standard"))
        self.assertLess(order.index("minimum-standard"), order.index("index"))


if __name__ == "__main__":
    unittest.main()
