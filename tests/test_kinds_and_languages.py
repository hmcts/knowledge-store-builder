"""Node kinds across format changes, the kinds the documents name, and step
definitions per language."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config, extract_gherkin as gherkin, kinds


def node(kind: str) -> dict:
    return {"metadata": {"kind": kind}}


class KindsTest(SettingsIsolated):
    def test_current_kinds_are_format_agnostic(self):
        self.assertEqual(kinds.node_kind(node("feature")), kinds.FEATURE)
        self.assertEqual(kinds.node_kind(node("scenario")), kinds.SCENARIO)
        self.assertEqual(kinds.node_kind(node("ticket")), kinds.TICKET)

    def test_legacy_format_specific_kinds_still_read(self):
        # stores built before the rename must keep working until re-run
        self.assertEqual(kinds.node_kind(node("gherkin_feature")), kinds.FEATURE)
        self.assertEqual(kinds.node_kind(node("gherkin_scenario")), kinds.SCENARIO)
        self.assertEqual(kinds.node_kind(node("jira_ticket")), kinds.TICKET)

    def test_unknown_and_missing_kinds_pass_through(self):
        self.assertEqual(kinds.node_kind(node("something_else")), "something_else")
        self.assertEqual(kinds.node_kind({}), "")

    def test_is_kind_accepts_either_form(self):
        self.assertTrue(kinds.is_kind(node("feature"), kinds.FEATURE))
        self.assertTrue(kinds.is_kind(node("gherkin_feature"), kinds.FEATURE))
        self.assertFalse(kinds.is_kind(node("scenario"), kinds.FEATURE))


ROOT = Path(__file__).resolve().parent.parent

# The spelling that says "this is the kind" - the value a reader will filter on.
# A mention of a kind's name in prose is deliberately not matched, and that
# distinction is the whole of how this gate stays usable: `kinds.py` keeps the
# older names readable on purpose, so a document is right to name one as an
# alias and wrong to give one as the kind. The first is prose about a value, the
# second is the value.
#
# Anchored on `metadata.` for the same reason. A Kubernetes manifest's
# `kind: HelmRelease` is a different field with the same short name, and one of
# the guides quotes it.
KIND_GIVEN = re.compile(r"metadata(?:\.kind|\[.kind.\])\s*(?::|==)\s*[\"\'`]?([a-z][a-z0-9_]*)")

# The written kinds are read out of the source rather than listed here, so a
# stage that starts writing a new one puts it under this gate with no edit.
KIND_WRITTEN = re.compile(
    r"[\"\']kind[\"\']\s*:\s*(?:kinds\.([A-Z_]+)|[\"\']([a-z][a-z0-9_]*)[\"\'])"
)


def documents() -> list[Path]:
    """Every file that tells a reader what to run or read, plans excluded.

    Historical plans record what was planned at a date rather than what to do
    today, which is the exclusion `test_documented_stages.py` makes and for the
    same reason.
    """
    found = sorted(ROOT.joinpath("skills").rglob("SKILL.md"))
    found += [
        path
        for path in sorted(ROOT.joinpath("docs").rglob("*.md"))
        if "superpowers" not in path.parts
    ]
    found += [ROOT / "README.md", ROOT / "CHEATSHEET.md"]
    return [path for path in found if path.exists()]


def written_kinds() -> set[str]:
    """Every `metadata.kind` value some stage in this library writes today."""
    written: set[str] = set()
    for path in sorted(ROOT.joinpath("src/knowledgestore").glob("*.py")):
        for constant, literal in KIND_WRITTEN.findall(path.read_text(encoding="utf-8")):
            written.add(getattr(kinds, constant) if constant else literal)
    return written


def kinds_given(text: str) -> list[str]:
    """The kinds a document gives as the value to match on."""
    return KIND_GIVEN.findall(text)


def kind_problems(text: str, written: set[str]) -> list[str]:
    """The complaints against one document's given kinds, worst case first.

    Separate from the assertion so the sensitivity checks can drive it with
    forged prose. A clean result from the one real file cannot say whether this
    can still tell a stale document from a current one.
    """
    complaints = []
    for given in kinds_given(text):
        current = kinds.node_kind({"metadata": {"kind": given}})
        if current != given:
            complaints.append(
                f"gives `{given}`, which is a read-alias no stage writes: a filter on it "
                f"returns a clean zero. The written kind is `{current}`."
            )
        elif given not in written:
            complaints.append(
                f"gives `{given}`, which no stage in this library writes. "
                f"Written today: {sorted(written)}."
            )
    return complaints


class DocumentedKindsTest(SettingsIsolated):
    """A kind a document gives as the value to filter on must be one the library
    writes.

    The break: the query skill gave `metadata.kind: gherkin_feature`, which
    `kinds.py` accepts on read and no stage has written since kinds became
    format-agnostic. An agent following it filtered on a value present in no
    current store and got zero features back - indistinguishable from an estate
    with no Gherkin in it, and reportable as one. Nothing failed, because both
    sides were correct: the alias is deliberate and the document was quoting a
    real value, just not a written one.
    """

    def setUp(self):
        super().setUp()
        self.written = written_kinds()

    def test_the_written_kinds_are_found_in_the_source(self):
        # The floor. A parse that matches nothing reads exactly like a tree with
        # no defects, and this is the gate's own vacuum: every judgement below
        # is made against this set.
        self.assertLessEqual({kinds.FEATURE, kinds.SCENARIO, kinds.TICKET}, self.written)

    def test_a_kind_is_given_somewhere_in_the_documents(self):
        # The other half of the floor: if no document gives a kind at all, the
        # pass below is about nothing.
        given = [k for path in documents() for k in kinds_given(path.read_text(encoding="utf-8"))]
        self.assertTrue(given, "no document gives a `metadata.kind` value; is the pattern stale?")

    def test_every_kind_the_documents_give_is_one_a_stage_writes(self):
        for path in documents():
            problems = kind_problems(path.read_text(encoding="utf-8"), self.written)
            self.assertEqual(problems, [], f"{path.relative_to(ROOT)} {'; '.join(problems)}")

    def test_a_read_alias_given_as_the_kind_is_reported(self):
        problems = kind_problems("nodes (`metadata.kind: gherkin_feature`), wired", self.written)
        self.assertEqual(len(problems), 1)
        self.assertIn("gherkin_feature", problems[0])
        self.assertIn("feature", problems[0])

    def test_the_written_kind_given_as_the_kind_passes(self):
        self.assertEqual(kind_problems("nodes (`metadata.kind: feature`), wired", self.written), [])

    def test_an_alias_named_in_prose_as_an_alias_is_not_read(self):
        # The distinction this gate rests on. Telling a reader that an older
        # store carries `gherkin_feature` is the correct advice, and a check
        # that fired on it would be removed rather than fixed.
        self.assertEqual(
            kind_problems(
                "A store built before kinds became format-agnostic carries the older "
                "alias `gherkin_feature` on the same nodes, so match either.",
                self.written,
            ),
            [],
        )

    def test_a_manifest_field_of_the_same_name_is_not_read_as_a_node_kind(self):
        self.assertEqual(kind_problems("the patches carry `kind: HelmRelease`.", self.written), [])

    def test_a_kind_no_stage_writes_is_reported(self):
        problems = kind_problems("`metadata.kind: invented_kind`", self.written)
        self.assertEqual(len(problems), 1)
        self.assertIn("no stage in this library writes", problems[0])


JAVA = """package com.steps;
public class PaymentSteps {
    @Given("a defendant owes {int} pounds")
    public void owes(int amount) {}
}
"""

PYTHON = """from behave import given

