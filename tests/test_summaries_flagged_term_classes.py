"""A flagged term is classified by where it was found, not only by the graph (#249).

`absent_from_estate` checks the graph, and the graph holds a ticket node only for
what the intent index mined. So a summary citing a ticket that was real for the
community it was written for, and which is not among the current community's
tickets, reads as absent from the estate. Measured on a large store, that was
nearly half of everything the estate pass reported — every one of them a real
ticket recorded in the history datasets.

The signal is right and the reporting was not: an invented class name and a real
ticket the graph does not hold were one figure, and they need different actions.
An invented identifier means the prose is wrong and has to be rewritten; a ticket
present only in history means the summary is keyed to a community that has moved,
which is a remap question. Reporting them together inflates the actionable count,
and an operator cannot tell which half to act on without chasing single terms by
hand.

Two properties carry the fix, and both are asserted here:

- the classes are reported separately, each with the action it implies
- **the counts reconcile** — the classes sum to the flagged total, in the output,
  because a breakdown that does not add up is worse than none and a tool's own
  count is not verification

Every store here is real: real digests, real summaries, a real graph file, real
`commits.ndjson` datasets, and the real `verify` stage reading them off disk.
Nothing is stubbed, so every assertion lands on what an operator is shown.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import mutation_gate as gate

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402


# One commit, in the shape `export-history` writes: the fields this lookup reads
# are `subject`, `body` and the recorded file paths.
def commit(subject: str, body: str = "", path: str = "src/AlphaService.java") -> dict:
    return {
        "repository": "svc-alpha",
        "sha": "0" * 40,
        "subject": subject,
        "body": body,
        "files": [{"path": path, "additions": 1, "deletions": 0}],
        "is_merge": False,
    }


class FlaggedTermClassesTest(SettingsIsolated):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        (self.root / "graphify-out").mkdir(parents=True)
        config.configure(root=str(self.root))

    def store(self, prose: str, digest_nodes: list[str], graph_labels: list[str]) -> None:
        """One community, its digest, its prose and the graph the estate pass reads."""
        config.SUMMARIES_INPUT_PATH.write_text(
            json.dumps([{"id": "1", "top_nodes": [{"label": n} for n in digest_nodes]}]),
            encoding="utf-8",
        )
        config.SUMMARIES_PATH.write_text(json.dumps({"1": prose}), encoding="utf-8")
        config.GRAPH_PATH.write_text(
            json.dumps({"nodes": [{"id": n, "label": n} for n in graph_labels]}),
            encoding="utf-8",
        )

    def history(self, commits: list[dict], repo: str = "svc-alpha") -> None:
        dataset = config.HISTORY_DIR / repo
        dataset.mkdir(parents=True)
        (dataset / "commits.ndjson").write_text(
            "\n".join(json.dumps(record) for record in commits) + "\n", encoding="utf-8"
        )

    def run_verify(self) -> str:
        captured = io.StringIO()
        with redirect_stdout(captured), redirect_stderr(io.StringIO()):
            summaries.verify(estate=True)
        return captured.getvalue()

    def lines_about_the_graph(self, output: str) -> list[str]:
        return [line for line in output.splitlines() if "not in graph" in line]

    def lines_tagged(self, output: str, tag: str) -> str:
        return "\n".join(line for line in output.splitlines() if tag in line)

    # --- the two classes -----------------------------------------------------

    def test_a_ticket_the_history_datasets_cite_is_not_counted_as_possible_invention(self):
        """Breaks if the classification collapses back to one count.

        The reported defect: `ZED-4242` is a real ticket, recorded in the history
        datasets, and the graph holds no node for it because the intent index did
        not mine it. Counting it beside an invented class name is what inflates the
        number an operator reads.
        """
        self.store(
            "AlphaService writes decisions, added under ZED-4242, through GammaThing.",
            ["AlphaService"],
            ["AlphaService"],
        )
        self.history([commit("ZED-4242: record the decision")])

        output = self.run_verify()

        self.assertIn("[not in graph, in history] community 1 cites: ZED-4242", output)
        self.assertIn("in the history datasets but not the graph: 1", output)
        self.assertNotIn("ZED-4242", self.lines_tagged(output, "[not in graph or history]"))

    def test_a_term_in_neither_the_graph_nor_history_is_the_class_that_can_contain_invention(self):
        """Breaks if the history lookup is skipped, or credits everything to history.

        This is the figure the change exposes: the only class that can contain
        invention. A lookup that answered "found" for every term would empty it and
        report a clean store, which is the reassuring direction and the one this
        assertion holds.
        """
        self.store(
            "AlphaService writes decisions, added under ZED-4242, through GammaThing.",
            ["AlphaService"],
            ["AlphaService"],
        )
        self.history([commit("ZED-4242: record the decision")])

        output = self.run_verify()

        self.assertIn("[not in graph or history] community 1 cites: GammaThing", output)
        self.assertIn("absent from the graph AND from history: 1", output)

    def test_the_two_classes_sum_to_the_flagged_total(self):
        """Breaks if a term is dropped from the breakdown or counted in both classes.

        Derived by hand: the prose cites `AlphaService` (in the digest, so never
        flagged), `ZED-4242` and `BetaWidget` (in history, not the graph) and
        `GammaThing` (in neither). Three terms flagged as absent from the graph, of
        which one can contain invention and two are in history, so the line must
        read 1 + 2 = 3.
        """
        self.store(
            "AlphaService writes decisions under ZED-4242, through GammaThing and BetaWidget.",
            ["AlphaService"],
            ["AlphaService"],
        )
        self.history(
            [
                commit("ZED-4242: record the decision"),
                commit("Rename the widget", body="BetaWidget replaces the old panel."),
            ]
        )

        output = self.run_verify()

        self.assertIn("3 term(s) in 1 summary(ies) are absent from the graph", output)
        self.assertIn("absent from the graph AND from history: 1", output)
        self.assertIn("in the history datasets but not the graph: 2", output)
        self.assertIn("reconciled: 1 + 2 = 3 flagged", output)

    # --- the sensitivity control --------------------------------------------

    def test_a_store_whose_prose_the_graph_corroborates_reports_neither_class(self):
        """Breaks if the class lines are printed unconditionally.

        A breakdown that appears when there is nothing to break down cannot be told
        from one that is measuring something, and an operator would read two zeroes
        as a finding.
        """
        self.store(
            "AlphaService writes decisions through BetaWidget.",
            ["AlphaService"],
            ["AlphaService", "BetaWidget"],
        )
        self.history([commit("ZED-4242: record the decision")])

        output = self.run_verify()

        self.assertIn("exists somewhere in the graph", output)
        self.assertEqual(self.lines_about_the_graph(output), [])
        self.assertNotIn("in the history datasets but not the graph", output)
        self.assertNotIn("absent from the graph AND from history", output)
        self.assertNotIn("reconciled:", output)

    def test_a_ticket_shaped_term_the_graph_holds_is_not_flagged_at_all(self):
        """Breaks if the classification changes what is flagged.

        The predicate is unchanged: a ticket the graph holds as a node was never an
        estate finding, and adding a history lookup must not make it one.
        """
        self.store(
            "AlphaService writes decisions, added under ZED-4242.",
            ["AlphaService"],
            ["AlphaService", "ZED-4242"],
        )
        self.history([commit("ZED-4242: record the decision")])

        output = self.run_verify()

        self.assertEqual(self.lines_about_the_graph(output), [])
        self.assertIn("exists somewhere in the graph", output)

    # --- what the history lookup claims -------------------------------------

    def test_history_credits_a_whole_token_rather_than_a_longer_name_containing_it(self):
        """Breaks if the history lookup becomes a substring match.

        A loose match moves terms out of the invention class, which is the only
        class an operator acts on - so it fails in the reassuring direction and
        empties the figure the change exists to expose. `svc-alpha-api` is not
        recorded by a commit that names `svc-alpha-api-gateway`.
        """
        self.store(
            "AlphaService writes decisions through svc-alpha-api.",
            ["AlphaService"],
            ["AlphaService"],
        )
        self.history([commit("Point at svc-alpha-api-gateway", path="svc/one.java")])

        output = self.run_verify()

        self.assertIn("[not in graph or history] community 1 cites: svc-alpha-api", output)
        self.assertIn("absent from the graph AND from history: 1", output)

    def test_a_term_a_recorded_file_path_names_is_credited_to_history(self):
        """Breaks if the lookup reads commit messages only.

        A path history records is evidence the estate held that file, whatever the
        graph extracted, so a term naming one is not a candidate for invention.
        """
        self.store(
            "AlphaService writes decisions declared in pom.xml.",
            ["AlphaService"],
            ["AlphaService"],
        )
        self.history([commit("Declare the dependency", path="svc-alpha/pom.xml")])

        output = self.run_verify()

        self.assertIn("[not in graph, in history] community 1 cites: pom.xml", output)
        self.assertIn("absent from the graph AND from history: 0", output)
        self.assertIn("reconciled: 0 + 1 = 1 flagged", output)

    def test_a_store_with_no_history_datasets_does_not_claim_the_split(self):
        """Breaks if the invention count is reported over history nobody read.

        Without datasets every term is absent from history by default, so printing
        the split would report the whole flagged total as possible invention on the
        strength of a check that read nothing.
        """
        self.store(
            "AlphaService writes decisions, added under ZED-4242.",
            ["AlphaService"],
            ["AlphaService"],
        )

        output = self.run_verify()

        self.assertIn("[not in graph] community 1 cites: ZED-4242", output)
        self.assertNotIn("absent from the graph AND from history", output)
        self.assertIn("no history datasets", output)
        self.assertIn("knowledgestore export-history", output)


class TheHistoryLookupIsBoundedTest(SettingsIsolated):
    """The datasets are large, so the cost of the lookup is part of the behaviour."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        config.configure(root=str(self.root))

    def dataset(self, repo: str, content: bytes) -> None:
        directory = config.HISTORY_DIR / repo
        directory.mkdir(parents=True)
        (directory / "commits.ndjson").write_bytes(content)

    def test_no_further_dataset_is_opened_once_every_term_is_located(self):
        """Breaks if the lookup reads every repository whatever it has already found.

        Cost is otherwise invisible to a test, so the datasets make it observable:
        the second one, which sorts after the first, holds bytes no UTF-8 decoder
        accepts. A pass that stops when its last term is found never opens it; one
        that reads them all raises. On a real estate that difference is dozens of
        repositories' complete commit history.
        """
        self.dataset(
            "svc-alpha", json.dumps(commit("ZED-4242: record the decision")).encode("utf-8") + b"\n"
        )
        self.dataset("svc-beta", b"\xff\xfe not a commit\n")

        self.assertEqual(summaries.cited_in_history({"ZED-4242"}), {"ZED-4242"})

    def test_the_pass_stops_reading_a_dataset_once_every_term_is_located(self):
        """Breaks if the lookup reads a dataset to the end after its last term is found.

        The same bound inside one repository, which is where a real dataset's size
        is: the line that answers the question comes first, and 200 kB later - well
        past any read-ahead - are bytes no UTF-8 decoder accepts. A pass that stops
        never decodes them.
        """
        filler = json.dumps(commit("Tidy the imports", path="src/Filler.java")).encode("utf-8")
        self.dataset(
            "svc-alpha",
            json.dumps(commit("ZED-4242: record the decision")).encode("utf-8")
            + b"\n"
            + (filler + b"\n") * (200_000 // len(filler))
            + b"\xff\xfe not a commit\n",
        )

        self.assertEqual(summaries.cited_in_history({"ZED-4242"}), {"ZED-4242"})

    def test_no_datasets_is_reported_as_unknown_rather_than_as_absent(self):
        """Breaks if the lookup returns an empty set where it read nothing.

        Empty and unknown are the same value to a caller that cannot tell them
        apart, and one of them says every term is a candidate for invention.
        """
        self.assertIsNone(summaries.cited_in_history({"ZED-4242"}))


class TheMutationEntriesForThisChangeTest(unittest.TestCase):
    """Both entries must name a line that exists, once.

    `apply` replaces the first occurrence and raises when there is none, so zero
    occurrences fails the gate loudly - but two would mutate whichever came first
    and the entry would then describe something other than what it changed.
    """

    ENTRIES = (
        "flagged terms reported as one class again",
        "the history lookup no longer reads the datasets",
    )

    def test_each_entry_matches_exactly_one_line_of_the_stage(self):
        source = Path(summaries.__file__).read_text(encoding="utf-8")
        named = [entry for entry in gate.MUTATIONS if entry.name in self.ENTRIES]

        for entry in named:
            with self.subTest(mutation=entry.name):
                self.assertEqual(
                    source.count(entry.find),
                    1,
                    f"{entry.find!r} appears {source.count(entry.find)} times in {entry.module}",
                )

        self.assertEqual(
            sorted(entry.name for entry in named),
            sorted(self.ENTRIES),
            "an entry was renamed or removed, so this check no longer reads it",
        )


if __name__ == "__main__":
    unittest.main()
