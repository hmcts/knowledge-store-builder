"""The content set: what a corpus search should read instead of the raw tree.

The graph is clean; this is about the **fallback**. Someone whose question the
graph could not answer reaches for `grep`, and on two measured estates the large
majority of what they then read is not corpus - much of it the pipeline's own
output, written into the tree the pipeline reads. Nothing exposed the set the
pipeline had already computed, so every consumer re-derived it badly or not at
all (#213).

Every test below names the production change that should make it fail. The two
that matter most are the ones guarding against this becoming a second,
hand-maintained model of the tool's own output:

- a detect category this library has never heard of must still be content, or a
  later graphify release moves real content into the noise silently;
- the noise attribution must be a partition, or its buckets sum to more than the
  population they describe and read as a larger problem than the estate has.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import build_content_set, cli, config, content_set, status


def write_tree(root: Path, files: dict[str, str]) -> None:
    """Create every named file, so a test's population is stated not implied."""
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def detect_for(root: Path, kinds: dict[str, list[str]]) -> None:
    """Write a detect result naming absolute paths, as graphify's own does."""
    config.DETECT_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.DETECT_PATH.write_text(
        json.dumps({"files": {k: [str(root / p) for p in v] for k, v in kinds.items()}}),
        encoding="utf-8",
    )


def run(argv: list[str] | None = None) -> tuple[int, str]:
    out = _io.StringIO()
    with contextlib.redirect_stdout(out):
        code = build_content_set.main(argv or [])
    return code, out.getvalue()


class ContentPathsTest(unittest.TestCase):
    def test_a_detect_category_this_library_never_heard_of_is_still_content(self):
        """Break it catches: replacing detect's own keys with a closed list of kinds.

        `build_chunk_plan` deliberately keeps a closed `KNOWN_KINDS`, so copying
        that here is the obvious change to make - and it is the drift this module
        exists to avoid. A graphify release adding a category would move those
        files out of the content set and into the noise, silently and by omission,
        which is exactly how every hand-maintained exclusion list has failed.
        """
        found = content_set.content_paths(
            {"files": {"spreadsheet": ["repositories/a/costs.csv"], "document": []}}
        )
        self.assertEqual(found, ["repositories/a/costs.csv"])

    def test_a_path_named_under_two_categories_appears_once(self):
        """Break it catches: concatenating the category lists without deduplicating.

        A duplicated path makes `grep` read the same file twice - two hits for one
        occurrence - and makes the committed count overstate the corpus.
        """
        found = content_set.content_paths(
            {"files": {"code": ["repositories/a/x.py"], "document": ["repositories/a/x.py"]}}
        )
        self.assertEqual(found, ["repositories/a/x.py"])

    def test_the_set_is_sorted_however_detect_ordered_it(self):
        """Break it catches: dropping the sort, or iterating the set directly.

        The list is a committed artefact, so an unsorted one churns its whole diff
        between two runs on identical inputs - and a set's iteration order is not
        stable across processes at all.
        """
        found = content_set.content_paths(
            {"files": {"document": ["repositories/b/z.md", "repositories/a/y.md"]}}
        )
        self.assertEqual(found, ["repositories/a/y.md", "repositories/b/z.md"])

    def test_a_malformed_detect_result_yields_nothing_rather_than_raising(self):
        """Break it catches: trusting detect's shape.

        The stage turns an empty set into a refusal with an exit code; a traceback
        instead would read as a library defect rather than as a missing input.
        """
        for bad in ({}, {"files": None}, {"files": []}, {"files": {"code": "not-a-list"}}):
            with self.subTest(bad=bad):
                self.assertEqual(content_set.content_paths(bad), [])

    def test_kind_counts_name_every_category_detect_reported(self):
        """Break it catches: reporting counts for a fixed set of categories."""
        counts = content_set.kind_counts(
            {"files": {"image": ["a"], "code": ["b", "c"], "spreadsheet": ["d"]}}
        )
        self.assertEqual(counts, {"code": 2, "image": 1, "spreadsheet": 1})


