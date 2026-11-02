"""One deployment fact describes one service in one reconciled overlay path (#542).

The Kustomize/Flux route used to take an environment from an `apps/<name>`
segment and to compose every overlay a service had under that name, so a fact
filed under a team's name quoted all the environments' configuration blended
together. These tests build both layouts the defect was seen in, with invented
names, and drive the real stage over them.

Layout 1 is the chained shape: a Flux Kustomization per cluster, plain
`kustomization.yaml` indexes under `clusters/<env>/`, and per-team Flux
Kustomizations whose `spec.path` carries a templated environment segment.
Layout 2 is a cluster reconciling several stack overlays of one environment.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

# Importing this first puts the working tree ahead of any installed copy.
from settings_isolation import SettingsIsolated
from knowledgestore import config, deploy_flux
from test_flux_kustomize_deployments import REPO, _deploy_repo, _facts, _graph, _run, needs_yaml


def _release(name: str, values: str, chart: bool = False) -> str:
    chart_block = (
        "  chart:\n    spec:\n      chart: ./stable/{name}\n      sourceRef:\n"
        "        kind: GitRepository\n        name: example-charts\n"
    ).format(name=name)
    return (
        "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\n"
        f"metadata:\n  name: {name}\nspec:\n{chart_block if chart else ''}"
        f"  values:\n{values}"
    )


def _flux(path: str | None) -> str:
    body = "" if path is None else f"  path: {path}\n"
    return (
        "apiVersion: kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization\n"
        f"metadata:\n  name: x\nspec:\n  interval: 1m\n{body}"
    )


def _index(resources: list[str], patches: list[str] | None = None) -> str:
    text = "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n"
    text += "".join(f"  - {r}\n" for r in resources)
    if patches:
        text += "patches:\n" + "".join(f"  - path: {p}\n" for p in patches)
    return text


def _team(team: str, service: str, replicas: dict[str, int]) -> dict[str, str]:
    """A team directory in layout 1: a base release, one overlay per environment,
    the team's own Flux Kustomization with a templated path, and a per-environment
    index selecting that environment's overlay and no other."""
    files = {
        f"apps/{team}/{service}/{service}.yaml": _release(
            service, "    replicas: 1\n    tier: base\n", chart=True
        ),
        f"apps/{team}/base/kustomize.yaml": _flux(f"./apps/{team}/${{ENVIRONMENT}}/base"),
    }
    for env, count in replicas.items():
        files[f"apps/{team}/{service}/{env}.yaml"] = _release(service, f"    replicas: {count}\n")
        files[f"apps/{team}/{env}/base/kustomization.yaml"] = _index(
            [f"../../{service}/{service}.yaml"], [f"../../{service}/{env}.yaml"]
        )
    return files


def layout_one() -> dict[str, str]:
    """Two clusters, two teams, each environment's overlay setting a different value
    for the same key (`replicas`)."""
    files: dict[str, str] = {}
    files.update(_team("orders", "svc-a", {"stage": 2, "live": 9}))
    files.update(_team("billing", "svc-b", {"stage": 4, "live": 7}))
    for env in ("stage", "live"):
        files[f"apps/flux-system/{env}/00/kustomize.yaml"] = _flux(f"./clusters/{env}/00")
        files[f"clusters/{env}/00/kustomization.yaml"] = _index(["../base"])
        files[f"clusters/{env}/base/kustomization.yaml"] = _index(
            [
                "../../../apps/orders/base/kustomize.yaml",
                "../../../apps/billing/base/kustomize.yaml",
            ]
        )
    return files


def layout_two() -> dict[str, str]:
    """One environment, two stacks, each stack setting a different `replicas`."""
    return {
        "apps/base/services/svc-c/helmrelease.yaml": _release(
            "svc-c", "    replicas: 1\n    shared: kept\n", chart=True
        ),
        "apps/environments/prod/services/svc-c.yaml": _release("svc-c", "    region: north\n"),
        "apps/overlays/stacks/prod-01/svc-c.yaml": _release("svc-c", "    replicas: 3\n"),
        "apps/overlays/stacks/prod-02/svc-c.yaml": _release("svc-c", "    replicas: 5\n"),
        "clusters/prod/apps.yaml": "---\n".join(
            _flux(path)
            for path in (
                "./apps/environments/prod",
                "./apps/overlays/stacks/prod-01",
                "./apps/overlays/stacks/prod-02",
            )
        ),
    }


