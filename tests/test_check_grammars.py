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


class _Fixture:
    """Shared fixture, deliberately not a TestCase so subclasses do not re-run each other."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        config.configure(root=self._tmp.name)
        self.root = config.ROOT
        (self.root / "graphify-out").mkdir(parents=True, exist_ok=True)
        self.bin = Path(self._tmp.name).resolve() / "bin"
        self.bin.mkdir()
        self.log = self.bin / "probes.log"

    def _interpreter(
        self, name: str, missing: "tuple[str, ...]" = (), delegate: bool = False
    ) -> str:
        """An executable that fails `import <m>` for each m in `missing`, and logs every probe.

        With `delegate` every other invocation runs the real interpreter, so graphify's
        own file detection really runs; without it the executable answers nothing to
        that script, which is what an interpreter with no usable graphify does.
        """
        path = self.bin / name
        cases = "".join(f"  *{module}*) exit 1;;\n" for module in missing)
        ending = f'exec "{sys.executable}" "$@"\n' if delegate else "exit 0\n"
        path.write_text(
            f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\ncase "$*" in\n{cases}esac\n{ending}',
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def _declare(self, *names: str) -> None:
        config_file = config.REPOSITORIES_CONFIG
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            "".join(f"{n}|https://example.invalid/{n}.git|main\n" for n in names),
            encoding="utf-8",
        )

    def _clone_file(self, repo: str, relative: str) -> None:
        path = self.root / "repositories" / repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('resource "a" "b" {}\n', encoding="utf-8")

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


class CheckGrammarsTest(_Fixture, unittest.TestCase):
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

    def test_a_content_set_listing_a_path_twice_counts_the_file_once(self):
        """Catches counting entries rather than files: a doubled detect file reported 2x."""
        python = self._interpreter("py-no-hcl", missing=("tree_sitter_hcl",))
        self._content("repo-a", "a.tf")
        self._content("repo-a", "a.tf")  # appends the same path again
        code, _, err = run(["check-grammars", "--interpreter", python])
        self.assertEqual(code, 1)
        self.assertIn("missing for 1 file(s)", err)

    def test_a_symlinked_spelling_of_the_same_file_counts_once(self):
        """Catches deduplicating on the spelling of the path rather than the file it names."""
        python = self._interpreter("py-no-hcl", missing=("tree_sitter_hcl",))
        self._content("repo-a", "a.tf")
        real = self.root / "repositories" / "repo-a" / "a.tf"
        alias = self.root / "repositories" / "repo-a" / "alias.tf"
        alias.symlink_to(real)
        self._content("repo-a", "alias.tf")
        code, _, err = run(["check-grammars", "--interpreter", python])
        self.assertEqual(code, 1)
        self.assertIn("missing for 1 file(s)", err)

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


class ClonesTest(_Fixture, unittest.TestCase):
    """`--clones` scans what is on disk, not a content set that may predate the resync.

    The interpreter here answers nothing to graphify's file detection, so the file
    list is the library's approximate walk.
    """

    def test_a_tf_the_content_set_does_not_list_is_caught(self):
        """Catches --clones trusting a stale content set: a resync's new .tf files go unchecked."""
        python = self._interpreter("py-no-hcl", missing=("tree_sitter_hcl",))
        self._declare("repo-a")
        self._content("repo-a", "old.py")  # the stale content set lists no .tf
        self._clone_file("repo-a", "new/main.tf")
        code, _, err = run(["check-grammars", "--clones", "--interpreter", python])
        self.assertEqual(code, 1, err)
        self.assertIn("HCL grammar (tree_sitter_hcl) is missing for 1 file(s)", err)

    def test_a_repository_not_declared_is_not_scanned(self):
        """Catches walking repositories/ instead of the declaration: a stray clone would refuse."""
        python = self._interpreter("py-no-hcl", missing=("tree_sitter_hcl",))
        self._declare("repo-a")
        self._clone_file("repo-a", "app.py")
        self._clone_file("stray", "main.tf")
        code, out, err = run(["check-grammars", "--clones", "--interpreter", python])
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("nothing to probe", out)

    def test_a_graphifyignored_tf_is_not_counted_and_an_unignored_one_is(self):
        """Catches counting files graphify will skip, and missing ones it will read (approximate)."""
        python = self._interpreter("py-no-hcl", missing=("tree_sitter_hcl",))
        self._declare("repo-a")
        self._clone_file("repo-a", "vendor/skipped.tf")
        (self.root / "repositories" / "repo-a" / ".graphifyignore").write_text(
            "vendor/\n", encoding="utf-8"
        )
        code, out, _ = run(["check-grammars", "--clones", "--interpreter", python])
        self.assertEqual(code, 0, out)
        self._clone_file("repo-a", "kept.tf")
        code, _, err = run(["check-grammars", "--clones", "--interpreter", python])
        self.assertEqual(code, 1)
        self.assertIn("missing for 1 file(s)", err)

    def test_a_file_pattern_in_graphifyignore_excludes_that_file_only(self):
        """Catches the fallback honouring directory patterns but not file patterns."""
        python = self._interpreter("py-no-hcl", missing=("tree_sitter_hcl",))
        self._declare("repo-a")
        self._clone_file("repo-a", "net.generated.tf")
        (self.root / "repositories" / "repo-a" / ".graphifyignore").write_text(
            "*.generated.tf\n", encoding="utf-8"
        )
        code, out, _ = run(["check-grammars", "--clones", "--interpreter", python])
        self.assertEqual(code, 0, out)
        self._clone_file("repo-a", "net.tf")
        code, _, err = run(["check-grammars", "--clones", "--interpreter", python])
        self.assertEqual(code, 1)
        self.assertIn("missing for 1 file(s)", err)

    def test_each_glob_form_in_graphifyignore_excludes_what_gitignore_would(self):
        """Catches a glob form translated wrongly: `*` crossing `/`, `?` matching `/` or
        more than one character, a class matching the wrong set, or a bad `[` raising.

        Each case is its own clone, so the count is that pattern's alone. The expected
        counts are hand-derived from gitignore: a pattern without a `/` matches a name at
        any depth, one with a `/` is anchored to the clone root. Every case but the last
        agrees with `git check-ignore`; the unterminated `[` is the one place this
        library follows graphify's literal reading, where git itself would never match.
        """
        python = self._interpreter("py-no-hcl", missing=("tree_sitter_hcl",))
        cases = [
            # `*` stays inside one path segment.
            ("docs/*.tf", ["docs/x.tf", "docs/deep/x.tf", "other/x.tf"], 2),
            # `*` may match nothing and applies at any depth when unanchored.
            ("a*.tf", ["a.tf", "ab.tf", "sub/ax.tf", "b.tf", "a/b.tf"], 2),
            # `?` is exactly one character, and never `/`.
            ("v?.tf", ["v1.tf", "sub/vx.tf", "v.tf", "v12.tf"], 2),
            ("x/a?b.tf", ["x/aXb.tf", "x/a/b.tf", "x/ab.tf"], 2),
            # A class matches one listed character.
            ("f[abc].tf", ["fa.tf", "fb.tf", "fc.tf", "fd.tf", "fab.tf"], 2),
            # A negated class matches one character that is not listed.
            ("f[!abc].tf", ["fa.tf", "fd.tf", "fe.tf", "f!.tf", "fab.tf"], 2),
            # An unterminated `[` is a literal `[`, not an error and not a class.
            ("a[b.tf", ["a[b.tf", "ab.tf", "a.tf"], 2),
        ]
        for index, (pattern, files, expected) in enumerate(cases):
            with self.subTest(pattern=pattern):
                repo = f"repo-{index}"
                self._declare(repo)
                for name in files:
                    self._clone_file(repo, name)
                (self.root / "repositories" / repo / ".graphifyignore").write_text(
                    pattern + "\n", encoding="utf-8"
                )
                code, out, err = run(["check-grammars", "--clones", "--interpreter", python])
                self.assertEqual(code, 1, out + err)
                self.assertIn(f"missing for {expected} file(s)", err)

    def test_a_pipeline_output_directory_and_git_are_not_counted(self):
        """Catches the fallback counting .tf inside .git or the pipeline's own output."""
        python = self._interpreter("py-no-hcl", missing=("tree_sitter_hcl",))
        self._declare("repo-a")
        self._clone_file("repo-a", ".git/x.tf")
        self._clone_file("repo-a", "graphify-out/y.tf")
        code, out, _ = run(["check-grammars", "--clones", "--interpreter", python])
        self.assertEqual(code, 0, out)

    def test_the_approximate_method_is_named(self):
        """Catches a silent fallback reading as the exact check."""
        python = self._interpreter("py-fallback", missing=("tree_sitter_hcl",))
        self._declare("repo-a")
        self._clone_file("repo-a", "main.tf")
        _, _, err = run(["check-grammars", "--clones", "--interpreter", python])
        self.assertIn("file list: library exclusions + .graphifyignore parse (approximate)", err)
        self.assertNotIn("(exact)", err)

    def test_a_missing_declaration_refuses(self):
        """Catches --clones passing when it had no repositories to look at."""
        code, _, err = run(["check-grammars", "--clones"])
        self.assertEqual(code, 1)
        self.assertIn("Cannot read the declared repositories", err)


