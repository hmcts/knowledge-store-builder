"""The drift stage: what a sync changed in the files the semantic layer was extracted from.

Every fixture here is a real git repository built in a temporary directory, because
the claim under test is what `git diff` between two recorded shas says - a stubbed
diff would only prove the parser reads the stub.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import config, drift

SOURCE = Path(__file__).resolve().parent.parent / "src"


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class Estate:
    """A store root holding real clones, two provenance files and a plan."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.before: dict[str, dict] = {}
        self.after: dict[str, dict] = {}
        self.plan: list[str] = []

    def clone(self, name: str) -> Path:
        repo = self.root / "repositories" / name
        repo.mkdir(parents=True)
        _git(repo, "init", "-q")
        # A user's `diff.renames` must not decide what the stage reports.
        _git(repo, "config", "diff.renames", "true")
        return repo

    def write(self, plan: list[str] | None = None) -> tuple[Path, Path]:
        before = self.root / "before.json"
        before.write_text(json.dumps({"repositories": self.before}), encoding="utf-8")
        after = self.root / "knowledge" / "provenance.json"
        after.parent.mkdir(parents=True, exist_ok=True)
        after.write_text(json.dumps({"repositories": self.after}), encoding="utf-8")
        plan_path = self.root / "graphify-out" / ".graphify_chunk_plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        files = self.plan if plan is None else plan
        plan_path.write_text(json.dumps({"0001": files}), encoding="utf-8")
        return before, after


