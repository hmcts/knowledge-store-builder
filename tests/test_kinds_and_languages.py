"""Node kinds across format changes, and step definitions per language."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from knowledgestore import config, extract_gherkin as gherkin, kinds


def node(kind: str) -> dict:
    return {"metadata": {"kind": kind}}


class KindsTest(unittest.TestCase):
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


JAVA = '''package com.steps;
public class PaymentSteps {
    @Given("a defendant owes {int} pounds")
    public void owes(int amount) {}
}
'''

PYTHON = '''from behave import given

@given("a defendant owes {amount:d} pounds")
def step_owes(context, amount):
    pass
'''

TYPESCRIPT = '''import { Given } from '@cucumber/cucumber';

Given('a defendant owes {int} pounds', async function (amount: number) {
  this.amount = amount;
});
'''


class StepDefinitionLanguageTest(unittest.TestCase):
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
        found = self._patterns({
            "src/test/java/com/steps/PaymentSteps.java": JAVA,
            "features/steps/other_steps.py": PYTHON.replace("owes", "paid"),
            "features/step_definitions/third.ts": TYPESCRIPT.replace("owes", "settled"),
        })
        self.assertEqual(len(found), 3)
        self.assertEqual(
            {name for name, _ in found.values()},
            {"PaymentSteps", "other_steps", "third"},
        )

    def test_files_without_step_definitions_are_ignored(self):
        found = self._patterns({"src/main/python/util.py": "def helper():\n    pass\n"})
        self.assertEqual(found, {})


class FeatureAreaTest(unittest.TestCase):
    def test_area_comes_from_the_segment_after_the_features_directory(self):
        self.assertEqual(gherkin.feature_area("e2e/features/payments/refund.feature"),
                         "payments")

    def test_features_directory_is_configurable(self):
        original = gherkin.FEATURES_DIR
        self.addCleanup(setattr, gherkin, "FEATURES_DIR", original)
        gherkin.FEATURES_DIR = "specs/"
        self.assertEqual(gherkin.feature_area("app/specs/listing/hearing.feature"),
                         "listing")

    def test_config_supplies_the_default(self):
        self.assertEqual(config.FEATURES_DIR, "features/")


if __name__ == "__main__":
    unittest.main()
