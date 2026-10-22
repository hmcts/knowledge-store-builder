"""The packages stage: cross-repository npm edges, grounded in committed files."""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import build_deployments as deployments  # noqa: E402
from knowledgestore import build_package_edges as packages  # noqa: E402
from knowledgestore import relationships  # noqa: E402

try:  # PyYAML is the `deploy` extra, not a runtime dependency of this library
    import yaml  # noqa: F401

    HAS_YAML = True
except ImportError:  # pragma: no cover - the default-install CI job takes this path
    HAS_YAML = False

needs_yaml = unittest.skipUnless(HAS_YAML, "needs the `deploy` extra (PyYAML)")


def _store(tmp: Path) -> None:
    """A provider publishing @acme/core from projects/core, and a consumer app."""
    provider = tmp / "repositories" / "corelib"
    (provider / ".git").mkdir(parents=True)
    (provider / "projects" / "core").mkdir(parents=True)
    (provider / "projects" / "core" / "package.json").write_text(
        json.dumps({"name": "@acme/core", "version": "0.0.0-PLACEHOLDER"}), encoding="utf-8"
    )
    (provider / "package.json").write_text(json.dumps({"name": "corelib.workspace"}))

    app = tmp / "repositories" / "app"
    (app / ".git").mkdir(parents=True)
    (app / "src").mkdir(parents=True)
    (app / "package.json").write_text(
        json.dumps({"name": "app", "dependencies": {"@acme/core": "^1.0.0"}}), encoding="utf-8"
    )
    (app / "src" / "main.ts").write_text("import { Widget } from '@acme/core';\n", encoding="utf-8")
    (app / "src" / "other.ts").write_text("export const x = 1;\n", encoding="utf-8")

    (tmp / "graphify-out").mkdir()
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {"id": "app::0", "label": "MainThing", "repo": "app", "source_file": "src/main.ts"},
            {"id": "app::9", "label": "AlsoMain", "repo": "app", "source_file": "src/main.ts"},
            {
                "id": "corelib::4",
                "label": "Widget",
                "repo": "corelib",
                "source_file": "projects/core/src/widget.ts",
            },
        ],
        "links": [],
    }
    (tmp / "graphify-out" / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    config.configure(root=str(tmp))


class PackageEdgesTest(SettingsIsolated):
    def test_shared_package_becomes_a_cited_node_with_cited_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store(Path(tmp))
            self.assertEqual(packages.main(), 0)
            graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))

        pkg = [n for n in graph["nodes"] if n.get("metadata", {}).get("kind") == "package"]
        self.assertEqual(len(pkg), 1)
        node = pkg[0]
        self.assertEqual(node["label"], "@acme/core")
        self.assertEqual(node["repo"], "corelib", "the package belongs to its provider")
        self.assertEqual(
            node["source_file"],
            "projects/core/package.json",
            "the node cites the manifest that declares the package",
        )
        self.assertEqual(node["metadata"]["consumers"], [{"repo": "app", "import_sites": 1}])

        imports = [e for e in graph["links"] if e.get("relation") == "imports_package"]
        self.assertEqual(len(imports), 1)
        self.assertEqual(
            imports[0]["source"], "app::0", "deterministic representative: lowest node id"
        )
        self.assertEqual(imports[0]["target"], node["id"])
        self.assertEqual(imports[0]["source_file"], "src/main.ts", "the edge cites the import")
        self.assertEqual(imports[0]["confidence"], "EXTRACTED")

        provided = [e for e in graph["links"] if e.get("relation") == "provided_by"]
        self.assertEqual(len(provided), 1)
        self.assertEqual(provided[0]["target"], "corelib::4")

    def test_rerun_replaces_the_layer_rather_than_duplicating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store(Path(tmp))
            self.assertEqual(packages.main(), 0)
            once = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
            self.assertEqual(packages.main(), 0)
            twice = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(once["nodes"]), len(twice["nodes"]))
        self.assertEqual(len(once["links"]), len(twice["links"]))

    def test_a_repo_owning_the_package_gets_no_self_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _store(root)
            # the provider also lists its own package (workspace convention)
            provider_manifest = root / "repositories" / "corelib" / "package.json"
            provider_manifest.write_text(
                json.dumps({"name": "corelib.workspace", "dependencies": {"@acme/core": "*"}}),
                encoding="utf-8",
            )
            self.assertEqual(packages.main(), 0)
            graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
        pkg = [n for n in graph["nodes"] if n.get("metadata", {}).get("kind") == "package"][0]
        self.assertEqual(
            [c["repo"] for c in pkg["metadata"]["consumers"]],
            ["app"],
            "self-consumption is not a cross-repository relationship",
        )

    def test_gz_artefact_is_rewritten_alongside(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store(Path(tmp))
            self.assertEqual(packages.main(), 0)
            gz = config.GRAPH_PATH.with_suffix(".json.gz")
            self.assertTrue(gz.exists(), "the committed form must never lag the working form")


# --- one parser, two consumers -------------------------------------------------
#
# The graph's module, chart and package edges come from the same parsers as the
# relationships layer (#455). The golden file below was written by the graph stages
# *before* they moved onto those parsers, over the relationships fixture estate and
# the shapes the other stage tests use. An equivalence scenario must still produce
# it exactly; a declared-change scenario holds the old output too, and its test
# names, by hand, the edges that changed and why.

ESTATE = Path(__file__).resolve().parent / "fixtures" / "relationships"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "graph-edges-golden.json"
RELATIONS = ("USES_MODULE", "uses_chart", "imports_package", "provided_by")
LAYER_KINDS = ("terraform_module", "chart_source", "package")


def _module(name: str, source: str) -> str:
    return f'module "{name}" {{\n  source = "{source}"\n}}\n'


def _release(name: str, chart: str, source_kind: str, source: str) -> str:
    return (
        "apiVersion: helm.toolkit.fluxcd.io/v2\n"
        "kind: HelmRelease\n"
        "metadata:\n"
        f"  name: {name}\n"
        "spec:\n"
        "  chart:\n"
        "    spec:\n"
        f"      chart: {chart}\n"
        "      version: 1.0.0\n"
        "      sourceRef:\n"
        f"        kind: {source_kind}\n"
        f"        name: {source}\n"
    )


_CLUSTER = (
    "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
    "kind: Kustomization\n"
    "metadata:\n"
    "  name: apps\n"
    "spec:\n"
    "  path: ./apps/environments/prod\n"
)
_PATCH = (
    "apiVersion: helm.toolkit.fluxcd.io/v2\n"
    "kind: HelmRelease\n"
    "metadata:\n"
    "  name: {name}\n"
    "spec:\n"
    "  values:\n"
    "    replicaCount: 2\n"
)


def _flux(releases: dict[str, str]) -> dict[str, str]:
    """A deployment clone's Flux tree: one base release per entry, patched in prod."""
    tree = {"example-deploy/clusters/prod/cluster-a/apps.yaml": _CLUSTER}
    for name, text in releases.items():
        tree[f"example-deploy/apps/base/services/{name}/helmrelease.yaml"] = text
        tree[f"example-deploy/apps/environments/prod/services/{name}.yaml"] = _PATCH.format(
            name=name
        )
    return tree


_EMPTY_MODULE = 'variable "x" {}\n'

# name -> (copy the relationships fixture estate?, files by store-relative path
# under repositories/, settings). Every clone gets a `.git`.
SCENARIOS: dict[str, tuple[bool, dict[str, str], dict[str, object]]] = {
    # The Task 1 fixture estate, with the two module repositories it names cloned,
    # import sites for its shared package, and a consumer that names the package
    # only as a peer, which the graph does not read.
    "estate": (
        True,
        {
            "tf-network/main.tf": _EMPTY_MODULE,
            "tf-storage/main.tf": _EMPTY_MODULE,
            "svc-b/src/app.ts": "import { Button } from '@example/ui';\n",
            "svc-d/package.json": json.dumps(
                {"name": "svc-d", "peerDependencies": {"@example/ui": "*"}}
            ),
            "svc-d/src/view.ts": "import { Button } from '@example/ui';\n",
        },
        # Matching the sources' organisation in another case changes nothing.
        {"GITHUB_ORG": "EXAMPLE-ORG"},
    ),
    # `_store` above: a workspace package under projects/, and its consumer.
    "workspace-package": (
        False,
        {
            "corelib/projects/core/package.json": json.dumps({"name": "@acme/core"}),
            "corelib/projects/core/src/widget.ts": "export class Widget {}\n",
            "corelib/package.json": json.dumps(
                {"name": "corelib.workspace", "dependencies": {"@acme/core": "*"}}
            ),
            "app/package.json": json.dumps(
                {"name": "app", "devDependencies": {"@acme/core": "^1.0.0"}}
            ),
            "app/src/main.ts": "import { Widget } from '@acme/core';\n",
            "app/src/other.ts": "export const x = 1;\n",
        },
        {},
    ),
    # Every source form `test_terraform_module_edges` reads, each in a module block,
    # and the forms that name no repository.
    "terraform-forms": (
        False,
        {
            "consumer/a.tf": _module("scp", "git@github.com:org/mod-scp?ref=master"),
            "consumer/b.tf": _module("https", "git::https://github.com/org/mod-https.git?ref=v1"),
            "consumer/c.tf": _module("bare", "github.com/org/mod-bare"),
            "consumer/d.tf": _module("ssh", "ssh://git@github.com/org/mod-ssh"),
            "consumer/e.tf": _module("registry", "hashicorp/consul/aws")
            + _module("local", "../modules/network")
            + _module("lookalike", "notgithub.com/org/mod-scp"),
            "consumer/.terraform/modules/x/main.tf": _module("cached", "github.com/org/mod-cached"),
            "consumer/self.tf": _module("self", "github.com/org/consumer"),
            "mod-scp/main.tf": _EMPTY_MODULE,
            "mod-https/main.tf": _EMPTY_MODULE,
        },
        {},
    ),
    # `test_flux_kustomize_deployments`: a chart from a source the estate does not
    # hold, a chart from a clone that defines it, and a module beside them.
    "flux": (
        False,
        {
            **_flux(
                {
                    "alpha": _release("alpha", "alpha", "GitRepository", "example-charts"),
                    "web": _release("web", "chart-web", "GitRepository", "chart-web"),
                }
            ),
            "chart-web/Chart.yaml": "apiVersion: v2\nname: chart-web\nversion: 0.3.0\n",
            "svc-alpha/main.tf": _module("charts", "git@github.com:org/example-charts"),
        },
        {"DEPLOY_REPOS": {"example-deploy"}},
    ),
    # Declared: a source in another organisation is not the estate's repository of
    # the same name, and so is not a self-reference either.
    "org-check": (
        False,
        {
            "svc-x/net.tf": _module("net", "github.com/example-org/tf-network"),
            "svc-x/other.tf": _module("other", "github.com/other-org/tf-network"),
            "svc-x/storage.tf": _module("storage", "github.com/Example-Org/tf-storage"),
            "svc-x/self.tf": _module("self", "github.com/other-org/svc-x"),
            "tf-network/main.tf": _EMPTY_MODULE,
            "tf-storage/main.tf": _EMPTY_MODULE,
        },
        {"GITHUB_ORG": "example-org"},
    ),
    # Declared: only a module block's own `source` is a module source, and one
    # built by interpolation names nothing the reader can trust.
    "module-block-only": (
        False,
        {
            "svc-x/main.tf": (
                'source = "git@github.com:example-org/tf-network"\n'
                'resource "aws_s3_object" "o" {\n'
                '  source = "github.com/example-org/tf-queue"\n'
                "}\n"
                'module "commented" {\n'
                '  # source = "github.com/example-org/tf-dns"\n'
                '  source = "../local"\n'
                "}\n"
                + _module(
                    "interpolated", "git::https://github.com/example-org/tf-cache.git?ref=${var.v}"
                )
                + _module("kept", "github.com/example-org/tf-storage")
            ),
            "tf-network/main.tf": _EMPTY_MODULE,
            "tf-queue/main.tf": _EMPTY_MODULE,
            "tf-dns/main.tf": _EMPTY_MODULE,
            "tf-cache/main.tf": _EMPTY_MODULE,
            "tf-storage/main.tf": _EMPTY_MODULE,
        },
        {},
    ),
    # Declared: a credential in the URL no longer hides the repository it names.
    "credentialed": (
        False,
        {
            "svc-x/main.tf": _module(
                "private",
                "git::https://x-access-token:xxxxx-not-a-real-token@github.com/example-org/tf-network.git?ref=v1",
            ),
            "tf-network/main.tf": _EMPTY_MODULE,
        },
        {},
    ),
    # Declared: a chart resolves to the clone that defines it, or to the clone the
    # store's naming pattern names, before falling back to the `sourceRef`.
    "chart-resolution": (
        False,
        {
            **_flux(
                {
                    "web": _release("web", "web", "HelmRepository", "example-helm"),
                    "api": _release("api", "api", "HelmRepository", "example-helm"),
                    "jobs": _release("jobs", "jobs", "HelmRepository", "example-helm"),
                }
            ),
            "web-chart-repo/charts/web/Chart.yaml": "apiVersion: v2\nname: web\nversion: 1.0.0\n",
            "chart-api/README.md": "The api chart.\n",
        },
        {"DEPLOY_REPOS": {"example-deploy"}, "CHART_REPO_PATTERN": "chart-{name}"},
    ),
}


# A release whose values hold a policy rule with a bare `=`, which PyYAML's safe
# loader refuses, and the clone defining its chart. No golden entry: the graph
# could not parse this release before, so it had no edge to compare against.
_BARE_EQUALS_RELEASE = _release("web", "web", "HelmRepository", "example-helm") + (
    "  values:\n    criteria:\n      - key: namespace\n        op: =\n        value: '*'\n"
)
SCENARIOS["bare-equals"] = (
    False,
    {
        **_flux({"web": _BARE_EQUALS_RELEASE}),
        "charts-repo/web/Chart.yaml": "apiVersion: v2\nname: web\nversion: 1.0.0\n",
    },
    {"DEPLOY_REPOS": {"example-deploy"}},
)


# A scenario's `config/repositories.txt`, for one whose clones come from more than
# one organisation. No golden entry: the stages before the move had no such check.
REPOSITORY_LISTS = {
    "two-organisations": (
        "shared-mod|git@github.com:Other-Org/shared-mod.git|main\n"
        "svc-x|git@github.com:example-org/svc-x.git|main\n"
        "tf-storage|https://git.example.com/infra/tf-storage.git|main\n"
    ),
}
SCENARIOS["two-organisations"] = (
    False,
    {
        "svc-x/a-theirs.tf": _module("theirs", "github.com/other-org/shared-mod"),
        "svc-x/b-wrong.tf": _module("wrong", "github.com/example-org/shared-mod"),
        "svc-x/c-net.tf": _module("net", "github.com/example-org/tf-network"),
        "svc-x/d-store.tf": _module("store", "github.com/example-org/tf-storage"),
        "shared-mod/main.tf": _EMPTY_MODULE,
        "tf-network/main.tf": _EMPTY_MODULE,
        "tf-storage/main.tf": _EMPTY_MODULE,
    },
    {"GITHUB_ORG": "example-org"},
)


# A scenario's clones the graph does not hold: on disk, with no node in the graph.
UNGRAPHED = {"chart-estate": {"charts-unheld"}}

_NO_SOURCE_REF = (
    "apiVersion: helm.toolkit.fluxcd.io/v2\n"
    "kind: HelmRelease\n"
    "metadata:\n"
    "  name: jobs\n"
    "spec:\n"
    "  chart:\n"
    "    spec:\n"
    "      chart: jobs\n"
    "      version: 1.0.0\n"
)
SCENARIOS["chart-estate"] = (
    False,
    {
        **_flux(
            {
                "web": _release("web", "web", "HelmRepository", "example-helm"),
                "api": _release("api", "api", "HelmRepository", "example-helm"),
                "jobs": _NO_SOURCE_REF,
            }
        ),
        "charts-held/web/Chart.yaml": "apiVersion: v2\nname: web\nversion: 1.0.0\n",
        "charts-held/jobs/Chart.yaml": "apiVersion: v2\nname: jobs\nversion: 1.0.0\n",
        "charts-unheld/api/Chart.yaml": "apiVersion: v2\nname: api\nversion: 1.0.0\n",
    },
    {"DEPLOY_REPOS": {"example-deploy"}},
)


@contextlib.contextmanager
def _scenario_store(name: str):
    """Run `packages`, then `deployments` when a deployment clone is named, over the
    scenario, and yield the store root they wrote into."""
    copy_estate, files, settings = SCENARIOS[name]
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        repos = root / "repositories"
        if name in REPOSITORY_LISTS:
            (root / "config").mkdir()
            (root / "config" / "repositories.txt").write_text(
                REPOSITORY_LISTS[name], encoding="utf-8"
            )
        if copy_estate:
            shutil.copytree(ESTATE, repos)
        for rel, text in files.items():
            path = repos / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        nodes = []
        for clone in sorted(repos.iterdir()):
            (clone / ".git").mkdir(exist_ok=True)
            if clone.name in UNGRAPHED.get(name, ()):
                continue
            for path in sorted(clone.rglob("*")):
                if path.is_file() and ".git" not in path.relative_to(clone).parts:
                    rel = path.relative_to(clone).as_posix()
                    nodes.append(
                        {"id": f"{clone.name}::{rel}", "repo": clone.name, "source_file": rel}
                    )
        (root / "graphify-out").mkdir()
        graph = {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": []}
        (root / "graphify-out" / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
        config.configure(root=root, **settings)
        with contextlib.redirect_stdout(io.StringIO()):
            if packages.main() != 0:
                raise AssertionError(f"{name}: packages failed")
            if settings.get("DEPLOY_REPOS") and deployments.main() != 0:
                raise AssertionError(f"{name}: deployments failed")
        yield root


def _scenario_graph(name: str) -> dict:
    """The graph `_scenario_store` wrote for the scenario."""
    with _scenario_store(name):
        return json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))


def scenario_edges(name: str) -> dict[str, list[list]]:
    """The scenario's dependency edges and the nodes they point at, as sorted lists."""
    graph = _scenario_graph(name)
    edges = sorted(
        [edge["relation"], edge["source"], edge["target"], edge["source_file"]]
        for edge in graph["links"]
        if edge["relation"] in RELATIONS
    )
    nodes = sorted(
        [node["id"], node["repo"], node["metadata"].get("provider_in_estate")]
        for node in graph["nodes"]
        if (node.get("metadata") or {}).get("kind") in LAYER_KINDS
    )
    return {"edges": edges, "nodes": nodes}


def _golden(name: str) -> dict[str, list[list]]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))[name]