class NoiseAttributionTest(unittest.TestCase):
    """Where the non-content files are - measured from the content set, not listed."""

    TREE = [
        "repositories/alpha/docs/guide.md",
        "repositories/alpha/docs/other.md",
        "repositories/alpha/README.md",
        "repositories/alpha/graphify-out/cache/ast/aa.json",
        "repositories/alpha/graphify-out/cache/ast/bb.json",
        "repositories/alpha/graphify-out/graph.json",
        "repositories/beta/src/main.py",
        "repositories/beta/graphify-out/graph.json",
        "repositories/beta/vendor/bundle/one.js",
        "repositories/gamma/only/noise.bin",
    ]
    CONTENT = [
        "repositories/alpha/docs/guide.md",
        "repositories/alpha/docs/other.md",
        "repositories/beta/src/main.py",
    ]

    def roots(self) -> dict[str, tuple[int, int]]:
        return {
            root.path: (root.files, root.repositories)
            for root in content_set.noise_roots(self.TREE, self.CONTENT)
        }

    def test_noise_is_attributed_to_the_shallowest_directory_holding_no_content(self):
        """Break it catches: attributing a file to its own parent directory.

        Walking up from the file rather than down from the repository splits these
        four pipeline artefacts across `graphify-out/cache/ast` and `graphify-out`
        - and on a real estate across thousands of content-hash
        directories, which turns the single dominant noise source into thousands of
        one-file rows nobody can read.
        """
        # Four files by hand: three under alpha's graphify-out, one under beta's.
        self.assertEqual(self.roots()["graphify-out"], (4, 2))

    def test_the_same_directory_across_repositories_aggregates_to_one_row(self):
        """Break it catches: keying the attribution on the full path.

        `repositories/alpha/graphify-out` and `repositories/beta/graphify-out` are
        the same finding. Keyed on the full path they are two rows out of hundreds,
        and the estate-wide magnitude - the whole argument for acting - disappears.
        """
        self.assertEqual(self.roots()["graphify-out"][1], 2)
        self.assertNotIn("repositories/alpha/graphify-out", self.roots())

    def test_every_non_content_file_is_attributed_exactly_once(self):
        """Break it catches: tallying a file under each of its ancestor directories.

        A per-file tally that credits several buckets sums to more than the
        population it describes, and then reads as a larger problem than the estate
        has. This repository has shipped that shape before, which is why the stage
        prints the reconciliation rather than assuming it.
        """
        roots = content_set.noise_roots(self.TREE, self.CONTENT)
        noise = [p for p in self.TREE if p not in self.CONTENT]
        # Ten files in the tree, three of them content: seven, counted by hand.
        self.assertEqual(len(noise), 7)
        self.assertEqual(sum(root.files for root in roots), len(noise))

    def test_a_content_file_is_never_counted_as_noise(self):
        """Break it catches: testing membership of the directory set, not the content set.

        The first version of `noise_roots` skipped a path found in `bearing` - which
        holds *directories*, so no file path is ever in it and every content file
        was counted as noise. The percentage came out higher, which is the direction
        that gets believed.
        """
        roots = content_set.noise_roots(self.TREE, self.CONTENT)
        self.assertEqual(sum(root.files for root in roots), len(self.TREE) - len(self.CONTENT))
        self.assertNotIn("docs", self.roots())

    def test_a_repository_holding_no_content_is_named_as_a_whole(self):
        """Break it catches: attributing a contentless repository to its subdirectory.

        `repositories/gamma` has nothing the store calls content, so the finding is
        the repository, not the `only/` directory inside it - naming the
        subdirectory implies the rest of the repository was fine.
        """
        self.assertEqual(self.roots()[content_set.WHOLE_REPOSITORY], (1, 1))

    def test_a_non_content_file_beside_content_is_named_rather_than_dropped(self):
        """Break it catches: skipping a file no noise root covers.

        `repositories/alpha/README.md` sits in a directory that does hold content,
        so it has no contentless ancestor. Dropping it is the easy mistake and it
        breaks the partition above: the buckets would then total 6 against 7.
        """
        self.assertEqual(self.roots()[content_set.LOOSE], (1, 1))

    def test_the_order_is_stable_when_two_directories_tie(self):
        """Break it catches: sorting on the count alone.

        Two directories of equal size then come out in whatever order the caller
        supplied the tree in, and the committed manifest differs between two builds
        that changed nothing. Nothing in this function's signature promises sorted
        input - `tree_files` sorting is a separate decision - so the tree is passed
        here in the *reverse* of the order the answer needs. Passing it already
        sorted would let a stable sort produce the right answer with no tiebreak at
        all, which is a test that cannot fail.
        """
        tree = [
            "repositories/a/zzz/1.bin",
            "repositories/a/keep.md",
            "repositories/a/aaa/1.bin",
        ]
        roots = content_set.noise_roots(tree, ["repositories/a/keep.md"])
        self.assertEqual([root.path for root in roots], ["aaa", "zzz"])


