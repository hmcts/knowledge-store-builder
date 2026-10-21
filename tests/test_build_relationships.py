"""The relationships layer: every reference resolved, aggregated and written.

`knowledgestore relationships` runs every kind's parser over every clone, resolves
each reference to a repository in the estate or to the unresolved report, and
writes one small file per repository. Every expected value here is written by hand
from the fixture estate (`tests/fixtures/relationships/` plus the clones written
below), never computed by the code under test.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_relationships, config, relationships, status  # noqa: E402
from knowledgestore.source_urls import SourceUrls  # noqa: E402

try:  # PyYAML is the `deploy` extra, not a runtime dependency of this library
    import yaml  # noqa: F401

    HAS_YAML = True
except ImportError:  # pragma: no cover - the default-install CI job takes this path
    HAS_YAML = False

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "relationships"

SHA_A = "a" * 40
SHA_C = "c" * 40
TOKEN = "xxxxx-not-a-real-token"
# Token-shaped, built so the source never holds the literal (as test_secret_mask does).
GITHUB = "gh" + "p_" + "x" * 36

# Clones the shared fixture estate does not carry: two module repositories svc-a
# uses, and a consumer of svc-a that also names itself.
EXTRA_CLONES = {
    "svc-c": {
        "main.tf": (
            'module "a_pinned" {\n'
            '  source = "github.com/example-org/svc-a?ref=v2"\n'
            "}\n"
            "\n"
            'module "a_floating" {\n'
            '  source = "git::https://github.com/example-org/svc-a.git"\n'
            "}\n"
            "\n"
            'module "itself" {\n'
            '  source = "github.com/example-org/svc-c//sub"\n'
            "}\n"
        ),
    },
    "tf-network": {"main.tf": 'variable "x" {}\n'},
    "tf-storage": {"main.tf": 'variable "y" {}\n'},
}


def _kind(name: str) -> relationships.Kind:
    return next(kind for kind in relationships.BUILTIN if kind.name == name)


def _write(clone: Path, files: dict[str, str]) -> None:
    (clone / ".git").mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = clone / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class _Store(SettingsIsolated):
    """A temporary store whose `repositories/` holds the fixture estate."""

    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.repos = self.root / "repositories"
        config.configure(
            root=self.root,
            GITHUB_ORG="",
            CHART_REPO_PATTERN="",
            UMBRELLA_REPOS=set(),
            RELATIONSHIP_EXAMPLES=3,
        )

    def estate(self, extra: dict[str, dict[str, str]] | None = None) -> None:
        shutil.copytree(FIXTURES, self.repos)
        for clone in self.repos.iterdir():
            (clone / ".git").mkdir()
        for name, files in {**EXTRA_CLONES, **(extra or {})}.items():
            _write(self.repos / name, files)
        if not HAS_YAML:
            # The stage refuses an estate holding a release it cannot read without
            # PyYAML (`MissingPyYamlTest`), so the default-install job builds the
            # rest of the estate without it.
            (self.repos / "svc-b" / "flux" / "helmrelease.yaml").unlink()

    def clones(self) -> list[Path]:
        return sorted(p for p in self.repos.iterdir() if (p / ".git").is_dir())

    def build(self, kinds=relationships.BUILTIN, org="", pattern="", umbrellas=(), examples=3):
        urls = SourceUrls(
            {
                "svc-a": "https://github.com/example-org/svc-a.git",
                "svc-c": "https://github.com/example-org/svc-c.git",
            },
            {"svc-a": SHA_A, "svc-c": SHA_C},
        )
        return relationships.build(
            self.root,
            self.clones(),
            kinds,
            org,
            pattern,
            set(umbrellas),
            examples,
            urls,
            self.root / "knowledge" / "relationships" / "extra",
        )

    def run_stage(self, *argv: str) -> tuple[int, str, str]:
        out, err = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = build_relationships.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def snapshot(self) -> dict[str, bytes]:
        base = self.root / "knowledge" / "relationships"
        return {
            str(path.relative_to(base)): path.read_bytes()
            for path in sorted(base.rglob("*"))
            if path.is_file()
        }


SVC_A_LINE = 'source = "git::https://github.com/example-org/tf-network.git?ref=v4.2.0"'


class SvcATest(_Store):
    def test_svc_a_depends_on_and_depended_on_by_exactly(self):
        """Break caught: any field of the edge object wrong - the other end, the
        chart key, the uncapped count, the per-kind count, a pin read as another, a
        floating reference counted as a pin rather than under `refs_unpinned`, an
        example's link or line - or the per-repository
        file's commit and base URL not taken from provenance."""
        self.estate()
        layer = self.build()
        self.assertEqual(
            layer.by_repo["svc-a"],
            {
                "repository": "svc-a",
                "commit": SHA_A,
                "base_url": f"https://github.com/example-org/svc-a/blob/{SHA_A}/",
                "depends_on": [
                    {
                        "repository": "tf-network",
                        "chart": "",
                        "references": 1,
                        "kinds": {"terraform_module": 1},
                        "refs_pinned": {"v4.2.0": 1},
                        "refs_unpinned": 0,
                        "examples": [
                            {
                                "url": f"https://github.com/example-org/svc-a/blob/{SHA_A}/main.tf#L2",
                                "line": SVC_A_LINE,
                            }
                        ],
                    },
                    {
                        "repository": "tf-storage",
                        "chart": "",
                        "references": 1,
                        "kinds": {"terraform_module": 1},
                        "refs_pinned": {},
                        "refs_unpinned": 1,
                        "examples": [
                            {
                                "url": f"https://github.com/example-org/svc-a/blob/{SHA_A}/main.tf#L6",
                                "line": 'source = "git@github.com:example-org/tf-storage.git"',
                            }
                        ],
                    },
                ],
                "depended_on_by": [
                    {
                        "repository": "svc-c",
                        "chart": "",
                        "references": 2,
                        "kinds": {"terraform_module": 2},
                        "refs_pinned": {"v2": 1},
                        "refs_unpinned": 1,
                        "examples": [
                            {
                                "url": f"https://github.com/example-org/svc-c/blob/{SHA_C}/main.tf#L2",
                                "line": 'source = "github.com/example-org/svc-a?ref=v2"',
                            },
                            {
                                "url": f"https://github.com/example-org/svc-c/blob/{SHA_C}/main.tf#L6",
                                "line": 'source = "git::https://github.com/example-org/svc-a.git"',
                            },
                        ],
                    }
                ],
                "extra": {},
            },
        )

    def test_a_repository_without_provenance_has_null_commit_and_base_url(self):
        """Break caught: an empty string or a guessed link standing in for "the store
        holds no provenance for this repository"."""
        self.estate()
        layer = self.build()
        self.assertIsNone(layer.by_repo["tf-network"]["commit"])
        self.assertIsNone(layer.by_repo["tf-network"]["base_url"])
        self.assertEqual(
            layer.by_repo["tf-network"]["depended_on_by"][0]["examples"][0]["url"],
            f"https://github.com/example-org/svc-a/blob/{SHA_A}/main.tf#L2",
        )

    def test_the_flat_edge_list_names_both_ends(self):
        """Break caught: `repository-dependencies.json` missing an edge or carrying
        the per-repository form, which names only one end."""
        self.estate()
        layer = self.build()
        terraform = [
            (e["consumer"], e["provider"], e["references"])
            for e in layer.dependencies["edges"]
            if "terraform_module" in e["kinds"]
        ]
        self.assertEqual(
            terraform,
            [("svc-a", "tf-network", 1), ("svc-a", "tf-storage", 1), ("svc-c", "svc-a", 2)],
        )
        self.assertEqual(layer.dependencies["format"], {"major": 1, "minor": 0})

    def test_references_are_never_capped_by_the_example_limit(self):
        """Break caught: `references` counted from the truncated example list, so a
        heavily used provider reads as used `examples` times."""
        self.estate()
        layer = self.build(examples=1)
        edge = layer.by_repo["svc-a"]["depended_on_by"][0]
        self.assertEqual(edge["references"], 2)
        self.assertEqual(edge["kinds"], {"terraform_module": 2})
        self.assertEqual(len(edge["examples"]), 1)
        self.assertTrue(edge["examples"][0]["url"].endswith("main.tf#L2"))

    def test_every_unresolved_terraform_target_is_reported_in_full(self):
        """Break caught: a dependency the store does not hold dropped instead of
        reported, or reported without who references it."""
        self.estate()
        layer = self.build()
        self.assertEqual(
            layer.dependencies["unresolved_targets_outside_estate"]["terraform_module"],
            {
                "example-org/tf-cache": {"references": 1, "referenced_by": ["svc-a"]},
                "example-org/tf-dns": {"references": 1, "referenced_by": ["svc-a"]},
                "example-org/tf-queue": {"references": 1, "referenced_by": ["svc-a"]},
                "hashicorp/consul/aws": {"references": 1, "referenced_by": ["svc-a"]},
            },
        )

    def test_referenced_by_is_complete_and_sorted(self):
        """Break caught: `referenced_by` holding only the first repository seen, so
        an outside dependency reads as one repository's concern."""
        self.estate()
        layer = self.build()
        self.assertEqual(
            layer.dependencies["unresolved_targets_outside_estate"]["npm_package"]["react"],
            {"references": 2, "referenced_by": ["svc-b", "ui-kit"]},
        )

    def test_a_reference_to_itself_is_skipped_not_an_edge_or_unresolved(self):
        """Break caught: svc-c's module source naming svc-c read as an edge to itself,
        or as a dependency outside the estate."""
        self.estate()
        layer = self.build()
        self.assertEqual([e["repository"] for e in layer.by_repo["svc-c"]["depends_on"]], ["svc-a"])
        unresolved = layer.dependencies["unresolved_targets_outside_estate"]
        self.assertNotIn("example-org/svc-c", unresolved["terraform_module"])
        self.assertEqual(layer.skipped["terraform_module"], {"local": 1, "self": 1})

    def test_a_pom_depending_on_a_sibling_module_is_a_self_reference(self):
        """Break caught: a multi-module repository read as depending on itself, or
        its sibling module reported as a coordinate the estate does not hold."""
        self.estate(
            {
                "multi": {
                    "pom.xml": _pom("multi-parent", ""),
                    "core/pom.xml": _pom("multi-core", ""),
                    "app/pom.xml": _pom("multi-app", "multi-core"),
                }
            }
        )
        layer = self.build(kinds=(_kind("maven_artifact"),))
        self.assertEqual(layer.by_repo["multi"]["depends_on"], [])
        self.assertNotIn(
            "com.example:multi-core",
            layer.dependencies["unresolved_targets_outside_estate"]["maven_artifact"],
        )
        self.assertEqual(layer.skipped["maven_artifact"]["self"], 1)

    def test_kinds_json_counts_each_kind(self):
        """Break caught: the manifest's per-kind counts wrong, or an empty kind
        omitted so a reader cannot tell "none" from "not run"."""
        self.estate()
        layer = self.build()
        self.assertEqual(
            sorted(layer.kinds), ["helm_chart", "maven_artifact", "npm_package", "terraform_module"]
        )
        self.assertEqual(
            layer.kinds["terraform_module"],
            {
                "origin": "builtin",
                "pins": "git refs",
                "edges": 3,
                "references": 4,
                "unresolved": 4,
                "skipped": {"local": 1, "self": 1},
            },
        )
        self.assertEqual(
            (layer.kinds["maven_artifact"]["edges"], layer.kinds["maven_artifact"]["references"]),
            (1, 1),
        )

    def test_kinds_json_says_what_a_pin_is_for_each_kind(self):
        """Break caught: `refs_pinned` read as one quantity across kinds - a chart
        version, a git ref and an npm range are compared as if alike - because the
        manifest does not say which each kind's pins are."""
        self.estate()
        layer = self.build()
        self.assertEqual(
            {name: counts["pins"] for name, counts in layer.kinds.items()},
            {
                "helm_chart": "chart versions",
                "maven_artifact": "version specs",
                "npm_package": "version specs",
                "terraform_module": "git refs",
            },
        )


