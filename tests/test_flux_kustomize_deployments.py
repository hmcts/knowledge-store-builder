"""The second deployment layout: a Kustomize/Flux tree read as declarations (#88).

The stage read one layout, so a store pointed at a Kustomize/Flux repository got
exactly the pair count it got without it - no error, no node, and nothing on the
page to say a whole repository contributed nothing. These tests drive the real
reader over real files on disk and assert on what lands in the graph.

Every fixture name here is invented. The layout is the one #88 documents; the
service, environment, stack, chart-source and vault names are not any estate's.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

# Importing this first puts the working tree ahead of any installed copy.
from settings_isolation import SettingsIsolated
from knowledgestore import build_deployments as deployments
from knowledgestore import config, deploy_flux, deploy_values

try:  # PyYAML is the `deploy` extra, not a runtime dependency of this library
    import yaml  # noqa: F401

    HAS_YAML = True
except ImportError:  # pragma: no cover - the default-install CI job takes this path
    HAS_YAML = False

needs_yaml = unittest.skipUnless(HAS_YAML, "needs the `deploy` extra (PyYAML)")

BASE_ALPHA = """\
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: alpha
spec:
  chart:
    spec:
      chart: alpha
      version: 1.4.0
      sourceRef:
        kind: GitRepository
        name: example-charts
  values:
    replicaCount: 1
    image: ${IMAGE_TAG_ALPHA}
    resources:
      limits:
        cpu: '1'
    externalSecrets:
      alphaApiToken:
        secretStore: invented-vault
        secretPath: alpha/api-token
"""

PATCH_STAGING = """\
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: alpha
spec:
  values:
    replicaCount: 3
"""

# Alphabetically ahead of `staging`, and it patches a value nested inside the
# base's own mapping - so an implementation that overlays onto the parsed base
# document instead of a copy corrupts `staging` while `prod` still looks right.
PATCH_PROD = """\
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: alpha
spec:
  values:
    replicaCount: 6
    resources:
      limits:
        cpu: '9'
"""

PATCH_STACK_BLUE = """\
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: alpha
spec:
  values:
    resources:
      limits:
        memory: 2Gi
"""

CLUSTER_STAGING = """\
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: apps
spec:
  interval: 10m
  path: ./apps/environments/staging
"""

# One cluster reconciling an environment overlay and a stack overlay together,
# which is what makes the stack patch part of this environment's fact.
CLUSTER_PROD = """\
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: apps
spec:
  path: ./apps/environments/prod
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: stack-blue
spec:
  path: ./apps/overlays/stacks/blue