class TreeWalkTest(SettingsIsolated):
    def test_a_directory_symlink_is_not_followed(self):
        """Break it catches: passing followlinks=True to the walk.

        A followed link reports the same files twice under two paths, so the tree
        figure - the denominator of every percentage this stage prints - overstates
        what a search would actually read. Symlinked source files are already a
        known duplication hazard in this pipeline.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root, {"repositories/a/real/one.txt": "x"})
            os.symlink(root / "repositories/a/real", root / "repositories/a/link")
            found = content_set.tree_files(root / "repositories")
            self.assertEqual(found, ["repositories/a/real/one.txt"])

    def test_an_absent_corpus_yields_no_files_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(content_set.tree_files(Path(tmp) / "nope"), [])


class StageTest(SettingsIsolated):
    """The stage, end to end, through the real writer and the real walk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config.configure(root=self.root)

    def build_a_store(self) -> None:
        write_tree(
            self.root,
            {
                "repositories/alpha/docs/guide.md": "the answer is here",
                "repositories/alpha/graphify-out/cache/ast/aa.json": "{}",
                "repositories/alpha/graphify-out/cache/ast/bb.json": "{}",
                "repositories/beta/src/main.py": "x = 1",
                "repositories/beta/vendor/bundle/one.js": "noise",
            },
        )
        detect_for(
            self.root,
            {
                "document": ["repositories/alpha/docs/guide.md"],
                "code": ["repositories/beta/src/main.py"],
            },
        )

    def test_it_writes_the_set_as_a_path_list_grep_can_consume(self):
        """Break it catches: emitting only the JSON manifest.

        The consumer is `grep`. A consumer that has to parse JSON first is a
        consumer that re-derives the set badly instead, which is the state this
        stage exists to end.
        """
        self.build_a_store()
        code, output = run()
        self.assertEqual(code, 0)
        self.assertEqual(
            config.CONTENT_FILES_PATH.read_text(encoding="utf-8"),
            "repositories/alpha/docs/guide.md\nrepositories/beta/src/main.py\n",
        )
        self.assertIn("2 content files", output)

    def test_the_written_list_actually_finds_the_answer_and_not_the_noise(self):
        """Break it catches: emitting paths a search cannot use.

        The end the artefact exists for, driven through the real `grep`: the same
        term matches once through the content set and three times over the raw
        tree. Asserting the file's contents would not catch a list that is
        unusable - wrong quoting, a stale relative root, a missing newline.
        """
        write_tree(
            self.root,
            {
                "repositories/alpha/docs/guide.md": "governance record",
                "repositories/alpha/graphify-out/cache/ast/aa.json": "governance record",
                "repositories/alpha/vendor/bundle/one.js": "governance record",
            },
        )
        detect_for(self.root, {"document": ["repositories/alpha/docs/guide.md"]})
        self.assertEqual(run()[0], 0)

        through_the_set = subprocess.run(
            "tr '\\n' '\\0' < "
            + str(config.CONTENT_FILES_PATH)
            + " | xargs -0 grep -lIs -- 'governance record'",
            shell=True,  # noqa: S602 - the documented route is a shell pipeline
            cwd=self.root,
            capture_output=True,
            text=True,
        ).stdout.split()
        naive = subprocess.run(
            ["grep", "-rlIs", "--", "governance record", "repositories"],
            cwd=self.root,
            capture_output=True,
            text=True,
        ).stdout.split()

        self.assertEqual(through_the_set, ["repositories/alpha/docs/guide.md"])
        self.assertEqual(len(naive), 3, f"the naive search should see the noise too: {naive}")

    def test_it_refuses_to_write_an_empty_set(self):
        """Break it catches: writing the file whatever detect held.

        An empty path list makes every search over it return no matches, and no
        matches reads as a confident answer about the estate rather than as a
        missing input. Nothing downstream can tell those apart, so the refusal has
        to happen here.
        """
        config.DETECT_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.DETECT_PATH.write_text(json.dumps({"files": {"document": []}}), encoding="utf-8")
        code, output = run()
        self.assertEqual(code, 2)
        self.assertFalse(config.CONTENT_FILES_PATH.exists())
        self.assertFalse(config.CONTENT_SET_PATH.exists())
        self.assertIn("nothing was written", output)

    def test_it_claims_no_noise_figure_when_the_corpus_is_not_on_disk(self):
        """Break it catches: reporting the ratio from an unmeasured tree.

        With no corpus the tree count is zero, and zero renders as "no noise" - the
        most flattering possible reading of the least measured case, on the artefact
        whose entire purpose is to say the tree is mostly noise. A sparse clone is
        the normal way to hold a store, so this is the common path, not the corner.
        """
        detect_for(self.root, {"document": ["repositories/alpha/docs/guide.md"]})
        code, output = run()
        self.assertEqual(code, 0)
        manifest = json.loads(config.CONTENT_SET_PATH.read_text(encoding="utf-8"))
        self.assertIs(manifest["corpus"]["measured"], False)
        self.assertNotIn("tree_files", manifest["corpus"])
        self.assertIn("NOT measured", output)
        self.assertNotIn("%", output)

    def test_two_runs_on_the_same_inputs_produce_identical_bytes(self):
        """Break it catches: any unordered iteration reaching either artefact.

        Both files are committed in consumer repositories, so non-determinism shows
        up as a whole-file diff on a build that changed nothing.
        """
        self.build_a_store()
        self.assertEqual(run()[0], 0)
        first = (
            config.CONTENT_FILES_PATH.read_bytes(),
            config.CONTENT_SET_PATH.read_bytes(),
        )
        self.assertEqual(run()[0], 0)
        self.assertEqual(
            (config.CONTENT_FILES_PATH.read_bytes(), config.CONTENT_SET_PATH.read_bytes()),
            first,
        )

    def test_it_names_the_dominant_noise_directory_and_reconciles_the_tally(self):
        """Break it catches: printing bucket counts without their total.

        A tally that does not sum to its population is how a bucket count comes to
        exceed the thing it counted, and the only way to see it is to print both.
        """
        self.build_a_store()
        code, output = run()
        self.assertEqual(code, 0)
        self.assertIn("graphify-out", output)
        self.assertIn("accounting for 3 of 3 non-content files", output)
        self.assertIn("of which 3 (60.0%) are not in the content set", output)

    def test_it_reports_content_files_that_are_no_longer_on_disk(self):
        """Break it catches: silence about a detect result older than the tree.

        Every one of those paths is a `grep` error rather than a search, so a
        consumer sees a wall of "No such file" and no explanation.
        """
        self.build_a_store()
        detect_for(
            self.root,
            {
                "document": ["repositories/alpha/docs/guide.md", "repositories/alpha/gone.md"],
                "code": ["repositories/beta/src/main.py"],
            },
        )
        code, output = run()
        self.assertEqual(code, 0)
        manifest = json.loads(config.CONTENT_SET_PATH.read_text(encoding="utf-8"))
        self.assertEqual(manifest["corpus"]["content_files_absent_from_the_tree"], 1)
        self.assertIn("are not on disk", output)

    def test_it_warns_when_a_path_could_not_be_made_relative(self):
        """Break it catches: committing an absolute path without saying so.

        Relativising fails silently for a corpus outside the store root - the path
        simply stays absolute and the file looks fine. The chunk plan carried tens
        of thousands of these, and each relocation rewrote every one.
        """
        with tempfile.TemporaryDirectory() as outside:
            elsewhere = Path(outside) / "corpus" / "note.md"
            elsewhere.parent.mkdir(parents=True)
            elsewhere.write_text("x", encoding="utf-8")
            config.DETECT_PATH.parent.mkdir(parents=True, exist_ok=True)
            config.DETECT_PATH.write_text(
                json.dumps({"files": {"document": [str(elsewhere)]}}), encoding="utf-8"
            )
            code, output = run()
        self.assertEqual(code, 0)
        self.assertIn("could not be made relative", output)
        self.assertIn(str(elsewhere), config.CONTENT_FILES_PATH.read_text(encoding="utf-8"))

    def test_the_manifest_records_which_detect_result_it_was_built_from(self):
        """Break it catches: recording nothing, leaving staleness undetectable.

        Commit dates cannot answer it: the ordinary workflow commits a refreshed
        detect result and the set built from it together, so the dates agree whether
        or not the set was rebuilt.
        """
        self.build_a_store()
        self.assertEqual(run()[0], 0)
        recorded = json.loads(config.CONTENT_SET_PATH.read_text(encoding="utf-8"))
        self.assertEqual(list(recorded["generated_from"]), ["graphify-out/.graphify_detect.json"])
        self.assertRegex(recorded["generated_from"]["graphify-out/.graphify_detect.json"], r"^\w+$")

    def test_the_command_it_prints_is_runnable_from_the_store_root(self):
        """Break it catches: printing the artefact's basename instead of its path.

        The line is there to be copied and pasted, and it is pasted at the store
        root - where `content-files.txt` alone names nothing. It was the basename
        until this run's own output was read rather than its exit code.
        """
        self.build_a_store()
        output = run()[1]
        self.assertIn("tr '\\n' '\\0' < knowledge/corpus/content-files.txt", output)

    def test_top_must_be_at_least_one(self):
        code, output = run(["--top", "0"])
        self.assertEqual(code, 2)
        self.assertIn("at least 1", output)