class TheGraphMatchesItsOutputBeforeTheMoveTest(SettingsIsolated):
    """Break caught: the graph stages reading a declaration differently once they
    read it through `relationships` - a dependency set widened to peers, a
    workspace publisher lost, a source form or the self-reference rule dropped. The
    golden file is the stages' own output from before the move."""

    def test_the_relationships_fixture_estate(self):
        self.assertEqual(scenario_edges("estate"), _golden("estate"))

    def test_a_workspace_package_and_its_consumer(self):
        self.assertEqual(scenario_edges("workspace-package"), _golden("workspace-package"))

    def test_every_terraform_source_form(self):
        self.assertEqual(scenario_edges("terraform-forms"), _golden("terraform-forms"))

    @needs_yaml
    def test_a_flux_tree_with_a_module_beside_it(self):
        self.assertEqual(scenario_edges("flux"), _golden("flux"))


class TheDeclaredGraphChangesTest(SettingsIsolated):
    """Each output change the move makes (CLAUDE.md 1.2), against the golden file.

    The expected difference is written by hand: what was there before is the golden
    entry, what changes is listed here and nothing else may.
    """

    def _changed(self, name: str) -> dict[str, list[list]]:
        before, after = _golden(name), scenario_edges(name)
        return {
            "edges added": sorted(e for e in after["edges"] if e not in before["edges"]),
            "edges removed": sorted(e for e in before["edges"] if e not in after["edges"]),
            "nodes added": sorted(n for n in after["nodes"] if n not in before["nodes"]),
            "nodes removed": sorted(n for n in before["nodes"] if n not in after["nodes"]),
        }

    def test_a_source_in_another_organisation_is_not_the_estates_repository(self):
        """Break caught: the organisation not compared, so `other-org/tf-network`
        is the estate's `tf-network` and `other-org/svc-x` reads as svc-x itself."""
        self.assertEqual(
            self._changed("org-check"),
            {
                "edges added": [
                    ["USES_MODULE", "svc-x::net.tf", "other-org/svc-x::module", "self.tf"],
                    ["USES_MODULE", "svc-x::net.tf", "other-org/tf-network::module", "other.tf"],
                ],
                "edges removed": [
                    ["USES_MODULE", "svc-x::net.tf", "tf-network::module", "other.tf"],
                ],
                "nodes added": [
                    ["other-org/svc-x::module", "", False],
                    ["other-org/tf-network::module", "", False],
                ],
                "nodes removed": [],
            },
        )

    def test_only_a_module_blocks_own_literal_source_is_a_module_source(self):
        """Break caught: a top-level or resource `source`, a commented-out one, or
        an interpolated one read as a module dependency."""
        self.assertEqual(
            self._changed("module-block-only"),
            {
                "edges added": [],
                "edges removed": [
                    ["USES_MODULE", "svc-x::main.tf", f"{repo}::module", "main.tf"]
                    for repo in ("tf-cache", "tf-dns", "tf-network", "tf-queue")
                ],
                "nodes added": [],
                "nodes removed": [
                    [f"{repo}::module", repo, True]
                    for repo in ("tf-cache", "tf-dns", "tf-network", "tf-queue")
                ],
            },
        )

    def test_a_credential_in_the_url_does_not_hide_the_repository(self):
        """Break caught: the GitHub form matched before the credential is stripped,
        so `https://user:token@github.com/...` names no repository at all."""
        self.assertEqual(
            self._changed("credentialed"),
            {
                "edges added": [["USES_MODULE", "svc-x::main.tf", "tf-network::module", "main.tf"]],
                "edges removed": [],
                "nodes added": [["tf-network::module", "tf-network", True]],
                "nodes removed": [],
            },
        )
        self.assertNotIn("xxxxx-not-a-real-token", json.dumps(_scenario_graph("credentialed")))

    @needs_yaml
    def test_a_chart_resolves_to_the_repository_that_provides_it(self):
        """Break caught: the graph resolving a chart by its `sourceRef` alone while
        the layer resolves it by the naming pattern and the defining clone, so the
        two name different repositories for one release."""
        expected = {
            "edges added": [
                ["uses_chart", f"example-deploy::deploy:{env}:{chart}", f"{repo}::chart", rel]
                for chart, repo in (("api", "chart-api"), ("web", "web-chart-repo"))
                for env in ("_base", "prod")
                for rel in [f"apps/base/services/{chart}/helmrelease.yaml"]
            ],
            "edges removed": [
                [
                    "uses_chart",
                    f"example-deploy::deploy:{env}:{chart}",
                    "example-helm::chart",
                    rel,
                ]
                for chart in ("api", "web")
                for env in ("_base", "prod")
                for rel in [f"apps/base/services/{chart}/helmrelease.yaml"]
            ],
            "nodes added": [
                ["chart-api::chart", "chart-api", True],
                ["web-chart-repo::chart", "web-chart-repo", True],
            ],
            "nodes removed": [],
        }
        self.assertEqual(
            self._changed("chart-resolution"),
            {change: sorted(entries) for change, entries in expected.items()},
        )


