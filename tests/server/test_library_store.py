"""The contract with the library: the dependency tools over a store that
knowledge-store-builder's own stages wrote, read with the query index and
without it.

`library_store/store/` is the library's output, committed, and
`library_store/generate.py` is how it was made and remakes it. The synthetic
fixture describes the layout as this server understands it; this store is the
layout as the library writes it, so a disagreement between the two fails here.
Every expected value is derived by hand from the four clones `generate.py`
writes: beta-app's `package.json` depends, on its line 4, on `@example/alpha-lib`
at `^1.2.0`, which alpha-lib publishes, and on `gamma-service`, which no clone
publishes; alpha-lib's and eta-portal's `package.json` each depend, on their
line 5, on `beta-app` at `^1.0.0`, the package beta-app publishes by its `name`;
delta-deploy holds deployment values and settings and no package manifest, so it
has no edge. So alpha-lib and beta-app depend on each other, a loop of two, and
beta-app has two consumers, alpha-lib and eta-portal, which the library lists
sorted by repository. A walk of alpha-lib's dependents reaches beta-app at one
hop and eta-portal at two. Git shas come from `library_store/inputs.json`, which
the generator wrote from the clones it made.

The kinds are v0.19.1's four built-in kinds (`relationships.BUILTIN`), since
the store registers none of its own (`config/relationship-kinds.json` is not
written): `kinds.json` lists each, sorted, whether a reference met it or not.
"""

import json
import unittest
from pathlib import Path

from knowledgestore.server import results, tools_graph, tools_relationships, tools_tickets
from knowledgestore.server.tools_graph import ArgumentRefused
from library_store import harness
from library_store.generate import EXTRA_LAYER, VERSION
from library_store.harness import SHAS

# beta-app -> alpha-lib, as each end's by-repo file names the other end: line 4 of
# beta-app's package.json, linked at the commit the store was built from.
NPM_EDGE = {
    "chart": "",
    "examples": [
        {
            "line": '"@example/alpha-lib": "^1.2.0",',
            "url": "https://github.com/example-org/beta-app/blob/"
            f"{SHAS['beta-app']}/package.json#L4",
        }
    ],
    "kinds": {"npm_package": 1},
    "references": 1,
    "refs_pinned": {"^1.2.0": 1},
    "refs_unpinned": 0,
}


def on_beta_app(consumer: str) -> dict:
    """`consumer` -> beta-app, as each end's by-repo file names the other end: line
    5 of the consumer's package.json, `"beta-app": "^1.0.0"`, the last entry of
    its `dependencies` so with no comma, linked at the consumer's built commit."""
    return {
        "chart": "",
        "examples": [
            {
                "line": '"beta-app": "^1.0.0"',
                "url": f"https://github.com/example-org/{consumer}/blob/"
                f"{SHAS[consumer]}/package.json#L5",
            }
        ],
        "kinds": {"npm_package": 1},
        "references": 1,
        "refs_pinned": {"^1.0.0": 1},
        "refs_unpinned": 0,
    }


ALPHA_ON_BETA = on_beta_app("alpha-lib")
ETA_ON_BETA = on_beta_app("eta-portal")
# The loop: alpha-lib depends on beta-app, which depends on alpha-lib.
LOOP = [["alpha-lib", "beta-app"]]

# beta-app's dependency no clone publishes, as the registry records it.
GAMMA = {
    "kind": "npm_package",
    "target": "gamma-service",
    "references": 1,
    "referenced_by": ["beta-app"],
    "outside_estate": True,
}

# v0.19.1's built-in kinds, as `kinds.json` lists them and a refusal quotes them.
KINDS = "helm_chart, maven_artifact, npm_package, terraform_module"


def known(name: str) -> dict:
    """A repository the graph holds nodes for."""
    return {"answer": "in the estate", "exact": name, "matched_case_insensitively": False}


