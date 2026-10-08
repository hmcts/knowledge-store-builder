"""The summary batch cutter, the brief's mechanical gate, and merge's refusals (#397).

Every authoring wave re-invented two pieces: cutting the communities without prose
into batches, and a checker for the brief. These pin the shipped ones, end to end
through `knowledgestore summaries ...` on a forged store, and the two ways `merge`
used to report success over nothing.

Each test names the production change that should make it fail. Expected rule names,
ids and counts are written by hand from the fixtures here.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import tempfile
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import cli, config  # noqa: E402

# Two sentences, 60-700 characters, citing only what `digest` below shows.
GOOD = (
    "Helm values for widget-ui in svc-charts, setting listenPort for the web tier. "
    "It holds the base chart configuration."
)


def digest(cid: int) -> dict:
    return {
        "id": cid,
        "label": f"Chart values {cid}",
        "size": 40,
        "repositories": ["svc-charts"],
        "top_nodes": [
            "widget-ui Helm values (base); wrapper nodejs; listenPort 3100 (values.yaml)"
        ],
        "business_features": [],
        "tickets": [],
    }


class StoreFixture(SettingsIsolated):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)

    def run_cli(self, *arguments: str) -> tuple[int, str]:
        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["--root", str(self.root), "summaries", *arguments])
        return code, buffer.getvalue()

    def write_digests(self, ids: list[int]) -> None:
        config.SUMMARIES_INPUT_PATH.write_text(
            json.dumps([digest(cid) for cid in ids]), encoding="utf-8"
        )

    def write_json(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path


class BatchesTest(StoreFixture):
    """`summaries batches` cuts only what has no prose, at the size asked for."""

    def cut(self, *extra: str) -> tuple[int, str, Path]:
        out_dir = self.root / "batches"
        code, output = self.run_cli("batches", "--out-dir", str(out_dir), *extra)
        return code, output, out_dir

    def read(self, out_dir: Path) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(out_dir.glob("batch_*.json"))]

    def test_only_communities_without_prose_are_batched(self):
        # breaks if the committed prose is not subtracted, re-authoring paid-for work
        config.configure(root=str(self.root))
        self.write_digests([5, 6, 7])
        config.SUMMARIES_PATH.write_text(json.dumps({"6": GOOD, "_metadata": {}}), encoding="utf-8")
        code, _, out_dir = self.cut()
        self.assertEqual(code, 0)
        batches = self.read(out_dir)
        self.assertEqual([[d["id"] for d in b["digests"]] for b in batches], [[5, 7]])

    def test_batches_are_cut_at_size_and_name_their_out_file(self):
        # breaks if --size is ignored, numbering stops matching, or `out` is absent
        config.configure(root=str(self.root))
        self.write_digests([1, 2, 3, 4, 5])
        code, output, out_dir = self.cut("--size", "2")
        self.assertEqual(code, 0)
        batches = self.read(out_dir)
        self.assertEqual([b["batch"] for b in batches], [1, 2, 3])
        self.assertEqual([len(b["digests"]) for b in batches], [2, 2, 1])
        self.assertEqual(batches[2]["out"], str(out_dir.absolute() / "out" / "summaries_03.json"))
        self.assertEqual(sorted(batches[0]), ["batch", "digests", "out"])
        self.assertIn("5 significant communities without prose -> 3 batch(es)", output)

    def test_the_default_size_is_one_hundred(self):
        # breaks if the measured default drifts back to the 50 the issue measured against
        config.configure(root=str(self.root))
        self.write_digests(list(range(1, 102)))
        self.cut()
        self.assertEqual([len(b["digests"]) for b in self.read(self.root / "batches")], [100, 1])

    def test_an_earlier_runs_batches_are_refused_not_overwritten(self):
        # breaks if a second run writes over the first: a shorter run would leave the
        # higher-numbered batches behind, valid and stale
        config.configure(root=str(self.root))
        self.write_digests([1, 2, 3])
        self.cut("--size", "1")
        before = (self.root / "batches" / "batch_03.json").read_bytes()
        code, output, _ = self.cut("--size", "5")
        self.assertEqual(code, 1)
        self.assertIn("nothing was written", output)
        self.assertEqual((self.root / "batches" / "batch_03.json").read_bytes(), before)


class CheckBatchTest(StoreFixture):
    """`summaries check-batch` fires each rule on its own and passes clean output."""

    def batch(self, authored: dict, ids: tuple[int, ...] = (1, 2), raw: str | None = None) -> str:
        out = self.root / "authored.json"
        out.write_text(raw if raw is not None else json.dumps(authored), encoding="utf-8")
        path = self.write_json(
            "batch_01.json",
            {"batch": 1, "out": str(out), "digests": [digest(cid) for cid in ids]},
        )
        return str(path)

    def check(self, path: str) -> tuple[int, list[str]]:
        code, output = self.run_cli("check-batch", path)
        return code, [line.split(":")[0] for line in output.splitlines()[:-1]]

    def test_a_clean_batch_passes(self):
        # breaks if any rule fires on prose that meets the brief - including the
        # grounding of an identifier embedded in a descriptive label
        code, output = self.run_cli("check-batch", self.batch({"1": GOOD, "2": GOOD}))
        self.assertEqual(code, 0, output)
        self.assertIn("checked 2 summaries over 1 batch(es); violations=0", output)

    def test_each_rule_fires_alone(self):
        # breaks if a rule stops firing, or fires on a summary that breaks another rule
        cases = {
            "LENGTH 1": "Too short. Really.",
            "SENTENCES 1": "Helm values for widget-ui in svc-charts, setting listenPort for the web tier",
            "UNGROUNDED 1": GOOD.replace("listenPort", "listenAddress"),
        }
        for rule, prose in cases.items():
            with self.subTest(rule=rule):
                code, rules = self.check(self.batch({"1": prose, "2": GOOD}))
                self.assertEqual((code, rules), (1, [rule]))

    def test_the_length_bound_is_merges(self):
        # breaks if the gate's upper bound drifts from merge's 700: 700 characters
        # pass and 701 fail (28 + pad + 15 characters, counted by hand)
        for pad, rules in ((657, []), (658, ["LENGTH 1"])):
            prose = f"Chart values in svc-charts. {'w' * pad}. It ends here."
            with self.subTest(length=28 + pad + 15):
                self.assertEqual(self.check(self.batch({"1": prose, "2": GOOD}))[1], rules)

    def test_missing_extra_and_repeated_ids_are_each_named(self):
        # breaks if an absent id, a stray id or a duplicated id passes
        code, rules = self.check(self.batch({"2": GOOD, "9": GOOD}))
        self.assertEqual((code, sorted(rules)), (1, ["EXTRA_ID 9", "MISSING 1"]))
        repeated = '{"1": "%s", "1": "%s", "2": "%s"}' % (GOOD, GOOD, GOOD)
        code, rules = self.check(self.batch({}, raw=repeated))
        self.assertEqual(code, 1)
        self.assertTrue(rules and rules[0].startswith("PARSE"), rules)

    def test_an_unwritten_out_file_is_a_violation_not_a_traceback(self):
        # breaks if a batch whose agent wrote nothing raises instead of failing cleanly
        path = self.write_json(
            "batch_01.json",
            {"batch": 1, "out": str(self.root / "absent.json"), "digests": [digest(1)]},
        )
        code, rules = self.check(str(path))
        self.assertEqual(code, 1)
        self.assertTrue(rules[0].startswith("PARSE"), rules)

    def test_a_batch_with_no_digests_fails(self):
        # breaks if checking nothing reads as a pass
        code, output = self.run_cli("check-batch", self.batch({}, ids=()))
        self.assertEqual(code, 1)
        self.assertIn("nothing was checked", output)


class MergeRefusesNothingTest(StoreFixture):
    """`summaries merge` no longer writes, or succeeds, having merged nothing."""

    def setUp(self):
        super().setUp()
        config.configure(root=str(self.root))
        self.write_digests([1])

    def test_a_batch_file_is_refused_with_its_out_file_named_and_nothing_written(self):
        # breaks if a batch file is read as authored prose again: that rejected its
        # keys one by one and still wrote the artefact
        batch = self.write_json(
            "batch_01.json", {"batch": 1, "out": "o/summaries_01.json", "digests": []}
        )
        authored = self.write_json("written.json", {"1": GOOD})
        code, output = self.run_cli("merge", str(authored), str(batch))
        self.assertEqual(code, 1)
        self.assertIn("is a batch file", output)
        self.assertIn("o/summaries_01.json", output)
        self.assertFalse(config.SUMMARIES_PATH.exists(), "a refused merge must write nothing")

    def test_merging_nothing_fails_and_leaves_the_committed_file_alone(self):
        # breaks if a run that merged no summary exits 0 or rewrites the artefact
        config.SUMMARIES_PATH.write_text(json.dumps({"1": GOOD}), encoding="utf-8")
        before = config.SUMMARIES_PATH.read_bytes()
        code, output = self.run_cli("merge", str(self.write_json("empty.json", {})))
        self.assertEqual(code, 1)
        self.assertIn("nothing was merged", output)
        self.assertEqual(config.SUMMARIES_PATH.read_bytes(), before)

    def test_a_batch_cut_by_batches_and_authored_merges_through_its_out_file(self):
        # breaks if the two ends of the loop stop agreeing on the shape between them
        out_dir = self.root / "batches"
        self.run_cli("batches", "--out-dir", str(out_dir))
        batch = json.loads((out_dir / "batch_01.json").read_text())
        Path(batch["out"]).write_text(json.dumps({"1": GOOD}), encoding="utf-8")
        self.assertEqual(self.run_cli("check-batch", str(out_dir / "batch_01.json"))[0], 0)
        code, _ = self.run_cli("merge", batch["out"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(config.SUMMARIES_PATH.read_text())["1"], GOOD)
