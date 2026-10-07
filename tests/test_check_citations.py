"""The citation gate, driven with citation shapes as published answers write them.

The store's own version of this gate was written against imagined shapes and broke
on the first real one that carried an argument (#357). Every span below is a shape
a published answer carries, and every expected verdict was derived by hand from the
graph in `GRAPH`, not by running the code under test.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import build_community_summaries, check_citations, cli, config

FIELD_HOLDER = "src/main/java/uk/example/form/FieldHolder.java"
REPOSITORY = "src/main/java/uk/example/repo/CaseRepository.java"

# Labels as the AST layer writes them: methods carry a leading dot and trailing
# brackets, classes and files are bare.
GRAPH = {
    "nodes": [
        {"id": "a", "label": "FieldHolder", "source_file": FIELD_HOLDER},
        {"id": "b", "label": ".setValue()", "source_file": FIELD_HOLDER},
        {"id": "c", "label": "CaseRepository", "source_file": REPOSITORY},
        {"id": "d", "label": ".findOne()", "source_file": REPOSITORY},
        {"id": "e", "label": "CaseRepository.java", "source_file": REPOSITORY},
        # A structural node: no label and no source file. Must not break the walk.
        {"id": "f"},
    ],
    "links": [],
}

ANSWER = """# Case lookup

The holder is updated with `setValue(null)`, which clears the field, and read back
through `FieldHolder.setValue()`. Lookups go through `CaseRepository.findOne(id)`
and are defined in `repo/CaseRepository.java`.

The optional is unwrapped by `ofNullable(...).ifPresent(...)`. A typo reads
`setValu(null)`. The path `epo/CaseRepository.java` is a suffix of a real path
by characters but not by segments. The store keeps `case_record.created_date`.

Words such as `null` and `retry` are not code.