"""

FLUX_TREE = {
    "apps/base/services/alpha/helmrelease.yaml": BASE_ALPHA,
    "apps/environments/staging/services/alpha.yaml": PATCH_STAGING,
    "apps/environments/prod/services/alpha.yaml": PATCH_PROD,
    "apps/overlays/stacks/blue/services/alpha.yaml": PATCH_STACK_BLUE,
    "clusters/staging/cluster-a/apps.yaml": CLUSTER_STAGING,
    "clusters/prod/cluster-b/apps.yaml": CLUSTER_PROD,
}

REPO = "example-deploy"


def _write(repo: Path, layout: dict[str, str]) -> None:
    for rel, text in layout.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _deploy_repo(tmp: Path, layout: dict[str, str] | None = None) -> Path:
    """The clone named in KSB_DEPLOY_REPOS, holding a Kustomize/Flux tree."""
    repo = tmp / "repositories" / REPO
    (repo / ".git").mkdir(parents=True)
    _write(repo, FLUX_TREE if layout is None else layout)
    return repo


def _values_layout(repo: Path) -> None:
    """The layout the stage already read, in the same clone as the Flux tree."""
    _write(
        repo,
        {"ansible/group_vars/prod/beta-service_values.yaml.j2": "replicas: 2\n"},
    )


def _graph(tmp: Path, extra_nodes: list[dict] | None = None) -> None:
    """A graph holding one node in a repository the service name can join to."""
    (tmp / "graphify-out").mkdir(parents=True, exist_ok=True)
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {
                "id": "svc-alpha::AlphaHandler",
                "label": "AlphaHandler",
                "repo": "svc-alpha",
                "source_file": "src/AlphaHandler.java",
            },
            *(extra_nodes or []),
        ],
        "links": [],
    }
    (tmp / "graphify-out" / "graph.json").write_text(json.dumps(graph), encoding="utf-8")


def _run(tmp: Path) -> tuple[dict, str]:
    """The stage, its written graph and everything it printed."""
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = deployments.main()
    if code != 0:
        raise AssertionError(f"the stage returned {code}: {printed.getvalue()}")
    graph = json.loads((tmp / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    return graph, printed.getvalue()


def _facts(graph: dict) -> dict[str, dict]:
    """Deployment nodes by label, which is `<service> (<environment>)`."""
    return {
        node["label"]: node
        for node in graph["nodes"]
        if (node.get("metadata") or {}).get("kind") == "deployment"
    }


class WhatAKustomizationReconciles(SettingsIsolated):
    """`spec.path` in the forms these trees write it, normalised to one.

    Break it catches: a path form left unhandled. The directory match is a string
    prefix, so `./apps/base` read as `./apps/base` matches nothing under
    `apps/base/...` - every patch there becomes unattributed, and the run reports
    a short answer rather than a wrong one, which is easy to read past.
    """

    def _kustomization(self, path: object) -> dict:
        return {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
            "kind": "Kustomization",
            "spec": {} if path is None else {"path": path},
        }

    def test_the_forms_these_trees_write(self):
        for written, expected in (
            ("./apps/environments/staging", "apps/environments/staging"),
            ("apps/environments/staging", "apps/environments/staging"),
            ("/apps/environments/staging", "apps/environments/staging"),
            ("./apps/base/", "apps/base"),
            (".", ""),
            ("./", ""),
            (None, ""),
        ):
            with self.subTest(path=written):
                self.assertEqual(
                    deploy_flux.reconciled_path(self._kustomization(written)), expected
                )

    def test_a_parent_relative_path_is_not_rewritten_into_a_child_one(self):
        """Stripping `.` and `/` as characters turns `../apps` into `apps`, which
        would attribute a sibling directory's patches to this environment."""
        self.assertNotEqual(deploy_flux.reconciled_path(self._kustomization("../apps")), "apps")


