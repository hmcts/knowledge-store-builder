"""The contract with the library over a partial store: layers never built, one
built only on the builder's machine, read with the query index and without it.

Real stores are rarely whole. `library_store/partial/` is the main store's
estate and pin built with `generate.PARTIAL_SKIP`'s stages not run -
`minimum-standard-settings`, `minimum-standard`, `topics`, `deepdive` and
`deployments` - and with a root `.gitignore` naming `knowledge/git-history/`.
`pipeline.py` refuses the build unless each skipped stage's layer is absent and
the history is recorded `local_only`, so these tests read what the library
wrote for those cases, never a hand-made manifest.

Every expected value is derived by hand from v0.19.1's `store_manifest`, before
the store is read:

- `_layers` probes each layer's path. A file layer no stage wrote is not on
  disk: `{"present": false, "digest": null}`. `minimum_standard` is a
  directory layer, absent the same way, and `compute` adds `"recognised": null`
  because there is nothing to judge. `topics` and `deep_dives` are authored
  layers, so `compute` adds `"authored_as_of": null`.
- `history` is on disk, but `_ignored` (`git ls-files -o -i
  --exclude-standard`) lists every file under it, so a clone would receive none:
  `"present": false`, no digest, and `"local_only": true`.
- `deployments` is not a file: `compute` reads it from telemetry's
  `deployments.pairs`, which only the `deployments` stage records. Nothing
  recorded it, so `_flag` gives `null`, unknown - never `false`, which would say
  the store was measured and has none.
- Every other layer is built as in the main store, `index` absent only in
  `no-index/`. `sources_as_of` and the provenance are the main store's: the same
  four clones at the same commits.

A repository that left the estate: `generate.RETIRED`'s `zeta-archive` was
synced and its history exported, then dropped from `config/repositories.txt`
and its clone deleted (`pipeline.py --retire`). The build's own `sync` writes
provenance for the four alone, but `export-history` never prunes, and v0.19.1's
`intent` indexes every `git-history/*/commits.ndjson` it finds
(`build_intent_index.main`), so the ticket layer holds zeta-archive's one
commit: `DEMO-2: archive the refund exports`, landing `exports/refunds.csv`
once. The index builder lists every repository with a node or a ticket
(`store_index._fill`), so the library's own `repos` names it. The server
follows the library, as `tests/fixture_store/DESIGN.md` 6.4 decides: a
repository only the ticket layer names is "in the estate" in both modes, listed
(so no `graph_nodes` or `cloned_at` note), with no graph node and no provenance
record. The root cause is upstream, in the library's `intent` stage indexing a
history directory the estate no longer names; that is being raised with the
library.

The server reports each layer's `present` as the manifest records it, so `null`
stays `null`: a layer it knows, absent or unknown, is never `unread`. Answers
come from the written answers alone, since the topic briefs and deep dives were
never built; their texts are `test_library_tickets`' literals, rendered from the
same Markdown with the same `config/repositories.txt`.
"""

import json
import unittest

import test_library_tickets as written
from knowledgestore.server import tools_graph, tools_minimum_standard, tools_tickets
from library_store import harness
from library_store.harness import LIBRARY_LAYERS

MANIFEST = "knowledge/store-manifest.json"

# The layers whose stage `PARTIAL_SKIP` names, as `_layers` and `compute` write
# them when nothing is on disk: the fields beside `present` that a reader meets.
UNBUILT = {
    "minimum_standard": {
        "paths": ["knowledge/minimum-standard/"],
        "present": False,
        "digest": None,
        "recognised": None,
    },
    "topics": {
        "paths": ["knowledge/topics/briefs.json"],
        "present": False,
        "digest": None,
        "authored_as_of": None,
    },
    "deep_dives": {
        "paths": ["knowledge/deep-dives/dives.json"],
        "present": False,
        "digest": None,
        "authored_as_of": None,
    },
    "deployments": {"paths": [], "present": None, "digest": None},
}
# On disk and ignored by the store's git: kept off a clone.
LOCAL_ONLY = {
    "history": {
        "paths": ["knowledge/git-history/"],
        "present": False,
        "digest": None,
        "local_only": True,
    }
}