def _pom(artifact: str, depends_on: str) -> str:
    dependency = (
        "<dependencies><dependency><groupId>com.example</groupId>"
        f"<artifactId>{depends_on}</artifactId><version>1.0</version></dependency></dependencies>"
        if depends_on
        else ""
    )
    return (
        "<project><groupId>com.example</groupId>"
        f"<artifactId>{artifact}</artifactId><version>1.0</version>{dependency}</project>\n"
    )


def _chart(name: str, *deps: str) -> str:
    text = f"apiVersion: v2\nname: {name}\nversion: 0.1.0\n"
    if deps:
        text += "dependencies:\n" + "".join(f"  - name: {d}\n    version: 1.0.0\n" for d in deps)
    return text


class _HelmStore(_Store):
    def helm(self, clones: dict[str, dict[str, str]], **kwargs):
        self.repos.mkdir()
        for name, files in clones.items():
            _write(self.repos / name, files)
        return self.build(kinds=(_kind("helm_chart"),), **kwargs)

    def provider(self, layer, consumer: str, chart: str) -> str | None:
        found = [
            e["repository"] for e in layer.by_repo[consumer]["depends_on"] if e["chart"] == chart
        ]
        return found[0] if found else None


class ChartResolutionTest(_HelmStore):
    """The five steps, in order, each shown deciding."""

    ESTATE = {
        "app": {"Chart.yaml": _chart("app", "web")},
        "chart-web": {"README.md": "no chart named web here\n"},
        "other": {"Chart.yaml": _chart("web")},
    }

    def test_the_pattern_step_wins_when_set(self):
        """Break caught: step 1 skipped, so the configured naming convention loses to
        whichever clone happens to define a chart of that name."""
        layer = self.helm(self.ESTATE, pattern="chart-{name}")
        self.assertEqual(self.provider(layer, "app", "web"), "chart-web")

    def test_an_empty_pattern_skips_step_one(self):
        """Break caught: step 1 run with the default empty pattern, or step 2 not
        reached when it is skipped. The default is empty because a public library
        carries no estate's naming convention."""
        layer = self.helm(self.ESTATE)
        self.assertEqual(self.provider(layer, "app", "web"), "other")

    def test_a_non_umbrella_definition_beats_an_umbrella(self):
        """Break caught: the umbrella preferred, so every chart it bundles resolves to
        it rather than to the repository that owns the chart."""
        layer = self.helm(
            {
                "app": {"Chart.yaml": _chart("app", "api")},
                "bundle": {"Chart.yaml": _chart("api")},
                "owner": {"Chart.yaml": _chart("api")},
            },
            umbrellas={"bundle"},
        )
        self.assertEqual(self.provider(layer, "app", "api"), "owner")
        self.assertEqual(layer.conflicts, {})

    def test_the_umbrella_is_the_fallback(self):
        """Break caught: a chart only an umbrella defines reported as outside the
        estate."""
        layer = self.helm(
            {"app": {"Chart.yaml": _chart("app", "db")}, "bundle": {"Chart.yaml": _chart("db")}},
            umbrellas={"bundle"},
        )
        self.assertEqual(self.provider(layer, "app", "db"), "bundle")

    def test_two_definitions_are_a_conflict_resolved_first_by_name(self):
        """Break caught: a conflict silently picked, or picked by walk order rather
        than by name so a rename elsewhere moves it."""
        layer = self.helm(
            {
                "app": {"Chart.yaml": _chart("app", "shared")},
                "b-charts": {"Chart.yaml": _chart("shared")},
                "a-charts": {"Chart.yaml": _chart("shared")},
            }
        )
        self.assertEqual(self.provider(layer, "app", "shared"), "a-charts")
        self.assertEqual(layer.conflicts, {"helm_chart": {"shared": ["a-charts", "b-charts"]}})
        self.assertEqual(layer.dependencies["conflicts"], layer.conflicts)

    def test_an_undefined_chart_is_unresolved(self):
        """Break caught: step 5 dropped, so a chart from an outside repository
        vanishes instead of being reported."""
        layer = self.helm({"app": {"Chart.yaml": _chart("app", "nowhere")}})
        self.assertEqual(
            layer.dependencies["unresolved_targets_outside_estate"]["helm_chart"],
            {"nowhere": {"references": 1, "referenced_by": ["app"]}},
        )

    def test_a_vendored_subchart_publishes_nothing_and_declares_nothing(self):
        """Break caught: `charts/<dep>/Chart.yaml` inside a chart read as this
        repository's own, so every consumer that vendors a dependency publishes it
        (a conflict with its owner) and its dependencies read as the consumer's.
        A `charts/<name>/` directory with no chart above it is a chart repository's
        layout, and publishes."""
        layer = self.helm(
            {
                "app": {
                    "Chart.yaml": _chart("app", "redis"),
                    "charts/redis/Chart.yaml": _chart("redis", "common"),
                },
                "redis-charts": {"charts/redis/Chart.yaml": _chart("redis")},
            }
        )
        self.assertEqual(self.provider(layer, "app", "redis"), "redis-charts")
        self.assertEqual(layer.conflicts, {})
        self.assertEqual(layer.dependencies["unresolved_targets_outside_estate"]["helm_chart"], {})

    def test_the_chart_is_part_of_the_aggregation_key(self):
        """Break caught: two charts from one repository of charts folded into one
        edge, so `dependents` of that repository cannot say which chart."""
        layer = self.helm(
            {
                "app": {"Chart.yaml": _chart("app", "api", "web")},
                "charts": {
                    "charts/api/Chart.yaml": _chart("api"),
                    "charts/web/Chart.yaml": _chart("web"),
                },
            }
        )
        self.assertEqual(
            [(e["repository"], e["chart"]) for e in layer.by_repo["app"]["depends_on"]],
            [("charts", "api"), ("charts", "web")],
        )

    def test_a_flux_source_ref_is_the_last_step(self):
        """Break caught: step 4 lost, so a `HelmRelease` whose chart no clone defines
        stops naming the repository its `sourceRef` points at - today's `uses_chart`
        behaviour, which the graph and the layer must agree on."""
        if not HAS_YAML:
            self.skipTest("needs the `deploy` extra (PyYAML)")
        release = (
            "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\nmetadata:\n  name: x\n"
            "spec:\n  chart:\n    spec:\n      chart: ext\n      version: 2.0.0\n"
            "      sourceRef:\n        kind: GitRepository\n        name: flux-src\n"
        )
        layer = self.helm({"deployer": {"release.yaml": release}, "flux-src": {"x.txt": "x\n"}})
        self.assertEqual(self.provider(layer, "deployer", "ext"), "flux-src")
        self.assertEqual(layer.by_repo["deployer"]["depends_on"][0]["refs_pinned"], {"2.0.0": 1})

    def test_a_release_of_a_chart_defined_in_a_subdirectory_resolves_to_that_clone(self):
        """Break caught: only a clone's root `Chart.yaml` read as a definition, or a
        subdirectory taken for a vendored copy, so a release of a chart from a
        `HelmRepository` - no clone to fall back on - resolves to nothing. A
        `Chart.yaml` at `<clone>/<chart>/` with no chart above it defines the chart."""
        if not HAS_YAML:
            self.skipTest("needs the `deploy` extra (PyYAML)")
        layer = self.helm(
            {
                "deployer": {"apps/x/release.yaml": RELEASE},
                "charts-repo": {"xchart/Chart.yaml": _chart("xchart")},
            }
        )
        self.assertEqual(self.provider(layer, "deployer", "xchart"), "charts-repo")
        self.assertEqual(layer.dependencies["unresolved_targets_outside_estate"]["helm_chart"], {})


