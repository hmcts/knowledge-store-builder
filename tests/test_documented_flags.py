"""The flags-and-defaults gate, run over the real documents and over forged ones.

Two directions for each rule, because either alone is worthless: a gate that cannot
fire is decoration, and one that fires on pip's `--upgrade` gets switched off. The
forged cases are therefore built in pairs - a document the gate must refuse and a
near-identical one it must accept - so that none of them can be passed by a gate that
is only answering "does this mention a flag".

The third direction is vacuity. The real-tree floors are well under today's counts;
they exist to catch an extractor that has stopped reading, not to pin a number that
moves when a document is rewrapped.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from documented_flags import (
    ROOT,
    THIRD_PARTY,
    UNRESOLVED,
    _forged_read,
    declared_arguments,
    default_matches,
    logical_lines,
    main,
    read_tree,
    sensitivity,
    stage_arguments,
    third_party_flags,
    third_party_problems,
    units,
)
from knowledgestore import cli

MENTIONS_FLOOR = 60
ATTRIBUTED_FLOOR = 10
THIRD_PARTY_FLOOR = 10
DEFAULTS_FLOOR = 3
DOCUMENTS_FLOOR = 8


class TheRealDocumentsMatchTheCode(unittest.TestCase):
    """Breaks when a document names a flag or default the stage does not declare."""

    def test_no_document_claims_a_flag_or_default_the_code_lacks(self):
        found = read_tree(ROOT, cli.STAGES)
        self.assertEqual(found.problems, [], "\n" + "\n".join(found.problems))

    def test_the_scan_is_reading_what_it_claims_to(self):
        """The vacuity guard, one floor per thing the gate reports having read."""
        found = read_tree(ROOT, cli.STAGES)
        self.assertGreater(found.documents, DOCUMENTS_FLOOR)
        self.assertGreater(found.mentions, MENTIONS_FLOOR)
        self.assertGreater(found.attributed, ATTRIBUTED_FLOOR, "few flags tied to a stage")
        self.assertGreater(found.third_party, THIRD_PARTY_FLOOR, "no third-party flags seen")
        self.assertGreaterEqual(found.defaults_compared, DEFAULTS_FLOOR, "few defaults compared")

    def test_every_mention_is_accounted_for_in_exactly_one_class(self):
        """A mention that lands in no class would be silently dropped."""
        found = read_tree(ROOT, cli.STAGES)
        self.assertEqual(found.problems, [], "a defect would be a sixth class; fix it first")
        classified = (
            found.attributed
            + found.global_flags
            + found.third_party
            + found.by_name
            + len(found.unattributed)
        )
        self.assertEqual(classified, found.mentions)

    def test_every_stage_module_is_read(self):
        declared = stage_arguments(ROOT, cli.STAGES)
        self.assertEqual(set(declared), set(cli.STAGES))
        self.assertIn("--timeout", declared["extract-ast"])
        self.assertIn("--limit", declared["gaps"])


class TheSourceIsReadAsTheCodeDeclaresIt(unittest.TestCase):
    def test_a_default_is_followed_through_a_constant(self):
        found = declared_arguments("N = 7\nM = N\np.add_argument('--n', default=M)\n")
        self.assertEqual(found, {"--n": [7]})

    def test_store_true_declares_false_and_a_bare_flag_declares_none(self):
        found = declared_arguments(
            "p.add_argument('--a', action='store_true')\np.add_argument('--b')\n"
        )
        self.assertEqual(found, {"--a": [False], "--b": [None]})

    def test_a_default_it_cannot_evaluate_is_not_guessed(self):
        found = declared_arguments("p.add_argument('--k', default=','.join(X))\n")
        self.assertIs(found["--k"][0], UNRESOLVED)

    def test_a_flag_reused_across_parsers_lists_every_default(self):
        found = declared_arguments(
            "p.add_argument('--s', default=1)\nq.add_argument('--s', default=2)\n"
        )
        self.assertEqual(found, {"--s": [1, 2]})

    def test_percentages_match_either_scale_and_nothing_else(self):
        self.assertTrue(default_matches("20%", 0.2))
        self.assertTrue(default_matches("600", 600))
        self.assertTrue(default_matches("exact", "exact"))
        self.assertFalse(default_matches("30", 600))
        self.assertFalse(default_matches("50%", 0.2))
        self.assertFalse(default_matches("overlap", "exact"))


class TheGateCanStillTell(unittest.TestCase):
    """Forged documents against forged source, each pair differing in one fact."""

    def test_it_reports_a_flag_no_stage_declares(self):
        found = _forged_read("```bash\nknowledgestore sync --prune\n```\n")
        self.assertEqual(len(found.problems), 1, found.problems)
        self.assertIn("`--prune`", found.problems[0])
        self.assertIn("`sync`", found.problems[0])

    def test_it_accepts_the_same_line_with_a_real_flag(self):
        found = _forged_read("```bash\nknowledgestore sync --timeout 5\n```\n")
        self.assertEqual((found.problems, found.attributed), ([], 1))

    def test_it_reports_a_wrong_default_and_names_both_values(self):
        found = _forged_read("Run `knowledgestore sync`; `--timeout` (default 30 seconds).")
        self.assertEqual(len(found.problems), 1, found.problems)
        self.assertIn("30", found.problems[0])
        self.assertIn("600", found.problems[0])

    def test_it_accepts_the_right_default_and_counts_the_comparison(self):
        found = _forged_read("Run `knowledgestore sync`; `--timeout` (default 600 seconds).")
        self.assertEqual((found.problems, found.defaults_compared), ([], 1))

    def test_it_reads_the_default_before_the_flag_form(self):
        wrong = _forged_read("Under the default `--timeout 30` it stops.")
        right = _forged_read("Under the default `--timeout 600` it stops.")
        self.assertEqual(len(wrong.problems), 1)
        self.assertEqual((right.problems, right.defaults_compared), ([], 1))

    def test_it_leaves_pips_flags_alone(self):
        found = _forged_read("```bash\npip install --upgrade --extra-index-url URL pkg\n```\n")
        self.assertEqual((found.problems, found.third_party, found.attributed), ([], 2, 0))

    def test_a_continuation_line_belongs_to_the_command_that_began_it(self):
        text = "```bash\npip install \\\n  --extra-index-url URL \\\n  pkg\n```\n"
        found = _forged_read(text)
        self.assertEqual((found.problems, found.third_party), ([], 1))
        self.assertEqual(list(logical_lines("a \\\n  --b\nc")), ["a    --b", "c"])

    def test_a_third_party_command_does_not_excuse_a_stage_invocation(self):
        """The other direction: the command word decides, not the flag's spelling."""
        found = _forged_read("```bash\ngit log --upgrade && knowledgestore sync --upgrade\n```\n")
        self.assertEqual(len(found.problems), 1, found.problems)
        self.assertEqual(found.third_party, 1)

    def test_a_declared_third_party_flag_in_prose_is_accepted_and_an_undeclared_one_is_not(self):
        sentence = "Do not add `--no-cluster` here."
        self.assertEqual(_forged_read(sentence, {"--no-cluster": "graphify"}).problems, [])
        self.assertEqual(len(_forged_read(sentence).problems), 1)

    def test_a_passage_naming_one_stage_holds_its_flags_to_that_stage(self):
        found = _forged_read("`knowledgestore sync` takes `--prune` to remove clones.")
        self.assertEqual(len(found.problems), 1, found.problems)

    def test_a_flag_one_stage_declares_and_nothing_names_is_counted_not_dropped(self):
        found = _forged_read("Add `--timeout` to bound it.")
        self.assertEqual((found.problems, found.by_name, found.unattributed), ([], 1, []))

    def test_a_flag_several_stages_declare_and_nothing_names_is_listed(self):
        found = _forged_read("Add `--strict` to fail the build.")
        self.assertEqual(found.problems, [])
        self.assertEqual(len(found.unattributed), 1)
        self.assertIn("`check`", found.unattributed[0])

    def test_a_default_with_no_literal_to_compare_is_listed_not_passed(self):
        declared = {"s": declared_arguments("p.add_argument('--x', type=int)\n")}
        from documented_flags import read_document

        found = read_document(
            "`knowledgestore s`: `--x` (default 5).", "f.md", declared, {"s": ("m", "h")}, {}
        )
        self.assertEqual(found.problems, [])
        self.assertEqual(found.defaults_compared, 0)
        self.assertEqual(len(found.uncomparable), 1)

    def test_the_built_in_sensitivity_run_passes(self):
        self.assertEqual(sensitivity(), [])

    def test_a_fence_is_one_unit_and_a_table_row_is_another(self):
        found = units("intro\n\n| a | `--x` |\n| b | `--y` |\n\n```\nl1\nl2\n```\n")
        self.assertEqual([u.fenced for u in found], [False, False, False, True])
        self.assertEqual(found[3].text, "l1\nl2")