```java
fakeThing(x);   // inside a fence, never a citation
`alsoFake()`
```
"""


class Bed(SettingsIsolated):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config.configure(root=self.tmp)
        self.answers = self.tmp / "docs" / "topics"
        self.answers.mkdir(parents=True)
        (self.answers / "case-lookup.md").write_text(ANSWER)
        self.graph = self.tmp / "graph.json"
        self.graph.write_text(json.dumps(GRAPH))

    def run_stage(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check_citations.main(list(args))
        return code, out.getvalue(), err.getvalue()


class VerdictsOnRealCitationShapes(Bed):
    def setUp(self) -> None:
        super().setUp()
        self.vocabulary = check_citations.load_vocabulary(self.graph)

    def kind(self, span: str) -> str:
        return check_citations.resolve(span, self.vocabulary).kind

    def test_a_method_cited_with_its_argument_resolves(self):
        """The defect: `rstrip("()")` looked this up as `setvalue(null` and failed a
        method that exists."""
        self.assertEqual(self.kind("setValue(null)"), "resolved")

    def test_a_type_and_method_resolve_as_parts(self):
        self.assertEqual(self.kind("FieldHolder.setValue()"), "resolved")
        self.assertEqual(self.kind("CaseRepository.findOne(id)"), "resolved")

    def test_a_chain_of_calls_the_graph_never_held_does_not_resolve(self):
        """The flip side: `split("(")[0]` left an empty member, and the empty string
        matches everything, so this used to pass."""
        self.assertEqual(self.kind("ofNullable(...).ifPresent(...)"), "unresolved")

    def test_a_partial_path_resolves_on_whole_segments(self):
        self.assertEqual(self.kind("repo/CaseRepository.java"), "resolved")
        self.assertEqual(self.kind("CaseRepository.java"), "resolved")

    def test_a_path_that_is_only_a_character_suffix_does_not_resolve(self):
        self.assertEqual(self.kind("epo/CaseRepository.java"), "unresolved")

    def test_a_path_with_the_wrong_directory_does_not_resolve(self):
        self.assertEqual(self.kind("form/CaseRepository.java"), "unresolved")

    def test_a_dotted_schema_name_is_neither_passed_nor_failed(self):
        self.assertEqual(self.kind("case_record.created_date"), "column")

    def test_a_dotted_name_with_a_call_is_never_a_column(self):
        self.assertEqual(self.kind("case_record.created_date()"), "unresolved")

    def test_a_column_shaped_name_whose_parts_both_resolve_is_an_ordinary_pass(self):
        # `holder.value` cannot be told from a column by shape, so it is decided by
        # the graph first: neither part is a label here, so it is a column ...
        self.assertEqual(self.kind("holder.value"), "column")
        # ... and once both parts are labels it resolves.
        self.vocabulary.add(".holder()", "x/Y.java")
        self.vocabulary.add(".value()", "x/Y.java")
        self.assertEqual(self.kind("holder.value"), "resolved")

    def test_plain_words_are_skipped_not_checked(self):
        self.assertEqual(self.kind("null"), "skipped")
        self.assertEqual(self.kind("retry"), "skipped")
        self.assertEqual(self.kind("GET /api/cases"), "skipped")

    def test_the_nearest_identifier_is_named(self):
        verdict = check_citations.resolve("setValu(null)", self.vocabulary)
        self.assertEqual((verdict.kind, verdict.nearest), ("unresolved", ".setValue()"))

    def test_the_nearest_for_a_wrong_directory_is_the_real_path(self):
        verdict = check_citations.resolve("form/CaseRepository.java", self.vocabulary)
        self.assertEqual(verdict.nearest, REPOSITORY)

    def test_a_citation_nothing_resembles_has_no_nearest(self):
        verdict = check_citations.resolve("ofNullable(...).ifPresent(...)", self.vocabulary)
        self.assertEqual(verdict.nearest, "")


class TheSpansOfAnAnswer(unittest.TestCase):
    def test_fenced_blocks_are_not_read_and_spans_are_not_repeated(self):
        text = "`a1B()` and `a1B()` again\n```\n`inFence()`\n```\nthen `c2D()`\n"
        self.assertEqual(check_citations.citations(text), ["a1B()", "c2D()"])


class TheSupportedName(unittest.TestCase):
    def test_normalise_is_the_function_the_summaries_stage_uses(self):
        """One rule: a second copy is how the two checks come to disagree."""
        self.assertIs(check_citations.normalise, build_community_summaries._normalise)
        self.assertEqual(check_citations.normalise(".setValue()"), "setvalue")

    def test_the_stage_is_registered_and_parses_its_own_arguments(self):
        self.assertIn("check-citations", cli.STAGES)
        self.assertIn("check-citations", cli.SELF_PARSING)


class TheStage(Bed):
    def test_it_fails_and_names_what_is_missing_with_the_nearest(self):
        code, out, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual(code, 1)
        # setValue(null), FieldHolder.setValue(), CaseRepository.findOne(id),
        # repo/CaseRepository.java resolve; three do not.
        self.assertIn("7 citations checked, 4 resolve, 3 do not", out)
        self.assertIn("1 database-column citations reported separately", out)
        self.assertIn("2 spans skipped", out)
        self.assertIn("`ofNullable(...).ifPresent(...)`  no near match", err)
        self.assertIn("`setValu(null)`  nearest: .setValue()", err)
        self.assertIn("`epo/CaseRepository.java`", err)
        self.assertNotIn("fakeThing", err)
        self.assertNotIn("alsoFake", err)

    def test_columns_are_named_and_do_not_fail_the_run(self):
        (self.answers / "case-lookup.md").write_text("Stored in `case_record.created_date`.\n")
        code, out, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("`case_record.created_date`", out)
        self.assertIn("0 citations checked", out)

    def test_a_clean_answer_passes(self):
        (self.answers / "case-lookup.md").write_text("Use `setValue(null)`.\n")
        code, _, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual((code, err), (0, ""))

    def test_generated_evidence_beside_the_answers_is_not_read(self):
        (self.answers / "case-lookup.md").write_text("Use `setValue(null)`.\n")
        (self.answers / "topics-input.json").write_text('{"x": "`inventedThing()`"}')
        code, _, _ = self.run_stage("--graph", str(self.graph))
        self.assertEqual(code, 0)

    def test_the_committed_archive_is_read_when_the_plain_graph_is_absent(self):
        archive = self.tmp / "graphify-out" / "graph.json.gz"
        archive.parent.mkdir(parents=True)
        with gzip.open(archive, "wt") as handle:
            json.dump(GRAPH, handle)
        (self.answers / "case-lookup.md").write_text("Use `setValue(null)`.\n")
        code, out, _ = self.run_stage()
        self.assertEqual(code, 0)
        self.assertIn("graph.json.gz", out)

    def test_named_paths_replace_the_default_locations(self):
        other = self.tmp / "other.md"
        other.write_text("Use `inventedThing()`.\n")
        code, _, err = self.run_stage("--graph", str(self.graph), str(other))
        self.assertEqual(code, 1)
        self.assertIn("other.md", err)
        self.assertNotIn("case-lookup.md", err)


class ItRefusesRatherThanPassVacuously(Bed):
    def test_a_path_that_climbs_out_of_the_store_is_refused_and_nothing_is_read(self):
        """Breaks if a named path reaches the reader unchecked: any file on the machine
        would be parsed. The climbing path names a real answer, so a stage that did
        read it would report a finding instead of refusing."""
        outside = self.tmp.parent / f"{self.tmp.name}-outside.md"
        outside.write_text("Use `inventedThing()`.\n")
        self.addCleanup(outside.unlink)
        climbing = self.answers / ".." / ".." / ".." / outside.name
        code, out, err = self.run_stage("--graph", str(self.graph), str(climbing))
        self.assertEqual(code, 2)
        self.assertIn("traverses upward", err)
        self.assertIn(str(climbing), err)
        self.assertNotIn("inventedThing", out + err)
        self.assertEqual(out, "")

    def test_no_graph_is_a_refusal(self):
        code, _, err = self.run_stage()
        self.assertEqual(code, 2)
        self.assertIn("No graph", err)

    def test_no_answers_is_a_refusal(self):
        (self.answers / "case-lookup.md").unlink()
        code, _, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual(code, 2)
        self.assertIn("nothing was asserted", err)


if __name__ == "__main__":
    unittest.main()
