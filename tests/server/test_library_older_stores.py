"""Older stores stay readable: every tool over stores older library releases wrote.

Real stores can lag the library by a format minor: manifest format 1.3, built
before the `json-schema` stage and before #510, is one such, while the contract
stores move with each release. These tests serve the frozen stores in
`library_store/frozen/` - the same inputs as the main store, built by v0.18.3
and by v0.19.0 (`generate.py --release`) - over the wire, in both read modes,
so a server change that reads only the newest release's shapes, or refuses an
older format, fails here.

What each release has, derived from its own source before the store is read:

- **v0.18.3** writes manifest format 1.3 (`store_manifest.FORMAT`). Its
  `store_manifest.LAYERS` are 17, from `graph` to `index`, with no
  `minimum_standard`, and `compute` adds `deployments`: 18 names, frozen in
  `V0_18_3`. Its `cli.STAGES` have no `json-schema` and no
  `minimum-standard*`, so the graph holds no `jsonschema:` node and the
  manifest does not name `minimum_standard` at all.
- **v0.19.0** writes format 1.4. Its `store_manifest.LAYERS` add
  `minimum_standard` after `relationships`: 18, and with `deployments` 19
  names, frozen in `V0_19_0`. It has `json-schema`, so the schema's root and
  every definition and property under it are nodes, one per JSON pointer of
  `generate.ALPHA_SCHEMA`.
- In both, `provided_by` runs from the package node to `_anchor_under(reps,
  repo, prefix)` (`build_package_edges`, the same function at both tags): the
  lowest id among the representatives of alpha-lib's files whose path starts
  with the manifest's directory. alpha-lib's `package.json` is at its root, so
  the prefix is "" and every file counts. graphify 0.9.93 gives the root
  `index.ts` the bare stem `index` (`extractors/base._file_stem`), so its file
  node is `index`, which sorts below `package`, `pkg:`, `ref_*` and `src_*`.
  No `jsonschema:` id competes: v0.18.3 has no `json-schema` stage, and in
  v0.19.0 `packages` runs before it. So both anchor on `alpha-lib::index`,
  where v0.19.1 (#510) takes the manifest's own node, `alpha-lib::package`
  (`test_library_graph`).
- In both, `beta-app` alone declares `@example/alpha-lib`, so it is alpha-lib's
  one dependent, and DEMO-1's commits landed `package.json` and `src/refund.ts`
  (twice) in alpha-lib and `package.json` and `src/pay.ts` in beta-app
  (`generate.COMMITS`).

Literals are written only where the inputs and the release's source settle
them: the format, the layer names, a node id graphify 0.9.93 gives every
release alike, the `provided_by` anchor each release's rule picks, the JSON
pointers, the repositories. Which community holds a node is graphify's choice,
so no test names one. Nothing is read back from a store as an expected value.
"""

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from mcp import Client
from mcp_types import TextContent

from knowledgestore.server import server
from knowledgestore.server.server import KnowledgeStoreServer
from library_store.generate import frozen
from library_store.harness import NOW, SHAS

REPOSITORIES = ["alpha-lib", "beta-app", "delta-deploy", "eta-portal"]

# graphify 0.9.93's id for `refund` in alpha-lib's `src/refund.ts`, under the
# merge prefix: the same in every release, which all run that graphify.
REFUND = "alpha-lib::src_refund_refund"
# The `packages` stage's node for the package alpha-lib publishes, in every
# release since before v0.18.3: `<provider>::pkg:<name>`.
PACKAGE = "alpha-lib::pkg:@example/alpha-lib"
# graphify 0.9.93's file node for alpha-lib's root `index.ts`: the lowest id in
# alpha-lib, where v0.18.3 and v0.19.0 anchor `provided_by`.
INDEX = "alpha-lib::index"
# The json-schema stage's ids for `generate.ALPHA_SCHEMA`: the root, then each
# definition and property, by JSON pointer.
SCHEMA = "alpha-lib::jsonschema:schemas/refund.schema.json#"
SCHEMA_NODES = [
    SCHEMA,
    SCHEMA + "/$defs/credit",
    SCHEMA + "/$defs/money",
    SCHEMA + "/$defs/money/properties/amount",
    SCHEMA + "/properties/requestedOn",
    SCHEMA + "/properties/total",
]
SCHEMA_URL = (
    "https://github.com/example-org/alpha-lib/blob/"
    + SHAS["alpha-lib"]
    + "/schemas/refund.schema.json"
)

# One call per tool, with arguments every release's store answers. `community`
# is given the community `node` reports for `REFUND`, filled in at run time.
CALLS: dict[str, dict[str, Any]] = {
    "store_info": {},
    "find_repo": {"name": "alpha-lib"},
    "search": {"text": "refund"},
    "node": {"id": REFUND},
    "neighbours": {"id": REFUND},
    "community": {},
    "repo_answer": {"repo": "alpha-lib"},
    "dependents": {"target": "alpha-lib"},
    "dependencies": {"repo": "beta-app"},
    "tickets": {"repo": "alpha-lib"},
    "ticket": {"key": "DEMO-1"},
    "answers": {"query": "refund"},
    "minimum_standard": {},
}


