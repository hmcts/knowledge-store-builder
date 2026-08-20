"""The page must be judged on what it was built from, not on when it was committed.

The timestamp check this replaces could not see the ordinary workflow: a
regenerated layer and an unrebuilt page committed together have identical commit
dates, so `status` reported the page current while it embedded the previous
build. An uncommitted layer edit moved no date at all.

That made it *wrongly reassuring*, which is worse than the symlink check that
could not see its own mitigation - a reader concludes the page they are about to
commit reflects the layers beside it.
"""

from __future__ import annotations

import contextlib
import io as _io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import io  # noqa: E402
from knowledgestore import status  # noqa: E402


class LayerDriftTest(SettingsIsolated):
    def _store(self, summaries: str, recorded: dict | None) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "graphify-out").mkdir(parents=True)
        (root / "knowledge" / "summaries").mkdir(parents=True)
        layer = root / "knowledge" / "summaries" / "communities.json"
        layer.write_text(summaries, encoding="utf-8")
        config.configure(root=str(root))
        config.configure(SUMMARIES_PATH=layer)
        if recorded is not None:
            io.write_json(config.EXPLORER_INPUTS_PATH, recorded)
        return root

    def _digests(self) -> dict:
        return io.layer_digests(
            [config.ROOT / name for name in status.EMBEDDED_LAYERS], config.ROOT
        )

    def test_a_layer_changed_since_the_page_was_built_is_reported(self):
        """The case commit dates cannot see: same commit, different content."""
        self._store('{"1": "before"}', recorded=None)
        built_from = self._digests()
        (config.SUMMARIES_PATH).write_text('{"1": "AFTER"}', encoding="utf-8")
        io.write_json(config.EXPLORER_INPUTS_PATH, built_from)
        drifted = status.embedded_layer_drift()
        self.assertTrue(drifted)
        self.assertTrue(any("communities.json" in name for name in drifted))

    def test_unchanged_layers_report_agreement(self):
        self._store('{"1": "same"}', recorded=None)
        io.write_json(config.EXPLORER_INPUTS_PATH, self._digests())
        self.assertEqual(status.embedded_layer_drift(), [])

    def test_a_deleted_layer_counts_as_drift(self):
        """Absent is a change, not a silence."""
        self._store('{"1": "here"}', recorded=None)
        io.write_json(config.EXPLORER_INPUTS_PATH, self._digests())
        config.SUMMARIES_PATH.unlink()
        self.assertTrue(status.embedded_layer_drift())

    def test_a_store_with_no_record_is_not_reported_as_agreeing(self):
        """A page built before this check exists cannot be judged, and saying
        nothing would read as a pass - the failure this whole family is about."""
        self._store('{"1": "x"}', recorded=None)
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            status._report_freshness()
        self.assertNotIn("was built from the layers now beside it", out.getvalue())

    def test_no_record_returns_no_drift_rather_than_total_drift(self):
        """Called directly, because the reporter guards on the file existing and
        so never exercises this branch. Without the guard every layer compares
        against nothing and reads as changed - which would report a store that
        simply predates this check as comprehensively stale.
        """
        self._store('{"1": "x"}', recorded=None)
        self.assertEqual(status.embedded_layer_drift(), [])

    def test_the_report_names_the_layers_that_moved(self):
        self._store('{"1": "before"}', recorded=None)
        built_from = self._digests()
        config.SUMMARIES_PATH.write_text('{"1": "AFTER"}', encoding="utf-8")
        io.write_json(config.EXPLORER_INPUTS_PATH, built_from)
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            status._report_freshness()
        text = out.getvalue()
        self.assertIn("built from different content", text)
        self.assertIn("communities.json", text)


class ExplorerRecordsItsInputsTest(SettingsIsolated):
    def test_the_recorded_manifest_covers_every_embedded_layer(self):
        """The page's record and status's re-hash must span the same list, or a
        mismatch means nothing."""
        from knowledgestore import build_explorer

        self.assertEqual(tuple(build_explorer.status_layers()), status.EMBEDDED_LAYERS)


if __name__ == "__main__":
    unittest.main()


