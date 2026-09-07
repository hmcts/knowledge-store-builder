"""Each committed prose layer records whether it still holds what the stage wrote.

Community summaries, topic briefs and deep dives are LLM-authored prose,
committed to a store, reviewed by pull request and cited as evidence. Between the
stage writing one and a reader believing it, nothing established that the file
still said what the stage wrote (#316): `content_digest` covered the summaries
alone and existed to gate a write, and the other two layers recorded nothing.

Each layer now records one digest per entry in its metadata block, and each merge
reports the entries whose prose no longer matches before it overwrites them. The
breaks these tests name:

- an entry edited in the committed artefact after the stage wrote it, reported for
  each of the three layers - the gap itself;
- the over-correction, which is the expensive half: "report mismatches" becoming
  "report everything" produces output indistinguishable from a working check at a
  glance, and is then ignored along with the finding that mattered;
- a record that includes the id its prose is keyed to. Community ids are
  positional and `summaries remap` re-keys prose onto new ones by design, so such
  a record would differ after every re-clustering and the check would fire on the
  operation this library exists to perform;
- refusing rather than reporting. Whether a legitimate hand edit happens at all is
  unmeasured, so no exit code may move on this yet;
- the write gate withholding the record. A run whose prose is unchanged skips the
  write, so on an existing store the digests would arrive only when somebody
  happened to edit a summary - and a feature that never turns on looks exactly
  like one that found nothing;
- the metadata block reaching a consumer that reads the artefact raw, where it is
  a brief in the explorer's topic list and one more in the count `status` prints.

Deliberately not pinned: a deleted entry is not reported. A digest per entry can
say an entry's prose is not one the stage wrote; it cannot say an entry the stage
wrote has gone, because the record is not tied to the ids. Chaining each entry's
hash onto the previous one is the shape that makes a removal break its successor,
and #316 records it and defers it. A test asserting today's silence would fail on
that intended change rather than on a defect.
"""

from __future__ import annotations

import contextlib
import gzip
import io as stdio
import json
import os
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import build_deep_dives as dives  # noqa: E402
from knowledgestore import build_explorer as explorer  # noqa: E402
from knowledgestore import build_topic_briefs as briefs  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import io  # noqa: E402
from knowledgestore import status  # noqa: E402


# The line `prose_drift` prints when it has something to say. Asserted absent as
# often as present: this whole change is worth nothing if it reports every run.
DRIFT_MARK = "no longer carry the prose recorded there"

# Long enough to clear both `MIN_BRIEF_LENGTH` and `MIN_DIVE_LENGTH`.
EVIDENCE = "Evidence sentence about one invented service. " * 30


def _captured(call) -> tuple[int, str]:
    out = stdio.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(stdio.StringIO()):
        code = call()
    return code, out.getvalue()


class DigestsExcludeTheEntryIdTest(unittest.TestCase):
    """The record is over prose content, and hand-derived rather than round-tripped."""

    ALPHA = "Prose about the alpha cluster."
    BETA = "Prose about the beta cluster."

    def test_re_keying_prose_leaves_the_record_identical(self):
        """The break: hashing the id alongside the prose. `summaries remap` moves
        every id it carries prose onto, so a record that moved with the ids could
        never be compared across a re-clustering."""
        self.assertEqual(
            io.prose_digests({"7": self.ALPHA, "9": self.BETA}.values()),
            io.prose_digests({"42": self.ALPHA, "1000": self.BETA}.values()),
        )

    def test_changed_prose_changes_the_record(self):
        """The other direction, and the reason the test above cannot stand alone:
        a digest function returning a constant satisfies it."""
        self.assertNotEqual(
            io.prose_digests([self.ALPHA]),
            io.prose_digests([self.ALPHA + " Rewritten."]),
        )

    def test_the_recorded_digest_is_the_sha256_of_the_prose_alone(self):
        """Derived by hand, so the expected value does not come from the code under
        test: the same hash of the same bytes, computed independently."""
        import hashlib

        self.assertEqual(
            io.prose_digest(self.ALPHA),
            hashlib.sha256(self.ALPHA.encode("utf-8")).hexdigest(),
        )

    def test_the_record_is_sorted_so_two_runs_agree(self):
        """The break: a record whose order followed the order entries happened to
        be built in, in an artefact whose byte-identical output is a stated
        property."""
        forward = io.prose_digests([self.ALPHA, self.BETA])
        self.assertEqual(forward, sorted(forward))
        self.assertEqual(forward, io.prose_digests([self.BETA, self.ALPHA]))

    def test_two_entries_holding_the_same_prose_need_two_digests(self):
        """The break: de-duplicating the record, after which editing one of a
        duplicated pair still matches the surviving digest and is not reported."""
        self.assertEqual(len(io.prose_digests([self.ALPHA, self.ALPHA])), 2)


