"""AST extraction per repository must not lose a run, and must not parse its own output (#111).

The documented route hands the whole corpus to the extractor in one call. Reported
on a several-hundred-repository estate: over eight minutes at full CPU producing no
output at all, with no way from outside to distinguish a hung run from a slow one.

Two failure classes are covered here, and they are different in kind.

**Losing the run.** In a single whole-corpus call, one repository the parser cannot
handle costs every other repository's result. The tests below require that a failing
repository is named, that the rest still extract, and that the exit code still
reports the failure — a partial layer that reports success is how a store commits a
hole and finds out weeks later.

**Parsing its own output.** Every store that drives extraction per repository has
hand-written a vendored-path exclusion regex. One store's list excluded dependency
bundles, build output and state files, and not the pipeline's own output directory,
so several hundred of the pipeline's own JSON artefacts were handed to the parser as
source. It was found by watching a run parse a graph file, not by any check, because
an exclusion list has no failing case: it is correct the day it is written and
silently wrong the next time the pipeline emits a new artefact.

`test_the_pipelines_own_output_is_refused` is that omission turned into a failing
case. `test_the_content_set_is_consumed_rather_than_re_derived` is the reason the
refusal is a backstop rather than the mechanism: the stage consumes the content set
the extractor already computed, so there is one model of what is content and not two.
"""

from __future__ import annotations

import io as _io
import json
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import settings_isolation  # noqa: F401  (path setup and settings isolation)

from knowledgestore import config, extract_ast  # noqa: E402


class _Extractor:
    """Stands in for the parser: records what it was given, raises where told to.

    `nodes_per_file` lets a second run yield fewer nodes for the same input, which
    is the whole point of the movement check - the input reconciles, the output
    shrank.  `hang_for` sleeps, so a real per-repository bound has to fire.
    """

    def __init__(self, raise_for=(), nodes_per_file=1, hang_for=(), swallows=False):
        self.calls: list[list[Path]] = []
        self.raise_for = tuple(raise_for)
        self.nodes_per_file = nodes_per_file
        self.hang_for = tuple(hang_for)
        # `swallows` reproduces the real extractor: it wraps each file in its own
        # `except Exception`, warns, and carries on. Measured, not assumed - a
        # TimeoutError raised by the bound was caught there and the call returned
        # successfully with the file skipped.
        self.swallows = swallows

    def _per_file(self, path):
        if any(name in str(path) for name in self.raise_for):
            raise RuntimeError("unparseable grammar")
        if any(name in str(path) for name in self.hang_for):
            time.sleep(5)

    def __call__(self, files, cache_root=None):
        self.calls.append(list(files))
        for path in files:
            if self.swallows:
                try:
                    self._per_file(path)
                except Exception:  # noqa: BLE001 - the behaviour under test
                    continue
            else:
                self._per_file(path)
        return {
            "nodes": [
                {"id": f"{path}#{n}", "label": path.name}
                for path in files
                for n in range(self.nodes_per_file)
            ],
            "edges": [{"source": str(files[0]), "target": str(path)} for path in files[1:]],
        }

    @property
    def files_seen(self) -> set[str]:
        return {str(path) for call in self.calls for path in call}


