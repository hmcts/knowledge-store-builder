"""The cheap half of #293: which observer sets a merge may have staled.

An entry's observers are the tests that fail when it is applied, so a test that
arrives later and observes the same defect makes a correct entry
under-describing. Nothing in the pull-request path notices: the fast gate runs
only the modules the entry names, `git merge-tree` sees no conflict because
nothing in the entry changed, and `--verify-mapping` - which does notice - costs
a whole-suite run per entry and runs nightly. So the window between "the mapping
became wrong" and "anything says so" is a day, and the merge that opens it
happens on the way into a pull request.

`observer_staleness` closes that window by comparing test *ids* between the last
verified state and now. The checks here are about the two ways such a thing goes
wrong, and only one of them is the obvious one:

- **It misses the arrival**, and then it has replaced a slow check with nothing.
- **It flags everything**, and then it is indistinguishable from working while
  being useless - a report naming 219 entries after every merge is a report
  nobody reads, and the day it is right nobody notices either. So the
  over-correction guards here are load-bearing rather than tidy: a merge that
  edits a test module's comments, and a merge that changes only `src/`, must both
  come back CLEAN.

And the third state, which is neither: a check that **cannot tell** must say so.
An unparsable module or a base this clone does not hold has to report suspicion,
because CLEAN over a question that could not be asked is the failure one level up
from the one this module exists to catch.

The reasoning is driven for real throughout. The merge is a real merge made by
real git in a real repository, the table entries are real `Mutation` objects, and
the parse runs over real Python source - the boundary is stubbed, never the
judgement.
"""

from __future__ import annotations

import ast
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

import mapping_trigger as trigger
import mutation_gate as gate
import observer_staleness as staleness

ALPHA = '''\
"""A module with two classes that share a method name."""

import unittest


class AlphaTest(unittest.TestCase):
    def test_one(self):
        pass


class OtherTest(unittest.TestCase):
    def test_one(self):
        pass
'''

ALPHA_WITH_A_NEW_METHOD = (
    ALPHA
    + """

class AlphaTest(unittest.TestCase):  # noqa: F811
    def test_two(self):
        pass
"""
)

BETA = """\
import unittest


class BetaTest(unittest.TestCase):
    def test_b(self):
        pass
"""


def entry(name: str, observers: tuple[str, ...]) -> gate.Mutation:
    """A real table entry, so the checks read the field the gate reads."""
    return gate.Mutation(name, "io.py", "find", "replace", "the escape it stood for", observers)


class TheParseReadsTestIdsTest(unittest.TestCase):
    """`test_ids`, which is the whole signal: everything else compares its output."""

    def test_two_classes_sharing_a_method_name_are_two_ids(self):
        """Catches the ids losing their class. `AlphaTest.test_one` and
        `OtherTest.test_one` are different tests and observe different things, so a
        bare method name would report a whole new class as nothing having arrived
        the moment it reused a name - and reusing `test_one` across classes is the
        norm in this suite, not an edge case."""
        self.assertEqual(
            staleness.test_ids(ALPHA), frozenset({"AlphaTest.test_one", "OtherTest.test_one"})
        )

    def test_a_method_that_is_not_a_test_is_not_an_id(self):
        """Catches the parse counting helpers. `setUp`, `_write_graph` and
        `write_graph` are not collected by unittest, so they cannot observe a
        defect, and counting them would make every refactor of a helper look like
        an arrival."""
        source = "class T:\n    def setUp(self): pass\n    def helper(self): pass\n    def test_real(self): pass\n"

        self.assertEqual(staleness.test_ids(source), frozenset({"T.test_real"}))

    def test_source_that_will_not_parse_raises_rather_than_reading_as_empty(self):
        """Catches the SyntaxError being swallowed. An empty set from an unparsable
        module means "nothing arrived here", which is a clean verdict over a module
        nothing read - the exact shape #293 is about, one level up. The caller turns
        this into CANNOT_TELL."""
        with self.assertRaises(SyntaxError):
            staleness.test_ids("class T:\n    def test_x(self)\n")