class SelfRuleTest(_HelmStore):
    """`self` means resolution landed on the consumer, and nothing else does."""

    def test_an_umbrella_using_a_chart_another_clone_owns_is_an_edge_to_that_clone(self):
        """Break caught: a reference skipped as `self` because its repository also
        publishes the target, before resolution runs - so an umbrella bundling a
        chart reads as depending on nothing, and the chart's owner loses a
        dependent."""
        layer = self.helm(
            {
                "bundle": {
                    "Chart.yaml": _chart("bundle", "api"),
                    "src/api/Chart.yaml": _chart("api"),
                },
                "owner": {"Chart.yaml": _chart("api")},
            },
            umbrellas={"bundle"},
        )
        self.assertEqual(self.provider(layer, "bundle", "api"), "owner")
        self.assertEqual(layer.skipped["helm_chart"], {})

    def test_an_umbrella_using_a_chart_the_pattern_names_is_an_edge_to_that_clone(self):
        """Break caught: the same pre-emptive skip overriding step 1, so the
        configured chart repository loses the umbrella as a dependent."""
        layer = self.helm(
            {
                "bundle": {
                    "Chart.yaml": _chart("bundle", "web"),
                    "src/web/Chart.yaml": _chart("web"),
                },
                "chart-web": {"README.md": "x\n"},
            },
            pattern="chart-{name}",
            umbrellas={"bundle"},
        )
        self.assertEqual(self.provider(layer, "bundle", "web"), "chart-web")

    def test_a_coordinate_two_clones_publish_follows_the_conflict_resolution(self):
        """Break caught: a multi-module repository's dependency on a coordinate it
        and another clone both publish skipped as `self` when the conflict
        resolved to the other clone."""
        self.repos.mkdir()
        _write(
            self.repos / "multi",
            {
                "pom.xml": _pom("multi-parent", ""),
                "core/pom.xml": _pom("shared", ""),
                "app/pom.xml": _pom("multi-app", "shared"),
            },
        )
        _write(self.repos / "a-shared", {"pom.xml": _pom("shared", "")})
        layer = self.build(kinds=(_kind("maven_artifact"),))
        self.assertEqual(
            [e["repository"] for e in layer.by_repo["multi"]["depends_on"]], ["a-shared"]
        )
        self.assertEqual(
            layer.conflicts, {"maven_artifact": {"com.example:shared": ["a-shared", "multi"]}}
        )
        self.assertEqual(layer.skipped["maven_artifact"], {})


