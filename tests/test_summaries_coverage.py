"""What the digest sampled, recorded beside what it left out.

A summary is written from a digest that caps three fields, and until now the
totals behind those caps were computed and thrown away. So a term the prose
cites and the digest does not hold had two indistinguishable explanations: the
author invented it, or it was real and simply outside the sample. `verify` could
only report, never conclude - and a report whose headline is mostly noise gets
ignored, taking the few real findings with it.

The `coverage` block records `shown`, `unshown` and `total` per capped field, at
both ends: in the digest the author reads, and in the merged artefact a reader
and the verifier read. `shown + unshown == total` is asserted wherever one is
written, because a block that does not add up reads as precision.

The write gate is the other half and cannot be deferred: counts move whenever
the graph is re-extracted, so an artefact carrying them would be rewritten on
every refresh whether or not a word of prose changed. The write is therefore
gated on a hash of the prose alone.

The tests here name three breaks: a coverage block that describes a sample of a
different size, a write that happens when nothing semantic moved (and one that
does not happen when something did), and - the one that matters most - a
verifier that stops failing on a term absent from a digest which withheld
nothing.
"""

from __future__ import annotations

import io as stdio
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import io  # noqa: E402
from knowledgestore import status  # noqa: E402


LABELLED_NODES = 40
FEATURE_NODES = 7
TICKETS_PER_NODE = 3


def _oversized_community() -> tuple[list[dict], dict[str, int]]:
    """A community that exceeds every cap, so all three fields are truncated.

    Degrees descend with the index, which is the order `community_digest` sorts
    by, so the sample is predictable from the fixture alone.
    """
    nodes: list[dict] = []
    for index in range(LABELLED_NODES):
        metadata: dict = {"tickets": [f"AAA-{index}{suffix}" for suffix in range(TICKETS_PER_NODE)]}
        if index < FEATURE_NODES:
            metadata["kind"] = "feature"
        nodes.append(
            {
                "id": f"n{index}",
                "label": f"Node{index}",
                "repo": "alpha-service",
                "source_file": f"src/Node{index}.java",
                "metadata": metadata,
            }
        )
    degree = {node["id"]: LABELLED_NODES - index for index, node in enumerate(nodes)}
    return nodes, degree


def _small_community() -> tuple[list[dict], dict[str, int]]:
    """A community small enough that no cap bites: nothing is withheld."""
    nodes = [
        {
            "id": "a",
            "label": "AlphaService",
            "repo": "alpha-service",
            "source_file": "src/AlphaService.java",
            "metadata": {"kind": "feature", "tickets": ["AAA-1"]},
        },
        {
            "id": "b",
            "label": "AlphaRepository",
            "repo": "alpha-service",
            "source_file": "src/AlphaRepository.java",
            "metadata": {},
        },
    ]
    return nodes, {"a": 2, "b": 1}


def _reconciles(case: unittest.TestCase, block: dict) -> None:
    for field in summaries.COVERAGE_FIELDS:
        entry = block[field]
        case.assertEqual(
            entry["shown"] + entry["unshown"],
            entry["total"],
            f"{field} does not add up: {entry}",
        )