@needs_yaml
class ComposingABaseWithItsPatches(SettingsIsolated):
    """A base plus its patches is one (service, environment) fact.

    Break it catches: dropping any layer from the composition - the base, the
    environment patch or a stack overlay the same cluster reconciles - which
    leaves a fact that reads as complete and answers with the wrong values.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        _graph(tmp)
        _deploy_repo(tmp)
        config.configure(root=tmp, DEPLOY_REPOS={REPO})
        self.graph, self.printed = _run(tmp)
        self.facts = _facts(self.graph)

    def test_the_environment_patch_wins_and_the_base_still_supplies_the_rest(self):
        """A fact carrying only the patch answers "what is set" wrongly, and a fact
        carrying only the base answers every environment identically."""
        staging = self.facts["alpha (staging)"]
        self.assertEqual(staging["metadata"]["config"]["replicaCount"], "3")
        self.assertEqual(staging["metadata"]["config"]["resources.limits.cpu"], "1")

    def test_the_fact_takes_the_node_shape_the_stage_already_emits(self):
        """A second node shape for the second layout puts two answers on the page
        for one question, and every consumer of the first shape misses the new one."""
        staging = self.facts["alpha (staging)"]
        self.assertEqual(staging["id"], f"{REPO}::deploy:staging:alpha")
        self.assertEqual(staging["local_id"], "deploy:staging:alpha")
        self.assertEqual(staging["metadata"]["service"], "alpha")
        self.assertEqual(staging["metadata"]["environment"], "staging")
        self.assertEqual(staging["repo"], REPO)
        self.assertEqual(staging["community_name"], "Deployments: staging")

    def test_every_fact_cites_a_file_that_exists_and_names_the_layers_it_composed(self):
        """A citation nobody can open is counted as dangling by `status`, and a
        composed fact with no layer list cannot be checked against the tree."""
        repo = Path(self.tmp.name) / "repositories" / REPO
        # Without this the loop below passes over an empty set, which is exactly
        # what the stage did before it could read this layout.
        self.assertEqual(sorted(self.facts), ["alpha (_base)", "alpha (prod)", "alpha (staging)"])
        for label, node in sorted(self.facts.items()):
            with self.subTest(fact=label):
                self.assertTrue((repo / node["source_file"]).is_file(), node["source_file"])
                layers = node["metadata"]["layers"]
                self.assertIn(node["source_file"], layers)
                for rel in layers:
                    self.assertTrue((repo / rel).is_file(), rel)

    def test_the_stack_overlay_the_cluster_reconciles_is_applied(self):
        """`prod`'s cluster reconciles the stack overlay as well as the environment
        overlay, so a reader that only follows environment directories loses a
        limit that is really set - and reports nothing missing."""
        self.assertEqual(
            self.facts["alpha (prod)"]["metadata"]["config"]["resources.limits.memory"], "2Gi"
        )
        self.assertNotIn(
            "resources.limits.memory", self.facts["alpha (staging)"]["metadata"]["config"]
        )

    def test_one_environments_patch_does_not_bleed_into_another(self):
        """`prod` patches a value nested inside the base's own mapping and is
        composed first. Overlaying onto the parsed base document rather than a copy
        gives `staging` prod's CPU limit, with both facts looking well-formed."""
        self.assertEqual(
            self.facts["alpha (prod)"]["metadata"]["config"]["resources.limits.cpu"], "9"
        )
        self.assertEqual(
            self.facts["alpha (staging)"]["metadata"]["config"]["resources.limits.cpu"], "1"
        )

    def test_the_base_layer_is_a_fact_of_its_own_named_as_the_base(self):
        """Without it, what the estate declares before any environment applies is
        unquotable, and the existing layout's base facts have no counterpart here."""
        self.assertIn(f"alpha ({config.DEPLOY_BASE_ENV})", self.facts)

    def test_the_service_joins_the_repository_that_holds_it(self):
        """The join runs off the service name, so a reader that never registers its
        services leaves the whole layout hanging off environments only."""
        edges = {(edge["source"], edge["target"], edge["relation"]) for edge in self.graph["links"]}
        self.assertIn(
            (f"{REPO}::deploy:staging:alpha", "svc-alpha::AlphaHandler", "deploys"), edges
        )

    def test_the_run_reports_what_it_read(self):
        """#88's premise is a silent stage: the report has to carry the document
        and fact counts, or a repository contributing nothing looks like a clean run."""
        self.assertIn("Kustomize/Flux:", self.printed)
        # One base declaration and three patches; three Kustomizations across two
        # cluster files; the base fact plus prod plus staging.
        self.assertIn("4 HelmRelease", self.printed)
        self.assertIn("3 Kustomization", self.printed)
        self.assertIn("3 service/environment fact(s)", self.printed)


