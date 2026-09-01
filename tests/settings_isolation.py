"""Every test starts from the settings the process started with.

Stage modules read `config` at call time rather than copying it at import, so a
setting one test overrides is visible to every test that follows it in the same
process. That is the point of the design - `configure()` now reaches code that
is already imported - but it means test independence has to be deliberate.

Before, overrides landed on individual stage modules (`summaries.GRAPH_PATH`),
so tests mostly missed each other by accident: a test overriding provenance's
copy left `config.PROVENANCE_PATH` pristine for the status tests that read it.
That accident is what kept the suite green, not isolation.

`run` is the hook, not `setUp`/`tearDown`: a subclass that defines its own
`setUp` without calling `super().setUp()` would silently opt out of isolation,
and most of the classes here do exactly that.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

# Prefer the working tree over any installed copy of the library. `python -m
# unittest discover`, which is what CI runs, gets no path help, so on a machine
# holding a non-editable install of the released package the suite would test
# site-packages instead - passing without running your changes at all. pytest
# gets this from pyproject's `pythonpath`; this covers the other runner. Every
# test module imports this one before it imports knowledgestore, so the path is
# already right by the time the library is first loaded.
_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from knowledgestore import config  # noqa: E402


def _settings() -> dict[str, object]:
    return {name: value for name, value in vars(config).items() if name.isupper()}


class SettingsIsolated(unittest.TestCase):
    """A TestCase whose settings changes cannot reach another test."""

    def run(self, result=None):
        saved = _settings()
        try:
            return super().run(result)
        finally:
            for name, value in saved.items():
                setattr(config, name, value)
            for name in set(_settings()) - set(saved):
                delattr(config, name)


class EstateGraphIsolated(SettingsIsolated):
    """A temporary store holding one graph, for tests about matching estate names.

    Two modules needed byte-identical scaffolding - a temp root, a configured
    `config.ROOT`, a one-node-per-label graph, and `absent_from_estate` with its
    stderr swallowed. Shared here rather than copied, because a copy drifts: the
    point of both modules is what the matcher does with a label, and a difference
    in how the graph was written would be indistinguishable from a difference in
    the matcher.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self._old_root = config.ROOT
        config.configure(root=self._tmp.name)
        # Take the root back *from* config rather than from `tempfile`.
        # `configure` resolves what it is given, and on macOS the temp directory
        # is reached through the /var -> /private/var symlink, so the two
        # spellings of the same directory are unequal strings. A harness holding
        # the unresolved spelling compares its own paths against stage output
        # spelled the other way and fails for a reason that cannot occur on
        # Linux. Deriving it here makes the divergence impossible rather than
        # something each module has to remember to undo.
        self.root = config.ROOT
        (self.root / "graphify-out").mkdir(parents=True)

    def tearDown(self):
        config.configure(root=str(self._old_root))
        self._tmp.cleanup()
        super().tearDown()

    def write_graph(self, labels):
        config.GRAPH_PATH.write_text(
            json.dumps({"nodes": [{"id": f"n{i}", "label": v} for i, v in enumerate(labels)]}),
            encoding="utf-8",
        )

    def _absent(self, unsupported):
        from knowledgestore import build_community_summaries as summaries

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return summaries.absent_from_estate(unsupported)
