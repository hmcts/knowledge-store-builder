"""The corpus can carry configuration the harness reads as its own.

Cloning the estate into `repositories/` inside the store's working directory puts
the corpus inside the tree a coding harness inspects for instructions. The
extraction spec already treats file *contents* as untrusted and that works —
agents meeting instruction-shaped text report it rather than acting on it. The
undefended path is the file an agent never chooses to open: a harness loads
`.claude/settings.json` itself, and hooks declared there are executed rather than
read (issue #118).

An operator enumerating these by hand missed 8 of 18 on a real estate, which is
why the detection errs wide and why a count is worth more than a path list.

This stage reports; it never removes. An `AGENTS.md` inside a repository is
legitimate corpus that a question might be about, and deleting it would make the
store quietly unfaithful to the estate.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import check_corpus_config as check  # noqa: E402


class ScanTest(SettingsIsolated):
    def _corpus(self, layout: dict) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "repositories"
        for rel, text in layout.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return root

    def test_it_finds_instruction_files_across_repositories(self):
        root = self._corpus(
            {"a/CLAUDE.md": "x", "b/AGENTS.md": "y", "c/docs/AGENTS.md": "z", "d/README.md": "no"}
        )
        found = check.scan([root])
        self.assertEqual(len(found["instructions"]["AGENTS.md"]), 2)
        self.assertEqual(len(found["instructions"]["CLAUDE.md"]), 1)
        self.assertNotIn("README.md", found["instructions"])

    def test_settings_declaring_hooks_are_called_out_separately(self):
        """Instruction files are read; hooks are run. Only one of those is execution."""
        root = self._corpus(
            {
                "a/.claude/settings.json": json.dumps({"hooks": {"Stop": []}}),
                "b/.claude/settings.json": json.dumps({"theme": "dark"}),
            }
        )
        found = check.scan([root])
        self.assertEqual(len(found["executable"]), 1)
        self.assertIn("a/.claude/settings.json", found["executable"][0])

    def test_a_malformed_settings_file_is_not_treated_as_executable(self):
        root = self._corpus({"a/.claude/settings.json": "{not json"})
        self.assertEqual(check.scan([root])["executable"], [])

    def test_the_stores_own_git_metadata_is_not_scanned(self):
        root = self._corpus({"a/.git/CLAUDE.md": "not corpus"})
        self.assertEqual(check.scan([root])["instructions"], {})

    def test_an_absent_corpus_is_not_an_error(self):
        """`check-corpus` before `sync` should say nothing rather than fail."""
        self.assertEqual(check.scan([Path("/nonexistent-corpus")])["instructions"], {})


class ExitCodeTest(SettingsIsolated):
    def _run(self, layout: dict, argv=None) -> int:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, text in layout.items():
            path = root / "repositories" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (root / "external").mkdir(exist_ok=True)
        config.configure(REPOSITORIES_DIR=root / "repositories", EXTERNAL_DIR=root / "external")
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            return check.main(argv or [])

    def test_a_clean_corpus_passes(self):
        self.assertEqual(self._run({"a/main.tf": "resource {}"}), 0)

    def test_executable_configuration_fails_without_strict(self):
        """Hooks are the case that does not need a flag to be worth stopping for."""
        code = self._run({"a/.claude/settings.json": json.dumps({"hooks": {"Stop": []}})})
        self.assertEqual(code, 1)

    def test_instruction_files_alone_pass_unless_strict(self):
        layout = {"a/CLAUDE.md": "x"}
        self.assertEqual(self._run(layout), 0)
        self.assertEqual(self._run(layout, ["--strict"]), 1)


if __name__ == "__main__":
    unittest.main()