@needs_yaml
class WithholdingWhatTheseFilesCarry(SettingsIsolated):
    """These files map a variable to a store entry to a vault, and never render.

    Break it catches: reading the Flux layout with `yaml.safe_load` alone, which
    publishes the `${ }` variable names and the secret locations that the values
    layout has withheld since #88's first half.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        _graph(tmp)
        _deploy_repo(tmp)
        config.configure(root=tmp, DEPLOY_REPOS={REPO})
        self.graph, _ = _run(tmp)
        self.facts = _facts(self.graph)
        self.written = (tmp / "graphify-out" / "graph.json").read_text(encoding="utf-8")

    def test_a_secret_reference_keeps_the_variable_and_loses_the_store_and_the_path(self):
        base = self.facts[f"alpha ({config.DEPLOY_BASE_ENV})"]["metadata"]["config"]
        self.assertEqual(
            base["externalSecrets.alphaApiToken.secretStore"], deploy_values.PLACEHOLDER
        )
        self.assertEqual(
            base["externalSecrets.alphaApiToken.secretPath"], deploy_values.PLACEHOLDER
        )

    def test_neither_the_vault_nor_the_entry_reaches_the_written_graph(self):
        """The assertion is on absence from the artefact, not on the key: a reader
        that flattened the mapping under a different key would still publish the map."""
        self.assertIn(
            "externalSecrets.alphaApiToken.secretStore",
            self.facts[f"alpha ({config.DEPLOY_BASE_ENV})"]["metadata"]["config"],
            "the mapping was never read, so its absence below says nothing",
        )
        self.assertNotIn("invented-vault", self.written)
        self.assertNotIn("alpha/api-token", self.written)

    def test_a_dollar_brace_reference_loses_the_variable_name(self):
        base = self.facts[f"alpha ({config.DEPLOY_BASE_ENV})"]["metadata"]["config"]
        self.assertEqual(base["image"], deploy_values.PLACEHOLDER)
        self.assertNotIn("IMAGE_TAG_ALPHA", self.written)


@needs_yaml
class BothRoutesReachOneEnvironment(SettingsIsolated):
    """One set of environments, whichever layout declared the service.

    Break it catches: minting a second environment node per route, which splits
    "which services reach prod" into two answers that each look complete.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        _graph(tmp)
        _values_layout(_deploy_repo(tmp))
        config.configure(root=tmp, DEPLOY_REPOS={REPO})
        self.graph, self.printed = _run(tmp)
        self.facts = _facts(self.graph)

    def test_prod_is_one_environment_node_not_one_per_route(self):
        prod = [node for node in self.graph["nodes"] if node["id"] == "deploy::env:prod"]
        self.assertEqual(len(prod), 1)
        # Both routes reached prod, so the count above is one node shared rather
        # than one route's node with the other route absent.
        routes = {
            node["metadata"]["route"]
            for node in self.facts.values()
            if node["metadata"]["environment"] == "prod"
        }
        self.assertEqual(routes, {"kustomize", "values"})

    def test_both_routes_reached_prod(self):
        self.assertIn("alpha (prod)", self.facts)
        self.assertIn("beta-service (prod)", self.facts)

    def test_each_fact_records_the_route_that_produced_it(self):
        """Without it, "which services reach this environment by which route" is
        answerable only by guessing from the file path a fact happens to cite."""
        self.assertEqual(self.facts["alpha (prod)"]["metadata"]["route"], "kustomize")
        self.assertEqual(self.facts["beta-service (prod)"]["metadata"]["route"], "values")

    def test_both_facts_hang_off_the_one_environment_by_an_edge(self):
        edges = {
            (edge["source"], edge["target"])
            for edge in self.graph["links"]
            if edge["relation"] == "deployed_in"
        }
        self.assertIn((f"{REPO}::deploy:prod:alpha", "deploy::env:prod"), edges)
        self.assertIn((f"{REPO}::deploy:prod:beta-service", "deploy::env:prod"), edges)


@needs_yaml
class OneCloneDeclaringAServiceTwice(SettingsIsolated):
    """Both layouts describing one (service, environment) is one node, not two.

    Break it catches: appending both facts, which puts two nodes with the same id
    into the graph - and a duplicate id is not an error anywhere downstream, it is
    two answers to one question with nothing saying which is read.
    """

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        tmp = Path(holder.name)
        _graph(tmp)
        repo = _deploy_repo(tmp)
        # The same (service, environment) the Flux tree declares for staging.
        _write(repo, {"ansible/group_vars/staging/alpha_values.yaml.j2": "replicaCount: 99\n"})
        config.configure(root=tmp, DEPLOY_REPOS={REPO})
        self.graph, self.printed = _run(tmp)

    def test_the_clash_is_one_node_and_the_existing_route_keeps_it(self):
        clashing = [
            node for node in self.graph["nodes"] if node["id"] == f"{REPO}::deploy:staging:alpha"
        ]
        self.assertEqual(len(clashing), 1)
        self.assertEqual(clashing[0]["metadata"]["route"], "values")
        self.assertEqual(clashing[0]["metadata"]["config"]["replicaCount"], "99")

    def test_the_clash_is_named_rather_than_resolved_quietly(self):
        self.assertIn("declared by both layouts", self.printed)
        self.assertIn("alpha (staging)", self.printed)


