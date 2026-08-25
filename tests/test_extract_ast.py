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
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import settings_isolation  # noqa: F401  (path setup and settings isolation)

from knowledgestore import config, extract_ast  # noqa: E402


class _Extractor:
    """Stands in for the parser: records what it was given, raises where told to."""

    def __init__(self, raise_for=()):
        self.calls: list[list[Path]] = []
        self.raise_for = tuple(raise_for)

    def __call__(self, files, cache_root=None):
        self.calls.append(list(files))
        for path in files:
            if any(name in str(path) for name in self.raise_for):
                raise RuntimeError("unparseable grammar")
        return {
            "nodes": [{"id": str(path), "label": path.name} for path in files],
            "edges": [{"source": str(files[0]), "target": str(path)} for path in files[1:]],
        }

    @property
    def files_seen(self) -> set[str]:
        return {str(path) for call in self.calls for path in call}


class ExtractAstTest(unittest.TestCase):
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
        self.assertIn(str(good), labels)
        self.assertIn(str(other), labels)
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
