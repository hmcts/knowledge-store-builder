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

import pathlib
import sys
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