@given("a defendant owes {amount:d} pounds")
def step_owes(context, amount):
    pass
"""

TYPESCRIPT = """import { Given } from '@cucumber/cucumber';

Given('a defendant owes {int} pounds', async function (amount: number) {
  this.amount = amount;
});
"""


class StepDefinitionLanguageTest(SettingsIsolated):
    def _patterns(self, files: dict[str, str]) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for rel, content in files.items():
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            return gherkin.parse_step_definitions(repo)

    def test_java_annotations_are_found_with_their_class(self):
        found = self._patterns({"src/test/java/com/steps/PaymentSteps.java": JAVA})
        self.assertEqual(len(found), 1)
        name, rel = next(iter(found.values()))
        self.assertEqual(name, "PaymentSteps")
        self.assertEqual(rel, "src/test/java/com/steps/PaymentSteps.java")

    def test_python_decorators_are_found_and_named_by_file(self):
        found = self._patterns({"features/steps/payment_steps.py": PYTHON})
        self.assertEqual(len(found), 1)
        name, rel = next(iter(found.values()))
        self.assertEqual(name, "payment_steps")
        self.assertEqual(rel, "features/steps/payment_steps.py")

    def test_typescript_calls_are_found_and_named_by_file(self):
        found = self._patterns({"features/step_definitions/payment.ts": TYPESCRIPT})
        self.assertEqual(len(found), 1)
        name, rel = next(iter(found.values()))
        self.assertEqual(name, "payment")

    def test_the_three_languages_normalise_to_the_same_step(self):
        # the same business step, written three ways, must match one pattern -
        # this is what lets a feature link to its implementation in any language
        java = self._patterns({"src/test/java/S.java": JAVA})
        python = self._patterns({"features/steps/s.py": PYTHON})
        typescript = self._patterns({"features/step_definitions/s.ts": TYPESCRIPT})
        self.assertEqual(set(java), set(python))
        self.assertEqual(set(java), set(typescript))

    def test_a_mixed_estate_yields_every_language(self):
        found = self._patterns(
            {
                "src/test/java/com/steps/PaymentSteps.java": JAVA,
                "features/steps/other_steps.py": PYTHON.replace("owes", "paid"),
                "features/step_definitions/third.ts": TYPESCRIPT.replace("owes", "settled"),
            }
        )
        self.assertEqual(len(found), 3)
        self.assertEqual(
            {name for name, _ in found.values()},
            {"PaymentSteps", "other_steps", "third"},
        )

    def test_files_without_step_definitions_are_ignored(self):
        found = self._patterns({"src/main/python/util.py": "def helper():\n    pass\n"})
        self.assertEqual(found, {})


class FeatureAreaTest(SettingsIsolated):
    def test_area_comes_from_the_segment_after_the_features_directory(self):
        self.assertEqual(gherkin.feature_area("e2e/features/payments/refund.feature"), "payments")

    def test_features_directory_is_configurable(self):
        original = config.FEATURES_DIR
        self.addCleanup(setattr, gherkin, "FEATURES_DIR", original)
        config.configure(FEATURES_DIR="specs/")
        self.assertEqual(gherkin.feature_area("app/specs/listing/hearing.feature"), "listing")

    def test_config_supplies_the_default(self):
        self.assertEqual(config.FEATURES_DIR, "features/")


if __name__ == "__main__":
    unittest.main()