class DigestCoverageTest(SettingsIsolated):
    """The digest handed to the author says how much of each field it is showing."""

    def test_a_capped_field_records_the_total_behind_the_cap(self):
        # the break: `top_nodes` shows 12 and the community holds 40 labelled
        # nodes; without the total the author cannot tell 12 of 12 from 12 of 40
        nodes, degree = _oversized_community()
        digest = summaries.community_digest(1, nodes, {}, {}, degree)
        self.assertEqual(
            digest["coverage"]["top_nodes"],
            {
                "shown": summaries.TOP_NODES,
                "unshown": LABELLED_NODES - summaries.TOP_NODES,
                "total": LABELLED_NODES,
            },
        )

    def test_every_capped_field_is_covered(self):
        nodes, degree = _oversized_community()
        digest = summaries.community_digest(1, nodes, {}, {}, degree)
        self.assertEqual(sorted(digest["coverage"]), sorted(summaries.COVERAGE_FIELDS))
        self.assertEqual(
            digest["coverage"]["business_features"],
            {
                "shown": summaries.TOP_FEATURES,
                "unshown": FEATURE_NODES - summaries.TOP_FEATURES,
                "total": FEATURE_NODES,
            },
        )
        # tickets are mined from the 30 highest-degree nodes, three each
        tickets = digest["coverage"]["tickets"]
        self.assertEqual(tickets["shown"], summaries.TOP_TICKETS)
        self.assertEqual(tickets["total"], 30 * TICKETS_PER_NODE)

    def test_the_block_reconciles_on_a_truncated_community(self):
        nodes, degree = _oversized_community()
        _reconciles(self, summaries.community_digest(1, nodes, {}, {}, degree)["coverage"])

    def test_a_field_no_cap_touched_withholds_nothing(self):
        # the distinction the verifier is built on: unshown 0 means the digest is
        # the whole evidence base for that field, so absence from it is a finding
        nodes, degree = _small_community()
        coverage = summaries.community_digest(1, nodes, {}, {}, degree)["coverage"]
        for field in summaries.COVERAGE_FIELDS:
            self.assertEqual(coverage[field]["unshown"], 0, field)
        _reconciles(self, coverage)

    def test_the_shown_count_is_checked_against_what_the_digest_holds(self):
        # the break a lowered cap causes: the block keeps claiming five while the
        # field carries four. Checked rather than derived from the list length,
        # because a count copied off the list can never disagree with it.
        nodes, degree = _oversized_community()
        digest = summaries.community_digest(1, nodes, {}, {}, degree)
        digest["business_features"] = digest["business_features"][:-1]
        with self.assertRaises(ValueError) as raised:
            summaries.checked_coverage(
                digest,
                {
                    "top_nodes": (len(digest["top_nodes"]), LABELLED_NODES),
                    "business_features": (summaries.TOP_FEATURES, FEATURE_NODES),
                    "tickets": (len(digest["tickets"]), 90),
                },
            )
        self.assertIn("business_features", str(raised.exception))

    def test_a_block_that_does_not_add_up_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            summaries.validate_coverage(
                "7",
                {
                    "top_nodes": {"shown": 12, "unshown": 5, "total": 40},
                    "business_features": {"shown": 0, "unshown": 0, "total": 0},
                    "tickets": {"shown": 0, "unshown": 0, "total": 0},
                },
            )
        self.assertIn("top_nodes", str(raised.exception))
        self.assertIn("7", str(raised.exception))


class MergeFixture(SettingsIsolated):
    """A store with digests and a batch to merge. No tests of its own."""

    PROSE = (
        "The alpha-service node cluster, holding the persistence and lookup paths "
        "that the hearing workflow reads."
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        config.configure(root=str(self.root))

    def write_digests(self, digests: list[dict]) -> None:
        config.SUMMARIES_INPUT_PATH.write_text(json.dumps(digests), encoding="utf-8")

    def batch(self, mapping: dict[str, str], name: str = "batch.json") -> str:
        path = self.root / name
        path.write_text(json.dumps(mapping), encoding="utf-8")
        return str(path)

    def run_merge(self, *paths: str) -> tuple[int, str]:
        buffer = stdio.StringIO()
        with redirect_stdout(buffer):
            code = summaries.merge(list(paths))
        return code, buffer.getvalue()

    def digest(self, cid: int, truncated: bool = True) -> dict:
        nodes, degree = _oversized_community() if truncated else _small_community()
        return summaries.community_digest(cid, nodes, {}, {}, degree)

    def _document(self) -> dict:
        return json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))