class WhatCountsAsAnArrivalTest(unittest.TestCase):
    """`arrivals`: the direction, and the new-module case."""

    def test_a_test_present_now_and_absent_at_the_base_arrived(self):
        """The mechanism itself. Without this the module reports nothing ever."""
        self.assertEqual(
            staleness.arrivals(frozenset({"T.test_a"}), frozenset({"T.test_a", "T.test_b"})),
            ("T.test_b",),
        )

    def test_a_departed_test_is_not_an_arrival(self):
        """Catches the comparison being inverted or made symmetric. A departed test
        is the *loud* failure - the gate reports `named and did not fail`, and
        `check_mapping` refuses outright when the whole module went - so reporting
        it here would spend the report's attention on the half that already has a
        gate, and dilute the half that has none."""
        self.assertEqual(
            staleness.arrivals(frozenset({"T.test_a", "T.test_gone"}), frozenset({"T.test_a"})),
            (),
        )

    def test_a_module_the_base_did_not_hold_arrives_whole(self):
        """Catches a new module being read as an empty diff. `test_read_path_policy`
        arriving is the first instance in #293's table: before that merge no entry
        named the module, so every test in it is a test no entry could have named."""
        self.assertEqual(
            staleness.arrivals(None, frozenset({"T.test_a", "T.test_b"})),
            ("T.test_a", "T.test_b"),
        )


class WhichEntriesAMergeMakesSuspectTest(unittest.TestCase):
    """`judge`, over real entries and an arrival map."""

    def test_an_entry_naming_a_module_that_gained_a_test_is_flagged_and_named(self):
        """The break this module exists to catch: an entry nobody edited whose set
        no longer describes what protects it. Reporting the verdict without the
        entry name would leave the reader where #293 was - dispatching a
        seven-minute job to find out which row moved."""
        table = (entry("gzip again", ("test_alpha.AlphaTest.test_one",)),)

        report = staleness.judge(table, {"test_alpha": ("AlphaTest.test_two",)}, "the base")

        self.assertEqual(report.verdict, staleness.SUSPECT)
        self.assertEqual([suspect.entry for suspect in report.suspects], ["gzip again"])
        self.assertEqual(report.suspects[0].arrived, ("AlphaTest.test_two",))
        self.assertIn("gzip again", "\n".join(staleness.lines(report)))

    def test_a_merge_that_brought_no_new_test_is_not_flagged(self):
        """The over-correction guard, and it is as important as the detection. A
        check that flags every merge is indistinguishable from one that works, and
        it is worse than nothing because it trains the reader to skip the report.
        An arrival map with nothing in it is what a merge of `src/` alone, or of a
        comment in a test module, produces."""
        table = (entry("gzip again", ("test_alpha.AlphaTest.test_one",)),)

        report = staleness.judge(table, {}, "the base")

        self.assertEqual(report.verdict, staleness.CLEAN)
        self.assertEqual(report.suspects, ())

    def test_an_arrival_in_a_module_no_entry_names_is_reported_rather_than_dropped(self):
        """Catches the rule being narrowed to modules entries already name, which
        reads as clean over #293's sharpest instance: a wholly new module cannot be
        named by any entry until someone names it, so 'no entry names this module'
        is the state that needs reporting, not the state that clears it."""
        table = (entry("gzip again", ("test_alpha.AlphaTest.test_one",)),)

        report = staleness.judge(table, {"test_beta": ("BetaTest.test_b",)}, "the base")

        self.assertEqual(report.verdict, staleness.SUSPECT)
        self.assertEqual(report.suspects, ())
        self.assertEqual(report.unattributed, ("test_beta.BetaTest.test_b",))

    def test_an_arrival_the_entry_already_names_clears_that_entry(self):
        """Catches the check flagging its own remedy for ever. A branch that adds a
        test and derives the set in the same change has done exactly the right
        thing; if that still reported the entry as suspect, the report could never
        be driven to CLEAN and would stop being read."""
        table = (
            entry("mapped", ("test_alpha.AlphaTest.test_one", "test_alpha.AlphaTest.test_two")),
            entry("unmapped", ("test_alpha.OtherTest.test_one",)),
        )

        report = staleness.judge(table, {"test_alpha": ("AlphaTest.test_two",)}, "the base")

        self.assertEqual([suspect.entry for suspect in report.suspects], ["unmapped"])

    def test_an_arrival_in_a_class_the_entry_names_is_ranked_above_one_elsewhere(self):
        """Catches the two tiers collapsing into one. Every one of the five
        attributable sets the first sharded run found stale gained a test in a class
        the entry already named, so that tier is where the answer has been; flattening
        it puts five findings in a list of twenty-five with nothing to read first."""
        table = (
            entry("far", ("test_alpha.OtherTest.test_one",)),
            entry("near", ("test_alpha.AlphaTest.test_one",)),
        )

        report = staleness.judge(table, {"test_alpha": ("AlphaTest.test_two",)}, "the base")

        self.assertEqual([suspect.entry for suspect in report.suspects], ["near", "far"])
        self.assertEqual([suspect.same_class for suspect in report.suspects], [True, False])

    def test_the_clean_verdict_does_not_claim_the_mapping_is_correct(self):
        """Catches the claim widening past what was read. This check sees arrivals
        only; a set also goes stale when `src/` puts an existing test onto a mutated
        line, which is one of the six the first sharded run found and is invisible
        here. A CLEAN that read as "the mapping is verified" would retire the
        nightly run in a reader's head."""
        report = staleness.judge((entry("e", ("test_alpha.AlphaTest.test_one",)),), {}, "the base")

        self.assertIn("src/", report.reason)
        self.assertIn("nightly", report.reason)