class EachCloneItsOwnOrganisationTest(SettingsIsolated):
    def test_a_source_is_compared_against_the_organisation_of_the_clone_it_names(self):
        """Break caught: every source compared against `KSB_GITHUB_ORG` alone, in a
        store holding clones from two organisations. Then `other-org/shared-mod`,
        which the store holds, becomes a module outside the estate, and
        `example-org/shared-mod`, which it does not, lands on the clone. A clone the
        list does not name, or names off GitHub, falls back to `KSB_GITHUB_ORG`."""
        self.assertEqual(
            scenario_edges("two-organisations"),
            {
                "edges": [
                    [
                        "USES_MODULE",
                        "svc-x::a-theirs.tf",
                        f"{provider}::module",
                        rel,
                    ]
                    for provider, rel in sorted(
                        [
                            ("example-org/shared-mod", "b-wrong.tf"),
                            ("shared-mod", "a-theirs.tf"),
                            ("tf-network", "c-net.tf"),
                            ("tf-storage", "d-store.tf"),
                        ],
                        key=lambda pair: f"{pair[0]}::module",
                    )
                ],
                "nodes": [
                    ["example-org/shared-mod::module", "", False],
                    ["shared-mod::module", "shared-mod", True],
                    ["tf-network::module", "tf-network", True],
                    ["tf-storage::module", "tf-storage", True],
                ],
            },
        )