class MetadataIsNotAnEntryTest(SettingsIsolated):
    """Nothing that reads a prose layer may read its metadata block as an entry."""

    BRIEFS = {
        "welsh-language": {"title": "Welsh language", "html": "<p>Prose.</p>"},
        io.SUMMARIES_METADATA_KEY: {io.PROSE_DIGESTS_KEY: ["0" * 64]},
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        config.configure(root=str(self.root))
        config.TOPICS_BRIEFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.TOPICS_BRIEFS_PATH.write_text(json.dumps(self.BRIEFS), encoding="utf-8")

    def test_the_shared_reader_returns_the_entries_alone(self):
        self.assertEqual(
            io.read_prose_layer(config.TOPICS_BRIEFS_PATH),
            {"welsh-language": self.BRIEFS["welsh-language"]},
        )

    def test_status_does_not_count_the_block_as_a_brief(self):
        """The break: one more brief in the coverage line `status` prints, which is
        ordinary-looking output nobody can tell is wrong."""
        self.assertEqual(status.layer_coverage()["briefs_written"], 1)

    def test_the_block_carries_no_prose_for_the_drift_check_to_read(self):
        """The break: the block's own values counted as an entry's prose, which
        would report a mismatch against a digest of the digests."""
        self.assertEqual(
            io.rendered_prose(json.loads(config.TOPICS_BRIEFS_PATH.read_text(encoding="utf-8"))),
            {"welsh-language": "<p>Prose.</p>"},
        )


class ExplorerDoesNotEmbedTheBlockTest(SettingsIsolated):
    """The page is the consumer where a leaked block becomes a phantom card.

    `status` counting one brief too many is a number nobody can tell is wrong; the
    explorer renders the block's contents as a topic and a deep dive a reader can
    click on. Both call sites of the shared reader are pinned, because the strip
    living in one reader is only a guarantee while every consumer uses it.
    """

    BRIEFS = {
        "payments": {"title": "Payments", "keywords": ["pay"], "html": "<p>Brief.</p>"},
        io.SUMMARIES_METADATA_KEY: {io.PROSE_DIGESTS_KEY: ["0" * 64]},
    }
    DIVES = {
        "repo-one": {"title": "Deep dive: repo-one", "html": "<p>Dossier.</p>", "sha": "abcd1234"},
        io.SUMMARIES_METADATA_KEY: {io.PROSE_DIGESTS_KEY: ["1" * 64]},
    }

    def test_the_page_embeds_the_entries_without_the_metadata_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            nodes = [
                {"id": f"n{i}", "label": f"ServiceComponent{i}", "source_file": f"src/f{i}.ts"}
                for i in range(6)
            ]
            io.write_json(
                config.GRAPH_PATH,
                {
                    "nodes": nodes,
                    "links": [
                        {"source": f"n{i}", "target": f"n{j}"}
                        for i in range(6)
                        for j in range(6)
                        if i != j
                    ],
                },
            )
            io.write_json(config.LABELS_PATH, {})
            io.write_json(config.SUMMARIES_PATH, {"1": "Prose about one invented cluster."})
            io.write_json(config.TOPICS_BRIEFS_PATH, self.BRIEFS)
            io.write_json(config.DEEPDIVES_PATH, self.DIVES)

            self.assertEqual(_captured(explorer.main)[0], 0)
            page = config.EXPLORER_PATH.read_text(encoding="utf-8")

        self.assertEqual(list(_embedded(page, "topics")), ["payments"])
        self.assertEqual(list(_embedded(page, "dives")), ["repo-one"])


def _embedded(page: str, block: str) -> dict:
    """One `<script id=...>` JSON block, parsed back out of the built page."""
    import re

    matched = re.search(
        rf'<script id="{block}" type="application/json">(.*?)</script>', page, re.DOTALL
    )
    assert matched is not None, f"the page has no {block} block"
    return json.loads(matched.group(1).replace("<\\/", "</"))


class SummariesDriftTest(SettingsIsolated):
    """`summaries merge` and `summaries remap` report edited committed prose."""

    WATCHED = "7"
    PINNED = 1_000_000_000

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        (self.root / "graphify-out").mkdir(parents=True)
        config.configure(root=str(self.root))

    # --- fixture -------------------------------------------------------------

    @staticmethod
    def prose(cid: str) -> str:
        return (
            f"Community {cid} groups the lookup and persistence paths that one "
            "invented service reads at build time."
        )

    def members(self, watched: str = WATCHED) -> dict[str, list[str]]:
        """More communities than the remap's plausibility floor asks for."""
        return {watched: ["a", "b", "c"]} | {str(i): [f"x{i}"] for i in range(1000, 1030)}

    def write_graph(self, members: dict[str, list[str]]) -> None:
        config.GRAPH_PATH.write_text(
            json.dumps(
                {
                    "nodes": [
                        {"id": node, "label": node, "community": int(cid)}
                        for cid, ids in members.items()
                        for node in ids
                    ],
                    "links": [],
                }
            ),
            encoding="utf-8",
        )

    def commit_through_merge(self) -> dict[str, str]:
        """Commit prose through the real merge, so the file under test is its output."""
        members = self.members()
        with gzip.open(config.SUMMARIES_SNAPSHOT_PATH, "wt", encoding="utf-8") as handle:
            json.dump(members, handle)
        self.write_graph(members)
        prose = {cid: self.prose(cid) for cid in members}
        config.SUMMARIES_INPUT_PATH.write_text(
            json.dumps([{"id": cid} for cid in prose]), encoding="utf-8"
        )
        code, _ = self.run_merge(prose, name="first.json")
        self.assertEqual(code, 0, "the fixture did not commit")
        return prose

    def run_merge(self, prose: dict[str, str], name: str = "again.json") -> tuple[int, str]:
        batch = self.root / name
        batch.write_text(json.dumps(prose), encoding="utf-8")
        return _captured(lambda: summaries.merge([str(batch)]))

    def document(self) -> dict:
        return json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))

    def recorded(self) -> list[str]:
        return self.document()[io.SUMMARIES_METADATA_KEY][io.PROSE_DIGESTS_KEY]

    def edit_committed_prose(self) -> None:
        """A hand edit: the prose changes and the digests beside it do not."""
        document = self.document()
        document[self.WATCHED] += " And a sentence nobody generated."
        config.SUMMARIES_PATH.write_text(json.dumps(document, indent=1), encoding="utf-8")

    # --- the record ----------------------------------------------------------

    def test_the_artefact_records_one_digest_per_summary(self):
        prose = self.commit_through_merge()
        self.assertEqual(self.recorded(), io.prose_digests(prose.values()))

    def test_a_run_that_changed_no_prose_still_records_the_digests(self):
        """The break: the write gate skipping the only run that would have recorded
        them. A store upgrading to this version has unchanged prose, so the gate
        skips, and the check then waits for an unrelated edit before it can work at
        all - indistinguishable from a check that is working and finding nothing."""
        prose = self.commit_through_merge()
        document = self.document()
        del document[io.SUMMARIES_METADATA_KEY][io.PROSE_DIGESTS_KEY]
        config.SUMMARIES_PATH.write_text(json.dumps(document, indent=1), encoding="utf-8")

        self.assertEqual(self.run_merge(prose)[0], 0)

        self.assertEqual(self.recorded(), io.prose_digests(prose.values()))

    def test_a_second_run_after_that_writes_nothing(self):
        """The over-correction on the migration above: withholding the recorded
        digest unconditionally would rewrite the artefact on every run, which is
        the churn #313 removed."""
        prose = self.commit_through_merge()
        os.utime(config.SUMMARIES_PATH, (self.PINNED, self.PINNED))

        _, output = self.run_merge(prose)

        self.assertEqual(int(config.SUMMARIES_PATH.stat().st_mtime), self.PINNED)
        self.assertIn("not rewritten", output)

    # --- the report ----------------------------------------------------------

    def test_an_edited_summary_is_reported(self):
        """The gap itself: prose edited in the committed artefact after the stage
        wrote it, which no stage established anything about."""
        prose = self.commit_through_merge()
        self.edit_committed_prose()

        _, output = self.run_merge(prose)

        self.assertIn(DRIFT_MARK, output)
        self.assertIn(f"  {self.WATCHED}: prose differs", output)

    def test_the_report_names_only_the_edited_summary(self):
        """The break: reporting that something moved rather than what. A report
        naming 31 entries when one was edited is not actionable."""
        prose = self.commit_through_merge()
        self.edit_committed_prose()

        _, output = self.run_merge(prose)

        named = [line for line in output.splitlines() if "prose differs" in line]
        self.assertEqual(
            named, [f"  {self.WATCHED}: prose differs from the digest recorded beside it"]
        )
        self.assertIn(f"1 of {len(prose)} entries", output)

    def test_an_untouched_artefact_is_not_reported(self):
        """The over-correction, and the one that looks most like success:
        "report mismatches" becoming "report everything"."""
        prose = self.commit_through_merge()

        _, output = self.run_merge(prose)

        self.assertNotIn(DRIFT_MARK, output)

    def test_an_edit_does_not_change_the_exit_code(self):
        """The break: refusing before anyone has measured how often a hand edit is
        legitimate, which produces a stage nobody adopts."""
        prose = self.commit_through_merge()
        clean, _ = self.run_merge(prose)
        self.edit_committed_prose()

        edited, output = self.run_merge(prose)

        self.assertIn(DRIFT_MARK, output)
        self.assertEqual(edited, clean)
        self.assertEqual(edited, 0)

    # --- across a re-clustering ---------------------------------------------

    def test_a_remap_that_moves_an_id_leaves_the_record_identical(self):
        """The constraint that decides whether any of this is usable: the record
        must survive a remap. `remap` re-keys prose onto the ids a re-clustering
        produced, so a record carrying the ids would differ after every
        re-clustering and the check would fire on the library's own operation."""
        prose = self.commit_through_merge()
        before = self.recorded()
        self.write_graph(self.members(watched="42"))

        self.assertEqual(_captured(summaries.remap)[0], 0)

        self.assertEqual(self.recorded(), before)
        body = io.read_summaries(config.SUMMARIES_PATH)
        self.assertEqual(body["42"], prose[self.WATCHED])
        self.assertNotIn(self.WATCHED, body)

    def test_a_remap_that_moves_an_id_reports_no_mismatch(self):
        """The same constraint end to end, and it catches a different mistake: a
        check comparing the recorded digests against the prose it is about to
        write, rather than against the prose already committed."""
        self.commit_through_merge()
        self.write_graph(self.members(watched="42"))

        _, output = _captured(summaries.remap)

        self.assertNotIn(DRIFT_MARK, output)

    def test_a_remap_reports_a_summary_edited_before_it_ran(self):
        """The other direction: a remap that reported nothing whatever the file
        held would pass the test above."""
        self.commit_through_merge()
        self.edit_committed_prose()
        self.write_graph(self.members(watched="42"))

        code, output = _captured(summaries.remap)

        self.assertIn(f"  {self.WATCHED}: prose differs", output)
        self.assertEqual(code, 0)


