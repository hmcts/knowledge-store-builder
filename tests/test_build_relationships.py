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
import shutil
import tempfile
from pathlib import Path

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
            self.root, self.clones(), kinds, org, pattern, set(umbrellas), examples, urls
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
        chart key, the uncapped count, the per-kind count, a pin read as another or
        floating lost as `""`, an example's link or line - or the per-repository
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
                        "refs_pinned": {"": 1},
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
                        "refs_pinned": {"": 1, "v2": 1},
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
                "edges": 3,
                "references": 4,
                "unresolved": 4,
                "skipped": {"local": 1, "self": 1},
                "notes": {},
            },
        )
        self.assertEqual(
            (layer.kinds["maven_artifact"]["edges"], layer.kinds["maven_artifact"]["references"]),
            (1, 1),
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


class ChartResolutionTest(_Store):
    """The five steps, in order, each shown deciding."""

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