class StageIsWiredTest(unittest.TestCase):
    def test_the_stage_is_registered_under_the_name_the_docs_use(self):
        """Break it catches: shipping the module without registering it.

        A module nobody can invoke is the whole of #213 restated: the pipeline knows
        the answer and nothing surfaces it.
        """
        self.assertIn("content-set", cli.STAGES)
        self.assertEqual(cli.STAGES["content-set"][0], "build_content_set")

    def test_asking_it_what_it_does_does_not_make_it_do_it(self):
        """Break it catches: omitting the stage from SELF_PARSING.

        An unhandled `--help` falls through to the stage's default action, so
        probing an unfamiliar subcommand would walk the whole corpus and overwrite
        two committed artefacts.
        """
        self.assertIn("content-set", cli.SELF_PARSING)
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            build_content_set.parse_args(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--top", out.getvalue())


class StatusReportsTheContentSetTest(SettingsIsolated):
    """`status` is where an operator looks for what the store is missing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config.configure(root=self.root)

    def status_output(self) -> tuple[int, str]:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            code = status.main([])
        return code, out.getvalue()

    def test_it_says_the_content_set_is_not_exposed_when_nothing_wrote_one(self):
        """Break it catches: reporting nothing when the artefact is absent.

        Silence is indistinguishable from a healthy store, and #213 is precisely
        that nothing said the content set existed to be exposed. An operator who
        never hears about it never runs the stage.
        """
        code, output = self.status_output()
        self.assertEqual(code, 0)
        self.assertIn("Content set: not exposed", output)

    def test_it_says_a_content_set_is_stale_when_detect_has_moved_on(self):
        """Break it catches: reading the recorded count without checking its input.

        A set built from the previous scan names the previous scan's files, so a
        search over it misses everything added since - and reports no matches, which
        reads as an answer.
        """
        write_tree(self.root, {"repositories/a/one.md": "x"})
        detect_for(self.root, {"document": ["repositories/a/one.md"]})
        self.assertEqual(run()[0], 0)
        detect_for(self.root, {"document": ["repositories/a/one.md", "repositories/a/two.md"]})
        code, output = self.status_output()
        self.assertEqual(code, 0)
        self.assertIn("different detect result", output)

    def test_it_does_not_call_a_set_stale_when_there_is_no_detect_result_to_compare(self):
        """Break it catches: treating "cannot be judged" as "disagrees".

        A store that does not keep the detect result - it is a graphify working file
        - would otherwise be told its content set was stale on every single run,
        which is how a real warning stops being read.
        """
        write_tree(self.root, {"repositories/a/one.md": "x"})
        detect_for(self.root, {"document": ["repositories/a/one.md"]})
        self.assertEqual(run()[0], 0)
        config.DETECT_PATH.unlink()
        code, output = self.status_output()
        self.assertEqual(code, 0)
        self.assertIn("Content set: 1 content files", output)
        self.assertNotIn("different detect result", output)

    def test_it_claims_no_tree_percentage_when_the_manifest_did_not_measure_one(self):
        """Break it catches: dividing by a tree count the manifest never recorded.

        Reading a missing `tree_files` as zero either raises or prints a made-up
        ratio, and on this artefact a made-up ratio is worse than none.
        """
        detect_for(self.root, {"document": ["repositories/a/one.md"]})
        self.assertEqual(run()[0], 0)
        code, output = self.status_output()
        self.assertEqual(code, 0)
        self.assertIn("not measured", output)
        self.assertNotIn("% of the tree", output)

    def test_status_still_returns_zero_with_no_content_set(self):
        """Break it catches: making this a failure. Drift is normal here by design."""
        self.assertEqual(self.status_output()[0], 0)


class ConfigureReachesEveryOutputTest(SettingsIsolated):
    def test_every_root_relative_path_constant_is_repointed_by_configure(self):
        """Break it catches: adding a path constant and forgetting `_recompute_paths`.

        Such a constant keeps the default root for the whole process, so
        `knowledgestore --root /elsewhere <stage>` writes one artefact into the
        wrong store and every other into the right one. Derived by reflection
        rather than from a hand list, because a hand list omits the new name for
        exactly the same reason `_recompute_paths` did.
        """
        rooted = [
            name
            for name, value in vars(config).items()
            if name.isupper() and isinstance(value, Path) and name != "ROOT"
        ]
        self.assertGreater(len(rooted), 20, f"the reflection found almost nothing: {rooted}")
        self.assertIn("CONTENT_FILES_PATH", rooted)

        original = config.ROOT
        self.addCleanup(config.configure, original)
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            resolved = str(Path(tmp).resolve())
            stale = [name for name in rooted if not str(getattr(config, name)).startswith(resolved)]
            self.assertEqual(
                stale,
                [],
                f"{stale} still point at the old root, so `--root` misses them entirely",
            )


if __name__ == "__main__":
    unittest.main()