class TheThirdPartyDeclarationIsHeldHonest(unittest.TestCase):
    def forged_root(self, entries: str, document: str, tmp: str) -> Path:
        root = Path(tmp)
        (root / "docs").mkdir()
        (root / THIRD_PARTY).write_text(entries, encoding="utf-8")
        (root / "README.md").write_text(document, encoding="utf-8")
        return root

    def test_the_shipped_declaration_is_read(self):
        self.assertGreater(len(third_party_flags(ROOT)), 3)

    def test_an_entry_a_stage_declares_is_reported(self):
        declared = {"sync": declared_arguments("p.add_argument('--timeout', default=1)\n")}
        with TemporaryDirectory() as tmp:
            root = self.forged_root("--timeout :: pip\n", "mentions --timeout\n", tmp)
            problems = third_party_problems(root, declared, {"README.md": "--timeout"})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("also declared by a stage", problems[0])

    def test_an_entry_no_document_names_is_reported_as_stale(self):
        with TemporaryDirectory() as tmp:
            root = self.forged_root("--gone :: pip\n", "nothing\n", tmp)
            problems = third_party_problems(root, {}, {"README.md": "nothing"})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("named by no shipped document", problems[0])

    def test_a_clean_declaration_reports_nothing(self):
        with TemporaryDirectory() as tmp:
            root = self.forged_root("--kept :: pip\n", "uses --kept\n", tmp)
            self.assertEqual(third_party_problems(root, {}, {"README.md": "uses --kept"}), [])


