"""The shipped per-chunk gate must catch what it names and nothing it was calibrated past (#394).

Every extraction agent on one large rebuild wrote its own copy of this checker, and
a first shipped version fired on two conventions real layers rely on: one id in two
repositories inside one chunk, and the `contains` relation. Both are pinned here, as
is the property a batch-level checker most often loses - each chunk is checked
against its own file list, not the batch's.

Each test names the production change that should make it fail. Expected rule names
are written by hand from fixtures written here, never computed by the code under
test. Ids are compared with graphify's own `normalize_id`, so these run where
graphify is installed - the "with extras" step in CI - and skip without it, except
the refusal that exists for exactly that case.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledgestore import check_chunk, cli  # noqa: E402

try:
    import graphify.ids  # noqa: F401

    HAS_GRAPHIFY = True
except ImportError:  # pragma: no cover - depends on the environment
    HAS_GRAPHIFY = False

needs_graphify = unittest.skipUnless(
    HAS_GRAPHIFY, "needs graphify for normalize_id (the `ast` extra; CI installs it)"
)


def run(argv: list[str]) -> tuple[int, str, str]:
    """Run `knowledgestore <argv>` through the real dispatcher; (exit, stdout, stderr)."""
    out, err = _io.StringIO(), _io.StringIO()
    saved = sys.argv
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
    finally:
        sys.argv = saved
    return code, out.getvalue(), err.getvalue()


def rules(stdout: str) -> set[str]:
    """The rule names on the violation lines of a run's output."""
    return {
        line.split(" ", 1)[0]
        for line in stdout.splitlines()
        if line.split(" ", 1)[0] in check_chunk.RULES
    }


class Batch:
    """A two-chunk batch written to disk: chunk 1 over two repositories, chunk 2 over one."""

    def __init__(self, root: Path):
        self.root = root
        self.a = str(root / "repositories" / "repo-one" / "a.yaml")
        self.b = str(root / "repositories" / "repo-two" / "b.md")
        self.c = str(root / "repositories" / "repo-two" / "c.md")
        self.files = {1: [self.a, self.b], 2: [self.c]}
        self.payloads = {
            1: {
                "nodes": [
                    self.node("one", self.a),
                    self.node("two", self.a),
                    self.node("three", self.b),
                ],
                "edges": [self.edge("one", "two", self.a)],
                "hyperedges": [
                    {
                        "id": "first_group",
                        "label": "first group",
                        "nodes": ["one", "two", "three"],
                        "relation": "form",
                        "confidence": "EXTRACTED",
                        "confidence_score": 1.0,
                        "source_file": self.a,
                    }
                ],
            },
            2: {"nodes": [self.node("four", self.c)], "edges": [], "hyperedges": []},
        }

    @staticmethod
    def node(nid: str, source_file: str) -> dict:
        return {
            "id": nid,
            "label": f"node {nid}",
            "file_type": "document",
            "source_file": source_file,
        }

    @staticmethod
    def edge(source: str, target: str, source_file: str) -> dict:
        return {
            "source": source,
            "target": target,
            "relation": "references",
            "confidence": "EXTRACTED",
            "confidence_score": 1.0,
            "source_file": source_file,
        }

    def write(self, name: str = "batch.json") -> Path:
        entries = []
        for number, payload in self.payloads.items():
            out = self.root / f".graphify_chunk_{number:04d}.json"
            out.write_text(json.dumps(payload), encoding="utf-8")
            entries.append({"n": number, "out": str(out), "files": self.files[number]})
        batch = self.root / name
        batch.write_text(json.dumps({"chunks": entries}), encoding="utf-8")
        return batch