class BriefsDriftTest(SettingsIsolated):
    """`topics merge` records digests for the briefs and reports edited ones."""

    SLUG = "present"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        docs = self.root / "docs"
        docs.mkdir()
        (docs / f"{self.SLUG}.md").write_text(f"# Present topic\n\n{EVIDENCE}", encoding="utf-8")
        topics = self.root / "topics.txt"
        topics.write_text(f"{self.SLUG} | Present topic | word\n", encoding="utf-8")
        config.configure(
            TOPICS_CONFIG_PATH=topics,
            TOPICS_DOCS_DIR=docs,
            TOPICS_BRIEFS_PATH=self.root / "briefs.json",
        )

    def document(self) -> dict:
        return json.loads(config.TOPICS_BRIEFS_PATH.read_text(encoding="utf-8"))

    def edit_committed_prose(self) -> None:
        document = self.document()
        document[self.SLUG]["html"] += "<p>A claim nobody generated.</p>"
        config.TOPICS_BRIEFS_PATH.write_text(json.dumps(document, indent=1), encoding="utf-8")

    def test_the_artefact_records_one_digest_per_brief(self):
        """The gap: `build_topic_briefs` recorded nothing at all, so a brief in the
        committed file could not be compared with what the stage rendered."""
        self.assertEqual(_captured(briefs.merge)[0], 0)
        document = self.document()
        self.assertEqual(
            document[io.SUMMARIES_METADATA_KEY][io.PROSE_DIGESTS_KEY],
            [io.prose_digest(document[self.SLUG]["html"])],
        )

    def test_an_edited_brief_is_reported(self):
        self.assertEqual(_captured(briefs.merge)[0], 0)
        self.edit_committed_prose()

        _, output = _captured(briefs.merge)

        self.assertIn(DRIFT_MARK, output)
        self.assertIn(f"  {self.SLUG}: prose differs", output)

    def test_an_untouched_artefact_is_not_reported(self):
        self.assertEqual(_captured(briefs.merge)[0], 0)

        _, output = _captured(briefs.merge)

        self.assertNotIn(DRIFT_MARK, output)

    def test_editing_the_markdown_source_is_not_reported_as_an_edit(self):
        """The break that would make this unusable: `docs/topics/<slug>.md` is the
        authoring route, and the stage renders from it. Reporting a rewritten brief
        as tampering would fire on the way briefs are meant to be written."""
        self.assertEqual(_captured(briefs.merge)[0], 0)
        (config.TOPICS_DOCS_DIR / f"{self.SLUG}.md").write_text(
            f"# Present topic\n\nRewritten by its author. {EVIDENCE}", encoding="utf-8"
        )

        _, output = _captured(briefs.merge)

        self.assertNotIn(DRIFT_MARK, output)

    def test_an_edit_does_not_change_the_exit_code(self):
        clean = _captured(briefs.merge)[0]
        self.edit_committed_prose()

        edited, output = _captured(briefs.merge)

        self.assertIn(DRIFT_MARK, output)
        self.assertEqual(edited, clean)
        self.assertEqual(edited, 0)