class FailTowardSuspicionTest(unittest.TestCase):
    """The branches nobody takes on a good day, and the direction they take."""

    def test_a_base_that_could_not_be_compared_reports_that_rather_than_clean(self):
        """Catches the fail-safe pointing the wrong way. This is the same asymmetry
        `mapping_trigger.cannot_tell` is built on: a CANNOT_TELL rendered as CLEAN
        tells a reader a table nothing could examine has been examined, and there is
        no output that distinguishes it from a real pass."""
        report = staleness.judge((entry("e", ("test_alpha.AlphaTest.test_one",)),), None, "no base")

        self.assertEqual(report.verdict, staleness.CANNOT_TELL)
        self.assertIn("no base", report.reason)
        self.assertIn("nothing is cleared", report.reason)

    def test_a_verdict_that_could_not_be_reached_still_names_a_command(self):
        """Catches CANNOT_TELL being a dead end. Its whole content is "go run the
        expensive check", so a report that cannot say which command that is has told
        the reader nothing they can act on."""
        report = staleness.cannot_tell("git could not diff")

        self.assertTrue(
            any("--verify-mapping" in command for command in staleness.remedies(report))
        )

    def test_no_base_at_all_is_not_read_as_a_verified_one(self):
        """Catches an absent `last_verified` becoming a comparison against nothing.
        `last_verified` answers None for every failure - no run with legs, a `gh api`
        that broke - and reading None as "compare against HEAD" would clear the whole
        table on an empty diff."""
        base, since = staleness.base_for(None, runner=lambda *a, **k: _Failed())

        self.assertIsNone(base)
        self.assertIn("no run", since)


class _Failed:
    """A `gh` that did not answer, which is every failure `last_verified` folds to None."""

    returncode = 1
    stdout = ""
    stderr = "gh: not logged in"