@needs_graphify
class CheckChunkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.batch = Batch(Path(tmp.name))

    def check(self) -> tuple[int, str]:
        code, stdout, _ = run(["check-chunk", "--batch", str(self.batch.write())])
        return code, stdout

    def test_the_self_test_passes_and_reports_every_rule_caught(self):
        # Breaks if any rule stops firing on its mutation, fires alongside another
        # rule, or a negative control starts firing - the self-test then exits 1.
        code, stdout, _ = run(["check-chunk", "--self-test"])
        self.assertEqual(code, 0, stdout)
        self.assertIn("failures=0", stdout)
        for rule in check_chunk.RULES:
            self.assertRegex(stdout, rf"(?m)^caught  {rule} ", rule)

    def test_a_clean_batch_exits_zero_and_prints_its_denominators(self):
        # Breaks if a rule fires on correct data, or the denominators stop printing.
        code, stdout = self.check()
        self.assertEqual(code, 0, stdout)
        self.assertEqual(rules(stdout), set())
        self.assertIn("checked: 2 chunks, 4 nodes, 1 edges, 1 hyperedges over 3 files", stdout)

    def test_each_chunk_is_held_to_its_own_files_not_the_batch_union(self):
        # Breaks if containment is checked against every file the batch names:
        # chunk 2's file is in the batch, but chunk 1 was not given it.
        self.batch.payloads[1]["nodes"].append(Batch.node("stray", self.batch.c))
        code, stdout = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(rules(stdout), {"SOURCE_FILE"})
        self.assertIn("SOURCE_FILE chunk 1: node 'stray'", stdout)

    def test_one_id_in_two_repositories_is_not_a_duplicate_but_in_one_is(self):
        # Breaks if DUP_NODE keys on the id alone (fires on the merge's legitimate
        # cross-repository collisions) or stops firing within one repository.
        self.batch.payloads[1]["nodes"].append(Batch.node("one", self.batch.b))
        code, stdout = self.check()
        self.assertEqual((code, rules(stdout)), (0, set()), stdout)

        self.batch.payloads[1]["nodes"].append(Batch.node("one", self.batch.a))
        code, stdout = self.check()
        self.assertEqual((code, rules(stdout)), (1, {"DUP_NODE"}), stdout)
        self.assertIn("node id 'one' twice in repository 'repo-one'", stdout)

    def test_contains_is_an_accepted_relation_and_an_unknown_one_is_not(self):
        # Breaks if `contains` is dropped from the vocabulary, or the vocabulary
        # stops being closed.
        self.batch.payloads[1]["edges"][0]["relation"] = "contains"
        code, stdout = self.check()
        self.assertEqual((code, rules(stdout)), (0, set()), stdout)

        self.batch.payloads[1]["edges"][0]["relation"] = "depends_on"
        code, stdout = self.check()
        self.assertEqual((code, rules(stdout)), (1, {"RELATION"}), stdout)

    def test_the_suffix_rule_fires_on_this_chunks_number_only(self):
        # Breaks if the rule becomes a digit pattern (fires on the form code) or
        # stops comparing against this chunk's own number.
        nodes = self.batch.payloads[1]["nodes"]
        nodes.append(Batch.node("form_c100", self.batch.a))
        nodes.append(Batch.node("other_c0002", self.batch.a))
        code, stdout = self.check()
        self.assertEqual((code, rules(stdout)), (0, set()), stdout)

        nodes.append(Batch.node("thing_c0001", self.batch.a))
        code, stdout = self.check()
        self.assertEqual((code, rules(stdout)), (1, {"CHUNK_SUFFIX"}), stdout)
        self.assertIn("'thing_c0001'", stdout)

    def test_a_hyperedge_id_is_held_to_canonical_form_like_a_node_id(self):
        # Breaks if the canonical-form rule reads node ids only: a real layer
        # carried hyperedge ids spelt as repository paths, and that passed.
        self.batch.payloads[1]["hyperedges"][0]["id"] = "repo-one/first-group"
        code, stdout = self.check()
        self.assertEqual((code, rules(stdout)), (1, {"ID_FORM"}), stdout)
        self.assertIn("hyperedge id 'repo-one/first-group' is not its canonical form", stdout)

    def test_a_hyperedge_id_reused_in_another_batch_file_is_reported(self):
        # Breaks if the seen-hyperedge record is reset per batch file.
        first = self.batch.write("first.json")
        second_root = self.batch.root / "second"
        second_root.mkdir()
        other = Batch(second_root)
        other.payloads[2]["hyperedges"] = [dict(other.payloads[1]["hyperedges"][0])]
        other.payloads[2]["hyperedges"][0]["source_file"] = other.c
        other.payloads[2]["hyperedges"][0]["nodes"] = ["four", "five", "six"]
        other.payloads[2]["nodes"] += [Batch.node(n, other.c) for n in ("five", "six")]
        del other.payloads[1]
        code, stdout, _ = run(["check-chunk", "--batch", str(first), str(other.write())])
        self.assertEqual((code, rules(stdout)), (1, {"DUP_HYPEREDGE"}), stdout)
        self.assertIn("'first_group' is used by chunk 1", stdout)

    def test_a_truncated_chunk_is_a_parse_violation_and_the_rest_are_still_read(self):
        # Breaks if a json.load is left unguarded (a traceback ends the run) or a
        # parse failure stops the batch.
        batch = self.batch.write()
        chunk_one = self.batch.root / ".graphify_chunk_0001.json"
        text = chunk_one.read_text(encoding="utf-8")
        chunk_one.write_text(text[: len(text) // 2], encoding="utf-8")
        self.batch.payloads[2]["edges"].append(Batch.edge("four", "nobody", self.batch.c))
        (self.batch.root / ".graphify_chunk_0002.json").write_text(
            json.dumps(self.batch.payloads[2]), encoding="utf-8"
        )
        code, stdout, _ = run(["check-chunk", "--batch", str(batch)])
        self.assertEqual(code, 1)
        self.assertEqual(rules(stdout), {"PARSE", "DANGLING"})
        self.assertIn("PARSE chunk 1:", stdout)

    def test_violations_print_before_the_zero_chunk_refusal(self):
        # Breaks if the refusal returns before printing, or a batch with no
        # checkable chunk is reported as clean.
        bad = self.batch.root / "bad.json"
        bad.write_text(json.dumps({"chunks": [{"n": 1, "out": "x.json"}]}), encoding="utf-8")
        code, stdout, _ = run(["check-chunk", "--batch", str(bad)])
        self.assertEqual(code, 1)
        lines = stdout.splitlines()
        shape = next(i for i, line in enumerate(lines) if line.startswith("SHAPE batch"))
        refused = next(i for i, line in enumerate(lines) if line.startswith("REFUSED"))
        self.assertLess(shape, refused)

    def test_an_unreadable_batch_file_is_a_parse_violation_not_a_traceback(self):
        # Breaks if the batch read is unguarded.
        truncated = self.batch.root / "truncated.json"
        truncated.write_text('{"chunks": [', encoding="utf-8")
        code, stdout, _ = run(["check-chunk", "--batch", str(truncated)])
        self.assertEqual(code, 1)
        self.assertIn(f"PARSE batch {truncated}: unreadable", stdout)
        self.assertIn("REFUSED", stdout)


class MissingGraphifyTest(unittest.TestCase):
    def test_without_graphify_the_stage_refuses_and_names_the_extra(self):
        # Breaks if the missing import escapes as a traceback or the refusal stops
        # naming the remedy. A None entry in sys.modules is how Python itself marks
        # a module unimportable. (A module-level graphify import breaks this whole
        # file's import in CI's default-install step, which has no graphify.)
        with mock.patch.dict(sys.modules, {"graphify": None, "graphify.ids": None}):
            code, stdout, stderr = run(["check-chunk", "--self-test"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("[ast]", stderr)


if __name__ == "__main__":
    unittest.main()
