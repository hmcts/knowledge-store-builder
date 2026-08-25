"""The extension-aware stem basis, opt-in, with its migration cost measured (#115).

Dropping the file extension from an id stem is the root of #115 and #129: two
files sharing a path stem — a component and its template, a doc and its config
sibling — are assigned one id by design. Measured on one estate, 98 collisions
between the AST and semantic layers, all with disagreeing labels, all describing
different files, carrying 311 edges; 92 of the 98 were an extension pair.

Keeping the extension removes that class rather than resolving instances of it.
It is **opt-in and not the default**, because it changes ids that stores have
already committed: adopting it is a re-archive, not an upgrade, and that is a
decision about existing data rather than a code change.

So what this file pins is both halves:

- the default basis is **unchanged**, byte for byte, because that is what every
  committed store's ids were generated with
- the opt-in basis removes the collision
- a default run reports what adopting would cost **on the operator's own corpus**,
  because a migration cost estimated from someone else's estate is not a number
  anyone can act on
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledgestore import merge_chunks  # noqa: E402


class SpecStemTest(unittest.TestCase):
    def test_the_default_basis_drops_the_extension(self):
        """Breaks if the default changes. Every committed store's ids were
        generated with this rule, so changing it silently re-keys their data."""
        self.assertEqual(
            merge_chunks.spec_stem("src/a/read-case.component.ts"), "src_a_read_case_component"
        )

    def test_the_default_basis_collides_across_extensions(self):
        """Breaks if this stops being true, which would mean the defect is gone and
        the opt-in basis has no purpose. Pinned so the reason for the flag stays
        visible rather than becoming folklore."""
        self.assertEqual(
            merge_chunks.spec_stem("a/x.ts"),
            merge_chunks.spec_stem("a/x.html"),
            "the collision this exists to remove is no longer reproducible",
        )

    def test_the_opt_in_basis_separates_them(self):
        """Breaks if the opt-in basis fails to remove the class it exists for."""
        self.assertNotEqual(
            merge_chunks.spec_stem("a/x.ts", keep_extension=True),
            merge_chunks.spec_stem("a/x.html", keep_extension=True),
        )

    def test_a_file_with_no_extension_is_unchanged_either_way(self):
        """Breaks if the two bases diverge where there is nothing to keep.

        An extensionless path must spell the same under both, or a store adopting
        the opt-in basis would re-key ids for no reason and inflate its own
        migration cost.
        """
        self.assertEqual(
            merge_chunks.spec_stem("scripts/deploy"),
            merge_chunks.spec_stem("scripts/deploy", keep_extension=True),
        )


class MigrationCostTest(unittest.TestCase):
    def _chunks(self, nodes):
        """A chunk in the format the extraction agents actually write.

        `source_file`, singular: `_collect` builds the plural `source_files`
        from it. An earlier version of this fixture used the internal shape, so
        every id fell back to the label and these tests exercised a path the
        real input never takes.
        """
        return [("chunk_1.json", {"nodes": nodes, "edges": []})]

    def test_the_cost_is_counted_on_a_default_run(self):
        """Breaks if a store has to adopt the change to learn what it would cost.

        The measurement is the whole reason this can be decided rather than
        argued: the issue's figures come from one estate, and a migration cost is
        only actionable when it is your own.
        """
        nodes = [
            {"id": "x", "label": "Component", "source_file": "a/x.ts", "kind": "class"},
            {"id": "x", "label": "Template", "source_file": "a/x.html", "kind": "class"},
        ]

        _nodes, _remap, counters = merge_chunks.merge_nodes(self._chunks(nodes))

        self.assertEqual(counters["namespaced"], 2)
        self.assertEqual(
            counters["basis_would_change"], 2, "both ids are spelled differently by the other basis"
        )

    def test_the_counter_is_seeded_for_a_caller_that_skips_main(self):
        """Breaks if a caller outside `main` gets a KeyError.

        Exactly the defect #194 fixed for two other keys in this dict: both
        functions are public and only `main` injected them.
        """
        _n, _r, counters = merge_chunks.merge_nodes([("chunk_1.json", {"nodes": [], "edges": []})])
        self.assertIn("basis_would_change", counters)

    def test_the_opt_in_basis_keeps_two_extension_siblings_apart(self):
        """Breaks if the flag is threaded but has no effect on the ids produced —
        the wiring escape this repository's mutation gate records four times."""
        nodes = [
            {"id": "x", "label": "Component", "source_file": "a/x.ts", "kind": "class"},
            {"id": "x", "label": "Template", "source_file": "a/x.html", "kind": "class"},
        ]

        merged, _remap, _counters = merge_chunks.merge_nodes(
            self._chunks(nodes), keep_extension=True
        )

        stems = {node["id"] for node in merged.values()}
        self.assertEqual(len(stems), 2)
        self.assertTrue(
            any("_ts_" in s or s.endswith("_ts") for s in stems),
            f"no id carries the extension: {stems}",
        )

    def test_the_default_output_is_unchanged_by_this_feature(self):
        """Breaks if adding the option changed what a default run emits.

        Stage outputs are committed artefacts in consumer repositories, so a change
        in what the default emits is a change to their data — and this feature's
        whole premise is that it does not make one until someone opts in.
        """
        nodes = [
            {"id": "x", "label": "Component", "source_file": "a/x.ts", "kind": "class"},
            {"id": "x", "label": "Template", "source_file": "a/x.html", "kind": "class"},
        ]

        merged, _remap, _counters = merge_chunks.merge_nodes(self._chunks(nodes))

        # Both namespaced onto the shared extensionless stem, then disambiguated -
        # which is the pre-existing behaviour this must not disturb.
        self.assertTrue(all(node["id"].startswith("a_x_x") for node in merged.values()))
        self.assertEqual(len(merged), 2)


class CliTest(unittest.TestCase):
    def test_the_flag_defaults_to_the_spec_basis(self):
        """Breaks if the default flips, which would re-key every store on upgrade."""
        self.assertEqual(merge_chunks.parse_args([]).stem_basis, "path")

    def test_the_flag_accepts_the_opt_in_basis(self):
        self.assertEqual(
            merge_chunks.parse_args(["--stem-basis", "path-with-extension"]).stem_basis,
            "path-with-extension",
        )

    def test_main_reports_the_cost(self):
        """Breaks if the measurement is computed and never printed — a number an
        operator cannot see is not a measurement they can act on."""
        import contextlib
        import io as io_module

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / ".graphify_chunk_1.json").write_text(
                json.dumps(
                    {
                        "nodes": [
                            {"id": "x", "label": "C", "source_file": "a/x.ts", "kind": "class"},
                            {
                                "id": "x",
                                "label": "T",
                                "source_file": "a/x.html",
                                "kind": "class",
                            },
                        ],
                        "edges": [],
                    }
                )
            )
            out = io_module.StringIO()
            with contextlib.redirect_stdout(out):
                code = merge_chunks.main(
                    ["--chunks", str(directory), "--out", str(directory / "sem.json")]
                )

        self.assertEqual(code, 0)
        self.assertIn("migration cost", out.getvalue())
        self.assertIn("--stem-basis path-with-extension", out.getvalue())


if __name__ == "__main__":
    unittest.main()
