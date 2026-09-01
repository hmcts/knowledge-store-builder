"""Tests for the shared pipeline modules (config, io)."""

from __future__ import annotations

import gzip
import json

import re
import tempfile
import unittest
from pathlib import Path


from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import io as pio  # noqa: E402


ROOTED = (
    "GRAPH_PATH",
    "LABELS_PATH",
    "EXPLORER_PATH",
    "CLUSTERING_RECORD_PATH",
    "INTENT_INDEX_PATH",
    "TICKET_DESCRIPTIONS_PATH",
    "TICKET_TITLES_PATH",
    "TICKET_TRACKER_PATH",
    "TRACKER_UNDECIDED_PATH",
    "SUMMARIES_PATH",
    "SUMMARIES_INPUT_PATH",
    "SYNONYMS_PATH",
    "CONTENT_FILES_PATH",
    "CONTENT_SET_PATH",
    "PROVENANCE_PATH",
    "TELEMETRY_PATH",
    "TOPICS_INPUT_PATH",
    "TOPICS_BRIEFS_PATH",
    "TOPICS_DOCS_DIR",
    "DEEPDIVES_INPUT_DIR",
    "DEEPDIVES_DOCS_DIR",
    "DEEPDIVES_PATH",
    "HISTORY_DIR",
    "CONTEXT_PATH",
    "MANIFEST_PATH",
    "FILTERS_PATH",
    "REPOSITORIES_CONFIG",
    "BOUNDARY_PATH",
    "REPOSITORIES_DIR",
    "TOPICS_CONFIG_PATH",
    "CONTENT_CUTS_PATH",
)


class ConfigTest(SettingsIsolated):
    def test_every_path_sits_under_root(self):
        for name in ROOTED:
            self.assertTrue(str(getattr(config, name)).startswith(str(config.ROOT)), name)

    def test_configure_root_repoints_every_derived_path(self):
        original = config.ROOT
        self.addCleanup(config.configure, original)
        with tempfile.TemporaryDirectory() as tmp:
            # Resolve before configuring, and compare against the same value.
            # Handing `configure` an unresolved path and asserting against a
            # resolved one makes this test depend on `configure` resolving - which
            # is invisible on Linux and, on macOS, quietly turns it into an
            # observer of a behaviour it is not about. `configure`'s resolve has
            # its own test in test_harness_root_spelling.
            root = Path(tmp).resolve()
            config.configure(root=str(root))
            for name in ROOTED:
                self.assertTrue(str(getattr(config, name)).startswith(str(root)), name)

    def test_configure_overrides_a_single_setting(self):
        original = config.GITHUB_ORG
        self.addCleanup(config.configure, None, GITHUB_ORG=original)
        config.configure(GITHUB_ORG="otherorg")
        self.assertEqual(config.GITHUB_ORG, "otherorg")

    def test_configure_rejects_unknown_settings(self):
        with self.assertRaises(KeyError):
            config.configure(NOT_A_SETTING=1)

    def test_ticket_pattern_matches_and_rejects(self):
        self.assertEqual(
            config.TICKET_PATTERN.findall("DD-1: fix CRC-12016 x"), ["DD-1", "CRC-12016"]
        )
        self.assertEqual(config.TICKET_PATTERN.findall("no tickets here 12-34"), [])


class IoTest(SettingsIsolated):
    def test_json_round_trip_and_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "x.json"
            self.assertEqual(pio.read_json(path, default={"d": 1}), {"d": 1})
            pio.write_json(path, {"a": [1, 2]})
            self.assertEqual(pio.read_json(path), {"a": [1, 2]})

    def test_gzip_json_round_trip_and_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "x.json.gz"
            self.assertIsNone(pio.read_gzip_json(path))
            pio.write_gzip_json(path, {"k": "v"})
            self.assertEqual(pio.read_gzip_json(path), {"k": "v"})

    def test_load_labels_empty_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pio.load_labels(Path(tmp) / "missing.json"), {})