class OrgCheckTest(_Store):
    def test_a_module_in_another_organisation_is_not_the_estates(self):
        """Break caught: a module source matched on the repository name alone, so a
        same-named module in another organisation reads as the estate's own."""
        self.estate()
        layer = self.build(kinds=(_kind("terraform_module"),), org="other-org")
        self.assertEqual(layer.by_repo["svc-a"]["depends_on"], [])
        unresolved = layer.dependencies["unresolved_targets_outside_estate"]["terraform_module"]
        self.assertEqual(
            unresolved["example-org/tf-network"], {"references": 1, "referenced_by": ["svc-a"]}
        )

    def test_the_organisation_matches_ignoring_case_and_empty_matches_any(self):
        """Break caught: a case-sensitive comparison (GitHub ignores case), or an
        unset organisation treated as matching nothing."""
        self.estate()
        for org in ("Example-Org", ""):
            with self.subTest(org=org):
                layer = self.build(kinds=(_kind("terraform_module"),), org=org)
                self.assertEqual(
                    [e["repository"] for e in layer.by_repo["svc-a"]["depends_on"]],
                    ["tf-network", "tf-storage"],
                )

    def test_each_clone_is_compared_against_its_own_organisation(self):
        """Break caught: every source compared against `KSB_GITHUB_ORG` alone, in a
        store holding clones from two organisations. Then a source naming the clone
        `other-org/shared-mod` is called somebody else's, and one naming
        `example-org/shared-mod`, which no clone is, lands on it. A clone the list
        does not name, or names off GitHub, falls back to `KSB_GITHUB_ORG`."""
        self.estate(
            {
                "shared-mod": {"main.tf": 'variable "z" {}\n'},
                "svc-d": {
                    "main.tf": (
                        'module "theirs" {\n  source = "github.com/other-org/shared-mod"\n}\n'
                        'module "wrong" {\n  source = "github.com/example-org/shared-mod"\n}\n'
                        'module "net" {\n  source = "github.com/example-org/tf-network"\n}\n'
                        'module "store" {\n  source = "github.com/example-org/tf-storage"\n}\n'
                    )
                },
            }
        )
        config.REPOSITORIES_CONFIG.parent.mkdir(parents=True)
        config.REPOSITORIES_CONFIG.write_text(
            "shared-mod|git@github.com:Other-Org/shared-mod.git|main\n"
            "svc-d|git@github.com:example-org/svc-d.git|main\n"
            "tf-storage|https://git.example.com/infra/tf-storage.git|main\n",
            encoding="utf-8",
        )
        config.configure(GITHUB_ORG="example-org")
        code, _, err = self.run_stage()
        self.assertEqual(code, 0, err)
        base = self.root / "knowledge" / "relationships"
        svc_d = json.loads((base / "by-repo" / "svc-d.json").read_text())
        self.assertEqual(
            [e["repository"] for e in svc_d["depends_on"]],
            ["shared-mod", "tf-network", "tf-storage"],
        )
        unresolved = json.loads((base / "repository-dependencies.json").read_text())[
            "unresolved_targets_outside_estate"
        ]["terraform_module"]
        self.assertNotIn("other-org/shared-mod", unresolved)
        self.assertEqual(
            unresolved["example-org/shared-mod"], {"references": 1, "referenced_by": ["svc-d"]}
        )