class TheManifestShape(SettingsIsolated):
    """A list of `{path, hash}` records, and the keyed shape still readable.

    The shape changed because keying by path put
    `knowledge/semantic/token-neighbours.json.gz` immediately beside a hex digest,
    and SonarCloud reads a "token"-bearing key adjacent to a hex string as a
    hard-coded secret (`json:S6418`, BLOCKER) - so one store could not commit the
    artefact at all.
    """

    def _store(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "graphify-out").mkdir(parents=True)
        config.configure(root=str(root))
        return root

    def test_the_written_manifest_is_a_sorted_list_of_records(self):
        self._store()
        io.write_json(
            config.EXPLORER_INPUTS_PATH,
            [{"path": "b.json", "hash": "22"}, {"path": "a.json", "hash": "11"}],
        )
        raw = io.read_json(config.EXPLORER_INPUTS_PATH)
        self.assertIsInstance(raw, list)
        self.assertEqual(status.recorded_digests(), {"a.json": "11", "b.json": "22"})

    def test_the_keyed_shape_is_still_read(self):
        """A store that has not rebuilt its page must not become unjudgeable - a state
        this check deliberately distinguishes from agreement."""
        self._store()
        io.write_json(config.EXPLORER_INPUTS_PATH, {"a.json": "11", "b.json": "22"})
        self.assertEqual(status.recorded_digests(), {"a.json": "11", "b.json": "22"})

    def test_both_shapes_yield_the_same_mapping(self):
        self._store()
        io.write_json(config.EXPLORER_INPUTS_PATH, {"a.json": "11"})
        keyed = status.recorded_digests()
        io.write_json(config.EXPLORER_INPUTS_PATH, [{"path": "a.json", "hash": "11"}])
        self.assertEqual(keyed, status.recorded_digests())

    def test_an_absent_layer_survives_the_round_trip_as_absent(self):
        """`layer_digests` records a missing layer as None rather than skipping it, so a
        layer that disappears is a change and not a silence.

        Coercing the values to `str` on read turned every absent layer into a
        permanent false drift - `"None" != None` - and reported seven drifted layers
        on a fixture holding one real layer. This pins the sentinel through both
        shapes.
        """
        self._store()
        for written in ({"gone.json": None}, [{"path": "gone.json", "hash": None}]):
            with self.subTest(shape=type(written).__name__):
                io.write_json(config.EXPLORER_INPUTS_PATH, written)
                self.assertIsNone(status.recorded_digests()["gone.json"])

    def test_a_malformed_manifest_reads_as_no_manifest(self):
        """Not as agreement. "Cannot be judged" and "agrees" are different answers."""
        self._store()
        io.write_json(config.EXPLORER_INPUTS_PATH, ["not a record", 7])
        self.assertEqual(status.recorded_digests(), {})


class ThisSuiteCanStillTell(SettingsIsolated):
    """Break the manifest, confirm drift detection notices, restore.

    The tests above assert that both shapes read to the same mapping. That would
    also be true of a reader returning `{}` for everything, so it says nothing on
    its own about whether drift can still be seen.
    """

    def _store_with(self, recorded) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "graphify-out").mkdir(parents=True)
        (root / "knowledge" / "summaries").mkdir(parents=True)
        layer = root / "knowledge" / "summaries" / "communities.json"
        layer.write_text('{"1": "a summary"}', encoding="utf-8")
        config.configure(root=str(root))
        config.configure(SUMMARIES_PATH=layer)
        io.write_json(config.EXPLORER_INPUTS_PATH, recorded)
        return root

    def test_a_wrong_digest_is_detected_in_the_record_shape(self):
        self._store_with([{"path": "knowledge/summaries/communities.json", "hash": "deadbeef"}])
        self.assertIn("knowledge/summaries/communities.json", status.embedded_layer_drift())

    def test_a_wrong_digest_is_detected_in_the_keyed_shape(self):
        self._store_with({"knowledge/summaries/communities.json": "deadbeef"})
        self.assertIn("knowledge/summaries/communities.json", status.embedded_layer_drift())