@needs_yaml
class SayingWhatItCouldNotRead(SettingsIsolated):
    """#88's premise is that the omission is silent. This reader has to be loud.

    Break it catches: an `except yaml.YAMLError: continue` in the walk, which
    turns a service whose configuration is missing from every answer into a run
    that reports nothing at all.
    """

    def _run_with(self, layout: dict[str, str]) -> tuple[dict, str]:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _graph(tmp)
            _deploy_repo(tmp, layout)
            config.configure(root=tmp, DEPLOY_REPOS={REPO})
            return _run(tmp)

    def test_a_malformed_document_is_counted_and_named_and_the_run_continues(self):
        broken = "apps/environments/staging/services/broken.yaml"
        graph, printed = self._run_with({**FLUX_TREE, broken: "values: [unclosed\n"})
        self.assertIn(broken, printed)
        self.assertIn("1 YAML file(s) in the Kustomize/Flux walk did not parse", printed)
        self.assertIn("alpha (staging)", _facts(graph), "one bad file stopped the rest")

    def test_a_patch_no_cluster_reconciles_is_named_rather_than_dropped(self):
        """A patch under a directory nothing reconciles carries no environment, so
        it cannot become a fact - and an estate whose clusters live somewhere this
        reader does not look would otherwise see a quietly short answer."""
        stray = "apps/environments/qa/services/alpha.yaml"
        _graph_, printed = self._run_with({**FLUX_TREE, stray: PATCH_STAGING})
        self.assertIn(stray, printed)
        self.assertIn("no Kustomization reconciles", printed)

    def test_a_tree_without_this_layout_yields_nothing_and_says_so(self):
        """The sensitivity control, and the defect this reader exists to fix:
        silently yielding nothing is exactly what the stage did before."""
        graph, printed = self._run_with(
            {
                "config/settings.yaml": "colour: blue\n",
                "manifests/web.yaml": (
                    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"
                ),
            }
        )
        self.assertEqual(_facts(graph), {})
        self.assertIn("no HelmRelease or Kustomization document", printed)
        self.assertIn("2 YAML file(s)", printed)


@needs_yaml
class ChartReferencesAreDependencies(SettingsIsolated):
    """`spec.chart.spec.sourceRef` names the repository providing the chart.

    Break it catches: reading the chart name and version but not the source, which
    loses a repository-to-repository dependency that nothing else in a store holds.
    """

    def _run_with_graph(self, extra_nodes: list[dict] | None = None) -> tuple[dict, str]:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        tmp = Path(holder.name)
        _graph(tmp, extra_nodes)
        _deploy_repo(tmp)
        config.configure(root=tmp, DEPLOY_REPOS={REPO})
        return _run(tmp)

    def test_the_chart_source_becomes_an_edge_citing_the_file_that_declares_it(self):
        graph, _ = self._run_with_graph()
        chart = [node for node in graph["nodes"] if node["id"] == "example-charts::chart"]
        self.assertEqual(len(chart), 1)
        self.assertEqual(chart[0]["metadata"]["kind"], "chart_source")
        self.assertEqual(chart[0]["label"], "example-charts")
        edges = [
            edge
            for edge in graph["links"]
            if edge["target"] == "example-charts::chart" and edge["relation"] == "uses_chart"
        ]
        self.assertIn(f"{REPO}::deploy:staging:alpha", {edge["source"] for edge in edges})
        self.assertEqual(
            {edge["source_file"] for edge in edges},
            {"apps/base/services/alpha/helmrelease.yaml"},
        )

    def test_the_chart_and_its_pinned_version_travel_on_the_fact(self):
        graph, _ = self._run_with_graph()
        metadata = _facts(graph)["alpha (staging)"]["metadata"]
        self.assertEqual(metadata["chart"], "alpha")
        self.assertEqual(metadata["chart_version"], "1.4.0")

    def test_a_source_the_estate_does_not_hold_claims_no_repository(self):
        """Naming a repository the store has never synced puts it into every
        per-repository aggregate, so the store claims to hold what it has not seen."""
        graph, _ = self._run_with_graph()
        chart = next(node for node in graph["nodes"] if node["id"] == "example-charts::chart")
        self.assertEqual(chart["repo"], "")
        self.assertFalse(chart["metadata"]["provider_in_estate"])

    def test_a_source_the_estate_does_hold_keeps_its_repository(self):
        graph, _ = self._run_with_graph(
            [
                {
                    "id": "example-charts::ChartIndex",
                    "label": "ChartIndex",
                    "repo": "example-charts",
                    "source_file": "index.yaml",
                }
            ]
        )
        chart = next(node for node in graph["nodes"] if node["id"] == "example-charts::chart")
        self.assertEqual(chart["repo"], "example-charts")
        self.assertTrue(chart["metadata"]["provider_in_estate"])