@needs_graphify
class ClonesWithGraphifyTest(_Fixture, unittest.TestCase):
    """The exact route: graphify's own file detection, run in the probed interpreter."""

    def test_the_exact_method_is_named(self):
        """Catches the exact route being reported as the approximate one, or the reverse."""
        python = self._interpreter("py-delegating", missing=("tree_sitter_hcl",), delegate=True)
        self._declare("repo-a")
        self._clone_file("repo-a", "main.tf")
        _, _, err = run(["check-grammars", "--clones", "--interpreter", python])
        self.assertIn("file list: graphify detection (exact)", err)
        self.assertIn("missing for 1 file(s)", err)

    def test_graphify_decides_what_is_vendored(self):
        """Catches the library re-deriving exclusions: graphify skips node_modules itself."""
        python = self._interpreter("py-delegating", missing=("tree_sitter_hcl",), delegate=True)
        self._declare("repo-a")
        self._clone_file("repo-a", "node_modules/pkg/x.tf")
        code, out, _ = run(["check-grammars", "--clones", "--interpreter", python])
        self.assertEqual(code, 0, out)
        self.assertIn("(exact)", out)

    def test_a_graphifyignored_tf_is_not_counted_by_graphify_and_an_unignored_one_is(self):
        """Catches graphify's .graphifyignore being bypassed on the exact route."""
        python = self._interpreter("py-delegating", missing=("tree_sitter_hcl",), delegate=True)
        self._declare("repo-a")
        self._clone_file("repo-a", "generated/skipped.tf")
        (self.root / "repositories" / "repo-a" / ".graphifyignore").write_text(
            "generated/\n", encoding="utf-8"
        )
        code, out, _ = run(["check-grammars", "--clones", "--interpreter", python])
        self.assertEqual(code, 0, out)
        self._clone_file("repo-a", "kept.tf")
        code, _, err = run(["check-grammars", "--clones", "--interpreter", python])
        self.assertEqual(code, 1)
        self.assertIn("missing for 1 file(s)", err)


if __name__ == "__main__":
    unittest.main()