class ChartEstateTest(SettingsIsolated):
    @needs_yaml
    def test_a_chart_is_in_the_estate_only_when_the_graph_holds_its_repository(self):
        """Break caught: a chart resolved to a clone on disk that the graph does not
        hold, written with `repo` set and `provider_in_estate: True` - a store
        claiming a repository it never extracted. Such a chart falls back to its
        `sourceRef`, outside the estate, as before the routing."""
        found = scenario_edges("chart-estate")
        charts = sorted([edge[1], edge[2]] for edge in found["edges"] if edge[0] == "uses_chart")
        self.assertEqual(
            charts,
            sorted(
                [f"example-deploy::deploy:{env}:{chart}", f"{provider}::chart"]
                for chart, provider in (("api", "example-helm"), ("web", "charts-held"))
                for env in ("_base", "prod")
            ),
        )
        self.assertEqual(
            [node for node in found["nodes"] if node[0].endswith("::chart")],
            [["charts-held::chart", "charts-held", True], ["example-helm::chart", "", False]],
        )

    @needs_yaml
    def test_a_helm_release_with_no_source_ref_gets_no_chart_edge(self):
        """Break caught: a `HelmRelease` naming no `sourceRef` given a `uses_chart`
        edge to the clone defining its chart. It had none before the routing, and
        the routing changes where an edge points, not which releases have one."""
        found = scenario_edges("chart-estate")
        self.assertEqual(
            [edge for edge in found["edges"] if edge[0] == "uses_chart" and ":jobs" in edge[1]],
            [],
        )