class TheRunnerReportsWhatItRead(unittest.TestCase):
    def stub_root(self, tmp: str, document: str) -> Path:
        root = Path(tmp)
        module_dir = root / "src" / "knowledgestore"
        module_dir.mkdir(parents=True)
        for module, _help in cli.STAGES.values():
            (module_dir / f"{module}.py").write_text("", encoding="utf-8")
        (root / "README.md").write_text(document, encoding="utf-8")
        return root

    def run_main(self, root: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(root)
        return code, out.getvalue(), err.getvalue()

    def test_a_tree_with_no_flags_fails_rather_than_passes(self):
        with TemporaryDirectory() as tmp:
            code, _out, err = self.run_main(self.stub_root(tmp, "No flags here.\n"))
        self.assertEqual(code, 1)
        self.assertIn("read no flag mentions at all", err)

    def test_a_defect_fails_the_run_and_is_named(self):
        with TemporaryDirectory() as tmp:
            root = self.stub_root(tmp, "```bash\nknowledgestore sync --prune\n```\n")
            code, out, err = self.run_main(root)
        self.assertEqual(code, 1)
        self.assertIn("--prune", err)
        self.assertIn("checked 1 flag mention(s)", out)

    def test_the_real_tree_reports_its_counts_and_exits_zero(self):
        code, out, err = self.run_main(ROOT)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("documented-flags: checked ", out)


if __name__ == "__main__":
    unittest.main()
