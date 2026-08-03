"""The install-command gate: documented commands must run as written.

The break this catches, from the estate this library was built for: the README
gave `pip install -r requirements.lock` as the rebuild step, while a comment ten
lines away in another file explained that the lock names no index and that this
exact command fails with "No matching distribution found". A comment saying
"remember the flag" is not a gate.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import check_install_docs as gate  # noqa: E402

FEED = "https://pkgs.example.com/feed/pypi/simple/"
LOCK_WITHOUT_INDEX = "thing==1.0 \\\n    --hash=sha256:abc\n"
LOCK_WITH_INDEX = f"--extra-index-url {FEED}\n--only-binary :all:\n\nthing==1.0\n"


class InstallDocsGateTest(SettingsIsolated):
    def store(self, tmp: str, lock: str, files: dict[str, str]) -> Path:
        root = Path(tmp)
        (root / "requirements.lock").write_text(lock, encoding="utf-8")
        (root / "requirements.txt").write_text(
            f"--extra-index-url {FEED}\n--only-binary :all:\nthing==1.0\n", encoding="utf-8"
        )
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        config.configure(root=str(root))
        return root

    def test_a_documented_command_that_cannot_work_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {"README.md": "Rebuild:\n\n```bash\npip install -r requirements.lock\n```\n"},
            )
            self.assertEqual(gate.main(), 1)

    def test_the_same_command_with_the_index_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {
                    "README.md": "Rebuild:\n\n```bash\npip install "
                    f"--extra-index-url {FEED} -r requirements.lock\n```\n"
                },
            )
            self.assertEqual(gate.main(), 0)

    def test_a_flag_on_a_continuation_line_still_counts(self):
        # the real command spans three lines; a line-at-a-time check would
        # report it as broken
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {
                    "README.md": "```bash\npip install --require-hashes \\\n"
                    f"  --extra-index-url {FEED} \\\n  -r requirements.lock\n```\n"
                },
            )
            self.assertEqual(gate.main(), 0)

    def test_prose_and_comments_about_the_failure_are_not_instructions(self):
        # a store is expected to document the trap without tripping this gate
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {
                    "CLAUDE.md": "`pip install -r requirements.lock` on its own fails.\n\n"
                    "```bash\n# `pip install -r requirements.lock` fails: no index\n"
                    f"pip install --extra-index-url {FEED} -r requirements.lock\n```\n"
                },
            )
            self.assertEqual(gate.main(), 0)

    def test_a_lock_that_names_its_index_needs_no_flag_anywhere(self):
        """Symmetric, so it survives the fix in either direction: recompiling
        the lock with --emit-index-url makes every command valid, and the gate
        goes quiet without being edited."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITH_INDEX,
                {"README.md": "```bash\npip install -r requirements.lock\n```\n"},
            )
            self.assertEqual(gate.main(), 0)

    def test_a_store_without_a_lock_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("no pins here\n", encoding="utf-8")
            config.configure(root=str(root))
            self.assertEqual(gate.main(), 0)

    def test_workflows_are_checked_outside_fences(self):
        # CI yaml has no fences; its run: blocks are the commands
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {".github/workflows/ci.yml": "steps:\n  - run: pip install -r requirements.lock\n"},
            )
            self.assertEqual(gate.main(), 1)

    def test_generated_directories_are_not_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {
                    "repositories/other/README.md": "```bash\npip install -r requirements.lock\n```\n"
                },
            )
            self.assertEqual(gate.main(), 0, "another repository's docs are not this store's")


if __name__ == "__main__":
    unittest.main()