def run_layout(layout: dict[str, str]) -> tuple[dict, str, dict[str, dict]]:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        _graph(tmp)
        _deploy_repo(tmp, layout)
        config.configure(root=tmp, DEPLOY_REPOS={REPO})
        graph, printed = _run(tmp)
        return graph, printed, _facts(graph)


def _environments(facts: dict[str, dict]) -> set[str]:
    return {node["metadata"]["environment"] for node in facts.values()}


@needs_yaml
class EnvironmentsComeFromTheReconcilingCluster(SettingsIsolated):
    """Layout 1: `clusters/<env>/` reconciles per-team `apps/<team>/<env>/...`.

    Break it catches: the environment taken from an `apps/<name>` segment, which
    produced environments named after teams and none of the real ones.
    """

    def setUp(self) -> None:
        self.graph, self.printed, self.facts = run_layout(layout_one())

    def test_the_only_environments_are_the_clusters_not_teams_or_the_flux_system(self):
        self.assertEqual(_environments(self.facts), {"stage", "live", config.DEPLOY_BASE_ENV})

    def test_each_fact_names_its_environment_its_layers_and_its_own_overlays_value(self):
        """Every environment sets `replicas` differently, so a blend shows as the
        wrong number, and a fact citing another environment's overlay shows as a
        wrong layer."""
        expected = {
            "svc-a (stage, 00)": ("stage", 2, "apps/orders/svc-a/stage.yaml"),
            "svc-a (live, 00)": ("live", 9, "apps/orders/svc-a/live.yaml"),
            "svc-b (stage, 00)": ("stage", 4, "apps/billing/svc-b/stage.yaml"),
            "svc-b (live, 00)": ("live", 7, "apps/billing/svc-b/live.yaml"),
        }
        for label, (env, replicas, overlay) in expected.items():
            with self.subTest(fact=label):
                metadata = self.facts[label]["metadata"]
                service = label.split(" ")[0]
                team = "orders" if service == "svc-a" else "billing"
                self.assertEqual(metadata["environment"], env)
                self.assertEqual(metadata["stack"], "00")
                self.assertEqual(
                    metadata["layers"], [f"apps/{team}/{service}/{service}.yaml", overlay]
                )
                self.assertEqual(metadata["config"]["replicas"], str(replicas))
                self.assertEqual(metadata["config"]["tier"], "base")

    def test_a_templated_team_path_is_followed_only_where_that_directory_exists(self):
        """The nested Kustomization's `${ENVIRONMENT}` segment is read as the
        environment being walked; a path that names no directory is reported, not
        guessed."""
        layout = layout_one()
        layout["apps/ghost/base/kustomize.yaml"] = _flux("./apps/ghost/${ENVIRONMENT}/base")
        layout["clusters/stage/base/kustomization.yaml"] = _index(
            ["../../../apps/orders/base/kustomize.yaml", "../../../apps/ghost/base/kustomize.yaml"]
        )
        _graph_, printed, facts = run_layout(layout)
        self.assertIn("name no directory in this clone", printed)
        self.assertIn("apps/ghost/base/kustomize.yaml: apps/ghost/stage/base", printed)
        self.assertIn("svc-a (stage, 00)", facts)

    def test_a_nested_kustomization_with_no_path_does_not_pull_in_every_environment(self):
        """Flux reads an absent `spec.path` as the source root. Followed from inside
        a walk, that reaches every environment's overlays and blends them."""
        layout = layout_one()
        layout["apps/orders/base/kustomize.yaml"] = _flux(None)
        graph, printed, facts = run_layout(layout)
        self.assertNotIn("REFUSED", printed)
        self.assertNotIn("svc-a (stage, 00)", facts)
        self.assertNotIn("svc-a (live, 00)", facts)

    def test_the_run_reports_no_warning_when_every_environment_has_a_cluster(self):
        self.assertNotIn("WARNING", self.printed)
        self.assertNotIn("REFUSED", self.printed)

    def test_an_overlay_that_declares_its_own_chart_is_not_taken_as_the_base(self):
        """One estate declares a full release in some overlays. A base chosen as the
        lowest-sorting chart declaration put one environment's file under every
        other environment's fact, which the guard then (rightly) refused."""
        layout = layout_one()
        layout["apps/orders/svc-a/live.yaml"] = _release("svc-a", "    replicas: 9\n", chart=True)
        _graph_, printed, facts = run_layout(layout)
        self.assertEqual(
            facts[f"svc-a ({config.DEPLOY_BASE_ENV})"]["metadata"]["layers"],
            ["apps/orders/svc-a/svc-a.yaml"],
        )
        self.assertNotIn("REFUSED", printed)


