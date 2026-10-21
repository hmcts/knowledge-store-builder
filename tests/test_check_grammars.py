"""`check-grammars` must refuse when the interpreter that runs graphify lacks a grammar.

graphify reads `.tf`, `.tfvars`, `.hcl` and `.sql` as nothing when the grammar
package is absent - no error, no node. These tests drive the stage through the
real CLI dispatcher. The IO boundary is the interpreter: each "interpreter" here
is a real executable that answers `-c "import <module>"` the way a python with or
without that package would, and logs what it was asked, so the assertions land on
what the stage did to a process rather than on a stand-in being called.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import settings_isolation  # noqa: F401  (path setup and settings isolation)

from knowledgestore import check_grammars, cli, config  # noqa: E402

try:
    import graphify.extract  # noqa: F401

    HAS_GRAPHIFY = True
except ImportError:  # pragma: no cover - depends on the environment
    HAS_GRAPHIFY = False

needs_graphify = unittest.skipUnless(
    HAS_GRAPHIFY, "needs graphify; extract-ast refuses without it before the grammar check"
)


def run(argv: "list[str]") -> "tuple[int, str, str]":
    out, err = _io.StringIO(), _io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class CheckGrammarsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        config.configure(root=self._tmp.name)
        self.root = config.ROOT
        (self.root / "graphify-out").mkdir(parents=True, exist_ok=True)
        self.bin = Path(self._tmp.name).resolve() / "bin"
        self.bin.mkdir()
        self.log = self.bin / "probes.log"

    def _interpreter(self, name: str, missing: "tuple[str, ...]" = ()) -> str:
        """An executable that fails `import <m>` for each m in `missing`, and logs every probe."""
        path = self.bin / name
        cases = "".join(f"  *{module}*) exit 1;;\n" for module in missing)
        path.write_text(
            f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\ncase "$*" in\n{cases}esac\nexit 0\n',
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def _probes(self) -> "list[str]":
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def _content(self, repo: str, *names: str) -> None:
        files = []
        for name in names:
            path = self.root / "repositories" / repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")
            files.append(str(path))
        detect = self.root / "graphify-out" / ".graphify_detect.json"
        existing = json.loads(detect.read_text("utf-8"))["files"]["code"] if detect.exists() else []
        detect.write_text(json.dumps({"files": {"code": existing + files}}), encoding="utf-8")

    def _pointer(self, where: Path, interpreter: str) -> None:
        where.mkdir(parents=True, exist_ok=True)
        (where / ".graphify_python").write_text(interpreter + "\n", encoding="utf-8")

    def test_a_missing_hcl_grammar_refuses_naming_grammar_count_interpreter_and_fix(self):
        """Catches the check passing when the grammar is absent: .tf then extracts to nothing."""
        python = self._interpreter("py-no-hcl", missing=("tree_sitter_hcl",))
        self._content("repo-a", "a.tf", "b.hcl", "c.py")
        code, out, err = run(["check-grammars", "--interpreter", python])
        self.assertEqual(code, 1, out + err)
        self.assertIn("HCL grammar (tree_sitter_hcl) is missing for 2 file(s)", err)
        self.assertIn(python, err)
        self.assertIn("hmcts-knowledge-store-builder[ast]", err)

    def test_a_missing_sql_grammar_refuses(self):
        """Catches the SQL grammar going unchecked while only HCL is."""
        python = self._interpreter("py-no-sql", missing=("tree_sitter_sql",))
        self._content("repo-a", "a.sql", "main.tf")
        code, _, err = run(["check-grammars", "--interpreter", python])
        self.assertEqual(code, 1)
        self.assertIn("SQL grammar (tree_sitter_sql) is missing for 1 file(s)", err)
        self.assertNotIn("HCL grammar", err)

    def test_both_grammars_present_passes_and_says_which_interpreter_was_probed(self):
        """Catches a refusal on a healthy environment, and output that cannot be audited."""
        python = self._interpreter("py-ok")
        self._content("repo-a", "a.tf", "a.sql")
        code, out, err = run(["check-grammars", "--interpreter", python])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"probed {python} (--interpreter)", out)

    def test_no_hcl_files_means_no_hcl_probe(self):
        """Catches probing for a grammar nothing needs, which would refuse stores with no HCL."""
        python = self._interpreter("py-no-hcl", missing=("tree_sitter_hcl",))
        self._content("repo-a", "schema.sql", "app.py")
        code, _, _ = run(["check-grammars", "--interpreter", python])
        self.assertEqual(code, 0)
        self.assertEqual(len(self._probes()), 1)
        self.assertNotIn("tree_sitter_hcl", " ".join(self._probes()))

    def test_no_hcl_or_sql_files_probes_nothing_and_says_so(self):
        """Catches a probe, or silence, when the extensions do not occur."""
        python = self._interpreter("py-none", missing=("tree_sitter_hcl", "tree_sitter_sql"))
        self._content("repo-a", "app.py")
        code, out, _ = run(["check-grammars", "--interpreter", python])
        self.assertEqual(code, 0)
        self.assertEqual(self._probes(), [])
        self.assertIn("nothing to probe", out)

    def test_a_repositorys_pointer_is_honoured(self):
        """Catches the pointer being ignored: the wrong interpreter would be certified."""
        python = self._interpreter("py-repo", missing=("tree_sitter_hcl",))
        self._pointer(self.root / "repositories" / "repo-a" / "graphify-out", python)
        self._content("repo-a", "a.tf")
        code, _, err = run(["check-grammars"])
        self.assertEqual(code, 1)
        self.assertIn(f"{python} (repo-a/graphify-out/.graphify_python)", err)

    def test_a_store_level_pointer_is_honoured_when_the_repository_has_none(self):
        """Catches a store that keeps one pointer at its root being probed with the wrong python."""
        python = self._interpreter("py-store", missing=("tree_sitter_hcl",))
        self._pointer(self.root / "graphify-out", python)
        self._content("repo-a", "a.tf")
        code, _, err = run(["check-grammars"])
        self.assertEqual(code, 1)
        self.assertIn(f"{python} (store-level graphify-out/.graphify_python)", err)

    def test_the_repository_pointer_beats_the_store_pointer(self):
        """Catches precedence inverted: a repository's own graphify would go unprobed."""
        broken = self._interpreter("py-broken", missing=("tree_sitter_hcl",))
        healthy = self._interpreter("py-healthy")
        self._pointer(self.root / "graphify-out", broken)
        self._pointer(self.root / "repositories" / "repo-a" / "graphify-out", healthy)
        self._content("repo-a", "a.tf")
        code, out, err = run(["check-grammars"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(healthy, out)
        self.assertNotIn("py-broken", " ".join(self._probes()))

    def test_the_explicit_interpreter_beats_every_pointer(self):
        """Catches --interpreter being overridden by a pointer it was given to override."""
        pointed = self._interpreter("py-pointed")
        explicit = self._interpreter("py-explicit", missing=("tree_sitter_hcl",))
        self._pointer(self.root / "graphify-out", pointed)
        self._pointer(self.root / "repositories" / "repo-a" / "graphify-out", pointed)
        self._content("repo-a", "a.tf")
        code, _, err = run(["check-grammars", "--interpreter", explicit])
        self.assertEqual(code, 1)
        self.assertIn(explicit, err)
        self.assertNotIn("py-pointed", " ".join(self._probes()))

    def test_with_no_pointer_the_probe_targets_the_running_interpreter(self):
        """Catches a fallback to a `graphify` on PATH: a store running `python -m graphify` means this one."""
        self._content("repo-a", "a.tf")
        code, out, err = run(["check-grammars"])
        self.assertIn(f"{sys.executable} (sys.executable)", out + err)
        # Whether it passes depends on this environment; the exit code must agree with
        # whether this interpreter can really import the grammar.
        try:
            import tree_sitter_hcl  # noqa: F401

            present = True
        except ImportError:
            present = False
        self.assertEqual(code, 0 if present else 1)

    @needs_graphify
    def test_extract_ast_refuses_before_extracting_when_the_grammar_is_missing(self):
        """Catches extract-ast writing a short layer for .tf: it must refuse, and write nothing."""
        self._content("repo-a", "a.tf")
        failed = subprocess.CompletedProcess([], 1)
        # The true IO boundary: the interpreter's answer to the import probe.
        with mock.patch.object(check_grammars.subprocess, "run", return_value=failed):
            code, _, err = run(["extract-ast", "--timeout", "0"])
        self.assertEqual(code, 1)
        self.assertIn("Refusing to extract", err)
        self.assertIn("HCL grammar (tree_sitter_hcl) is missing for 1 file(s)", err)
        self.assertIn(f"{sys.executable} (extract-ast's own interpreter)", err)
        self.assertFalse((self.root / "graphify-out" / ".graphify_ast.json").exists())


if __name__ == "__main__":
    unittest.main()