class MaskingTest(_Store):
    def test_an_example_line_is_masked_and_a_target_key_too(self):
        """Break caught: an example `line` or an unresolved target written as the
        file has it, so an `access_token=` in a module source reaches a committed
        file. The `user:token@` credential is stripped earlier, by the reference."""
        self.estate(
            {
                "svc-m": {
                    "main.tf": (
                        'module "m" {\n'
                        '  source = "git::https://github.com/example-org/tf-network.git'
                        f'?ref=v1&access_token={TOKEN}"\n'
                        "}\n"
                        'module "n" {\n'
                        f'  source = "git::https://user:{TOKEN}@git.example.test/o/r.git'
                        f'?ref=v2&access_token={TOKEN}"\n'
                        "}\n"
                    )
                }
            }
        )
        layer = self.build(kinds=(_kind("terraform_module"),))
        edge = layer.by_repo["svc-m"]["depends_on"][0]
        self.assertEqual(edge["repository"], "tf-network")
        self.assertEqual(edge["refs_pinned"], {"v1": 1})
        self.assertEqual(
            edge["examples"][0]["line"],
            'source = "git::https://github.com/example-org/tf-network.git'
            '?ref=v1&access_token=[masked]"',
        )
        unresolved = layer.dependencies["unresolved_targets_outside_estate"]["terraform_module"]
        self.assertIn("git::https://git.example.test/o/r.git?access_token=[masked]", unresolved)
        self.assertNotIn(TOKEN, json.dumps(layer.dependencies))
        self.assertNotIn(TOKEN, json.dumps(layer.by_repo))


class PinMaskingTest(_Store):
    def test_pin_and_conflict_keys_are_masked(self):
        """Break caught: `refs_pinned` and `conflicts` keys written as the file has
        them. A pin is store text - an npm spec can be a URL with `access_token=`,
        a Terraform `?ref=` can hold a token - and both are keys of committed files."""
        self.estate(
            {
                "svc-p": {
                    "package.json": json.dumps(
                        {
                            "name": "svc-p",
                            "dependencies": {
                                "@example/ui": "https://registry.example.test/ui.tgz"
                                f"?access_token={TOKEN}"
                            },
                        }
                    ),
                    "main.tf": (
                        f'module "m" {{\n  source = "github.com/example-org/tf-network?ref={GITHUB}"\n}}\n'
                    ),
                },
                "pub-a": {
                    "package.json": json.dumps(
                        {"name": f"https://p.example.test/x?access_token={TOKEN}"}
                    )
                },
                "pub-b": {
                    "package.json": json.dumps(
                        {"name": f"https://p.example.test/x?access_token={TOKEN}"}
                    )
                },
            }
        )
        layer = self.build(kinds=(_kind("terraform_module"), _kind("npm_package")))
        pins = {e["repository"]: e["refs_pinned"] for e in layer.by_repo["svc-p"]["depends_on"]}
        self.assertEqual(
            pins,
            {
                "tf-network": {"[masked]": 1},
                "ui-kit": {"https://registry.example.test/ui.tgz?access_token=[masked]": 1},
            },
        )
        self.assertEqual(
            layer.conflicts,
            {"npm_package": {"https://p.example.test/x?access_token=[masked]": ["pub-a", "pub-b"]}},
        )
        for written in (layer.dependencies, layer.by_repo, layer.conflicts):
            self.assertNotIn(TOKEN, json.dumps(written))
            self.assertNotIn(GITHUB, json.dumps(written))

    def test_an_ordinary_pin_is_unchanged(self):
        """Break caught: masking that rewrites a version (`v4.2.0`, `^2.1.0`), so
        every pin reads as a secret and the drift question has no answer."""
        self.estate()
        layer = self.build()
        pins = {e["repository"]: e["refs_pinned"] for e in layer.by_repo["svc-b"]["depends_on"]}
        self.assertEqual(pins["ui-kit"], {"^2.1.0": 1})
        self.assertEqual(pins["libs-java"], {"1.2.0": 1})