class RelationshipsContract(harness.Contract):
    def test_the_store_is_the_library_release_and_layout_it_claims(self):
        """Catches: the committed store regenerated from another release without the
        record moving, or a library that stops naming the layer by its directory -
        either way the tests below would no longer describe what they say."""
        manifest = json.loads((self.STORE / "knowledge/store-manifest.json").read_text("utf-8"))
        # Moves with the pin; tests/test_library_generation.py asserts the literal "0.19.1".
        self.assertEqual(manifest["library_version"], VERSION)
        relationships = manifest["layers"]["relationships"]
        self.assertEqual(relationships["paths"], ["knowledge/relationships/"])
        self.assertIs(relationships["present"], True)

    def test_dependents_and_dependencies_return_the_library_edge(self):
        """Catches: the per-repository directory not found in the library's layout,
        so alpha-lib's consumer is answered as none - an empty list "in the
        estate" - and beta-app as depending on nothing."""
        found = tools_relationships.dependents(self.session, "alpha-lib")
        self.assertEqual(found["repository"], known("alpha-lib"))
        self.assertEqual(found["items"], [{**NPM_EDGE, "repository": "beta-app"}])
        self.assertEqual(found["total"], 1)

        depends = tools_relationships.dependencies(self.session, "beta-app")
        self.assertEqual(depends["items"], [{**NPM_EDGE, "repository": "alpha-lib"}, GAMMA])
        self.assertEqual(depends["totals"], {"depends_on": 1, "outside_estate": 1})

        walk = tools_relationships.dependents(self.session, "alpha-lib", transitive=True)
        self.assertEqual(
            [(item["repository"], item["path"]) for item in walk["items"]],
            [
                ("beta-app", ["alpha-lib", "beta-app"]),
                ("eta-portal", ["alpha-lib", "beta-app", "eta-portal"]),
            ],
        )
        answer = tools_relationships.repo_answer(self.session, "beta-app")
        self.assertEqual(answer["totals"], {"depends_on": 1, "depended_on_by": 2})

    def test_only_by_repo_files_are_repositories(self):
        """Catches: every `.json` in the layer directory read as a repository, so
        `kinds`, `repository-dependencies` and the store's own extra layer join the
        estate and inflate every `searched` count."""
        loaded = self.session.current()[0]
        self.assertEqual(
            sorted(results.by_repo_files(loaded)),
            ["alpha-lib", "beta-app", "delta-deploy", "eta-portal"],
        )
        for name in ("kinds", "repository-dependencies", Path(EXTRA_LAYER).stem):
            with self.subTest(name=name):
                found = tools_graph.find_repo(self.session, name)
                self.assertEqual(found["answer"], "not in the estate")
        unknown = tools_relationships.dependents(self.session, "epsilon-web")
        self.assertTrue(unknown["searched"].startswith("searched 4 repositories "))

    def test_an_extra_layer_in_the_layer_directory_is_read_as_its_own_layer(self):
        """Catches: the store's extra layer lost to the relationships layer - not
        served by `answers` - when it sits in the same directory."""
        found = tools_tickets.answers(self.session, "owns", 5)
        self.assertEqual(
            [(item["layer"], item["id"], item["summary"]) for item in found["items"]],
            [("ownership_notes", "who-owns-alpha-lib", "Who owns alpha-lib?")],
        )

    def test_a_kind_keeps_the_edges_and_targets_of_that_kind(self):
        """Catches: `kind=` matched against anything but an edge's `kinds` keys as
        the library writes them, so the npm edge is lost under its own kind, or
        kept under a kind the library lists but no reference met."""
        found = tools_relationships.dependents(self.session, "alpha-lib", kind="npm_package")
        self.assertEqual(found["items"], [{**NPM_EDGE, "repository": "beta-app"}])
        self.assertEqual(found["total"], 1)

        depends = tools_relationships.dependencies(self.session, "beta-app", kind="npm_package")
        self.assertEqual(depends["items"], [{**NPM_EDGE, "repository": "alpha-lib"}, GAMMA])

        for kind in ("helm_chart", "maven_artifact", "terraform_module"):
            with self.subTest(kind=kind):
                none = tools_relationships.dependents(self.session, "alpha-lib", kind=kind)
                self.assertEqual(none["repository"], known("alpha-lib"))
                self.assertEqual(none["items"], [])
                none = tools_relationships.dependencies(self.session, "beta-app", kind=kind)
                self.assertEqual(none["items"], [])

    def test_a_kind_the_library_does_not_list_is_refused_with_its_kinds(self):
        """Catches: `kinds.json` not found or not read as the library writes it (a
        map keyed by kind), so a misspelt kind is answered "in the estate, nothing
        depends on it" instead of refused with the kinds the library lists."""
        for call, repo in (
            (tools_relationships.dependents, "alpha-lib"),
            (tools_relationships.dependencies, "beta-app"),
        ):
            with self.subTest(call=call.__name__):
                with self.assertRaises(ArgumentRefused) as raised:
                    call(self.session, repo, kind="no-such-kind")
                self.assertEqual(
                    str(raised.exception),
                    f"kind is not one the relationships layer lists: {KINDS}",
                )

    def test_ref_prefix_not_keeps_a_consumer_by_the_refs_the_library_pinned(self):
        """Catches: the library's `refs_pinned` map read under another key or as a
        list, so `ref_prefix_not` drops every consumer or keeps every one: beta-app
        pins `^1.2.0`, so it is dropped under `^1` and kept under `~`. And a
        target outside the estate - gamma-service, whose registry entry records
        a count and its referrers but no pins - filtered by a prefix it cannot be
        judged by, instead of returned whole with the filter named as not
        applied."""
        pinned = tools_relationships.dependents(self.session, "alpha-lib", ref_prefix_not="^1")
        self.assertEqual(pinned["repository"], known("alpha-lib"))
        self.assertEqual(pinned["items"], [])
        self.assertEqual(pinned["total"], 0)

        other = tools_relationships.dependents(self.session, "alpha-lib", ref_prefix_not="~")
        self.assertEqual(other["items"], [{**NPM_EDGE, "repository": "beta-app"}])

        outside = tools_relationships.dependents(self.session, "gamma-service", ref_prefix_not="^1")
        self.assertEqual(outside["repository"]["answer"], "referenced by the estate but not held")
        self.assertEqual(
            outside["items"], [{key: GAMMA[key] for key in GAMMA if key != "outside_estate"}]
        )
        self.assertEqual(
            outside["not_applied"],
            {"ref_prefix_not": "targets outside the estate record no pins"},
        )

    def test_max_depth_bounds_the_walk_and_names_what_lies_beyond(self):
        """Catches: `max_depth` ignored, so a walk of one hop from alpha-lib runs on
        to eta-portal, two hops away; or read as "fewer than", so beta-app - one
        hop away - is cut; or the cut made silently, without naming beta-app as
        the repository whose dependents the walk did not reach; or the loop back
        to alpha-lib, read at the boundary, not reported. At two hops nothing is
        cut."""
        walk = tools_relationships.dependents(
            self.session, "alpha-lib", transitive=True, max_depth=1
        )
        self.assertEqual(walk["max_depth"], 1)
        self.assertEqual(
            [(item["repository"], item["depth"], item["path"]) for item in walk["items"]],
            [("beta-app", 1, ["alpha-lib", "beta-app"])],
        )
        self.assertEqual(walk["beyond_max_depth"], ["beta-app"])
        self.assertEqual(walk["cycles"], LOOP)

        deeper = tools_relationships.dependents(
            self.session, "alpha-lib", transitive=True, max_depth=2
        )
        self.assertEqual(
            [(item["repository"], item["depth"]) for item in deeper["items"]],
            [("beta-app", 1), ("eta-portal", 2)],
        )
        self.assertEqual(deeper["beyond_max_depth"], [])

    def test_a_transitive_walk_reports_the_loop_once_and_never_follows_it(self):
        """Catches: `cycles` not found over the library's `depended_on_by` edges, so
        the loop alpha-lib <-> beta-app goes unreported; the loop followed, so
        alpha-lib is listed among its own dependents or the walk repeats; or a
        repository reached at two hops given the wrong path or edge. From
        alpha-lib: beta-app at one hop, then eta-portal at two, through beta-app;
        beta-app's other dependent is alpha-lib itself, already visited. From
        beta-app: alpha-lib and eta-portal at one hop, and the loop the same."""
        walk = tools_relationships.dependents(self.session, "alpha-lib", transitive=True)
        self.assertEqual(walk["repository"], known("alpha-lib"))
        self.assertEqual(walk["max_depth"], 4)
        self.assertEqual(walk["cycles"], LOOP)
        self.assertEqual(walk["beyond_max_depth"], [])
        self.assertEqual(
            walk["items"],
            [
                {
                    "repository": "beta-app",
                    "depth": 1,
                    "path": ["alpha-lib", "beta-app"],
                    "edges": [{**NPM_EDGE, "repository": "beta-app"}],
                },
                {
                    "repository": "eta-portal",
                    "depth": 2,
                    "path": ["alpha-lib", "beta-app", "eta-portal"],
                    "edges": [{**ETA_ON_BETA, "repository": "eta-portal"}],
                },
            ],
        )
        self.assertEqual(walk["total"], 2)

        other = tools_relationships.dependents(self.session, "beta-app", transitive=True)
        self.assertEqual(
            [(item["repository"], item["depth"], item["path"]) for item in other["items"]],
            [
                ("alpha-lib", 1, ["beta-app", "alpha-lib"]),
                ("eta-portal", 1, ["beta-app", "eta-portal"]),
            ],
        )
        self.assertEqual(other["cycles"], LOOP)

    def test_dependencies_page_the_edge_and_the_target_outside_by_their_cursor(self):
        """Catches: `cursor` not followed over beta-app's dependencies, so the
        target outside the estate, gamma-service, is lost or repeated on the second
        page, or a page's `totals` count only what it shows."""
        found = harness.pages(
            lambda **paging: tools_relationships.dependencies(self.session, "beta-app", **paging)
        )
        self.assertEqual(
            [page["items"] for page in found], [[{**NPM_EDGE, "repository": "alpha-lib"}], [GAMMA]]
        )
        self.assertEqual(
            [page["totals"] for page in found], [{"depends_on": 1, "outside_estate": 1}] * 2
        )

    def test_dependents_take_only_their_own_cursor(self):
        """Catches: `cursor` not followed over beta-app's two consumers, so the
        second page repeats alpha-lib, loses eta-portal, or restarts; a page's
        `total` counting only what it shows; or `cursor` not judged against the
        dependents listing, so a page cursor from beta-app's dependencies is
        followed into alpha-lib's dependents instead of refused."""
        found = harness.pages(
            lambda **paging: tools_relationships.dependents(self.session, "beta-app", **paging)
        )
        self.assertEqual(
            [page["items"] for page in found],
            [
                [{**ALPHA_ON_BETA, "repository": "alpha-lib"}],
                [{**ETA_ON_BETA, "repository": "eta-portal"}],
            ],
        )
        self.assertEqual([(page["page"], page["total"]) for page in found], [(1, 2), (2, 2)])
        other = tools_relationships.dependencies(self.session, "beta-app", cap_bytes=1)
        self.assertIsNotNone(other["next_page"])
        with self.assertRaises(results.CursorRefused):
            tools_relationships.dependents(
                self.session, "alpha-lib", cursor=other["next_page"], cap_bytes=1
            )

    def test_repo_answer_over_the_cap_pages_the_library_file(self):
        """Catches: the library's by-repo file not split as written when it is over
        the cap - its fields other than the two lists lost from a page, or an
        edge not named by list and position - or `cursor` not followed over
        beta-app's three edges, `depends_on` then `depended_on_by`, so a page
        repeats one, drops one, or loses its place across the two lists; or
        `cursor` not judged against this listing."""
        found = harness.pages(
            lambda **paging: tools_relationships.repo_answer(self.session, "beta-app", **paging)
        )
        self.assertEqual(
            [page["items"] for page in found],
            [
                [
                    {
                        "list": "depends_on",
                        "position": 0,
                        "edge": {**NPM_EDGE, "repository": "alpha-lib"},
                    }
                ],
                [
                    {
                        "list": "depended_on_by",
                        "position": 0,
                        "edge": {**ALPHA_ON_BETA, "repository": "alpha-lib"},
                    }
                ],
                [
                    {
                        "list": "depended_on_by",
                        "position": 1,
                        "edge": {**ETA_ON_BETA, "repository": "eta-portal"},
                    }
                ],
            ],
        )
        self.assertEqual(
            [page["totals"] for page in found], [{"depends_on": 1, "depended_on_by": 2}] * 3
        )
        for page in found:
            with self.subTest(page=page["page"]):
                self.assertEqual(page["relationships"]["commit"], SHAS["beta-app"])
                self.assertEqual(page["relationships"]["repository"], "beta-app")
                self.assertNotIn("depends_on", page["relationships"])
                self.assertNotIn("depended_on_by", page["relationships"])
        other = tools_relationships.dependencies(self.session, "beta-app", cap_bytes=1)
        with self.assertRaises(results.CursorRefused):
            tools_relationships.repo_answer(
                self.session, "beta-app", other["next_page"], cap_bytes=1
            )

    def test_a_repository_with_no_edges_is_in_the_estate_and_answered_empty(self):
        """Catches: delta-deploy - a clone with no package manifest, whose by-repo
        file has two empty lists - answered as not in the estate, or
        given another repository's edges."""
        for call in (tools_relationships.dependents, tools_relationships.dependencies):
            with self.subTest(call=call.__name__):
                found = call(self.session, "delta-deploy")
                self.assertEqual(found["repository"], known("delta-deploy"))
                self.assertEqual(found["items"], [])
                self.assertEqual(found["total"], 0)
        answer = tools_relationships.repo_answer(self.session, "delta-deploy")
        self.assertEqual(answer["repository"], known("delta-deploy"))
        self.assertEqual(answer["totals"], {"depends_on": 0, "depended_on_by": 0})
        self.assertEqual(answer["relationships"]["depends_on"], [])
        self.assertEqual(answer["relationships"]["depended_on_by"], [])
        self.assertEqual(answer["relationships"]["commit"], SHAS["delta-deploy"])


RelationshipsWithIndex, RelationshipsWithoutIndex = harness.both_modes(RelationshipsContract)


if __name__ == "__main__":
    unittest.main()
