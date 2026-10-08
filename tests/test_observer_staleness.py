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

try:  # PyYAML is the `deploy` extra, not a runtime dependency of this library
    import yaml

    HAS_YAML = True
except ImportError:  # pragma: no cover - the default-install CI job takes this path
    HAS_YAML = False

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "tests.yml"

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


# The purpose-built tree the settle checks run over. Each test reads one source file
# and fails while that file is mutated, so which entries a test observes is fixed by
# construction and readable here rather than derived by the code under test.
SETTLED_SOURCES = {"first.py": "FIRST", "second.py": "SECOND", "third.py": "THIRD"}

READS = """
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "knowledgestore"


def holds(module, value):
    return value in (PACKAGE / module).read_text(encoding="utf-8")
"""

# Present at the base: observes `third`, which does not name it.
EXISTING_MODULE = (
    READS
    + """

class Old(unittest.TestCase):
    def test_reads_first(self):
        self.assertTrue(holds("first.py", "FIRST"))

    def test_reads_third(self):
        self.assertTrue(holds("third.py", "THIRD"))
"""
)

# The module a branch brought tests into. `Existing` was there before the branch,
# and observes `third` without being named by it - staleness the branch did not
# cause, so settling must not blame the branch for it.
ARRIVED_MODULE = (
    READS
    + """

class Existing(unittest.TestCase):
    def test_reads_third(self):
        self.assertTrue(holds("third.py", "THIRD"))


class Arrived(unittest.TestCase):
    def test_reads_first(self):
        self.assertTrue(holds("first.py", "FIRST"))

    def test_reads_second(self):
        self.assertTrue(holds("second.py", "SECOND"))

    def test_reads_nothing(self):
        self.assertTrue(True)

    def test_fails_already(self):
        self.fail("broken before any mutation is applied")
"""
)

SETTLED_TABLE = (
    gate.Mutation(
        "the first value is lost",
        "first.py",
        "FIRST",
        "MUTATED",
        "a purpose-built target",
        ("test_old.Old.test_reads_first",),
    ),
    gate.Mutation(
        "the second value is lost",
        "second.py",
        "SECOND",
        "MUTATED",
        "a purpose-built target",
        ("test_arrived.Arrived.test_reads_second",),
    ),
    gate.Mutation(
        "the third value is lost",
        "third.py",
        "THIRD",
        "MUTATED",
        "a purpose-built target",
        ("test_old.Old.test_reads_third",),
    ),
)


