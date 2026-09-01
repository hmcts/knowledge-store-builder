"""One rule for paths in a store's own files, and the defect that motivated it.

The regression these pin reached a real store's `main`: `relative()` opened with
`Path(s).resolve().relative_to(root)`, and `resolve()` follows symlinks, so converting a
corpus inventory rewrote each symlink's path into its *target's* path. A change of
identity wearing the costume of a change of format.

It survived review because every check a maintainer would naturally run still passed: the
entry counts were unchanged, the JSON was well-formed, and every resulting path existed on
disk. `test_relative_does_not_follow_symlinks` is what catches it, and it is three lines.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import config, store_paths


class StorePathsTest(SettingsIsolated):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # `config` resolves the root it is given, and on macOS /var is a symlink to
        # /private/var — so a fixture comparing against the unresolved temp path fails on
        # the symlink the module under test exists to handle. Resolve here to match.
        self.root = Path(self._tmp.name).resolve()
        (self.root / "repositories").mkdir()
        (self.root / "graphify-out").mkdir()
        config.configure(root=str(self.root))
        self.addCleanup(self._tmp.cleanup)

    def _repo(self, name: str) -> Path:
        d = self.root / "repositories" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_a_store_living_under_a_directory_called_repositories(self):
        """`~/repositories/<store>` is an ordinary place to keep clones, and it broke this.

        `relative()` matched the FIRST `repositories/` in the string, which for such a
        store is the parent holding the clone, not the corpus inside it. Every path then
        kept the store's own directory name as a prefix, so `absolute()` re-rooted it a
        second time and the round trip named a file that does not exist.

        Silent in exactly the way that matters: the conversion succeeds, the entry counts
        reconcile, the JSON is well-formed - and every path in it is unresolvable.
        """
        outer = Path(self._tmp.name).resolve() / "repositories" / "a-store"
        (outer / "repositories" / "infra").mkdir(parents=True)
        (outer / "graphify-out").mkdir()
        config.configure(root=str(outer))
        f = outer / "repositories" / "infra" / "main.tf"
        f.write_text("resource {}\n")

        self.assertEqual(store_paths.relative(f), "repositories/infra/main.tf")
        # And the round trip must land back on the real file, not one directory deeper.
        self.assertEqual(Path(store_paths.absolute(store_paths.relative(f))), f)

    def test_relative_does_not_follow_symlinks(self):
        """A link and its target must relativise to DIFFERENT paths.

        This is the whole defect. If `relative()` resolves, both sides of this assertion
        become the target and it passes vacuously — so it asserts they differ, not merely
        that each is right.
        """
        repo = self._repo("infra")
        (repo / "environments").mkdir()
        (repo / "environments" / "variables.tf").write_text("variable {}\n")
        (repo / "components").mkdir()
        (repo / "components" / "a").mkdir()
        (repo / "components" / "a" / "variables.tf").symlink_to("../../environments/variables.tf")

        link = repo / "components" / "a" / "variables.tf"
        target = repo / "environments" / "variables.tf"

        self.assertEqual(store_paths.relative(link), "repositories/infra/components/a/variables.tf")
        self.assertEqual(
            store_paths.relative(target), "repositories/infra/environments/variables.tf"
        )
        self.assertNotEqual(store_paths.relative(link), store_paths.relative(target))

    def test_relative_is_idempotent(self):
        once = store_paths.relative(self.root / "repositories/a/b.tf")
        self.assertEqual(store_paths.relative(once), once)

    def test_relative_accepts_a_foreign_absolute_root(self):
        """What makes an archive written before a relocation readable after one."""
        foreign = "/some/other/machine/store/repositories/x/main.tf"
        self.assertEqual(store_paths.relative(foreign), "repositories/x/main.tf")

    def test_relative_leaves_a_path_outside_the_store_alone(self):
        self.assertEqual(store_paths.relative("/etc/hosts"), "/etc/hosts")

    def test_absolute_is_the_inverse_and_idempotent(self):
        rel = "repositories/x/main.tf"
        absolute = store_paths.absolute(rel)
        self.assertEqual(absolute, str(self.root / rel))
        self.assertEqual(store_paths.absolute(absolute), absolute)

    def test_load_plan_returns_absolute_whatever_the_file_holds(self):
        """Readers keep the behaviour they had; only the storage format changes.

        The extraction spec requires `source_file` to reach an agent "verbatim and
        absolute", so the in-flight form is not free to change. Storage format and wire
        format are separate decisions, and conflating them is what produced the defect.
        """
        plan = self.root / "graphify-out/.graphify_chunk_plan.json"
        plan.write_text(json.dumps({"0001": ["repositories/x/main.tf"]}))

        loaded = store_paths.load_plan(plan)
        self.assertEqual(loaded["0001"], [str(self.root / "repositories/x/main.tf")])

    def test_store_relative_plan_round_trips(self):
        original = {"0001": ["repositories/x/main.tf", "repositories/y/b.tf"]}
        plan = self.root / "graphify-out/p.json"
        plan.write_text(json.dumps(original))
        self.assertEqual(store_paths.store_relative_plan(store_paths.load_plan(plan)), original)

    def test_the_root_is_read_at_call_time_not_import_time(self):
        """A module-level ROOT constant binds whichever root was current at import.

        That is how a suite silently measures the wrong store, so the seam is asserted
        rather than assumed.
        """
        with tempfile.TemporaryDirectory() as other:
            # Resolve before configuring, and compare against the same value.
            # Handing `configure` an unresolved path and asserting against a
            # resolved one makes this test depend on `configure` resolving - which
            # is invisible on Linux and, on macOS, quietly turns it into an
            # observer of a behaviour it is not about. `configure`'s resolve has
            # its own test in test_harness_root_spelling.
            root = Path(other).resolve()
            config.configure(root=str(root))
            self.assertEqual(
                store_paths.absolute("repositories/x/main.tf"),
                str(root / "repositories/x/main.tf"),
            )