class UnreadableTest(_Store):
    def test_a_pom_that_will_not_parse_is_counted_and_named(self):
        """Break caught: one malformed POM ending the stage for the whole estate, or
        skipped without saying which file."""
        self.estate({"broken": {"pom.xml": "<project><unclosed></project>\n"}})
        layer = self.build(kinds=(_kind("maven_artifact"),))
        self.assertEqual(layer.unreadable, ["repositories/broken/pom.xml"])
        self.assertEqual(layer.skipped["maven_artifact"]["unreadable"], 1)
        self.assertEqual(
            [e["repository"] for e in layer.by_repo["svc-b"]["depends_on"]], ["libs-java"]
        )

    def test_a_file_that_cannot_be_opened_is_counted_and_named_per_kind(self):
        """Break caught: an unreadable `.tf`, `Chart.yaml` or `HelmRelease` read as
        empty, so its references vanish with nothing said - the read error swallowed
        by the helper that the parsers' own tests use."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads a mode-000 file")
        release = (
            "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\nmetadata:\n  name: x\n"
            "spec:\n  chart:\n    spec:\n      chart: ext\n"
        )
        self.estate(
            {
                "locked": {
                    "main.tf": 'module "m" {\n  source = "github.com/example-org/tf-network"\n}\n',
                    "Chart.yaml": _chart("locked", "redis"),
                    "release.yaml": release,
                }
            }
        )
        locked = [
            self.repos / "locked" / name for name in ("main.tf", "Chart.yaml", "release.yaml")
        ]
        for path in locked:
            path.chmod(0)
            self.addCleanup(path.chmod, 0o644)
        layer = self.build(kinds=(_kind("terraform_module"), _kind("helm_chart")))
        self.assertEqual(
            layer.unreadable,
            [
                "repositories/locked/Chart.yaml",
                "repositories/locked/main.tf",
                "repositories/locked/release.yaml",
            ],
        )
        self.assertEqual(layer.skipped["terraform_module"].get("unreadable"), 1)
        self.assertEqual(layer.skipped["helm_chart"].get("unreadable"), 2)


RELEASE = (
    "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\nmetadata:\n  name: x\n"
    "spec:\n  chart:\n    spec:\n      chart: xchart\n      version: 1.2.3\n"
    "      sourceRef:\n        kind: HelmRepository\n        name: some-helm-repo\n"
)


class MissingPyYamlTest(_Store):
    """PyYAML stays optional, and a file that needs it is never skipped for want of it."""

    def without_pyyaml(self) -> None:
        # `import yaml` raises ImportError while the module's entry is None: the
        # import boundary at which the reader finds PyYAML missing.
        patcher = mock.patch.dict(sys.modules, {"yaml": None})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_release_without_pyyaml_fails_the_stage_and_compare_naming_the_extra(self):
        """Break caught: a Flux `HelmRelease` reported as "not read" while the stage
        exits 0, so every reference in it is lost from a layer that reads as
        complete. The run and `--compare` both stop, name the file, the missing
        library and the extra that provides it, and write nothing."""
        self.estate({"deployer": {"apps/x/release.yaml": RELEASE}})
        same = self.root / "same.json"
        same.write_text('{"edges": []}\n', encoding="utf-8")
        self.without_pyyaml()
        for argv in ((), ("--compare", str(same))):
            with self.subTest(argv=argv):
                code, out, err = self.run_stage(*argv)
                self.assertEqual(code, 1, out + err)
                self.assertIn("PyYAML", err)
                self.assertIn("hmcts-knowledge-store-builder[deploy]", err)
                self.assertIn("deployer/apps/x/release.yaml", err)
                self.assertFalse((self.root / "knowledge" / "relationships").exists())

    def test_an_estate_with_no_release_builds_without_pyyaml(self):
        """Break caught: PyYAML made a requirement of every run, or of every YAML
        file, so an estate with no Flux release cannot build its layer on the
        default install."""
        self.repos.mkdir()
        _write(
            self.repos / "app",
            {"Chart.yaml": _chart("app", "web"), "values.yaml": "replicaCount: 2\n"},
        )
        _write(self.repos / "web-charts", {"web/Chart.yaml": _chart("web")})
        self.without_pyyaml()
        code, out, err = self.run_stage()
        self.assertEqual(code, 0, out + err)
        body = json.loads(
            (self.root / "knowledge" / "relationships" / "by-repo" / "app.json").read_text()
        )
        self.assertEqual([e["repository"] for e in body["depends_on"]], ["web-charts"])


class UnparsedReleaseTest(_Store):
    def test_a_release_that_will_not_parse_is_counted_and_named(self):
        """Break caught: a file holding a `HelmRelease` that PyYAML refuses read as
        holding no release, so its chart reference vanishes with nothing counted -
        the drop a store's parity run found. It is unreadable, and named."""
        if not HAS_YAML:
            self.skipTest("needs the `deploy` extra (PyYAML)")
        self.estate({"broken": {"release.yaml": RELEASE + "  values: {unclosed: [\n"}})
        layer = self.build(kinds=(_kind("helm_chart"),))
        self.assertEqual(layer.unreadable, ["repositories/broken/release.yaml"])
        self.assertEqual(layer.skipped["helm_chart"], {"unreadable": 1})