@needs_yaml
class StacksOfOneEnvironmentAreFactsOfTheirOwn(SettingsIsolated):
    """Layout 2: one cluster reconciles an environment overlay and several stacks.

    Break it catches: composing every stack overlay of an environment into one
    fact, which quotes a configuration that no stack runs.
    """

    def setUp(self) -> None:
        self.graph, self.printed, self.facts = run_layout(layout_two())

    def test_each_stack_is_a_fact_with_its_own_value_and_the_environment_kept(self):
        self.assertEqual(
            sorted(label for label in self.facts if label.startswith("svc-c (prod")),
            ["svc-c (prod, prod-01)", "svc-c (prod, prod-02)"],
        )
        for stack, replicas in (("prod-01", "3"), ("prod-02", "5")):
            with self.subTest(stack=stack):
                node = self.facts[f"svc-c (prod, {stack})"]
                self.assertEqual(node["metadata"]["environment"], "prod")
                self.assertEqual(node["metadata"]["stack"], stack)
                self.assertEqual(node["metadata"]["config"]["replicas"], replicas)
                self.assertEqual(node["metadata"]["config"]["region"], "north")
                self.assertEqual(node["metadata"]["config"]["shared"], "kept")
                self.assertEqual(
                    node["metadata"]["layers"],
                    [
                        "apps/base/services/svc-c/helmrelease.yaml",
                        "apps/environments/prod/services/svc-c.yaml",
                        f"apps/overlays/stacks/{stack}/svc-c.yaml",
                    ],
                )

    def test_the_stack_is_part_of_the_node_id_so_two_stacks_are_two_nodes(self):
        ids = {self.facts[f"svc-c (prod, {s})"]["id"] for s in ("prod-01", "prod-02")}
        self.assertEqual(
            ids, {f"{REPO}::deploy:prod:svc-c@prod-01", f"{REPO}::deploy:prod:svc-c@prod-02"}
        )

    def test_both_stacks_hang_off_the_one_environment(self):
        envs = [n for n in self.graph["nodes"] if n["id"] == "deploy::env:prod"]
        self.assertEqual(len(envs), 1)


