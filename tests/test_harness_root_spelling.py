"""A configured root has one spelling, and every harness must hold that spelling.

`config.configure` resolves the root it is given, because every ROOT-relative
path is re-derived from it and two spellings of one directory would make paths
written under one unequal to paths read under the other.

A test harness that keeps `Path(tempfile.TemporaryDirectory().name)` as its own
root breaks that. On Linux nothing happens: the temp directory has no symlink in
its path, so the resolved and unresolved spellings are the same string. On macOS
the temp directory is reached through /var -> /private/var, the two spellings
differ, and the harness silently compares stage output against paths spelled the
other way.

The cost was not a red suite - it was a *disagreeing* one. The mutation gate's
observer table records which tests notice each historical defect, and three tests
were recorded as observing `the file-list route grows a directory walk` because
they failed on a maintainer's machine under that mutation. They failed on the
path spelling, not on the walk. CI disagreed, main went red, and the table in the
repository was wrong in a way that reads as a platform bug.

Both tests below construct their own symlink rather than relying on the operating
system to supply one, so each fails on Linux as well as macOS. A check for this
that only fired on macOS would not have caught the thing that went wrong, because
the disagreement was CI reporting the truth.
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

import settings_isolation  # noqa: F401  (path setup and settings isolation)

from knowledgestore import config  # noqa: E402


class ConfiguredRootHasOneSpelling(unittest.TestCase):
    def setUp(self):
        self._old_root = config.ROOT
        self.addCleanup(lambda: config.configure(root=str(self._old_root)))
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # A symlink we make ourselves, so the divergence exists on every platform.
        self.target = pathlib.Path(self._tmp.name).resolve() / "target"
        self.target.mkdir()
        self.link = pathlib.Path(self._tmp.name).resolve() / "link"
        self.link.symlink_to(self.target)

    def test_configure_resolves_a_root_reached_through_a_symlink(self):
        """Drop the `.resolve()` in `configure` and ROOT keeps the link spelling.

        Every path in `_recompute_paths` hangs off ROOT, so an unresolved ROOT
        makes `config.GRAPH_PATH` and a path the caller derived from the real
        directory two unequal strings naming one file.
        """
        config.configure(root=str(self.link))
        self.assertEqual(config.ROOT, self.target)
        self.assertEqual(config.REPOSITORIES_DIR, self.target / "repositories")

    def test_the_shared_harness_keeps_the_spelling_config_uses(self):
        """`self.root = Path(self._tmp.name)` in the harness fails here.

        The harness is driven for real - its own setUp runs - and the assertion
        is the one the mutation gate needed: a path built from the harness root
        is the same string as the path a walk of the configured tree returns.
        """
        # `tempfile.tempdir`, not the TMPDIR environment variable: tempfile
        # caches the directory on first use, so setting TMPDIR here is ignored
        # and the harness lands under the operating system's own temp path.
        # That is not a failing test but a *vacuous* one - on Linux, whose temp
        # path holds no symlink, it would pass with the bug present.
        old_tmpdir = tempfile.tempdir
        tempfile.tempdir = str(self.link)

        # Defined here rather than at module scope: a TestCase subclass at
        # module scope is collected by `unittest discover` and run as a test.
        class _Harness(settings_isolation.EstateGraphIsolated):
            """The shared harness, driven for real as the subject under test."""

            def runTest(self):  # pragma: no cover - only setUp is wanted
                raise AssertionError("instantiated only to exercise setUp")

        try:
            harness = _Harness()
            harness.setUp()
        finally:
            tempfile.tempdir = old_tmpdir
        try:
            # Sensitivity, asserted in the same run: unless the harness really
            # landed under the symlink this test built there is no divergence
            # to detect, and a green result would mean nothing.
            self.assertTrue(
                str(harness.root).startswith((str(self.target), str(self.link))),
                f"vacuous: the harness ignored the symlinked temp root ({harness.root})",
            )
            self.assertEqual(harness.root, config.ROOT)
            walked = harness.root / "repositories" / "repo-a" / "a.py"
            walked.parent.mkdir(parents=True)
            walked.write_text("x = 1\n", encoding="utf-8")
            found = list(config.REPOSITORIES_DIR.rglob("*.py"))
            self.assertEqual([str(path) for path in found], [str(walked)])
        finally:
            harness.tearDown()


if __name__ == "__main__":
    unittest.main()