class DriftTest(SettingsIsolated):
    def _estate(self) -> Estate:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        config.configure(root=str(root))
        return Estate(root)

    def _moved(self, estate: Estate) -> Estate:
        """One repository that moved in every way a diff can report, one that did not."""
        repo = estate.clone("alpha")
        for name in ("keep.yaml", "change.yaml", "gone.yaml", "old-name.yaml", "unplanned.yaml"):
            (repo / name).write_text(f"{name}: 1\n", encoding="utf-8")
        (repo / "docs").mkdir()
        (repo / "docs" / "a space é.md").write_text("# one\n", encoding="utf-8")
        first = _commit(repo, "one")
        (repo / "change.yaml").write_text("change: 2\n", encoding="utf-8")
        (repo / "unplanned.yaml").write_text("unplanned: 2\n", encoding="utf-8")
        (repo / "docs" / "a space é.md").write_text("# two\n", encoding="utf-8")
        (repo / "gone.yaml").unlink()
        (repo / "new.yaml").write_text("new: 1\n", encoding="utf-8")
        (repo / "notes.txt").write_text("not an extension the plan holds\n", encoding="utf-8")
        (repo / "old-name.yaml").rename(repo / "new-name.yaml")
        second = _commit(repo, "two")
        # A commit past the recorded sha: the diff is between the two recorded shas,
        # not against whatever the clone holds now.
        (repo / "keep.yaml").write_text("keep: 2\n", encoding="utf-8")
        _commit(repo, "three")
        estate.before["alpha"] = {"sha": first}
        estate.after["alpha"] = {"sha": second}

        still = estate.clone("still")
        (still / "s.yaml").write_text("s: 1\n", encoding="utf-8")
        recorded = _commit(still, "s")
        # The clone has moved on past the recorded sha; the provenance has not, so
        # nothing here is stale. Diffing the working tree would say otherwise.
        (still / "s.yaml").write_text("s: 2\n", encoding="utf-8")
        _commit(still, "later")
        estate.before["still"] = {"sha": recorded}
        estate.after["still"] = {"sha": recorded}

        estate.plan = [
            "repositories/alpha/keep.yaml",
            "repositories/alpha/change.yaml",
            "repositories/alpha/gone.yaml",
            "repositories/alpha/old-name.yaml",
            "repositories/alpha/docs/a space é.md",
            "repositories/still/s.yaml",
        ]
        return estate

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = drift.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _measure(self, estate: Estate, plan: list[str] | None = None) -> tuple[int, dict, str]:
        before, _ = estate.write(plan)
        out = estate.root / "drift.json"
        code, text, _ = self._run("--before", str(before), "--out", str(out))
        return code, (json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}), text

    def test_stale_is_the_diff_between_the_recorded_shas_filtered_to_the_plan(self):
        """Breaks if the stage diffs the wrong pair, keeps changes to files the layer
        never extracted, or lets an unmoved repository contribute."""
        code, result, _ = self._measure(self._moved(self._estate()))
        self.assertEqual(code, 0)
        self.assertEqual(
            result,
            {
                "stale": [
                    "repositories/alpha/change.yaml",
                    "repositories/alpha/docs/a space é.md",
                    "repositories/alpha/gone.yaml",
                    "repositories/alpha/new-name.yaml",
                    "repositories/alpha/new.yaml",
                    "repositories/alpha/old-name.yaml",
                ],
                "new": ["repositories/alpha/new-name.yaml", "repositories/alpha/new.yaml"],
                "changed": [
                    "repositories/alpha/change.yaml",
                    "repositories/alpha/docs/a space é.md",
                ],
                "deleted": ["repositories/alpha/gone.yaml", "repositories/alpha/old-name.yaml"],
            },
        )

    def test_a_rename_is_its_old_path_deleted_and_its_new_path_new(self):
        """Breaks if renames reach the parser as one two-path record: the clone sets
        `diff.renames`, so only the stage's own `--no-renames` keeps the old path in
        `deleted` rather than misreading the record."""
        _, result, _ = self._measure(self._moved(self._estate()))
        self.assertIn("repositories/alpha/old-name.yaml", result["deleted"])
        self.assertIn("repositories/alpha/new-name.yaml", result["new"])
        self.assertNotIn("repositories/alpha/old-name.yaml", result["changed"])

    def test_an_extensionless_new_file_counts_only_when_the_plan_holds_one(self):
        """Breaks if "" is admitted unconditionally: a delta must extract what a full
        build would, and a plan with no extensionless path would never extract one."""
        estate = self._estate()
        repo = estate.clone("alpha")
        (repo / "a.md").write_text("a\n", encoding="utf-8")
        first = _commit(repo, "one")
        (repo / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        second = _commit(repo, "two")
        estate.before["alpha"] = {"sha": first}
        estate.after["alpha"] = {"sha": second}

        _, without, _ = self._measure(estate, ["repositories/alpha/a.md"])
        self.assertEqual(without["new"], [])
        _, with_one, _ = self._measure(
            estate, ["repositories/alpha/a.md", "repositories/x/LICENSE"]
        )
        self.assertEqual(with_one["new"], ["repositories/alpha/Dockerfile"])

    def test_the_report_names_counts_share_and_repositories(self):
        """Breaks if the report counts a neighbouring quantity: 6 stale of a
        6-file plan is 100%, across one repository, and the unmoved one is not named."""
        _, _, text = self._measure(self._moved(self._estate()))
        self.assertIn(
            "6 stale (100.0% of the 6-file extraction plan) = 2 new + 2 changed + 2 deleted "
            "across 1 repositories",
            text,
        )
        self.assertIn("1 of 2 repositories moved", text)
        self.assertIn("top: alpha 6", text)

    def test_without_out_the_json_alone_goes_to_stdout(self):
        """Breaks if the report is printed into the JSON a caller pipes onwards."""
        estate = self._moved(self._estate())
        before, _ = estate.write()
        code, text, err = self._run("--before", str(before))
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(text)["stale"]), 6)
        self.assertIn("6 stale", err)

    def test_a_failed_diff_writes_nothing(self):
        """Breaks if a repository whose diff failed is skipped: a partial drift reads as
        a small one, so the stage refuses and names the repository."""
        estate = self._moved(self._estate())
        estate.before["alpha"] = {"sha": "0" * 40}
        before, _ = estate.write()
        out = estate.root / "drift.json"
        code, text, _ = self._run("--before", str(before), "--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("alpha", text)
        self.assertFalse(out.exists(), "a partial drift was written")

    def test_a_plan_from_after_the_before_provenance_is_refused(self):
        """Breaks if a plan rewritten by the resync is accepted: it already holds the
        files added since "before", so they would read as unchanged and the drift
        would undercount without a word. A file the diff says was added after
        "before" cannot have been extracted at "before"."""
        estate = self._moved(self._estate())
        before, _ = estate.write([*estate.plan, "repositories/alpha/new.yaml"])
        out = estate.root / "drift.json"
        code, text, _ = self._run("--before", str(before), "--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("repositories/alpha/new.yaml", text)
        self.assertIn("git show", text)
        self.assertFalse(out.exists(), "a drift against the wrong plan was written")

    def test_no_plan_is_a_refusal(self):
        """Breaks if a missing plan reads as an empty one, which would call nothing stale."""
        estate = self._moved(self._estate())
        before, _ = estate.write()
        (estate.root / "graphify-out" / ".graphify_chunk_plan.json").unlink()
        code, text, _ = self._run("--before", str(before), "--out", str(estate.root / "d.json"))
        self.assertEqual(code, 2)
        self.assertIn("plan", text)

    def test_two_runs_under_different_hash_seeds_are_byte_identical(self):
        """Breaks if any set reaches the output unsorted: stable inside one process,
        different between two, which is the case a refresh is."""
        estate = self._moved(self._estate())
        before, _ = estate.write()
        outputs = []
        for seed in ("0", "1", "2"):
            out = estate.root / f"drift-{seed}.json"
            subprocess.run(
                [sys.executable, "-m", "knowledgestore.cli", "--root", str(estate.root)]
                + ["drift", "--before", str(before), "--out", str(out)],
                capture_output=True,
                check=True,
                env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(SOURCE)},
            )
            outputs.append(out.read_bytes())
        self.assertEqual(len(set(outputs)), 1, "the drift changed with the hash seed")
        self.assertEqual(len(json.loads(outputs[0])["stale"]), 6)


if __name__ == "__main__":
    import unittest

    unittest.main()