class TheBlendingGuard(SettingsIsolated):
    """A fact whose layers carry two environments' or two stacks' overlays is refused.

    Break it catches: the guard removed or loosened, which lets a constructed
    multi-overlay fact through as if it described one deployment.
    """

    ENVIRONMENTS = {"stage", "live"}

    def test_two_environments_overlays_are_refused(self):
        reason = deploy_flux.blending(
            "stage", ["apps/a/svc.yaml", "apps/a/stage.yaml", "apps/a/live.yaml"], self.ENVIRONMENTS
        )
        self.assertIn("other environments than stage: live", reason)

    def test_two_stacks_of_one_environment_are_refused(self):
        reason = deploy_flux.blending(
            "live",
            ["apps/a/svc.yaml", "apps/s/live-01/svc.yaml", "apps/s/live-02/svc.yaml"],
            self.ENVIRONMENTS,
        )
        self.assertIn("more than one stack overlay of live: live-01, live-02", reason)

    def test_an_environment_overlay_and_one_of_its_stacks_is_one_deployment(self):
        self.assertEqual(
            deploy_flux.blending(
                "live",
                ["apps/a/svc.yaml", "apps/a/live.yaml", "apps/a/live-01.yaml"],
                self.ENVIRONMENTS,
            ),
            "",
        )

    def test_a_name_merely_containing_an_environment_is_not_that_environment(self):
        self.assertEqual(
            deploy_flux.blending(
                "stage", ["apps/a/stage-gate.yaml", "apps/live-x/a.yaml"], {"live"}
            ),
            "",
        )

    @needs_yaml
    def test_the_stage_skips_and_counts_a_fact_whose_directory_holds_both_overlays(self):
        """A cluster reconciling a whole team directory reaches every environment's
        overlay of the service in it. The fact is not emitted; the run says so."""
        layout = {
            "apps/orders/svc-a/svc-a.yaml": _release("svc-a", "    replicas: 1\n", chart=True),
            "apps/orders/svc-a/stage.yaml": _release("svc-a", "    replicas: 2\n"),
            "apps/orders/svc-a/live.yaml": _release("svc-a", "    replicas: 9\n"),
            "clusters/stage/apps.yaml": _flux("./apps/orders"),
            "clusters/live/apps.yaml": _flux("./apps/elsewhere"),
            "apps/elsewhere/svc-z.yaml": _release("svc-z", "    replicas: 1\n", chart=True),
        }
        graph, printed, facts = run_layout(layout)
        self.assertNotIn("svc-a (stage)", facts)
        self.assertIn("1 fact(s) REFUSED", printed)
        self.assertIn("svc-a (stage)", printed)
        self.assertIn("other environments than stage: live", printed)
        self.assertIn("svc-z (live)", facts)


@needs_yaml
class AnEnvironmentNameBesideTeamDirectories(SettingsIsolated):
    """A Kustomization under `apps/<team>/` names no environment.

    Break it catches: the old behaviour returning silently - an environment named
    after a team directory, with no sign anything was wrong.
    """

    def test_it_warns_and_takes_no_environment_from_the_team_directory(self):
        layout = {
            "apps/orders/svc-a/svc-a.yaml": _release("svc-a", "    replicas: 1\n", chart=True),
            "apps/orders/svc-a/stage.yaml": _release("svc-a", "    replicas: 2\n"),
            "apps/orders/kustomize.yaml": _flux("./apps/orders"),
        }
        graph, printed, facts = run_layout(layout)
        self.assertIn("WARNING", printed)
        self.assertIn("apps/orders", printed)
        self.assertEqual(_environments(facts), {config.DEPLOY_BASE_ENV})

    def test_a_declared_environment_directory_is_an_environment(self):
        layout = {
            "apps/orders/svc-a/svc-a.yaml": _release("svc-a", "    replicas: 1\n", chart=True),
            "apps/environments/stage/svc-a.yaml": _release("svc-a", "    replicas: 2\n"),
            "apps/environments/stage/apps.yaml": _flux("./apps/environments/stage"),
        }
        graph, printed, facts = run_layout(layout)
        self.assertIn("svc-a (stage)", facts)
        self.assertNotIn("WARNING", printed)


@needs_yaml
class RunningTwice(SettingsIsolated):
    """Two runs over the same tree write the same bytes.

    Break it catches: an iteration over a set or a dict keyed by unordered data in
    the walk or the composition, which reorders layers or facts between runs.
    """

    def test_a_second_run_is_byte_identical(self):
        written = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                _graph(tmp)
                _deploy_repo(tmp, {**layout_one(), **layout_two()})
                config.configure(root=tmp, DEPLOY_REPOS={REPO})
                _run(tmp)
                written.append((tmp / "graphify-out" / "graph.json").read_bytes())
        self.assertEqual(written[0], written[1])
        self.assertIn(b"svc-c@prod-01", written[0])


if __name__ == "__main__":
    unittest.main()