class DivesDriftTest(SettingsIsolated):
    """`deepdive merge` records digests for the dossiers and reports edited ones."""

    REPO = "good"
    SHA = "a" * 40

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        config.configure(
            DEEPDIVES_INPUT_DIR=self.root / "in",
            DEEPDIVES_DOCS_DIR=self.root / "docs",
            DEEPDIVES_PATH=self.root / "in" / "dives.json",
        )
        (self.root / "in").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "in" / f"{self.REPO}-input.json").write_text(
            json.dumps({"repo": self.REPO, "provenance": {"sha": self.SHA}}), encoding="utf-8"
        )
        (self.root / "docs" / f"{self.REPO}.md").write_text(
            f"# Deep dive: {self.REPO}\n\nMeasured at `{self.SHA[:8]}`.\n\n{EVIDENCE}",
            encoding="utf-8",
        )

    def document(self) -> dict:
        return json.loads(config.DEEPDIVES_PATH.read_text(encoding="utf-8"))

    def edit_committed_prose(self) -> None:
        document = self.document()
        document[self.REPO]["html"] += "<p>A claim nobody generated.</p>"
        config.DEEPDIVES_PATH.write_text(json.dumps(document, indent=1), encoding="utf-8")

    def test_the_artefact_records_one_digest_per_dossier(self):
        """The gap: `build_deep_dives` recorded nothing at all, in the layer whose
        every figure is stamped with the build it measured."""
        self.assertEqual(_captured(dives.merge)[0], 0)
        document = self.document()
        self.assertEqual(
            document[io.SUMMARIES_METADATA_KEY][io.PROSE_DIGESTS_KEY],
            [io.prose_digest(document[self.REPO]["html"])],
        )

    def test_an_edited_dossier_is_reported(self):
        self.assertEqual(_captured(dives.merge)[0], 0)
        self.edit_committed_prose()

        _, output = _captured(dives.merge)

        self.assertIn(DRIFT_MARK, output)
        self.assertIn(f"  {self.REPO}: prose differs", output)

    def test_an_untouched_artefact_is_not_reported(self):
        self.assertEqual(_captured(dives.merge)[0], 0)

        _, output = _captured(dives.merge)

        self.assertNotIn(DRIFT_MARK, output)

    def test_an_edit_does_not_change_the_exit_code(self):
        clean = _captured(dives.merge)[0]
        self.edit_committed_prose()

        edited, output = _captured(dives.merge)

        self.assertIn(DRIFT_MARK, output)
        self.assertEqual(edited, clean)
        self.assertEqual(edited, 0)

    def test_a_rejected_dossier_still_leaves_the_exit_code_to_validation(self):
        """The report must not be able to mask the validation verdict either: a
        dossier that cannot say which build it measured still fails the run."""
        (self.root / "in" / "bad-input.json").write_text(
            json.dumps({"repo": "bad", "provenance": {"sha": "b" * 40}}), encoding="utf-8"
        )
        (self.root / "docs" / "bad.md").write_text(
            f"# Deep dive: bad\n\n{EVIDENCE}", encoding="utf-8"
        )

        code, output = _captured(dives.merge)

        self.assertEqual(code, 1)
        self.assertIn("bad: dossier does not state the build", output)


if __name__ == "__main__":
    unittest.main()