class _StoreCase:
    """Shared fixture. Deliberately not a TestCase: unittest collects every TestCase
    subclass, so a shared base that *was* one re-ran the whole parent suite inside
    each child - 41 tests for 18, and three duplicate failures pointing at one bug."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        config.configure(root=str(self.root))
        (self.root / "graphify-out").mkdir(parents=True, exist_ok=True)

    def _repo_file(self, repo: str, name: str) -> Path:
        path = self.root / "repositories" / repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
        return path

    def _detect(self, code_files, **extra) -> Path:
        path = self.root / "graphify-out" / ".graphify_detect.json"
        payload = {"files": {"code": [str(p) for p in code_files], **extra}}
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _run(self, extractor, argv=None):
        """Run main() with the parser stubbed, returning (exit code, stdout, stderr)."""
        module = types.ModuleType("graphify")
        submodule = types.ModuleType("graphify.extract")
        submodule.extract = extractor
        module.extract = submodule
        saved = {k: sys.modules.get(k) for k in ("graphify", "graphify.extract")}
        sys.modules["graphify"] = module
        sys.modules["graphify.extract"] = submodule
        out, err = _io.StringIO(), _io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = extract_ast.main(argv or [])
        finally:
            for name, previous in saved.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous
        return code, out.getvalue(), err.getvalue()

    def _layer(self) -> dict:
        return json.loads(
            (self.root / "graphify-out" / ".graphify_ast.json").read_text(encoding="utf-8")
        )


class ExtractAstTest(_StoreCase, unittest.TestCase):
    # ---- parsing the pipeline's own output -------------------------------------

    def test_the_pipelines_own_output_is_refused(self):
        """A store's exclusion list omitted this directory and its artefacts were parsed.

        Without this refusal the stage extracts the graph it just built, which
        succeeds, reports healthy counts, and feeds the pipeline's output back into
        its own input. Nothing downstream distinguishes those nodes from source.
        """
        artefact = self.root / "graphify-out" / "graph.json"
        artefact.write_text("{}", encoding="utf-8")
        detect = self._detect([self._repo_file("repo-a", "a.py"), artefact])
        extractor = _Extractor()
        code, _, err = self._run(extractor, ["--detect", str(detect)])
        self.assertEqual(code, 1)
        self.assertIn("own output", err)
        self.assertEqual(
            extractor.calls, [], "the parser ran despite the input holding pipeline output"
        )

    def test_the_refusal_covers_an_explicit_file_list_too(self):
        """The `--files` route bypasses the content set, so it bypasses its cleanliness.

        A caller supplying its own list is exactly the caller most likely to have
        hand-rolled the exclusions that produced this defect, so the refusal cannot
        live on the content-set branch alone.
        """
        artefact = self.root / "graphify-out" / ".graphify_semantic.json"
        artefact.write_text("{}", encoding="utf-8")
        listing = self.root / "files.txt"
        listing.write_text(
            f"{self._repo_file('repo-a', 'a.py')}\n{artefact}\n",
            encoding="utf-8",
        )
        extractor = _Extractor()
        code, _, err = self._run(extractor, ["--files", str(listing)])
        self.assertEqual(code, 1)
        self.assertIn("own output", err)
        self.assertEqual(extractor.calls, [])

    def test_a_per_clone_output_directory_is_refused_too(self):
        """`sync` preserves `graphify-out/` in every clone, so every clone can hold one.

        A refusal anchored only at the store root passes a per-clone artefact
        straight to the parser. That is not a near miss of the original defect, it
        is the original defect: the exclusion list that missed the pipeline's output
        missed it wherever it sat, and the corpus is where most of it sits.
        """
        artefact = self.root / "repositories" / "repo-a" / "graphify-out" / "graph.json"
        artefact.parent.mkdir(parents=True, exist_ok=True)
        artefact.write_text("{}", encoding="utf-8")
        detect = self._detect([self._repo_file("repo-a", "a.py"), artefact])
        extractor = _Extractor()
        code, _, err = self._run(extractor, ["--detect", str(detect)])
        self.assertEqual(code, 1)
        self.assertIn("own output", err)
        self.assertEqual(extractor.calls, [])

    def test_a_clean_content_set_is_not_refused(self):
        """The guard on the refusal: one that fired always would pass the tests above.

        Both refusal tests assert an exit code of 1, which a stage that refused
        every input would satisfy. This is the case that must still run.
        """
        detect = self._detect([self._repo_file("repo-a", "a.py")])
        extractor = _Extractor()
        code, out, err = self._run(extractor, ["--detect", str(detect)])
        self.assertEqual(code, 0, f"a clean content set was refused: {err}")
        self.assertEqual(len(extractor.calls), 1)
        self.assertEqual(len(self._layer()["nodes"]), 1)
        self.assertIn("repo-a", out)

    # ---- consuming the content set rather than deriving one --------------------

    def test_the_content_set_is_consumed_rather_than_re_derived(self):
        """Deriving the list here is what produced the drifting exclusion list.

        A file present in the corpus but absent from the content set must not be
        extracted. If this stage walked the tree it would pick that file up, and it
        would then need its own model of what to exclude — the second model whose
        omission is the defect this whole file is about.
        """
        named = self._repo_file("repo-a", "named.py")
        self._repo_file("repo-a", "vendored.min.js")  # on disk, absent from the content set
        detect = self._detect([named])
        extractor = _Extractor()
        code, _, err = self._run(extractor, ["--detect", str(detect)])
        self.assertEqual(code, 0, err)
        self.assertEqual(extractor.files_seen, {str(named)})

    def test_kinds_the_parser_cannot_read_are_not_handed_to_it(self):
        """Handing an image to a source parser costs a parse per file and yields nothing.

        `detect` classifies content the semantic layer reads and the AST parser
        cannot. Passing those through inflates the run with work that produces no
        nodes, which reads as a slow parser rather than as a wrong file list.
        """
        source = self._repo_file("repo-a", "a.py")
        picture = self._repo_file("repo-a", "diagram.png")
        detect = self._detect([source], image=[str(picture)], paper=[])
        extractor = _Extractor()
        code, _, err = self._run(extractor, ["--detect", str(detect)])
        self.assertEqual(code, 0, err)
        self.assertEqual(extractor.files_seen, {str(source)})

    # ---- not losing the run ---------------------------------------------------

    def test_one_repository_failing_does_not_lose_the_others(self):
        """In a whole-corpus call, one unparseable repository costs every other result.

        That is the reported failure this stage exists for: an estate-wide run that
        produced nothing, with no way to attribute it. The other repositories'
        nodes must survive and the failing one must be named.
        """
        good = self._repo_file("repo-a", "a.py")
        self._repo_file("repo-bad", "broken.py")
        other = self._repo_file("repo-c", "c.py")
        detect = self._detect([good, self.root / "repositories" / "repo-bad" / "broken.py", other])
        extractor = _Extractor(raise_for=("repo-bad",))
        code, out, err = self._run(extractor, ["--detect", str(detect)])
        self.assertIn("repo-bad", err, "the failing repository was not named")
        labels = {node["id"] for node in self._layer()["nodes"]}
        self.assertIn(f"{good}#0", labels)
        self.assertIn(f"{other}#0", labels)
        self.assertIn("repo-c", out, "extraction stopped at the failure instead of continuing")
        self.assertEqual(code, 1)

    def test_a_partial_layer_still_exits_non_zero(self):
        """A partial layer reporting success is how a store commits a hole.

        The layer is written deliberately even when repositories failed, so the
        exit code is the only thing carrying the failure. If it reported 0 a CI
        build would go green over a layer missing whole repositories.
        """
        good = self._repo_file("repo-a", "a.py")
        self._repo_file("repo-bad", "broken.py")
        detect = self._detect([good, self.root / "repositories" / "repo-bad" / "broken.py"])
        code, _, _ = self._run(_Extractor(raise_for=("repo-bad",)), ["--detect", str(detect)])
        self.assertEqual(code, 1)
        self.assertTrue(
            (self.root / "graphify-out" / ".graphify_ast.json").is_file(),
            "the partial layer was discarded, so the run was lost after all",
        )
        self.assertEqual(len(self._layer()["nodes"]), 1)

    # ---- refusing an empty run ------------------------------------------------

    def test_an_empty_content_set_is_refused(self):
        """An empty layer written as a success is only caught downstream, if at all.

        `merge-layers` refuses an empty layer, so the symptom surfaces two stages
        later as a merge failure rather than here as an extraction failure.
        """
        detect = self._detect([])
        extractor = _Extractor()
        code, _, err = self._run(extractor, ["--detect", str(detect)])
        self.assertEqual(code, 1)
        self.assertIn("empty layer", err)
        self.assertFalse((self.root / "graphify-out" / ".graphify_ast.json").exists())
        self.assertEqual(extractor.calls, [])

    def test_a_missing_content_set_names_what_to_run(self):
        """`unknown file` on the layer's own input is the least diagnosable failure.

        The content set is written by a tool installed separately from this library,
        so its absence is routine rather than exceptional and the message has to say
        what produces it.
        """
        code, _, err = self._run(_Extractor(), ["--detect", str(self.root / "absent.json")])
        self.assertEqual(code, 1)
        self.assertIn("--files", err)

    # ---- reconciling the count ------------------------------------------------

    def test_a_file_outside_the_corpus_is_grouped_rather_than_dropped(self):
        """Silently dropping input makes this stage's count disagree with its input.

        A content set naming a file outside `repositories/` is a fact about the
        store's layout, not a reason to extract fewer files than were handed over.
        Dropping it quietly would make the printed total reconcile against nothing.
        """
        inside = self._repo_file("repo-a", "a.py")
        outside = self.root / "loose.py"
        outside.write_text("y = 2\n", encoding="utf-8")
        groups = extract_ast.by_repository([inside, outside], config.REPOSITORIES_DIR)
        self.assertEqual(sum(len(v) for v in groups.values()), 2)
        self.assertIn("", groups)
        self.assertEqual(groups[""], [outside])


if __name__ == "__main__":
    unittest.main()


class MovementTest(_StoreCase, unittest.TestCase):
    """Nodes-per-repository against the previous run - the check the other tests cannot make.

    Reconciling a run against the content set it was handed catches input dropped
    *within* a run. It cannot catch a repository extracting materially less than it
    did last time: a parser version moves, a file is renamed, language detection
    changes, and the stage succeeds with its own counts reconciling perfectly while
    the store loses a repository's worth of structure.
    """

    def _counts(self):
        return json.loads(
            (self.root / "graphify-out" / ".graphify_ast_counts.json").read_text(encoding="utf-8")
        )

    def test_a_repository_that_shrank_is_named_and_does_not_fail_the_run(self):
        """A quiet decrease is the common case, and neither empty nor partial covers it.

        The existing refusals fire on an empty layer and on a repository that did
        not extract. A repository that extracted *less* is neither: every count
        reconciles against the input it was given. Naming it is the only way an
        operator sees it, and failing on it would be wrong - deleted code is a
        legitimate decrease, so the judgement is a person's.
        """
        detect = self._detect([self._repo_file("repo-a", "a.py")])
        first, _, _ = self._run(_Extractor(nodes_per_file=3), ["--detect", str(detect)])
        self.assertEqual(first, 0)
        self.assertEqual(self._counts(), {"repo-a": 3})

        code, out, err = self._run(_Extractor(nodes_per_file=1), ["--detect", str(detect)])
        self.assertEqual(code, 0, f"a decrease must not fail the run: {err}")
        self.assertIn("repo-a", out)
        self.assertIn("3 -> 1", out)
        self.assertIn("-2", out)

    def test_nothing_moving_says_so_rather_than_printing_a_movement_report(self):
        """The guard on the instrument: a report that always prints proves nothing.

        The test above asserts a name appears in the output. A movement report
        that listed every repository unconditionally would satisfy it while
        detecting nothing, so the unchanged case has to be silent.
        """
        detect = self._detect([self._repo_file("repo-a", "a.py")])
        self._run(_Extractor(nodes_per_file=2), ["--detect", str(detect)])
        code, out, _ = self._run(_Extractor(nodes_per_file=2), ["--detect", str(detect)])
        self.assertEqual(code, 0)
        self.assertIn("no per-repository movement", out)
        self.assertNotIn("->", out.split("no per-repository movement")[-1])

    def test_a_failed_repository_is_not_recorded_as_zero(self):
        """Recording a failure as 0 poisons the next run's baseline in both directions.

        A 0 would make the next successful run look like a large increase, and a
        second consecutive failure look unchanged. Neither is true, and both hide
        the failure behind a movement number that reconciles.
        """
        good = self._repo_file("repo-a", "a.py")
        self._repo_file("repo-bad", "broken.py")
        detect = self._detect([good, self.root / "repositories" / "repo-bad" / "broken.py"])
        code, _, _ = self._run(_Extractor(raise_for=("repo-bad",)), ["--detect", str(detect)])
        self.assertEqual(code, 1)
        self.assertEqual(self._counts(), {"repo-a": 1})
        self.assertNotIn("repo-bad", self._counts())

    def test_a_failure_does_not_erase_the_baseline_it_would_be_measured_against(self):
        """A failed run destroying the record of what the failure cost is self-concealing.

        Dropping a failed repository from the sidecar reads as "absent from the
        content set" next run and "new" the run after - two false movements from
        one failure - and throws away the count its recovery would be compared to.
        """
        good = self._repo_file("repo-a", "a.py")
        flaky = self._repo_file("repo-flaky", "b.py")
        detect = self._detect([good, flaky])
        self._run(_Extractor(nodes_per_file=4), ["--detect", str(detect)])
        self.assertEqual(self._counts(), {"repo-a": 4, "repo-flaky": 4})

        code, _, _ = self._run(_Extractor(raise_for=("repo-flaky",)), ["--detect", str(detect)])
        self.assertEqual(code, 1)
        self.assertEqual(
            self._counts()["repo-flaky"],
            4,
            "the failed repository's last known count was discarded",
        )

        # And the recovery is measured against it rather than reported as brand new.
        code, out, _ = self._run(_Extractor(nodes_per_file=1), ["--detect", str(detect)])
        self.assertEqual(code, 0)
        self.assertIn("repo-flaky", out)
        self.assertIn("4 -> 1", out)
        self.assertNotIn("new since the last run", out)

    def test_a_run_where_everything_fails_leaves_the_baseline_standing(self):
        """The degenerate case: an empty write makes the next run report no history.

        With every repository failing, an unfiltered write replaces the baseline
        with nothing, and the following run says there is no previous run at all -
        a failure erasing the evidence of itself.
        """
        first = self._repo_file("repo-a", "a.py")
        detect = self._detect([first])
        self._run(_Extractor(nodes_per_file=7), ["--detect", str(detect)])

        code, _, _ = self._run(_Extractor(raise_for=("repo-a",)), ["--detect", str(detect)])
        self.assertEqual(code, 1)
        self.assertEqual(self._counts(), {"repo-a": 7}, "the whole baseline was wiped")

    def test_gone_and_new_are_reported_as_different_things(self):
        """One "changed" number cannot tell three events apart, and responses differ.

        A repository that shrank, one that is gone from the content set, and one
        that is new need different actions. Collapsing them into a single delta is
        what makes a movement report ignorable.
        """
        first_file = self._repo_file("repo-a", "a.py")
        detect_one = self._detect([first_file])
        self._run(_Extractor(), ["--detect", str(detect_one)])

        second_file = self._repo_file("repo-b", "b.py")
        detect_two = self._detect([second_file])
        code, out, _ = self._run(_Extractor(), ["--detect", str(detect_two)])
        self.assertEqual(code, 0)
        self.assertIn("repo-a", out)
        self.assertIn("absent from the content set", out)
        self.assertIn("repo-b", out)
        self.assertIn("new since the last run", out)

    def test_the_first_run_says_there_is_no_baseline_rather_than_inventing_one(self):
        """An absent baseline read as "nothing changed" is a clean report of nothing.

        This is the same class as every other silent-empty failure: with no
        previous run, a movement check has nothing to say, and saying "no movement"
        would be indistinguishable from a real all-clear.
        """
        detect = self._detect([self._repo_file("repo-a", "a.py")])
        code, out, _ = self._run(_Extractor(), ["--detect", str(detect)])
        self.assertEqual(code, 0)
        self.assertIn("no previous run recorded", out)


class TimeLimitTest(_StoreCase, unittest.TestCase):
    """A pathological repository is only identifiable by name if the run ends."""

    def test_a_repository_that_hangs_is_timed_out_named_and_counted(self):
        """Per-repository attribution without a bound still loses the night.

        The motivation for this stage is that a whole-corpus call gave no way to
        tell a hung run from a slow one. Naming the repository that is hanging is
        no use to an operator who is asleep: the run has to end, the partial layer
        has to survive, and the exit code has to carry it.
        """
        good = self._repo_file("repo-a", "a.py")
        self._repo_file("repo-slow", "huge.min.js")
        detect = self._detect([good, self.root / "repositories" / "repo-slow" / "huge.min.js"])
        extractor = _Extractor(hang_for=("repo-slow",))
        code, out, err = self._run(extractor, ["--detect", str(detect), "--timeout", "1"])
        self.assertEqual(code, 1)
        self.assertIn("repo-slow", err)
        self.assertIn("TIMED OUT", err)
        self.assertIn("repo-a", out, "the run stopped at the timeout instead of continuing")
        self.assertEqual(len(self._layer()["nodes"]), 1)

    def test_an_extractor_that_swallows_exceptions_cannot_swallow_the_bound(self):
        """The real extractor catches Exception per file, warns, and returns success.

        Measured against it: a `TimeoutError` raised by the alarm was caught there,
        the file was skipped, and the call returned *successfully* with fewer
        nodes. The bound then did the opposite of its job - instead of naming a
        repository and failing, it silently converted a hang into a smaller layer,
        which the movement check would not surface until a whole run later.

        `RepositoryTimeout` derives from BaseException for exactly this reason, the
        same one `KeyboardInterrupt` does. This test fails if it is ever "tidied"
        back to Exception, which reads like a correctness fix.
        """
        good = self._repo_file("repo-a", "a.py")
        self._repo_file("repo-slow", "huge.min.js")
        detect = self._detect([good, self.root / "repositories" / "repo-slow" / "huge.min.js"])
        extractor = _Extractor(hang_for=("repo-slow",), swallows=True)
        code, out, err = self._run(extractor, ["--detect", str(detect), "--timeout", "1"])
        self.assertEqual(code, 1, "a swallowed bound reports the run as a success")
        self.assertIn("TIMED OUT", err)
        self.assertIn("repo-slow", err)
        self.assertIn("repo-a", out)

    def test_the_limit_is_off_when_asked_and_the_run_still_completes(self):
        """The guard on the bound: one that fired always would pass the test above.

        `--timeout 0` is the documented escape for a store with a genuinely slow
        repository, and it has to actually extract rather than time out at zero.
        """
        detect = self._detect([self._repo_file("repo-a", "a.py")])
        code, _, err = self._run(_Extractor(), ["--detect", str(detect), "--timeout", "0"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self._layer()["nodes"]), 1)

    def test_the_alarm_is_cleared_afterwards(self):
        """A left-armed alarm fires during an unrelated later stage, far from its cause.

        `signal.alarm` is process-wide. Leaving it set means a subsequent stage in
        the same process dies with a timeout naming a repository it never touched -
        the hardest possible failure to attribute.
        """
        import signal as _signal

        with extract_ast.repository_time_limit(30) as bounded:
            self.assertTrue(bounded)
        self.assertEqual(_signal.alarm(0), 0, "an alarm was left armed after the block")