class DeterministicGzipTest(SettingsIsolated):
    """Committed .gz artefacts must not churn when their content is unchanged.

    Python's gzip writer embeds the current time and the output filename in
    the header by default, so every rebuild rewrote byte-different artefacts
    with identical content - quietly defeating the byte-identical guarantee
    the README makes and dirtying version control on every run. Found while
    writing docs/how-it-works.md, whose determinism claim was checked against
    the code and turned out to be false.
    """

    def test_same_content_same_bytes_regardless_of_time_and_name(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "first.json.gz", Path(tmp) / "second.json.gz"
            with mock.patch("gzip.time.time", return_value=1_000_000):
                pio.write_gzip_json(first, {"k": [1, 2, 3]})
            with mock.patch("gzip.time.time", return_value=2_000_000):
                pio.write_gzip_json(second, {"k": [1, 2, 3]})
            self.assertEqual(
                first.read_bytes(), second.read_bytes(), "header must not carry time or name"
            )
            self.assertEqual(first.read_bytes()[4:8], b"\x00\x00\x00\x00", "mtime field zero")
            self.assertEqual(pio.read_gzip_json(second), {"k": [1, 2, 3]}, "round trip intact")


class NoImportTimeCopiesTest(SettingsIsolated):
    """No stage module may copy a setting at import time.

    `NAME = config.SETTING` at module scope freezes the value when the module is
    imported. Stage modules are imported before a caller can configure
    anything, so every such copy made `configure()` a silent no-op: the override
    was accepted - `configure()` raises only for an *unknown* setting - and the
    stage carried on reading the default. There were 70 of these across 13
    modules.

    What it cost: an estate declaring its BDD steps in a fourth language
    configured `STEP_DEFINITION_LANGUAGES`, the gherkin stage searched the three
    defaults, matched none of that estate's step definitions, and reported
    success.

    That settings now reach the stages is proved by the rest of the suite: every
    test that calls `configure()` depends on it. This test exists to stop the
    pattern returning, because its failure mode is silence.
    """

    COPY = re.compile(r"^[A-Z_]+ = config\.[A-Z_]+$")

    def test_no_stage_module_copies_a_setting_at_import(self):
        offenders = []
        for path in sorted(Path(config.__file__).parent.glob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if self.COPY.match(line):
                    offenders.append(f"{path.name}:{number}: {line}")
        self.assertEqual(
            offenders,
            [],
            "read config.<SETTING> where the value is used, not once at import",
        )


if __name__ == "__main__":
    unittest.main()


class TicketBrowseUrlTest(SettingsIsolated):
    """A consumer-supplied tracker URL gets a usable separator.

    The setting is estate configuration, and both consumers - the explorer page's
    embedded config and the brief renderer - build a link by concatenating the
    ticket id onto it. A URL without a trailing separator therefore produced
    `https://tracker/browseCCT-890`: a broken link, silently, in every brief and
    every search result. Normalising here rather than at each call site means the
    page's embedded value is already correct, so app.js needs no change.
    """

    def test_a_missing_trailing_slash_is_added(self):
        config.configure(TICKET_BROWSE_URL="https://tracker/browse")
        self.assertEqual(config.TICKET_BROWSE_URL, "https://tracker/browse/")

    def test_an_existing_trailing_slash_is_left_alone(self):
        config.configure(TICKET_BROWSE_URL="https://tracker/browse/")
        self.assertEqual(config.TICKET_BROWSE_URL, "https://tracker/browse/")

    def test_a_query_style_url_is_not_given_a_slash(self):
        """Not every tracker puts the id in a path segment.

        `https://tracker/issue?key=` wants the id appended directly; adding a
        slash would break it, so a URL already ending in a separator is trusted.
        """
        for url in (
            "https://tracker/issue?key=",
            "https://tracker/i?a=1&key=",
            "https://tracker/x#",
        ):
            config.configure(TICKET_BROWSE_URL=url)
            self.assertEqual(config.TICKET_BROWSE_URL, url)

    def test_unset_stays_empty_so_linking_stays_off(self):
        config.configure(TICKET_BROWSE_URL="")
        self.assertEqual(config.TICKET_BROWSE_URL, "")


class TheJsonReaderHandlesGzip(unittest.TestCase):
    """`read_json` dispatches on the `.gz` suffix, and three stages depend on it.

    Shipped in v0.12.0 without this: a stage handed a gzipped path died on the gzip
    magic byte (`UnicodeDecodeError: 0x8b in position 1`). It was reported through
    `record-clustering`, but the reason the fix went here is that
    `build_community_summaries` reads `GRAPH_PATH` the same way in three places.

    Tested directly rather than through a stage, deliberately. It WAS covered
    through `record-clustering` until that stage switched to streaming its counts -
    at which point the mutation gate reported this behaviour as removable with the
    suite still green, which is precisely what it is for.
    """

    def test_a_gzipped_json_object_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "layer.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                json.dump({"nodes": [{"id": "a"}], "note": "Cymraeg"}, handle)
            self.assertEqual(pio.read_json_dict(path), {"nodes": [{"id": "a"}], "note": "Cymraeg"})

    def test_an_uncompressed_file_still_takes_the_path_it_always_took(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "layer.json"
            path.write_text(json.dumps({"a": 1}), encoding="utf-8")
            self.assertEqual(pio.read_json_dict(path), {"a": 1})

    def test_an_absent_file_returns_the_default_either_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pio.read_json(Path(tmp) / "gone.json.gz", default={"d": 1}), {"d": 1})
            self.assertEqual(pio.read_json(Path(tmp) / "gone.json", default={"d": 1}), {"d": 1})


class TheGraphLoaderHandlesGzip(unittest.TestCase):
    """`load_graph` reads the committed archive too, not only the plain graph.

    `read_json` was given a `.gz` dispatch after a stage handed a gzipped path
    died on the gzip magic byte. `load_graph` is another reader of the same
    artefact and was left behind, so every stage that reads the graph through it
    could open only the uncompressed file - which is the one every store
    gitignores, and therefore the one a fresh checkout does not have. The
    archive fallback the reading stages gained stopped at this function, and
    nothing at the call site said why.

    Tested directly rather than through a stage, deliberately. The last time this
    behaviour lost its only observer it was because the coverage was incidental:
    `read_json`'s dispatch was exercised only as a side effect of
    `record-clustering` reading a `.gz`, and when that stage switched to
    streaming its counts nothing was left to notice. Incidental coverage cannot
    report that it has gone.
    """

    # Written by hand and never derived from the reader: both fixtures are
    # serialised *from* this literal by the stdlib, and both reads must return
    # it. Non-ASCII on purpose - the gzip branch decodes explicitly, and a branch
    # that dropped the encoding would still pass on an ASCII-only fixture.
    GRAPH = {
        "directed": False,
        "nodes": [
            {"id": "svc-alpha::Ledger", "label": "Ledger", "repo": "svc-alpha"},
            {"id": "svc-beta::Depot", "label": "Depot — Cymraeg", "repo": "svc-beta"},
        ],
        "links": [{"source": "svc-alpha::Ledger", "target": "svc-beta::Depot"}],
    }

    def test_a_gzipped_graph_is_read(self):
        """Breaks if `load_graph` stops dispatching on the `.gz` suffix.

        The defect exactly as reported: handed `graph.json.gz` it raised
        `UnicodeDecodeError: 0x8b in position 1` on the gzip magic byte, so a
        store holding only its committed archive could not be read at all.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "graph.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                json.dump(self.GRAPH, handle)
            self.assertEqual(pio.load_graph(path), self.GRAPH)

    def test_an_uncompressed_graph_reads_identically(self):
        """The sensitivity control, and it is not decoration.

        A reader rewritten to open everything through gzip passes a test that
        only ever checks a `.gz`, and would then fail on the uncompressed graph
        that every mid-pipeline stage writes and reads back. Both branches have
        to be named for either assertion to mean anything.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "graph.json"
            path.write_text(json.dumps(self.GRAPH, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(pio.load_graph(path), self.GRAPH)

    def test_an_unreadable_graph_raises_rather_than_returning_part_of_one(self):
        """Breaks if a reader is wrapped in a try/except that returns what it got.

        The quiet direction is the dangerous one. Every caller treats this
        mapping as the estate, so a partial graph is reported as a smaller estate
        rather than as a failure, and every count downstream of it reconciles.
        Three shapes, because the suffix decides which reader opens the file and
        each has its own way of going wrong: a truncated archive, an archive that
        was never compressed, and compressed bytes under a plain name.
        """
        with tempfile.TemporaryDirectory() as tmp:
            here = Path(tmp)
            whole = here / "graph.json.gz"
            with gzip.open(whole, "wt", encoding="utf-8") as handle:
                json.dump(self.GRAPH, handle)
            packed = whole.read_bytes()
            self.assertEqual(pio.load_graph(whole), self.GRAPH, "precondition: whole file reads")

            truncated = here / "truncated.json.gz"
            truncated.write_bytes(packed[: len(packed) // 2])
            uncompressed_under_gz = here / "uncompressed.json.gz"
            uncompressed_under_gz.write_text(json.dumps(self.GRAPH), encoding="utf-8")
            compressed_under_json = here / "compressed.json"
            compressed_under_json.write_bytes(packed)

            for path in (truncated, uncompressed_under_gz, compressed_under_json):
                with self.subTest(path.name):
                    # EOFError from a truncated member, BadGzipFile (an OSError)
                    # from bytes that are not gzip, UnicodeDecodeError (a
                    # ValueError) from gzip bytes read as text.
                    with self.assertRaises((EOFError, OSError, ValueError)):
                        pio.load_graph(path)

    def test_an_absent_graph_still_raises_rather_than_defaulting(self):
        """Breaks if `load_graph` is made to delegate to `read_json` itself.

        That is the obvious way to share one implementation and it is the wrong
        one, because the two disagree about an absent file: `read_json` returns
        its default and this must raise. Every caller treats a missing graph as
        fatal and prints its own message, so a default turns "there is no graph"
        into an empty estate that every downstream count then reports as real.
        """
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("gone.json", "gone.json.gz"):
                with self.subTest(name):
                    with self.assertRaises(FileNotFoundError):
                        pio.load_graph(Path(tmp) / name)