@needs_yaml
class TheTerraformModuleLayerIsLeftAlone(SettingsIsolated):
    """#122's edges are another stage's output, and this stage runs after it.

    Break it catches: giving the chart node the `<repo>::module` id #122 uses.
    Both stages strip their own layer by `_origin`, so a shared id makes each
    stage delete the other's edges - and the loss shows up a stage later, in a
    count nobody is comparing.
    """

    def test_the_module_node_and_its_edge_survive_two_deployment_runs(self):
        from knowledgestore import build_package_edges as packages

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _graph(tmp)
            _deploy_repo(tmp)
            consumer = tmp / "repositories" / "svc-alpha"
            (consumer / ".git").mkdir(parents=True)
            (consumer / "main.tf").write_text(
                'module "charts" {\n  source = "git@github.com:org/example-charts"\n}\n',
                encoding="utf-8",
            )
            config.configure(root=tmp, DEPLOY_REPOS={REPO})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(packages.main(), 0)
            before = json.loads((tmp / "graphify-out" / "graph.json").read_text())
            module_edges = [edge for edge in before["links"] if edge["relation"] == "USES_MODULE"]
            self.assertEqual(len(module_edges), 1, "the fixture must produce one to protect")

            _run(tmp)
            graph, _ = _run(tmp)

        ids = {node["id"] for node in graph["nodes"]}
        self.assertIn("example-charts::module", ids)
        # The chart layer has to be present for this to be a claim about two
        # layers coexisting rather than about one of them being absent.
        self.assertIn("example-charts::chart", ids)
        self.assertEqual(
            [edge for edge in graph["links"] if edge["relation"] == "USES_MODULE"],
            module_edges,
        )


@needs_yaml
class RunningTwice(SettingsIsolated):
    """Idempotence by reconstruction, with the new nodes included.

    Break it catches: a chart node or a Flux fact this stage does not strip on the
    next run, which grows the committed graph every refresh.
    """

    def test_the_graph_is_the_same_size_after_a_second_run(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _graph(tmp)
            _values_layout(_deploy_repo(tmp))
            config.configure(root=tmp, DEPLOY_REPOS={REPO})
            first, _ = _run(tmp)
            second, _ = _run(tmp)
        # A run that added nothing is trivially idempotent, so name what has to
        # survive being stripped and rebuilt.
        self.assertIn("example-charts::chart", {node["id"] for node in first["nodes"]})
        self.assertIn("alpha (staging)", _facts(first))
        self.assertEqual(len(first["nodes"]), len(second["nodes"]))
        self.assertEqual(len(first["links"]), len(second["links"]))
        self.assertEqual(json.dumps(first), json.dumps(second))


if __name__ == "__main__":
    unittest.main()