# Two clones defining the release's chart, neither the store's to choose by name
# (#475). No golden entry: the graph took the first by name before.
_TWO_DEFINERS = {
    **_flux({"web": _release("web", "web", "HelmRepository", "example-helm")}),
    "a-charts/web/Chart.yaml": "apiVersion: v2\nname: web\nversion: 1.0.0\n",
    "b-charts/web/Chart.yaml": "apiVersion: v2\nname: web\nversion: 1.0.0\n",
}
SCENARIOS["chart-two-definers"] = (False, _TWO_DEFINERS, {"DEPLOY_REPOS": {"example-deploy"}})
SCENARIOS["chart-two-definers-one-umbrella"] = (
    False,
    _TWO_DEFINERS,
    {"DEPLOY_REPOS": {"example-deploy"}, "UMBRELLA_REPOS": {"a-charts"}},
)


class ChartDefinedTwiceTest(SettingsIsolated):
    """The graph follows the layer's chart rule (#475), so the two name the same
    repository for one release, or neither names one."""

    def _chart_targets(self, name: str) -> list[list]:
        found = scenario_edges(name)
        return sorted(
            [edge[1], edge[2]] + [node[1:] for node in found["nodes"] if node[0] == edge[2]]
            for edge in found["edges"]
            if edge[0] == "uses_chart"
        )

    @needs_yaml
    def test_a_chart_two_clones_define_falls_back_to_its_source_ref(self):
        """Break caught: the graph resolving the chart to the first definer by name,
        `a-charts`, while the layer leaves it unresolved - an in-estate provider
        claimed on a guess. With no rule to choose, the release keeps its
        `sourceRef`, outside the estate."""
        self.assertEqual(
            self._chart_targets("chart-two-definers"),
            [
                [f"example-deploy::deploy:{env}:web", "example-helm::chart", ["", False]]
                for env in ("_base", "prod")
            ],
        )

    @needs_yaml
    def test_a_declared_umbrella_leaves_the_chart_to_the_other_definer(self):
        """Break caught: the graph ignoring `KSB_UMBRELLA_REPOS`, so the umbrella
        that sorts first takes the chart its owner defines."""
        self.assertEqual(
            self._chart_targets("chart-two-definers-one-umbrella"),
            [
                [f"example-deploy::deploy:{env}:web", "b-charts::chart", ["b-charts", True]]
                for env in ("_base", "prod")
            ],
        )


