"""`knowledgestore manifest` writes the store manifest, and `status` reads it back.

Every test builds a real store layout in a temporary directory, runs the real
stage through `cli.main` the way an operator does, and asserts on the file it
wrote and the lines it printed. The git repository behind the store identity is
a real one. Each test names the break it catches.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated
from knowledgestore import cli, config, status
from test_store_manifest import _git_repo, _touch


class _Run(SettingsIsolated):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Resolved: configure() resolves the root, so a temp dir under a symlinked
        # prefix would otherwise differ from the one the stage reports.
        self.root = Path(tmp.name).resolve()
        self.path = self.root / "knowledge" / "store-manifest.json"

    def run_stage(self, store_id: str | None = "example-store") -> tuple[int, str, str]:
        config.configure(root=self.root, STORE_ID=store_id or "")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["manifest"])
        return code, out.getvalue(), err.getvalue()


class WritingTheManifest(_Run):
    def test_a_rerun_writes_a_byte_identical_file(self):
        """Breaks if the file holds anything that moves between runs - a clock, a
        modification time, an unordered key - so a rebuild churns the commit."""
        _touch(self.root, "knowledge/provenance.json")
        _touch(self.root, "graphify-out/graph.json.gz", "graph")
        self.assertEqual(self.run_stage()[0], 0)
        first = self.path.read_bytes()
        for path in self.root.rglob("*"):
            os.utime(path, (1_700_000_000, 1_700_000_000))
        self.assertEqual(self.run_stage()[0], 0)
        self.assertEqual(self.path.read_bytes(), first)

    def test_the_file_is_the_manifest_in_a_fixed_text_form(self):
        """Breaks if the file stops being sorted two-space-free indent-1 JSON with a
        trailing newline: that exact text is what a committed diff is read against."""
        _touch(self.root, "knowledge/provenance.json")
        self.run_stage()
        text = self.path.read_text(encoding="utf-8")
        document = json.loads(text)
        self.assertEqual(text, json.dumps(document, indent=1, sort_keys=True) + "\n")
        self.assertEqual(document["store"]["id"], "example-store")
        self.assertTrue(document["layers"]["provenance"]["present"])

    def test_a_fresh_store_still_gets_a_manifest_and_exit_0(self):
        """Breaks if a store with nothing built is refused: the manifest must say
        every layer is absent, not be missing."""
        code, _, _ = self.run_stage()
        self.assertEqual(code, 0)
        layers = json.loads(self.path.read_text(encoding="utf-8"))["layers"]
        measured = {name: layer["present"] for name, layer in layers.items()}
        self.assertEqual(
            {name for name, present in measured.items() if present is not False}, {"deployments"}
        )

    def test_the_write_leaves_no_temporary_file_beside_it(self):
        """Breaks if the atomic write leaves its temporary file behind, which a
        `git add knowledge/` would then commit."""
        self.run_stage()
        self.assertEqual(
            sorted(p.name for p in self.path.parent.iterdir()), ["store-manifest.json"]
        )

    def test_a_failed_write_leaves_the_previous_manifest_intact(self):
        """Breaks if the file is written in place: a write that dies half way would
        leave a reader a truncated manifest where the old, whole one stood."""
        self.run_stage()
        before = self.path.read_bytes()
        _touch(self.root, "knowledge/provenance.json")
        # A directory where the temporary file goes makes the write fail for real;
        # an in-place write would not touch it and would replace the old manifest.
        (self.path.parent / "store-manifest.json.tmp").mkdir()
        code, _, err = self.run_stage()
        self.assertEqual(code, 2)
        self.assertIn("store-manifest.json", err)
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_failed_rename_leaves_no_temporary_file(self):
        """Breaks if a write that fails at the rename leaves its temporary file for
        a `git add knowledge/` to commit."""
        self.path.mkdir(parents=True)
        code, _, _ = self.run_stage()
        self.assertEqual(code, 2)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["store-manifest.json"])

    def test_the_output_names_each_layer_then_the_format_and_id(self):
        """Breaks if a present layer prints as absent or the reverse, an unknown
        flag prints as either, or the closing line loses the format or id."""
        _touch(self.root, "knowledge/provenance.json")
        code, out, _ = self.run_stage()
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertIn("✓ provenance", out)
        self.assertIn("✗ graph", out)
        # Nothing recorded whether the deployments stage added any: unknown, not absent.
        self.assertIn("? deployments", out)
        self.assertEqual(lines[-1].split()[:4], ["Store", "manifest:", "format", "1.2,"])
        self.assertIn("id example-store", lines[-1])


class RecognisedLayers(_Run):
    STAMP = "knowledge/relationships/repository-dependencies.json"

    def test_an_unstamped_present_layer_is_not_printed_as_built(self):
        """Breaks if a present layer this library cannot read prints the same tick
        as a good one: a maintainer would not see it without opening the JSON."""
        _touch(self.root, "knowledge/relationships/by-repo/example-repo.json")
        code, out, _ = self.run_stage()
        self.assertEqual(code, 0)
        self.assertIn(
            "! relationships (present, not recognised: no format stamp this library reads)\n",
            out,
        )
        self.assertNotIn("✓ relationships", out)

    def test_a_stamped_layer_still_prints_the_plain_tick(self):
        """Breaks if the new mark spreads to a layer whose stamp is known."""
        _touch(self.root, "knowledge/relationships/by-repo/example-repo.json")
        stamp = {"format": {"layer": "relationships", "major": 1, "minor": 0}}
        _touch(self.root, self.STAMP, json.dumps(stamp))
        _, out, _ = self.run_stage()
        self.assertIn("✓ relationships\n", out)
        self.assertNotIn("not recognised", out)


class LocalOnlyLayers(_Run):
    def test_a_layer_git_ignores_is_printed_as_local_only(self):
        """Breaks if the stage prints an ignored layer as plainly absent: the
        operator would rebuild a layer that exists and only needs committing."""
        _git_repo(self.root, "https://example.com/example-org/example-store.git")
        _touch(self.root, ".gitignore", "knowledge/git-history/\n")
        _touch(self.root, "knowledge/git-history/example-repo.ndjson", "history")
        code, out, _ = self.run_stage()
        self.assertEqual(code, 0)
        self.assertIn("✗ history (local only: git ignores it)", out)
        self.assertIn("✗ graph\n", out)


class Refusals(_Run):
    def test_an_undeterminable_id_exits_1_naming_both_sources(self):
        """Breaks if the stage writes a manifest with no store id, or exits with
        anything but 1: a reader identifies the store by that id."""
        _git_repo(self.root, None)
        code, _, err = self.run_stage(store_id=None)
        self.assertEqual(code, 1)
        self.assertIn("KSB_STORE_ID", err)
        self.assertIn("origin", err)
        self.assertFalse(self.path.exists())

    def test_a_bad_extra_layer_exits_1_and_writes_nothing(self):
        """Breaks if a declaration climbing out of the store is published."""
        _touch(
            self.root,
            "config/store-extra-layers.json",
            json.dumps({"bad": {"path": "../outside", "description": "x"}}),
        )
        code, _, err = self.run_stage()
        self.assertEqual(code, 1)
        self.assertIn("bad", err)
        self.assertFalse(self.path.exists())

    def test_a_missing_root_exits_2_and_creates_nothing(self):
        """Breaks if a mistyped `--root` is created as a store, or the refusal is
        read as a usage error (1) rather than an unwritable root (2)."""
        gone = self.root / "not-a-store"
        config.configure(root=gone, STORE_ID="example-store")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["manifest"])
        self.assertEqual(code, 2)
        self.assertFalse(gone.exists())

    @unittest.skipIf(os.geteuid() == 0, "root writes anywhere")
    def test_an_unwritable_knowledge_directory_exits_2(self):
        """Breaks if a permission failure surfaces as a traceback."""
        (self.root / "knowledge").mkdir()
        (self.root / "knowledge").chmod(0o555)
        self.addCleanup((self.root / "knowledge").chmod, 0o755)
        code, _, err = self.run_stage()
        self.assertEqual(code, 2)
        self.assertIn("store-manifest.json", err)

    @unittest.skipIf(os.geteuid() == 0, "root reads anything")
    def test_an_unreadable_layer_file_exits_2_with_a_message(self):
        """Breaks if an OSError while hashing escapes as a traceback."""
        _touch(self.root, "knowledge/registry.json", "{}")
        _touch(
            self.root,
            "config/store-extra-layers.json",
            json.dumps({"reg": {"path": "knowledge/registry.json", "description": "x"}}),
        )
        locked = self.root / "knowledge" / "registry.json"
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o644)
        code, _, err = self.run_stage()
        self.assertEqual(code, 2)
        self.assertIn("registry.json", err)
        self.assertFalse(self.path.exists())


class StatusReadsTheManifest(_Run):
    def status_output(self) -> str:
        config.configure(root=self.root, STORE_ID="example-store")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(status.main([]), 0)
        return out.getvalue()

    def manifest_line(self) -> str:
        lines = self.status_output().splitlines()
        return next(line for line in lines if line.startswith("Store manifest"))

    def test_it_prints_the_format_digest_sources_date_and_absent_layers(self):
        """Breaks if `status` does not show the manifest it can read: the format,
        the digest a reader compares, the source date and what is not built."""
        _touch(
            self.root,
            "knowledge/provenance.json",
            json.dumps(
                {"repositories": {"a": {"sha": "1", "committed": "2026-01-01T00:00:00+00:00"}}}
            ),
        )
        self.run_stage()
        document = json.loads(self.path.read_text(encoding="utf-8"))
        out = self.status_output()
        self.assertIn("Store manifest: format 1.2", out)
        self.assertIn(f"digest {document['digest']}", out)
        self.assertIn("sources as of 2026-01-01T00:00:00+00:00", out)
        self.assertIn("absent: ", out)
        absent = next(line for line in out.splitlines() if "absent: " in line)
        self.assertIn("graph", absent)
        self.assertNotIn("provenance", absent)
        # Nothing recorded whether deployments were added: unknown, which is not absent.
        self.assertNotIn("deployments", absent)

    def test_files_no_layer_covers_are_listed(self):
        """Breaks if a file under `knowledge/` that no layer and no declared extra
        layer names goes unreported: a reader is never told the store holds it."""
        _touch(
            self.root,
            "config/store-extra-layers.json",
            json.dumps({"journeys": {"path": "knowledge/journeys", "description": "J."}}),
        )
        _touch(self.root, "knowledge/journeys/apply.md", "# Apply")
        _touch(self.root, "knowledge/provenance.json")
        _touch(self.root, "knowledge/history-notes/stray.md", "stray")
        # Begins with a layer's path without being inside it.
        _touch(self.root, "knowledge/provenance.json.bak", "stray")
        _touch(self.root, "knowledge/intent/tracker-undecided.json", "an excused report")
        self.run_stage()
        line = next(line for line in self.status_output().splitlines() if "uncovered: " in line)
        # Whole items, not substrings: the stray backup's name contains a covered path.
        self.assertEqual(
            line.split("uncovered: ", 1)[1].split(", "),
            ["knowledge/history-notes/stray.md", "knowledge/provenance.json.bak"],
        )

    def test_a_store_with_no_uncovered_files_says_none(self):
        """Breaks if the section prints nothing when all is covered, which reads as
        a check that did not run."""
        _touch(self.root, "knowledge/provenance.json")
        self.run_stage()
        self.assertIn("uncovered: none", self.status_output())

    def test_a_local_only_layer_is_listed_as_local_only(self):
        """Breaks if `status` lists a layer git ignores as plainly absent."""
        _git_repo(self.root, "https://example.com/example-org/example-store.git")
        _touch(self.root, ".gitignore", "knowledge/git-history/\n")
        _touch(self.root, "knowledge/git-history/example-repo.ndjson", "history")
        self.run_stage()
        out = self.status_output()
        self.assertIn("history (local only)", out)
        self.assertNotIn("graph (local only)", out)

    def test_a_present_unrecognised_layer_is_named_with_what_it_means(self):
        """Breaks if `status` stays silent about a layer that consumers will read
        as absent: the maintainer is never told a rebuild is due."""
        _touch(self.root, "knowledge/relationships/by-repo/example-repo.json")
        self.run_stage()
        lines = self.status_output().splitlines()
        self.assertIn(
            "  not recognised: relationships - present, but it reads as absent to "
            "consumers until rebuilt with this library",
            lines,
        )

    def test_a_store_with_no_unrecognised_layer_prints_no_such_line(self):
        """Breaks if the line prints for a recognised or absent layer."""
        _touch(self.root, "knowledge/relationships/by-repo/example-repo.json")
        stamp = {"format": {"layer": "relationships", "major": 1, "minor": 0}}
        _touch(self.root, "knowledge/relationships/repository-dependencies.json", json.dumps(stamp))
        self.run_stage()
        self.assertNotIn("not recognised", self.status_output())

    def test_without_a_manifest_it_says_how_to_write_one_and_does_not_fail(self):
        """Breaks if a store with no manifest makes `status` fail or go silent
        (CLAUDE.md 6.7)."""
        self.assertIn("knowledgestore manifest", self.manifest_line())

    def test_a_damaged_manifest_is_reported_and_status_still_succeeds(self):
        """Breaks if a hand-edited or merge-conflicted manifest makes `status`
        raise: it never fails (CLAUDE.md 6.7)."""
        _touch(self.root, "knowledge/store-manifest.json", "<<<<<<< not json")
        line = self.manifest_line()
        self.assertIn("unreadable", line)

    def test_a_manifest_of_the_wrong_shape_is_reported_and_status_still_succeeds(self):
        """Breaks if valid JSON of another shape raises: a manifest from a later
        format is exactly that."""
        _touch(self.root, "knowledge/store-manifest.json", '["not", "a", "manifest"]')
        line = self.manifest_line()
        self.assertIn("unreadable", line)

    def test_a_manifest_path_that_is_a_directory_is_unreadable_not_none(self):
        """Breaks if a directory at the manifest path reads as "none": the advice to
        run `manifest` would then fail to write."""
        (self.root / "knowledge" / "store-manifest.json").mkdir(parents=True)
        line = self.manifest_line()
        self.assertIn("unreadable", line)
        self.assertNotIn("none", line)


if __name__ == "__main__":
    unittest.main()