@dataclass(frozen=True)
class Release:
    """What a release's store holds, from that release's source."""

    version: str
    format: dict[str, int]
    layers: tuple[str, ...]
    json_schema: bool
    # The node `provided_by` leads to from alpha-lib's package (see above), and
    # the file graphify made it for: a file node is labelled with its file name.
    provided_by: str
    provided_by_file: str


# v0.18.3's `store_manifest.LAYERS`, in its order, then `deployments`.
V0_18_3 = Release(
    "0.18.3",
    {"major": 1, "minor": 3},
    (
        "graph",
        "labels",
        "provenance",
        "repository_manifest",
        "context",
        "content_files",
        "history",
        "intent",
        "ticket_descriptions",
        "ticket_tracker",
        "summaries",
        "topics",
        "deep_dives",
        "semantic",
        "explorer",
        "relationships",
        "index",
        "deployments",
    ),
    json_schema=False,
    provided_by=INDEX,
    provided_by_file="index.ts",
)
# v0.19.0's `store_manifest.LAYERS`, in its order, then `deployments`.
V0_19_0 = Release(
    "0.19.0",
    {"major": 1, "minor": 4},
    (
        "graph",
        "labels",
        "provenance",
        "repository_manifest",
        "context",
        "content_files",
        "history",
        "intent",
        "ticket_descriptions",
        "ticket_tracker",
        "summaries",
        "topics",
        "deep_dives",
        "semantic",
        "explorer",
        "relationships",
        "minimum_standard",
        "index",
        "deployments",
    ),
    json_schema=True,
    provided_by=INDEX,
    provided_by_file="index.ts",
)

# DEMO-1's landed files, from `generate.COMMITS`: the same in every release.
DEMO_1_LANDED = [
    {"commits": 1, "path": "package.json", "repository": "alpha-lib"},
    {"commits": 2, "path": "src/refund.ts", "repository": "alpha-lib"},
    {"commits": 1, "path": "package.json", "repository": "beta-app"},
    {"commits": 1, "path": "src/pay.ts", "repository": "beta-app"},
]


class OlderStoreCase(unittest.IsolatedAsyncioTestCase):
    """The server over one frozen store, called over the wire in-process."""

    RELEASE: Release
    STORE: Path
    INDEXED: bool

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name).resolve() / "cache"

    async def calls(self, *requests: tuple[str, dict[str, Any]]) -> list[tuple[dict, bool]]:
        """Each request's decoded answer and whether it is an error result, from
        one server over `STORE`."""
        served = KnowledgeStoreServer(self.STORE, self.cache, clock=lambda: NOW)
        self.addCleanup(served.close)
        found = []
        async with Client(served.server, mode="legacy") as client:
            for tool, arguments in requests:
                result = await client.call_tool(tool, arguments)
                self.assertEqual(len(result.content), 1)
                block = result.content[0]
                assert isinstance(block, TextContent)
                found.append((json.loads(block.text), bool(result.is_error)))
        return found

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict:
        """One answer, which must not be an error result."""
        [(answer, is_error)] = await self.calls((tool, arguments))
        self.assertFalse(is_error, answer)
        return answer


# The mixin is `OlderStoreCase` to the type checker and `object` at run time, so
# it is never collected and run on its own, with no store (as `harness.Contract`).
if TYPE_CHECKING:
    Contract = OlderStoreCase
else:
    Contract = object