class MergedArtefactTest(MergeFixture):
    """The merged artefact carries the evidence base each summary was written from."""

    def test_the_artefact_carries_a_coverage_block_per_community(self):
        self.write_digests([self.digest(1)])
        self.run_merge(self.batch({"1": self.PROSE}))
        coverage = self._document()[io.SUMMARIES_METADATA_KEY]["coverage"]
        self.assertEqual(list(coverage), ["1"])
        _reconciles(self, coverage["1"])
        self.assertEqual(coverage["1"]["top_nodes"]["total"], LABELLED_NODES)

    def test_a_coverage_block_that_does_not_add_up_stops_the_write(self):
        # a block that reads as precision and is wrong is worse than none, so the
        # merge refuses rather than committing it
        broken = self.digest(1)
        broken["coverage"]["top_nodes"]["unshown"] += 1
        self.write_digests([broken])
        prose = self.batch({"1": self.PROSE})

        with self.assertRaises(ValueError):
            self.run_merge(prose)
        self.assertFalse(config.SUMMARIES_PATH.exists(), "nothing may be written")

    def test_a_summary_with_no_digest_gets_no_invented_coverage(self):
        # prose retained for a cluster now below the significance threshold has no
        # digest, so nothing can say what its evidence base was
        self.write_digests([self.digest(1)])
        config.SUMMARIES_PATH.write_text(
            json.dumps({"7": "Retained prose for a cluster below the threshold. " * 2}),
            encoding="utf-8",
        )
        self.run_merge(self.batch({"1": self.PROSE}))
        coverage = self._document()[io.SUMMARIES_METADATA_KEY]["coverage"]
        self.assertEqual(list(coverage), ["1"])
        self.assertIn("7", self._document(), "the prose itself is still retained")

    def test_the_metadata_block_keys_are_written_in_a_fixed_order(self):
        # two runs must be byte-identical, so nothing here may depend on the order
        # a dict happened to be built in
        self.write_digests([self.digest(1)])
        self.run_merge(self.batch({"1": self.PROSE}))
        text = config.SUMMARIES_PATH.read_text(encoding="utf-8")
        self.assertLess(text.index('"content_digest"'), text.index('"coverage"'))
        self.assertLess(text.index('"shown"'), text.index('"unshown"'))
        self.assertLess(text.index('"unshown"'), text.index('"total"'))


class WriteGateTest(MergeFixture):
    """A run that changed nothing writes nothing, and one that changed something writes."""

    OTHER_PROSE = (
        "The alpha-service cluster, rewritten: persistence and lookup for the "
        "hearing workflow, with the scheduling paths described as well."
    )
    # A fixed point in the past, so "was this file rewritten?" is answered by the
    # filesystem rather than by comparing bytes that a rewrite would reproduce.
    PINNED = 1_000_000_000

    def pin(self) -> None:
        self.assertTrue(
            config.SUMMARIES_PATH.is_file(), "the merge before this point wrote nothing at all"
        )
        os.utime(config.SUMMARIES_PATH, (self.PINNED, self.PINNED))

    def rewritten(self) -> bool:
        return int(config.SUMMARIES_PATH.stat().st_mtime) != self.PINNED

    def test_a_refresh_that_moved_only_a_count_does_not_rewrite_the_file(self):
        # the break this half exists for: coverage counts move on every
        # re-extraction, so without the gate every refresh rewrites every summary
        # in every consuming store's diff
        self.write_digests([self.digest(1)])
        self.run_merge(self.batch({"1": self.PROSE}))
        before = config.SUMMARIES_PATH.read_text(encoding="utf-8")
        self.pin()

        moved = self.digest(1)
        moved["coverage"]["top_nodes"]["total"] += 100
        moved["coverage"]["top_nodes"]["unshown"] += 100
        self.write_digests([moved])
        _, output = self.run_merge(self.batch({"1": self.PROSE}))

        self.assertFalse(self.rewritten(), "the prose did not change, so nothing may be written")
        self.assertEqual(config.SUMMARIES_PATH.read_text(encoding="utf-8"), before)
        self.assertIn("not rewritten", output)

    def test_changed_prose_rewrites_the_file(self):
        # the other direction: a gate that never writes would pass the test above
        self.write_digests([self.digest(1)])
        self.run_merge(self.batch({"1": self.PROSE}))
        self.pin()

        self.run_merge(self.batch({"1": self.OTHER_PROSE}, name="second.json"))

        self.assertTrue(self.rewritten(), "the prose changed, so the file must be written")
        self.assertEqual(self._document()["1"], self.OTHER_PROSE)

    def test_a_new_community_rewrites_the_file(self):
        self.write_digests([self.digest(1), self.digest(2)])
        self.run_merge(self.batch({"1": self.PROSE}))
        self.pin()

        self.run_merge(self.batch({"2": self.PROSE}, name="second.json"))

        self.assertTrue(self.rewritten())
        self.assertEqual(sorted(k for k in self._document() if not k.startswith("_")), ["1", "2"])

    def test_the_recorded_digest_covers_the_prose_and_not_the_metadata(self):
        self.write_digests([self.digest(1)])
        self.run_merge(self.batch({"1": self.PROSE}))
        recorded = self._document()[io.SUMMARIES_METADATA_KEY]["content_digest"]
        self.assertEqual(recorded, summaries.content_digest({"1": self.PROSE}))
        self.assertNotEqual(recorded, summaries.content_digest({"1": self.OTHER_PROSE}))