# Every declaration in the fixture estate plus EXTRA_CLONES, counted by hand from the
# files. terraform_module: svc-a/main.tf's seven module sources and svc-c's three.
# helm_chart: chart-web's two dependencies and svc-b's release. maven_artifact: four
# in libs-java/pom.xml, one in lib-b, one in svc-b. npm_package: ui-kit's one and
# svc-b's three.
DECLARED = {"terraform_module": 10, "helm_chart": 3, "maven_artifact": 6, "npm_package": 4}


class ConservationTest(_Store):
    """Every reference ends as exactly one of an edge, an unresolved target, or a
    counted skip with a reason."""

    def test_every_declaration_in_the_estate_is_placed_exactly_once(self):
        """Break caught: a reference read and then neither drawn as an edge, listed
        as unresolved nor counted as skipped - a dependency missing from a layer
        that reads as complete. The stage's own conservation check stops the build
        on that before this test reads anything."""
        self.estate()
        layer = self.build()
        unresolved = layer.dependencies["unresolved_targets_outside_estate"]
        placed = {
            kind: counts["references"]
            + sum(entry["references"] for entry in unresolved[kind].values())
            + sum(counts["skipped"].values())
            for kind, counts in layer.kinds.items()
        }
        expected = dict(DECLARED)
        if not HAS_YAML:
            expected["helm_chart"] -= 1  # svc-b's release is removed without PyYAML
        self.assertEqual(placed, expected)

    def test_a_kind_that_does_not_balance_is_named_with_each_figure(self):
        """Break caught: the check comparing the wrong quantities - counting an
        unreadable file as a placed reference, or a balanced kind reported - so it
        passes over a dropped reference or fails a sound build."""
        self.assertEqual(
            relationships.unaccounted(
                {"npm_package": 5, "helm_chart": 3},
                {"npm_package": 2, "helm_chart": 1},
                {"npm_package": 1, "helm_chart": 1},
                {"npm_package": {"self": 1, "unreadable": 4}, "helm_chart": {"self": 1}},
            ),
            ["npm_package: 5 references read, 4 placed (2 in edges, 1 unresolved, 1 skipped)"],
        )