class OlderStoreContract(Contract):
    """The contract each frozen store keeps, made into test classes by `_forms`."""

    def test_the_manifest_records_the_release_and_its_layers(self):
        """Catches: a frozen store regenerated by another release, or by a
        pipeline that ran a stage the release lacks or skipped one it has, so
        these tests no longer describe the older store a real estate holds."""
        manifest = json.loads((self.STORE / "knowledge/store-manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["library_version"], self.RELEASE.version)
        self.assertEqual(manifest["format"], self.RELEASE.format)
        self.assertEqual(
            {name: layer["present"] for name, layer in manifest["layers"].items()},
            {name: self.INDEXED or name != "index" for name in self.RELEASE.layers},
        )

    async def test_every_tool_answers_without_an_error(self):
        """Catches: any tool failing on an older store - a read that assumes the
        newest release's layers, fields or format, so a real store that lags the
        library is answered with an error; and a tool added with no call here."""
        self.assertEqual(sorted(CALLS), sorted(server.BY_NAME))
        node = await self.call("node", CALLS["node"])
        self.assertEqual(node["node"]["id"], REFUND)
        requests = [
            (tool, {"id": node["node"]["community"]} if tool == "community" else arguments)
            for tool, arguments in CALLS.items()
        ]
        for (tool, _), (answer, is_error) in zip(requests, await self.calls(*requests)):
            with self.subTest(tool=tool):
                self.assertFalse(is_error, answer)
                self.assertEqual(answer["store"]["library_version"], self.RELEASE.version)

    async def test_dependents_and_a_ticket_answer_from_the_inputs(self):
        """Catches: an older store's relationships or intent layers read as empty
        rather than failing - every tool answering, with nothing in the answer:
        alpha-lib's one dependent, beta-app, or DEMO-1's landed files lost."""
        dependents = await self.call("dependents", {"target": "alpha-lib"})
        self.assertEqual([item["repository"] for item in dependents["items"]], ["beta-app"])
        ticket = await self.call("ticket", {"key": "DEMO-1"})
        self.assertEqual(ticket["landed"], DEMO_1_LANDED)
        self.assertEqual(ticket["proposed"], [])

    async def test_store_info_reads_every_layer_and_leaves_none_unread(self):
        """Catches: an older store's layers reported unread or unrecognised, as a
        layer from a newer minor is; or a layer the release never wrote - v0.18.3's
        `minimum_standard` - invented as present, unread or failing rather than
        left out, as the store's manifest leaves it."""
        answer = await self.call("store_info", {})
        self.assertEqual(answer["unread"], [])
        self.assertEqual(answer["store"]["format"], self.RELEASE.format)
        self.assertEqual(
            answer["layers"],
            {name: {"present": self.INDEXED or name != "index"} for name in self.RELEASE.layers},
        )
        self.assertEqual(answer["index"]["fallback"] is None, self.INDEXED)

    async def test_minimum_standard_reads_absent_or_answers(self):
        """Catches: a store without the minimum-standard layer answered with an
        error, or as unrecognised, rather than as absent (v0.18.3); or a v0.19.0
        layer, written before the `now=` seam, not read for every repository."""
        answer = await self.call("minimum_standard", {})
        if "minimum_standard" in self.RELEASE.layers:
            self.assertEqual([item["repository"] for item in answer["items"]], REPOSITORIES)
            self.assertEqual(answer["store"]["layers_used"], ["minimum_standard"])
        else:
            self.assertEqual(answer["items"], [])
            self.assertEqual(
                answer["searched"], "searched no layer; the minimum_standard layer is absent"
            )
            self.assertEqual(answer["store"]["layers_used"], [])

    async def test_a_package_is_provided_by_the_node_its_release_anchored(self):
        """Catches: the `provided_by` edge dropped or read the wrong way round, or
        a read that follows only v0.19.1's anchor - the manifest's own node - so
        the edge an older release anchored on alpha-lib's lowest code node,
        `index`, is lost; or that node not served. The package node leads out to
        exactly the node the release's own rule picks."""
        found = await self.call("neighbours", {"id": PACKAGE, "relation": "provided_by"})
        self.assertEqual(found["node"]["id"], PACKAGE)
        self.assertEqual(
            [(item["depth"], item["node"]["id"], item["via"]) for item in found["items"]],
            [
                (
                    1,
                    self.RELEASE.provided_by,
                    [{"from": PACKAGE, "relation": "provided_by", "direction": "out"}],
                )
            ],
        )
        served = await self.call("node", {"id": self.RELEASE.provided_by})
        file = self.RELEASE.provided_by_file
        self.assertEqual(
            (served["node"]["repo"], served["node"]["file"], served["node"]["label"]),
            ("alpha-lib", file, PurePosixPath(file).name),
        )

    async def test_json_schema_nodes_are_served_where_the_release_wrote_them(self):
        """Catches: JSON Schema nodes not served from a release that wrote them
        (v0.19.0), or a store from one that did not (v0.18.3) answered with an
        error rather than "no such node"."""
        answers = await self.calls(*(("node", {"id": node_id}) for node_id in SCHEMA_NODES))
        for node_id, (answer, is_error) in zip(SCHEMA_NODES, answers):
            with self.subTest(node=node_id):
                self.assertFalse(is_error, answer)
                if self.RELEASE.json_schema:
                    self.assertEqual(answer["node"]["id"], node_id)
                    self.assertEqual(answer["node"]["url"].split("#")[0], SCHEMA_URL)
                else:
                    self.assertIsNone(answer["node"])


def _forms(release: Release) -> tuple[type, type]:
    """`OlderStoreContract` over `release`'s frozen stores: with its index, and
    without, where the graph answers."""
    stores = frozen(release.version)
    tag = release.version.replace(".", "_")
    return (
        type(
            f"V{tag}Indexed",
            (OlderStoreContract, OlderStoreCase),
            {"RELEASE": release, "STORE": stores.store, "INDEXED": True, "__module__": __name__},
        ),
        type(
            f"V{tag}Unindexed",
            (OlderStoreContract, OlderStoreCase),
            {
                "RELEASE": release,
                "STORE": stores.no_index,
                "INDEXED": False,
                "__module__": __name__,
            },
        ),
    )


V0_18_3Indexed, V0_18_3Unindexed = _forms(V0_18_3)
V0_19_0Indexed, V0_19_0Unindexed = _forms(V0_19_0)


if __name__ == "__main__":
    unittest.main()