class MetadataIsNotACommunityTest(SettingsIsolated):
    """Nothing downstream may read the metadata block as a summary."""

    DOCUMENT = {
        "1": "Prose about the alpha-service cluster and its persistence paths.",
        io.SUMMARIES_METADATA_KEY: {
            "content_digest": "0" * 64,
            "coverage": {"1": {"top_nodes": {"shown": 1, "unshown": 0, "total": 1}}},
        },
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        config.configure(root=str(self.root))
        config.SUMMARIES_PATH.write_text(json.dumps(self.DOCUMENT), encoding="utf-8")

    def test_the_shared_reader_returns_the_prose_alone(self):
        self.assertEqual(
            io.read_summaries(config.SUMMARIES_PATH),
            {"1": self.DOCUMENT["1"]},
        )

    def test_status_does_not_count_it_as_a_summary(self):
        self.assertEqual(status.layer_coverage()["summaries_written"], 1)

    def test_verify_does_not_read_it_as_prose(self):
        config.SUMMARIES_INPUT_PATH.write_text(
            json.dumps(
                [
                    {
                        "id": 1,
                        "label": "alpha-service",
                        "size": 2,
                        "repositories": ["alpha-service"],
                        "top_nodes": ["AlphaService (src/AlphaService.java)"],
                        "business_features": [],
                        "tickets": [],
                    }
                ]
            ),
            encoding="utf-8",
        )
        buffer = stdio.StringIO()
        with redirect_stdout(buffer):
            code = summaries.verify()
        self.assertEqual(code, 0)
        self.assertIn("Verified 1 of 1", buffer.getvalue())
        self.assertNotIn(io.SUMMARIES_METADATA_KEY, buffer.getvalue())


class SharedReaderIsUsedEverywhereTest(unittest.TestCase):
    """Every stage that reads the merged artefact goes through the one reader.

    A stage reading the raw document treats the metadata block as a community:
    it lands in the explorer page, in the semantic index's vocabulary, in the
    summary count `status` prints and in a remap's displaced prose. This is the
    trap the metadata block introduces, so it is checked in the source rather
    than left to whoever adds the next reader.
    """

    SOURCE = Path(__file__).resolve().parent.parent / "src" / "knowledgestore"
    READS = ("read_text", "read_json", "json.load", "read_summaries")
    # The one raw read, and why: `merge` writes the artefact and is the only
    # caller that needs the metadata, because the recorded content digest is what
    # it decides to write on.
    ALLOWED = {
        ("build_community_summaries.py", "document = io.read_json_dict(config.SUMMARIES_PATH)")
    }

    def test_no_stage_reads_the_merged_summaries_raw(self):
        offenders = []
        for module in sorted(self.SOURCE.glob("*.py")):
            for number, line in enumerate(module.read_text(encoding="utf-8").splitlines(), 1):
                text = line.strip()
                if "config.SUMMARIES_PATH" not in text:
                    continue
                if not any(verb in text for verb in self.READS):
                    continue
                if "read_summaries" in text or (module.name, text) in self.ALLOWED:
                    continue
                offenders.append(f"{module.name}:{number}: {text}")
        self.assertEqual(offenders, [], "read these through io.read_summaries")

    def test_the_allowance_still_names_a_line_that_exists(self):
        # an allowance for a line that has moved on excuses nothing and hides the
        # next raw read behind a rule that looks maintained
        for name, expected in self.ALLOWED:
            body = (self.SOURCE / name).read_text(encoding="utf-8")
            self.assertIn(expected, body, f"{name} no longer holds the allowed read")


class VerifyReadsCoverageTest(SettingsIsolated):
    """The subtraction: absence from a complete digest is a finding, absence from
    a truncated one is not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        config.configure(root=str(self.root))

    def store(self, coverage: dict | None, prose: str, cid: str = "1") -> None:
        digest = {
            "id": cid,
            "label": "AlphaService",
            "size": 40,
            "repositories": ["alpha-service"],
            "top_nodes": ["AlphaService (src/AlphaService.java)"],
            "business_features": [],
            "tickets": [],
        }
        if coverage is not None:
            digest["coverage"] = coverage
        config.SUMMARIES_INPUT_PATH.write_text(json.dumps([digest]), encoding="utf-8")
        config.SUMMARIES_PATH.write_text(json.dumps({cid: prose}), encoding="utf-8")

    @staticmethod
    def complete() -> dict:
        return {
            "top_nodes": {"shown": 1, "unshown": 0, "total": 1},
            "business_features": {"shown": 0, "unshown": 0, "total": 0},
            "tickets": {"shown": 0, "unshown": 0, "total": 0},
        }

    @staticmethod
    def truncated() -> dict:
        block = VerifyReadsCoverageTest.complete()
        block["top_nodes"] = {"shown": 1, "unshown": 39, "total": 40}
        return block

    def run_verify(self, **kwargs) -> tuple[int, str]:
        buffer = stdio.StringIO()
        with redirect_stdout(buffer):
            code = summaries.verify(**kwargs)
        return code, buffer.getvalue()

    def test_a_term_absent_from_a_digest_that_withheld_nothing_still_fails(self):
        # THE sensitivity test. A change that downgraded everything to
        # informational would pass every other test in this module and leave the
        # verifier unable to fail on anything.
        self.store(self.complete(), "Alpha-service logic around InventedAggregate.")
        code, output = self.run_verify(strict=True)
        self.assertEqual(code, 1)
        self.assertIn("InventedAggregate", output)
        self.assertIn("nothing withheld", output)

    def test_a_term_absent_from_a_truncated_digest_is_informational(self):
        self.store(self.truncated(), "Alpha-service logic around HearingAggregate.")
        code, output = self.run_verify(strict=True)
        self.assertEqual(code, 0, "the digest showed 1 of 40; absence proves nothing")
        self.assertIn("HearingAggregate", output)
        self.assertIn("1 of 40", output)

    def test_a_digest_with_no_coverage_block_keeps_the_previous_meaning(self):
        # an unknown evidence base must not read as an excuse: every store built
        # before this change has digests with no coverage, and downgrading them
        # would turn an existing CI invocation green without anything changing
        self.store(None, "Alpha-service logic around InventedAggregate.")
        code, _ = self.run_verify(strict=True)
        self.assertEqual(code, 1)

    def test_a_summary_with_no_digest_still_fails_however_truncated_the_rest_are(self):
        self.store(self.truncated(), "Alpha-service logic around HearingAggregate.")
        prose = json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))
        prose["99"] = "Prose for a community with no digest at all."
        config.SUMMARIES_PATH.write_text(json.dumps(prose), encoding="utf-8")
        code, _ = self.run_verify(strict=True)
        self.assertEqual(code, 1)

    def test_the_report_reconciles_the_two_classes_against_the_total(self):
        # a split that does not add up is how a count stops being a finding
        self.store(self.complete(), "Alpha-service logic around InventedAggregate.")
        prose = json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))
        digests = json.loads(config.SUMMARIES_INPUT_PATH.read_text(encoding="utf-8"))
        second = dict(digests[0], id="2", coverage=self.truncated())
        digests.append(second)
        prose["2"] = "Alpha-service logic around HearingAggregate."
        config.SUMMARIES_INPUT_PATH.write_text(json.dumps(digests), encoding="utf-8")
        config.SUMMARIES_PATH.write_text(json.dumps(prose), encoding="utf-8")
        _, output = self.run_verify()
        self.assertIn("1 of them cite a digest that withheld nothing", output)
        self.assertIn("1 cite one that did not show everything", output)


if __name__ == "__main__":
    unittest.main()