class BareEqualsReleaseTest(SettingsIsolated):
    @needs_yaml
    def test_a_release_with_a_bare_equals_in_its_values_gives_the_same_edge_in_both(self):
        """Break caught: the graph reading Flux YAML with PyYAML's plain safe loader
        while the layer reads it with the one that takes a bare `=` as `"="`. The
        release then fails to parse in `deployments` alone, so the layer names the
        chart's repository and the graph has no `uses_chart` edge for it."""
        with _scenario_store("bare-equals") as root:
            graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
            repos = root / "repositories"
            layer = relationships.build(
                root,
                sorted(repos.iterdir()),
                tuple(k for k in relationships.BUILTIN if k.name == "helm_chart"),
                "",
                "",
                set(),
                3,
                None,
                root / "extra",
            )
        rel = "apps/base/services/web/helmrelease.yaml"
        self.assertEqual(
            sorted(
                [edge["source"], edge["target"], edge["source_file"]]
                for edge in graph["links"]
                if edge["relation"] == "uses_chart"
            ),
            [
                ["example-deploy::deploy:_base:web", "charts-repo::chart", rel],
                ["example-deploy::deploy:prod:web", "charts-repo::chart", rel],
            ],
        )
        self.assertEqual(
            [
                (edge["repository"], edge["chart"])
                for edge in layer.by_repo["example-deploy"]["depends_on"]
            ],
            [("charts-repo", "web")],
        )