class AMergeInARealRepositoryTest(unittest.TestCase):
    """The whole thing, over a real merge made by real git.

    A hand-written arrival map proves nothing about the flags `git diff` is called
    with or about `git show` reaching a file at a commit, and a merge is the
    specific event #293 is about - so the merge here is a real one.
    """

    def _repository(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self._git(root, "init", "-b", "main")
        self._git(root, "config", "user.email", "gate@example.invalid")
        self._git(root, "config", "user.name", "gate")
        (root / "tests").mkdir()
        (root / "src").mkdir()
        self._write(root, "tests/test_alpha.py", ALPHA)
        self._write(root, "src/io.py", "def read():\n    return 1\n")
        self._git(root, "add", "tests/test_alpha.py", "src/io.py")
        self._git(root, "commit", "-m", "the verified state")

        self.addCleanup(setattr, trigger, "ROOT", trigger.ROOT)
        self.addCleanup(setattr, staleness, "ROOT", staleness.ROOT)
        trigger.ROOT = root
        staleness.ROOT = root
        return root

    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
        )
        return completed.stdout.strip()

    @staticmethod
    def _write(root: Path, path: str, text: str) -> None:
        (root / path).write_text(text, encoding="utf-8")

    def _merge_main_into_a_branch(self, root: Path, changes: dict[str, str]) -> str:
        """Branch off, advance main with `changes`, merge it in, and return the base."""
        base = self._git(root, "rev-parse", "HEAD")
        self._git(root, "checkout", "-b", "feature")
        # A file main never touches, so the merge below is the clean fast-forward-
        # style merge a developer actually performs. A conflict would abort the
        # merge and leave the checks measuring the pre-merge tree.
        self._write(root, "src/branch_only.py", "def branch():\n    return 1\n")
        self._git(root, "add", "src/branch_only.py")
        self._git(root, "commit", "-m", "the branch's own work")

        self._git(root, "checkout", "main")
        for path, text in changes.items():
            self._write(root, path, text)
        if changes:
            self._git(root, "add", *changes)
            self._git(root, "commit", "-m", "what main gained")

        self._git(root, "checkout", "feature")
        self._git(root, "merge", "main", "--no-edit")
        return base

    def test_a_merge_that_brings_a_test_observing_an_existing_entry_is_flagged(self):
        """The break: a merge makes a correct entry under-describe what protects it,
        and every existing check reads clean. The fast gate runs only what the entry
        names, `git merge-tree` finds no conflict, and the nightly is a day away."""
        root = self._repository()
        base = self._merge_main_into_a_branch(
            root, {"tests/test_alpha.py": ALPHA_WITH_A_NEW_METHOD, "tests/test_beta.py": BETA}
        )
        table = (entry("gzip again", ("test_alpha.AlphaTest.test_one",)),)

        arrived, why = staleness.what_arrived(base, "HEAD")
        report = staleness.judge(table, arrived, why or base)

        self.assertEqual(report.verdict, staleness.SUSPECT)
        self.assertEqual([suspect.entry for suspect in report.suspects], ["gzip again"])
        self.assertEqual(report.suspects[0].arrived, ("AlphaTest.test_two",))
        self.assertTrue(report.suspects[0].same_class)
        self.assertEqual(report.unattributed, ("test_beta.BetaTest.test_b",))

    def test_a_merge_that_brings_no_new_test_is_not_flagged(self):
        """The over-correction guard, on a real merge. Main advanced by a comment in
        a test module and a change under `src/` - the ordinary case, and the one a
        file-level signal flags. A check that reports this merge is a check whose
        every report is noise."""
        root = self._repository()
        base = self._merge_main_into_a_branch(
            root,
            {
                "tests/test_alpha.py": ALPHA.replace(
                    "two classes", "two classes (a comment nobody tests)"
                ),
                "src/io.py": "def read():\n    return 3\n",
            },
        )
        table = (entry("gzip again", ("test_alpha.AlphaTest.test_one",)),)

        arrived, why = staleness.what_arrived(base, "HEAD")
        report = staleness.judge(table, arrived, why or base)

        self.assertEqual(arrived, {})
        self.assertEqual(report.verdict, staleness.CLEAN)

    def test_a_base_this_clone_does_not_hold_reports_that_it_cannot_tell(self):
        """The shallow-checkout and force-push case. git exits non-zero, which is not
        an empty diff - reading it as one is the silent clean, and CI is exactly where
        a shallow clone happens."""
        self._repository()

        arrived, why = staleness.what_arrived("0" * 40, "HEAD")

        self.assertIsNone(arrived)
        self.assertIn("may not hold the base", why)

    def test_a_test_module_that_will_not_parse_reports_that_it_cannot_tell(self):
        """Catches a broken module reading as an empty arrival set. The module is
        real, on disk, committed and unparsable - a mid-merge conflict marker is the
        way this happens - and the verdict over it must not be CLEAN."""
        root = self._repository()
        base = self._merge_main_into_a_branch(
            root, {"tests/test_alpha.py": "class T:\n    def test_x(self)\n"}
        )
        table = (entry("gzip again", ("test_alpha.AlphaTest.test_one",)),)

        arrived, why = staleness.what_arrived(base, "HEAD")
        report = staleness.judge(table, arrived, why or base)

        self.assertEqual(report.verdict, staleness.CANNOT_TELL)
        self.assertIn("could not be parsed", report.reason)

    def test_a_renamed_test_module_arrives_at_its_new_name_and_is_not_read_at_its_old_one(self):
        """Catches a rename being read as a wholesale departure. `changes_from`
        splits a rename into a delete and an add, so the new path must be compared
        against a base that does not hold it - every test in it arrives - while the
        old path, absent at head, must contribute nothing rather than raising."""
        root = self._repository()
        base = self._git(root, "rev-parse", "HEAD")
        self._git(root, "mv", "tests/test_alpha.py", "tests/test_renamed.py")
        self._git(root, "commit", "-m", "renamed")

        arrived, why = staleness.what_arrived(base, "HEAD")

        self.assertEqual(why, "")
        self.assertEqual(arrived, {"test_renamed": ("AlphaTest.test_one", "OtherTest.test_one")})

    def test_the_report_reaches_the_step_summary_and_the_job_output(self):
        """`main` end to end over the files Actions actually reads. A verdict that
        stays in stdout is a verdict nobody sees on a pull request, which is the one
        place this check exists to be read."""
        root = self._repository()
        base = self._merge_main_into_a_branch(
            root, {"tests/test_alpha.py": ALPHA_WITH_A_NEW_METHOD}
        )
        summary, output = root / "summary.md", root / "output.txt"
        for variable, path in (("GITHUB_STEP_SUMMARY", summary), ("GITHUB_OUTPUT", output)):
            self.addCleanup(os.environ.pop, variable, None)
            os.environ[variable] = str(path)

        self.assertEqual(staleness.main(["--base", base]), 0)

        self.assertIn("Observer staleness:", summary.read_text(encoding="utf-8"))
        self.assertIn("verdict=", output.read_text(encoding="utf-8"))

    def test_refuse_exits_non_zero_only_for_a_verdict_that_is_not_clean(self):
        """Catches `--refuse` being wired to the wrong condition, in both directions.
        It is the switch a maintainer flips to take #293's option 3, so it must block
        on suspicion and must not block on a clean merge - a flag that always exits 1
        makes every pull request red and gets reverted rather than read."""
        root = self._repository()
        base = self._merge_main_into_a_branch(
            root, {"tests/test_alpha.py": ALPHA_WITH_A_NEW_METHOD}
        )

        self.assertEqual(staleness.main(["--base", base, "--refuse"]), 1)
        self.assertEqual(staleness.main(["--base", "HEAD", "--head", "HEAD", "--refuse"]), 0)


