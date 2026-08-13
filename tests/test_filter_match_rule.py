"""A `match <glob>` rule, and the structural guard that stops it being half-added.

`prefix` cannot express a convention that puts the distinguishing part at the
end — `{product}-shared-infrastructure` and the like. An estate hit that and had
to write one `repo` line per repository; the omission was invisible beforehand,
because every rule in the file was working exactly as written (issue #109).

One glob rule rather than a `suffix` rule: `match cpp-*` is what `prefix cpp-`
already does, and `match *-shared-infrastructure` is what was missing, so a glob
generalises the existing rule instead of adding a third way to say the same
thing.

The second class of test here matters more than the first. A rule is wired in two
places — `_apply_rule`, which records it, and the unmatched-rule reporter, which
says when it selected nothing. `fetch` was once added to the first and not the
second, so an unmatched `fetch` rule reported nothing: the silence the reporter
exists to prevent. `test_every_selecting_kind_is_covered_by_the_reporter` fails
if any future rule repeats that, which is the point of it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import generate_repository_list as repo_list  # noqa: E402


def _filters(text: str) -> repo_list.Filters:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "repository-filters.txt"
        path.write_text(text, encoding="utf-8")
        return repo_list.read_filters(path)


class MatchRuleTest(SettingsIsolated):
    def test_a_trailing_convention_can_be_expressed(self):
        """The case `prefix` cannot reach, and the reason the rule exists."""
        filters = _filters("match *-shared-infrastructure\n")
        self.assertTrue(filters.matches("crime-shared-infrastructure"))
        self.assertTrue(filters.matches("civil-shared-infrastructure"))
        self.assertFalse(filters.matches("shared-infrastructure-module-terraform"))
        self.assertFalse(filters.matches("unrelated"))

    def test_it_also_covers_what_prefix_does(self):
        """So it generalises the existing rule rather than sitting beside it."""
        by_glob = _filters("match cpp-ui-*\n")
        by_prefix = _filters("prefix cpp-ui-\n")
        for name in ("cpp-ui-hearing", "cpp-ui-e2e", "cpp-context-hearing", "other"):
            with self.subTest(name=name):
                self.assertEqual(by_glob.matches(name), by_prefix.matches(name))

    def test_exclude_still_wins(self):
        filters = _filters("match *-shared-infrastructure\nexclude legacy-shared-infrastructure\n")
        self.assertTrue(filters.matches("crime-shared-infrastructure"))
        self.assertFalse(filters.matches("legacy-shared-infrastructure"))

    def test_a_match_rule_alone_is_enough_to_define_an_estate(self):
        """It is a selecting rule, so it must satisfy the "no include rules" check."""
        filters = _filters("match *-shared-infrastructure\n")
        self.assertTrue(filters.matches("crime-shared-infrastructure"))

    def test_a_glob_that_selects_everything_is_refused(self):
        """`match *` would pull an entire organisation from one character, and the
        estate would look deliberate. Refuse rather than let it through."""
        for glob in ("*", "**", "?*"):
            with self.subTest(glob=glob):
                with self.assertRaises(ValueError):
                    _filters(f"match {glob}\n")

    def test_an_unknown_kind_is_still_refused(self):
        with self.assertRaises(ValueError):
            _filters("glob *-shared-infrastructure\n")


class ReporterCoverageTest(SettingsIsolated):
    """The guard: a rule recorded but not reported is a silent estate gap."""

    def test_an_unmatched_match_rule_is_reported(self):
        filters = _filters("match *-shared-infrastructure\n")
        problems = repo_list.unmatched_rules(filters, [{"name": "something-else"}], runner=None)
        self.assertEqual(
            [rule for _, rule in problems],
            ["match *-shared-infrastructure"],
            "a glob that selected nothing said nothing",
        )

    def test_a_matched_match_rule_is_not_reported(self):
        filters = _filters("match *-shared-infrastructure\n")
        problems = repo_list.unmatched_rules(
            filters, [{"name": "crime-shared-infrastructure"}], runner=None
        )
        self.assertEqual(problems, [])

    def test_every_selecting_kind_is_covered_by_the_reporter(self):
        """Enumerates the selecting kinds and asserts each is reported when it
        matches nothing. Adding a rule to `_apply_rule` without wiring the
        reporter fails here rather than in an estate months later."""
        cases = {
            "prefix": "prefix zz-nothing-",
            "repo": "repo zz-nothing",
            "fetch": "fetch zz-nothing",
            "match": "match zz-nothing-*",
        }
        for kind, rule in cases.items():
            with self.subTest(kind=kind):
                filters = _filters(f"repo anchor\n{rule}\n")
                problems = repo_list.unmatched_rules(filters, [{"name": "anchor"}], runner=None)
                self.assertIn(
                    rule,
                    [r for _, r in problems],
                    f"an unmatched `{kind}` rule was not reported, so it would be silent",
                )


if __name__ == "__main__":
    unittest.main()