class StageTest(_Store):
    def test_extra_is_read_from_its_input_file(self):
        """Break caught: `extra` not read, or a previous run's value preserved, which
        would make the stage depend on its own output."""
        self.estate()
        extra = self.root / "knowledge" / "relationships" / "extra"
        extra.mkdir(parents=True)
        (extra / "svc-a.json").write_text('{"team": "platform"}\n', encoding="utf-8")
        code, _, err = self.run_stage()
        self.assertEqual(code, 0, err)
        by_repo = self.root / "knowledge" / "relationships" / "by-repo"
        self.assertEqual(
            json.loads((by_repo / "svc-a.json").read_text())["extra"], {"team": "platform"}
        )
        self.assertEqual(json.loads((by_repo / "svc-b.json").read_text())["extra"], {})

    def test_a_rebuild_is_byte_identical(self):
        """Break caught: anything order- or time-dependent in the output (CLAUDE.md
        1.3): unsorted keys, a set iterated, a timestamp."""
        self.estate()
        self.assertEqual(self.run_stage()[0], 0)
        first = self.snapshot()
        self.assertEqual(self.run_stage()[0], 0)
        self.assertEqual(self.snapshot(), first)
        self.assertEqual(
            sorted(first),
            [
                "by-repo/chart-web.json",
                "by-repo/libs-java.json",
                "by-repo/svc-a.json",
                "by-repo/svc-b.json",
                "by-repo/svc-c.json",
                "by-repo/tf-network.json",
                "by-repo/tf-storage.json",
                "by-repo/ui-kit.json",
                "kinds.json",
                "repository-dependencies.json",
            ],
        )
        text = first["repository-dependencies.json"].decode()
        self.assertEqual(
            text,
            json.dumps(json.loads(text), indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        )

    def test_a_removed_clone_loses_its_file_and_nothing_else_is_touched(self):
        """Break caught: `by-repo/` only ever added to, so a repository that left the
        estate keeps answering dependency questions; or the cleanup removing a file
        that is not a repository's."""
        self.estate()
        self.run_stage()
        by_repo = self.root / "knowledge" / "relationships" / "by-repo"
        (by_repo / "README.txt").write_text("kept\n", encoding="utf-8")
        shutil.rmtree(self.repos / "tf-storage")
        code, _, err = self.run_stage()
        self.assertEqual(code, 0, err)
        self.assertFalse((by_repo / "tf-storage.json").exists())
        self.assertEqual((by_repo / "README.txt").read_text(), "kept\n")
        svc_a = json.loads((by_repo / "svc-a.json").read_text())
        self.assertEqual([e["repository"] for e in svc_a["depends_on"]], ["tf-network"])

    def test_the_stage_prints_one_line_per_kind_and_records_telemetry(self):
        """Break caught: the run silent about what it found, or its counts not
        recorded, so the next build cannot say what moved."""
        self.estate()
        code, out, _ = self.run_stage()
        self.assertEqual(code, 0)
        self.assertIn("terraform_module: 3 edges, 4 references, 4 unresolved, 2 skipped", out)
        self.assertIn("between 8 repositories", out)
        recorded = json.loads((self.root / "knowledge" / "telemetry.json").read_text())
        self.assertEqual(recorded["measurements"]["relationships.edges"], 6 if HAS_YAML else 5)

    def test_a_missing_repositories_directory_exits_1(self):
        """Break caught: a store with no clones writing an empty layer over a real one."""
        code, _, err = self.run_stage()
        self.assertEqual(code, 1)
        self.assertIn("repositories", err)
        self.assertFalse((self.root / "knowledge" / "relationships").exists())

    def test_an_unwritable_output_exits_2(self):
        """Break caught: a failed write reported as success, or as a configuration
        error the operator would look for in the wrong place."""
        self.estate()
        (self.root / "knowledge").mkdir()
        (self.root / "knowledge" / "relationships").write_text("a file, not a directory\n")
        code, _, err = self.run_stage()
        self.assertEqual(code, 2, err)

    def test_compare_exits_0_on_an_equal_file_and_1_on_a_difference(self):
        """Break caught: the parity gate unable to fail, or failing on an equal file.
        It reports the (consumer, provider, chart, kind) that moved."""
        self.estate()
        self.run_stage()
        written = self.root / "knowledge" / "relationships" / "repository-dependencies.json"
        same = self.root / "same.json"
        shutil.copy(written, same)
        code, out, err = self.run_stage("--compare", str(same))
        self.assertEqual(code, 0, out + err)

        other = json.loads(written.read_text())
        for edge in other["edges"]:
            if (edge["consumer"], edge["provider"]) == ("svc-c", "svc-a"):
                edge["references"] = 5
                edge["kinds"]["terraform_module"] = 5
        del other["unresolved_targets_outside_estate"]["npm_package"]["react"]
        differing = self.root / "differing.json"
        differing.write_text(json.dumps(other), encoding="utf-8")
        code, out, _ = self.run_stage("--compare", str(differing))
        self.assertEqual(code, 1)
        self.assertIn("svc-c -> svc-a", out)
        self.assertIn("terraform_module", out)
        self.assertIn("react", out)

    def test_compare_reports_a_difference_in_unpinned_references(self):
        """Break caught: `refs_unpinned` left out of the comparison, so a parity run
        passes while floating references moved between pinned and not."""
        self.estate()
        self.run_stage()
        written = self.root / "knowledge" / "relationships" / "repository-dependencies.json"
        other = json.loads(written.read_text())
        for edge in other["edges"]:
            if (edge["consumer"], edge["provider"]) == ("svc-c", "svc-a"):
                edge["refs_unpinned"] = 0
                edge["refs_pinned"] = {"v2": 2}
        differing = self.root / "differing.json"
        differing.write_text(json.dumps(other), encoding="utf-8")
        code, out, _ = self.run_stage("--compare", str(differing))
        self.assertEqual(code, 1)
        self.assertIn("svc-c -> svc-a", out)
        self.assertIn("refs_unpinned 1 here, 0 there", out)

    def test_compare_help_says_it_reads_only_this_stages_own_format(self):
        """Break caught: `--compare` described as reading another build or another
        tool's output, so a store points it at its script's file and reads the
        shape refusal as a failed parity check. Parity with a script is reached by
        converting the script's output to this format first."""
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            build_relationships.main(["--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("in this stage's own layer format only", text)
        self.assertNotIn("another", text)

    def test_compare_refuses_a_file_of_the_wrong_shape_cleanly(self):
        """Break caught: a malformed comparison file ending in a traceback rather
        than an exit 1 that says what is wrong with it."""
        self.estate()
        for body in (
            {"edges": [{"consumer": "a", "provider": "b", "kinds": ["helm_chart"]}]},
            {"edges": {"not": "a list"}},
            {"edges": [], "unresolved_targets_outside_estate": ["x"]},
            {"edges": [], "unresolved_targets_outside_estate": {"npm_package": {"x": 3}}},
            {"edges": [{"consumer": "a", "provider": "b", "refs_unpinned": "2"}]},
            [],
        ):
            with self.subTest(body=body):
                path = self.root / "bad.json"
                path.write_text(json.dumps(body), encoding="utf-8")
                code, out, err = self.run_stage("--compare", str(path))
                self.assertEqual(code, 1)
                self.assertNotIn("Traceback", out + err)
                self.assertIn("bad.json", err)

    def test_written_files_get_the_umask_not_owner_only(self):
        """Break caught: files written through `mkstemp` keep its 0600, so a store
        committed from one account is unreadable to another and to CI."""
        self.estate()
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        self.run_stage()
        base = self.root / "knowledge" / "relationships"
        for path in (
            base / "kinds.json",
            base / "repository-dependencies.json",
            base / "by-repo" / "svc-a.json",
        ):
            with self.subTest(path=path.name):
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_compare_writes_nothing(self):
        """Break caught: a parity check that also rebuilds the store's layer."""
        self.estate()
        self.run_stage()
        written = self.root / "knowledge" / "relationships" / "repository-dependencies.json"
        same = self.root / "same.json"
        shutil.copy(written, same)
        shutil.rmtree(self.root / "knowledge" / "relationships")
        self.run_stage("--compare", str(same))
        self.assertFalse((self.root / "knowledge" / "relationships").exists())


class StatusTest(_Store):
    def _status(self) -> str:
        self.addCleanup(setattr, status, "run_git", status.run_git)
        status.run_git = lambda arguments: ""
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
            self.assertEqual(status.main([]), 0)
        return out.getvalue()

    def test_status_reports_the_layer_or_that_it_is_not_built(self):
        """Break caught: `status` silent about the layer, or failing on a store that
        has not built it (CLAUDE.md 6.7)."""
        self.assertIn("Relationships: not built", self._status())
        self.estate()
        self.run_stage()
        text = self._status()
        self.assertIn("Relationships:", text)
        self.assertIn("terraform_module 3 edges", text)

    def test_status_survives_a_pathologically_nested_kinds_file(self):
        """Break caught: `json.loads` raising `RecursionError` on a deeply nested
        file, which is neither `OSError` nor `ValueError`, so `status` fails
        (CLAUDE.md 6.7)."""
        path = self.root / "knowledge" / "relationships" / "kinds.json"
        path.parent.mkdir(parents=True)
        path.write_text("[" * 200000 + "]" * 200000, encoding="utf-8")
        self.assertIn(
            "Relationships: knowledge/relationships/kinds.json is unreadable", self._status()
        )