class TheReportIsActionableTest(unittest.TestCase):
    """What the reader is left holding, which is the difference this makes."""

    def test_the_command_it_prints_parses_as_a_shell_command(self):
        """Catches the remedy being unrunnable, which it was: seven entries in this
        table carry an apostrophe in their name, so a hand-quoted `--only '...'`
        produces a command the shell cannot parse. A remedy nobody can paste sends
        the reader back to dispatching the whole job by hand, which is the cost #293
        is about."""
        table = (
            entry(
                "the page's edge list falls back to set order", ("test_alpha.AlphaTest.test_one",)
            ),
        )

        report = staleness.judge(table, {"test_alpha": ("AlphaTest.test_two",)}, "the base")
        command = staleness.remedies(report)[0]

        self.assertIn("--only", command)
        self.assertIn("the page's edge list falls back to set order", shlex.split(command))

    def test_the_entries_naming_one_module_are_reported_as_one_arrival(self):
        """Catches the report repeating one arrival per entry. Fifteen entries name
        `test_build_explorer`, so an ungrouped report prints the same six test names
        fifteen times and buries the lines that are not that."""
        table = tuple(entry(f"e{index}", ("test_alpha.AlphaTest.test_one",)) for index in range(15))

        report = staleness.judge(table, {"test_alpha": ("AlphaTest.test_two",)}, "the base")
        written = staleness.lines(report)

        self.assertEqual(len(report.suspects), 15)
        self.assertEqual(
            [line for line in written if line.startswith("  test_alpha")],
            ["  test_alpha gained 1 test(s): AlphaTest.test_two"],
        )
        self.assertTrue(any("15 entry(s)" in line for line in written))

    def test_the_json_report_carries_what_the_text_carries(self):
        """Catches the machine-readable form drifting into a summary of the text. A
        caller reading `--json` to decide whether to dispatch the legs needs the
        entry names and the tier, not a rendered sentence."""
        table = (entry("gzip again", ("test_alpha.AlphaTest.test_one",)),)

        report = staleness.judge(table, {"test_alpha": ("AlphaTest.test_two",)}, "the base")
        decoded = json.loads(json.dumps(staleness.as_dict(report)))

        self.assertEqual(decoded["verdict"], staleness.SUSPECT)
        self.assertEqual(decoded["suspects"][0]["entry"], "gzip again")
        self.assertTrue(decoded["suspects"][0]["same_class"])
        self.assertTrue(decoded["remedies"])

    def test_the_same_arrivals_report_identically_whatever_order_they_arrive_in(self):
        """Catches a set or dict leaking into the output. The report is diffed
        between runs and pasted into an issue, and this repository has shipped a
        non-deterministic artefact before because a tiebreak was left to hash
        order."""
        table = (
            entry("b", ("test_alpha.AlphaTest.test_one",)),
            entry("a", ("test_alpha.AlphaTest.test_one",)),
        )
        arrived = {"test_alpha": ("AlphaTest.test_two", "AlphaTest.test_three")}

        first = staleness.lines(staleness.judge(table, arrived, "the base"))
        second = staleness.lines(staleness.judge(tuple(reversed(table)), arrived, "the base"))

        self.assertEqual(first, second)