class ChartPatternRefusalTest(SettingsIsolated):
    def test_a_malformed_chart_pattern_is_refused_with_exit_1_and_writes_nothing(self):
        """Break caught: `deployments` formatting `KSB_CHART_REPO_PATTERN` unchecked,
        so `chart-{chart}` ends the stage in a `KeyError` traceback while
        `relationships` refuses the same setting with a message."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "repositories" / "example-deploy" / ".git").mkdir(parents=True)
            (root / "graphify-out").mkdir()
            graph = root / "graphify-out" / "graph.json"
            graph.write_text(
                json.dumps({"directed": False, "graph": {}, "nodes": [], "links": []}),
                encoding="utf-8",
            )
            before = graph.read_bytes()
            config.configure(
                root=root, DEPLOY_REPOS={"example-deploy"}, CHART_REPO_PATTERN="chart-{chart}"
            )
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = deployments.main()
            self.assertEqual(code, 1)
            self.assertIn("KSB_CHART_REPO_PATTERN", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())
            self.assertEqual(graph.read_bytes(), before)


SRC = Path(__file__).resolve().parent.parent / "src" / "knowledgestore"


class OneParserTest(unittest.TestCase):
    """Break caught: a second Terraform-source regex or `package.json` dependency
    reader growing back beside `relationships`. Two readers drift - that is how the
    graph and the layer came to disagree about module scope, organisations and
    credentials - and a reader nobody routes through the registry is invisible to
    every test of it.

    Narrow by design, and so a tripwire rather than a proof: it catches only a
    regex holding a literal `source\\s*=`, and a dependency section read by its
    literal name. A reader spelled any other way - a `source =` with a literal
    space, a section name built in a variable, a Maven or Helm reader - passes it.
    """

    # A regex matching a Terraform `source =` attribute, and a manifest's dependency
    # section read by name (`data.get("dependencies")`, `data["devDependencies"]`).
    # Naming a section to *ask* `relationships.npm_dependencies` for is not reading.
    READERS = (
        re.compile(r"source\\s\*="),
        re.compile(r"""(?:\.get\(|\[)\s*["'](?:dev|peer)?[dD]ependencies["']"""),
    )

    def _readers(self, text: str) -> list[str]:
        return [reader.pattern for reader in self.READERS if reader.search(text)]

    def test_no_module_outside_relationships_parses_these_declarations(self):
        found = {
            path.name: self._readers(path.read_text(encoding="utf-8"))
            for path in sorted(SRC.rglob("*.py"))
            if path.name != "relationships.py"
        }
        self.assertEqual(
            {name: hits for name, hits in found.items() if hits},
            {},
            "parse these through `relationships`",
        )
        self.assertIn("build_package_edges.py", found, "the check must read the stage it guards")

    def test_the_check_recognises_the_readers_it_forbids(self):
        """The forms the graph stage used to hold, verbatim: a check that cannot
        see them passes over their return."""
        self.assertEqual(len(self._readers('_TF_SOURCE = re.compile(r"""source\\s*=\\s*"')), 1)
        self.assertEqual(len(self._readers('return set(data.get("dependencies", {}))')), 1)
        self.assertEqual(len(self._readers('set(data.get("devDependencies", {}))')), 1)
        self.assertEqual(
            self._readers('CONSUMER_SECTIONS = ("dependencies", "devDependencies")'), []
        )


if __name__ == "__main__":
    unittest.main()
