"""The shared harness holds the spelling `config` holds.

The break this catches: `store_root` handing back the raw `TemporaryDirectory`
name. On Linux that is indistinguishable from the resolved path and every
assertion still passes, so the regression would ship green and only show up on
macOS - as a path that stays absolute where it should have relativised, which
reads as a product defect rather than a harness one.

Asserted against `config.ROOT` rather than against `Path.resolve()` alone,
because the property that matters is agreement with the thing the product
reads, not resolution for its own sake.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from knowledgestore import config  # noqa: E402

from settings_isolation import SettingsIsolated, store_root  # noqa: E402


class StoreRootMatchesConfig(SettingsIsolated):
    def test_the_root_it_returns_is_already_resolved(self):
        root = store_root(self)
        self.assertEqual(root, root.resolve(), "store_root handed back an unresolved path")

    def test_configure_holds_the_same_spelling(self):
        """The invariant the helper exists for, not resolution in the abstract."""
        root = store_root(self)
        config.configure(root=str(root))
        self.assertEqual(
            config.ROOT,
            root,
            "the harness and config disagree on how to spell the same directory; "
            "store_paths cannot relativise against a root it is not given",
        )

    def test_an_unresolved_root_is_what_this_prevents(self):
        """The control, so the two assertions above are not passing vacuously.

        On a platform where the temporary directory is reached through a symlink
        the raw name differs from the resolved one; where it is not, the two
        agree and there was nothing to prevent. Either outcome is fine - what
        must not happen is `store_root` returning the raw name.
        """
        raw = tempfile.TemporaryDirectory()
        self.addCleanup(raw.cleanup)
        unresolved = pathlib.Path(raw.name)
        config.configure(root=str(unresolved))
        self.assertEqual(
            config.ROOT,
            unresolved.resolve(),
            "configure is expected to resolve; if it stops, store_root's reason is gone",
        )


if __name__ == "__main__":
    unittest.main()