class SettlingReadsWhatTheArrivalsObserveTest(unittest.TestCase):
    """`--settle`, over a real tree with real mutations and real suite runs.

    The id comparison can only say an entry *might* have gained an observer, and
    for a test arriving in a module no entry names it cannot even say which. These
    checks hold the step that answers it: every entry applied through the gate's
    own `each_mutation`, the arrived modules run in a child process, and the verdict
    read off which arrived tests failed. The table and the arrivals are forged; the
    mutation, the run and the restore are the gate's own.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolved, because the gate compares a child's import path against its own
        # `SRC`, and macOS hands out temporary directories behind a symlink.
        self.root = Path(temporary.name).resolve()
        package = self.root / "src" / "knowledgestore"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        for module, value in SETTLED_SOURCES.items():
            (package / module).write_text(f'VALUE = "{value}"\n', encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_old.py").write_text(EXISTING_MODULE, encoding="utf-8")
        (self.root / "tests" / "test_arrived.py").write_text(ARRIVED_MODULE, encoding="utf-8")
        self.originals = {path: path.read_bytes() for path in package.iterdir()}

        for name in ("ROOT", "SRC", "RECOVERY_PATH"):
            self.addCleanup(setattr, gate, name, getattr(gate, name))
        gate.ROOT = self.root
        gate.SRC = package
        gate.RECOVERY_PATH = self.root / "sidecar"
        previous = os.environ.get("PYTHONPATH")
        self.addCleanup(
            lambda: (
                os.environ.__setitem__("PYTHONPATH", previous)
                if previous is not None
                else os.environ.pop("PYTHONPATH", None)
            )
        )
        os.environ["PYTHONPATH"] = str(self.root / "src")

    def _settle(self, *arrived: str, budget: float = 600.0) -> staleness.Report:
        report = staleness.settle(SETTLED_TABLE, {"test_arrived": arrived}, "the base", budget)
        # Every check below also holds the tree to what it was: a settle run that
        # left a mutation behind would be a worse defect than any verdict it got wrong.
        self.assertEqual({path: path.read_bytes() for path in self.originals}, self.originals)
        self.assertFalse(gate.RECOVERY_PATH.exists(), "a recovery record was left behind")
        return report

    def test_an_arrived_observer_the_entry_does_not_name_is_stale(self):
        """Catches settling losing the arrival it exists for: a test arriving that
        fails under an entry's mutation and is not in its set is that set going
        stale, and must fail the job by name rather than be reported as a suspicion.

        The same run carries its own control. `test_reads_second` arrives too and
        observes `second`, which names it; `Existing.test_reads_third` sits in the
        same module, observes `third` and is not named by it. Exactly one entry may
        come back stale, so a comparison that blamed every failing test, or none,
        cannot pass here.
        """
        report = self._settle("Arrived.test_reads_first", "Arrived.test_reads_second")
        written = "\n".join(staleness.lines(report))

        self.assertEqual(report.verdict, staleness.STALE)
        self.assertEqual(
            [(stale.entry, stale.unnamed) for stale in report.stale],
            [("the first value is lost", ("test_arrived.Arrived.test_reads_first",))],
        )
        self.assertIn("test_arrived.Arrived.test_reads_first", written)
        self.assertIn(
            "python3 tests/mutation_gate.py --derive-mapping --only 'the first value is lost'",
            written,
        )
        self.assertNotIn("the second value is lost", written)
        self.assertNotIn("the third value is lost", written)
        self.assertEqual(staleness.exit_code(report, refuse=False), 1)

    def test_an_arrived_observer_the_entry_already_names_is_clean(self):
        """Catches settling flagging its own remedy. A branch that adds a test and
        derives the set in the same change names it, and the entry must come back
        clear - otherwise the job stays red after the fix it asked for. The module
        also holds `Existing.test_reads_third`, which observes `third` unnamed and
        did not arrive: blaming the branch for it - by running or reading the whole
        module rather than the arrived ids - is the over-reach this rules out."""
        report = self._settle("Arrived.test_reads_second")

        self.assertEqual(report.verdict, staleness.CLEAN)
        self.assertEqual(report.stale, ())
        self.assertEqual(staleness.exit_code(report, refuse=False), 0)

    def test_an_arrived_test_that_observes_nothing_is_clean(self):
        """Catches settling treating arrival as observation - a test that fails under
        no mutation is no entry's observer, and the id comparison's suspicion over it
        must resolve to CLEAN rather than survive as a red job."""
        report = self._settle("Arrived.test_reads_nothing")

        self.assertEqual(report.verdict, staleness.CLEAN)
        self.assertEqual(report.stale, ())

    def test_an_arrived_test_failing_before_any_mutation_cannot_be_judged(self):
        """Catches a baseline failure being read as an observation. A test that fails
        with nothing applied fails under every entry too, so without the baseline it
        would be named as an unnamed observer of the whole table. It has to be
        reported as unjudgeable instead - a suspicion, never a verdict about entries."""
        report = self._settle("Arrived.test_fails_already")
        written = "\n".join(staleness.lines(report))

        self.assertEqual(report.verdict, staleness.SUSPECT)
        self.assertEqual(report.stale, ())
        self.assertEqual(report.unjudged, ("test_arrived.Arrived.test_fails_already",))
        self.assertIn("test_arrived.Arrived.test_fails_already", written)
        self.assertIn("cannot be judged", written)
        self.assertEqual(staleness.exit_code(report, refuse=False), 0)

    def test_a_module_the_budget_cannot_cover_is_unsettled_and_never_clean(self):
        """Catches the budget never refusing, which is the unbounded job: a pull
        request adding one slow test costs the number of entries times that test,
        and one branch's run went past half an hour before it was stopped.

        A budget of nothing cannot cover any module, so the arrival that would
        settle STALE above - `test_reads_first`, observing an entry that does not
        name it - must not be run under any entry. It is reported UNSETTLED with its
        measured and projected cost, and the verdict is SUSPECT: not asked is not
        clean, and not observed is not stale.
        """
        report = self._settle("Arrived.test_reads_first", budget=0.0)
        written = "\n".join(staleness.lines(report))

        self.assertEqual(report.verdict, staleness.SUSPECT)
        self.assertEqual(report.stale, ())
        self.assertEqual([each.module for each in report.unsettled], ["test_arrived"])
        unsettled = report.unsettled[0]
        self.assertEqual(unsettled.tests, ("test_arrived.Arrived.test_reads_first",))
        self.assertGreater(unsettled.baseline, 0.0, "the module was never timed")
        self.assertAlmostEqual(unsettled.projected, unsettled.baseline * len(SETTLED_TABLE))
        self.assertIn("UNSETTLED test_arrived", written)
        self.assertIn("--settle --budget", written)
        self.assertEqual(staleness.exit_code(report, refuse=False), 0)
        self.assertEqual(staleness.exit_code(report, refuse=True), 1)

    def test_nothing_arrived_settles_without_running_anything(self):
        """Catches settling running the table over an empty selection. `unittest`
        reports a run of no tests as a pass, so nothing here could fail - and an
        empty arrival set is CLEAN by the id comparison already."""
        report = staleness.settle(SETTLED_TABLE, {}, "the base")

        self.assertEqual(report.verdict, staleness.CLEAN)
        self.assertFalse(gate.RECOVERY_PATH.exists())

    def test_an_arrival_set_that_could_not_be_read_still_cannot_tell(self):
        """Catches settling turning the third state into a verdict: no arrivals read
        is not no arrivals, and must stay CANNOT_TELL."""
        report = staleness.settle(SETTLED_TABLE, None, "git could not diff the range")

        self.assertEqual(report.verdict, staleness.CANNOT_TELL)


class TheBudgetChoosesCheapestFirstTest(unittest.TestCase):
    """`affordable`, over hand-derived timings, because a real one is not repeatable."""

    def test_the_cheapest_modules_are_settled_until_the_next_does_not_fit(self):
        """Catches the choice being made in name order, or stopping at the first
        refusal of a module the rest could have fitted around. Ten entries and a
        budget of 16: `c` projects 6, `b` 10 (16 in all, exactly the budget) and `a`
        15. Cheapest first settles `c` and `b`; name order would settle `a` alone,
        and a module that fits exactly must be settled rather than refused."""
        chosen, refused = staleness.affordable({"a": 1.5, "b": 1.0, "c": 0.6}, 10, 16.0)

        self.assertEqual(chosen, ("b", "c"))
        self.assertEqual(refused, ("a",))

    def test_a_budget_that_covers_everything_refuses_nothing(self):
        """The control for the check above: a budget that refused when everything
        fitted would leave every pull request SUSPECT and nothing settled."""
        chosen, refused = staleness.affordable({"a": 1.5, "b": 1.0}, 10, 1000.0)

        self.assertEqual((chosen, refused), (("a", "b"), ()))


@unittest.skipUnless(HAS_YAML, "needs the `deploy` extra (PyYAML)")
class TheWorkflowSettlesOnAPullRequestTest(unittest.TestCase):
    """The job that runs `--settle`, held to the environment the mapping is derived in."""

    def setUp(self) -> None:
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.job = self.workflow["jobs"]["observer-staleness"]

    def test_the_job_settles_in_the_environment_the_tests_job_builds(self):
        """Catches settling running where the arrived tests cannot observe anything.
        Without the package and the pinned extras installed, a test that needs one
        skips - and a skipped test fails under no mutation, so every entry it
        observes comes back CLEAN. A prefix of the `tests` job's steps rather than a
        hand-picked subset, the discipline `test_mutation_gate_shards` holds the
        `mapping` job to, and it has to reach the extras or the equality is vacuous."""
        steps = self.job["steps"]
        environment = steps[: len(steps) - 1]

        self.assertEqual(environment, self.workflow["jobs"]["tests"]["steps"][: len(environment)])
        self.assertTrue(
            any("graphifyy" in str(step.get("run", "")) for step in environment),
            "the job settles without the extras the mapping is derived with",
        )
        self.assertIn("--settle", str(steps[-1]["run"]))
        self.assertIn("observer_staleness.py", str(steps[-1]["run"]))

    def test_the_job_runs_on_pull_requests_and_nothing_skips_the_workflow(self):
        """Catches the job leaving the event where staleness is created, and the
        workflow gaining a `paths-ignore` - the `tests` check is required, and a
        workflow skipped by one never reports it, which deadlocks the pull request."""
        self.assertEqual(self.job["if"], "github.event_name == 'pull_request'")
        # `self.workflow[True]`, because YAML reads a bare `on:` key as the boolean.
        self.assertNotIn("paths-ignore", str(self.workflow[True]))


if __name__ == "__main__":
    unittest.main()