class PartialContract(harness.Contract):
    def _layers(self) -> dict[str, dict]:
        """`store_info`'s layers in this mode: each as the manifest records it."""
        layers = {name: {"present": True} for name in LIBRARY_LAYERS}
        for name, entry in {**UNBUILT, **LOCAL_ONLY}.items():
            layers[name] = {"present": entry["present"]}
        if not self.INDEXED:
            layers["index"] = {"present": False}
        return layers

    def test_the_manifest_writes_each_unbuilt_layer_in_the_library_s_shape(self):
        """Catches: the partial store regenerated with a skipped stage run after
        all, or its history committed, so nothing here reads an unbuilt layer;
        and, after a library upgrade, a change in how v0.19.1 records one - a
        `false` for an unmeasured `deployments`, or a `local_only` history
        given a digest - that the server's reads below were not written for."""
        manifest = json.loads((self.STORE / MANIFEST).read_text("utf-8"))
        self.assertEqual(manifest["library_version"], "0.19.1")
        self.assertEqual(sorted(manifest["layers"]), sorted(LIBRARY_LAYERS))
        for name, entry in {**UNBUILT, **LOCAL_ONLY}.items():
            with self.subTest(layer=name):
                self.assertEqual(manifest["layers"][name], entry)
        built = set(LIBRARY_LAYERS) - set(UNBUILT) - set(LOCAL_ONLY)
        if not self.INDEXED:
            built.discard("index")
            self.assertIs(manifest["layers"]["index"]["present"], False)
        self.assertEqual(
            {name for name, entry in manifest["layers"].items() if entry["present"] is True},
            built,
        )

    def test_store_info_reports_each_unbuilt_layer_absent_and_none_unread(self):
        """Catches: a layer the manifest names but records absent - or `null`,
        as the unmeasured `deployments` is - reported `unread` or unrecognised
        as if a newer minor had added it, rewritten as present, or turned from
        unknown into `false`; and the `deployments.present` flag read as
        anything but the `null` the library wrote."""
        info = tools_graph.store_info(self.session)
        self.assertEqual(info["layers"], self._layers())
        self.assertEqual(info["unread"], [])
        self.assertEqual(
            info["flags"],
            {"graph.source_urls": True, "intent.reach": True, "deployments.present": None},
        )
        self.assertEqual(info["total"], 4)

    def test_minimum_standard_answers_that_the_layer_is_absent(self):
        """Catches: a `minimum_standard` layer the manifest records absent, with
        `recognised: null`, answered with an error, as unrecognised, or with
        records from somewhere else, rather than as absent with nothing read."""
        found = tools_minimum_standard.minimum_standard(self.session)
        self.assertEqual(found["items"], [])
        self.assertEqual(found["total"], 0)
        self.assertEqual(
            found["searched"], "searched no layer; the minimum_standard layer is absent"
        )
        self.assertEqual(found["store"]["layers_used"], [])

    def test_answers_come_from_the_written_answers_alone(self):
        """Catches: a topic-brief or deep-dive layer recorded absent read anyway,
        failing the whole search or listed as not read; or the answers that were
        built lost with it. Of the two written answers, both hold `refund`, and
        they rank by id."""
        found = tools_tickets.answers(self.session, "refund")
        self.assertEqual(found["items"], [written.HOW_REFUNDS_WORK, written.WHO_APPROVES_REFUNDS])
        self.assertEqual(found["matches"], 2)
        self.assertNotIn("not_read", found)
        self.assertEqual(found["store"]["layers_used"], ["answers", "ownership_notes"])

    def test_a_retired_repository_the_ticket_layer_names_is_in_the_estate(self):
        """Catches: a repository the library's ticket layer and index name - here
        one retired from the estate, whose history outlived it - answered
        differently by the two read modes (DESIGN.md 6.4): dropped from the
        estate without the index, or with it, so `find_repo` says "not in the
        estate" and `tickets` loses its `DEMO-2` row in one mode only; or given a
        graph node or a provenance record it does not have."""
        known = {
            "answer": "in the estate",
            "exact": "zeta-archive",
            "matched_case_insensitively": False,
        }
        found = tools_graph.find_repo(self.session, "zeta-archive")
        self.assertEqual({k: v for k, v in found.items() if k != "store"}, known)
        self.assertEqual(found["store"]["layers_used"], [self.reader])
        tickets = tools_tickets.tickets(self.session, "zeta-archive")
        self.assertEqual(tickets["repository"], known)
        self.assertEqual(
            tickets["landed"], [{"ticket": "DEMO-2", "path": "exports/refunds.csv", "commits": 1}]
        )
        self.assertEqual(
            (tickets["proposed"], tickets["totals"]), ([], {"landed": 1, "proposed": 0})
        )
        # No graph node: graphify never saw its clone. "refund" begins words in
        # four alpha-lib labels (`test_library_graph`), so the same search scoped
        # to alpha-lib is the control that this zero is not a blind read.
        alpha = tools_graph.search(self.session, "refund", repo="alpha-lib")
        self.assertGreaterEqual(alpha["matches"], 1)
        zeta = tools_graph.search(self.session, "refund", repo="zeta-archive")
        self.assertEqual((zeta["matches"], zeta["items"]), (0, []))
        self.assertEqual(zeta["repository"], known)
        # The id graphify's scheme would give `exports/refunds.csv` had it been
        # extracted (`_file_stem`, `make_id`, the merge prefix): never served.
        # graphify 0.9.93 extracts no `.csv` (none of `detect.py`'s extension
        # sets names it), so this holds even for a cloned repository: it guards
        # only against a node appearing by some other route. The scoped search
        # above, with its control, is what shows graphify never saw the clone.
        self.assertIsNone(tools_graph.node(self.session, "zeta-archive::exports_refunds")["node"])
        # No provenance record: the build's own `sync` ran without it.
        provenance = tools_graph.store_info(self.session)["items"]
        self.assertNotIn("zeta-archive", [item["repository"] for item in provenance])


PartialContractIndexed, PartialContractUnindexed = harness.partial_modes(PartialContract)


if __name__ == "__main__":
    unittest.main()