class TheParseHasNoHiddenBlindSpotTest(unittest.TestCase):
    """The one thing `test_ids` cannot see, asserted so it cannot open quietly.

    A test method a class inherits from a base defined in *another* module is
    invisible to a single-module parse: the class would arrive holding tests this
    check never counted. No base class in this suite provides one - `SettingsIsolated`
    provides `setUp` isolation and nothing else - so the blind spot is currently
    empty. That is a property of the suite rather than of the parser, which means
    nothing in `observer_staleness` can notice it changing. This can.
    """

    def test_no_test_class_in_this_suite_inherits_a_test_from_another_module(self):
        """Catches the blind spot opening. The day a mixin in `settings_isolation` or
        a shared harness starts carrying `test_*` methods, every class inheriting it
        gains tests `test_ids` does not count - and the failure mode is a CLEAN
        verdict over a real arrival, which is silent. If this fails, the parse needs
        to resolve bases across modules; it is not a licence to widen the base."""
        suite = Path(__file__).resolve().parent
        borrowed: list[str] = []
        for module in sorted(suite.glob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            defined = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                for base in node.bases:
                    name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                    if name in defined or name == "TestCase":
                        continue
                    if self._provides_a_test(suite, name):
                        borrowed.append(f"{module.stem}.{node.name} inherits tests from {name}")

        self.assertEqual(borrowed, [])

    @staticmethod
    def _provides_a_test(suite: Path, name: str) -> bool:
        """Whether a class defined anywhere in this suite carries a `test_*` method."""
        for module in suite.glob("*.py"):
            for node in ast.parse(module.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.ClassDef) and node.name == name:
                    if staleness.test_ids(module.read_text(encoding="utf-8")):
                        return any(
                            member.name.startswith(staleness.TEST_PREFIX)
                            for member in node.body
                            if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
                        )
        return False


if __name__ == "__main__":
    unittest.main()
