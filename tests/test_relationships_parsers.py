"""The relationship parsers read what a repository declares, and nothing else.

Four built-in kinds - Terraform module sources, Helm charts, Maven artifacts and
npm packages - each turn declaration files into `Reference`s. Every expected
value here is written by hand from the fixture estate under
`tests/fixtures/relationships/`, never computed by the code under test: the
parsers are held to what the files say.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import deploy_flux, relationships  # noqa: E402

try:  # PyYAML is the `deploy` extra, not a runtime dependency of this library
    import yaml

    HAS_YAML = True
except ImportError:  # pragma: no cover - the default-install CI job takes this path
    HAS_YAML = False

needs_yaml = unittest.skipUnless(HAS_YAML, "needs the `deploy` extra (PyYAML)")

ESTATE = Path(__file__).resolve().parent / "fixtures" / "relationships"


def _clone(files: dict[str, str]) -> tempfile.TemporaryDirectory:
    """A throwaway clone holding exactly `files`, for a shape the shared fixture
    estate does not carry."""
    holder = tempfile.TemporaryDirectory()
    for rel, text in files.items():
        path = Path(holder.name) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return holder


def _kind(name: str) -> relationships.Kind:
    return next(kind for kind in relationships.BUILTIN if kind.name == name)


def _text(repo: str, rel: str) -> str:
    return (ESTATE / repo / rel).read_text(encoding="utf-8")


class RegistryTest(SettingsIsolated):
    def test_the_built_in_kinds_are_the_four_in_order(self):
        """Break caught: a kind dropped or reordered. Order is the order the layer
        reports in, so it is part of the output."""
        self.assertEqual(
            [kind.name for kind in relationships.BUILTIN],
            ["terraform_module", "helm_chart", "maven_artifact", "npm_package"],
        )
        self.assertEqual({kind.origin for kind in relationships.BUILTIN}, {"builtin"})


class TerraformSourcesTest(SettingsIsolated):
    def test_every_documented_form_gives_org_repo_ref_and_line(self):
        """Break caught: a form the regex stops matching, a `.git` suffix left in
        the repo name, or a `?ref=` that is read as part of the repository or lost.
        The five forms are the ones `_TF_SOURCE` documents; the line numbers are
        those of `tests/fixtures/relationships/svc-a/main.tf`."""
        got = [
            (ref.org, ref.target, ref.ref, ref.line, ref.text)
            for ref in relationships.scan(ESTATE / "svc-a", _kind("terraform_module"))
            if ref.resolved_repo
        ]
        self.assertEqual(
            got,
            [
                (
                    "example-org",
                    "tf-network",
                    "v4.2.0",
                    2,
                    'source = "git::https://github.com/example-org/tf-network.git?ref=v4.2.0"',
                ),
                (
                    "example-org",
                    "tf-storage",
                    "",
                    6,
                    'source = "git@github.com:example-org/tf-storage.git"',
                ),
                (
                    "example-org",
                    "tf-queue",
                    "main",
                    10,
                    'source = "ssh://git@github.com/example-org/tf-queue.git?ref=main"',
                ),
                (
                    "example-org",
                    "tf-dns",
                    "v1.0.1",
                    14,
                    'source = "github.com/example-org/tf-dns//modules/zone?ref=v1.0.1"',
                ),
                (
                    "example-org",
                    "tf-cache",
                    "",
                    18,
                    'source = "github.com/example-org/tf-cache"',
                ),
            ],
        )

    def test_a_ref_after_another_query_argument_is_still_the_ref(self):
        """Break caught: the ref read only when `?ref=` is first, which loses the
        pin on `?depth=1&ref=v2`."""
        text = 'module "m" {\n  source = "github.com/o/m?depth=1&ref=v2"\n}\n'
        got = _kind("terraform_module").references(Path(), "main.tf", text)
        self.assertEqual([ref.ref for ref in got], ["v2"])


class TerraformKindTest(SettingsIsolated):
    def test_a_repository_source_resolves_outright_and_a_registry_one_does_not(self):
        """Break caught: a registry source resolved to a repository (a guess), or
        dropped (a dependency the store does not hold, which must be reported)."""
        refs = relationships.scan(ESTATE / "svc-a", _kind("terraform_module"))
        by_target = {ref.target: ref for ref in refs}
        self.assertEqual(
            sorted(by_target),
            [
                "hashicorp/consul/aws",
                "tf-cache",
                "tf-dns",
                "tf-network",
                "tf-queue",
                "tf-storage",
            ],
        )
        pinned = by_target["tf-network"]
        self.assertEqual(
            (pinned.kind, pinned.chart, pinned.ref, pinned.rel, pinned.line),
            ("terraform_module", "", "v4.2.0", "main.tf", 2),
        )
        self.assertEqual(pinned.resolved_repo, "tf-network")
        registry = by_target["hashicorp/consul/aws"]
        self.assertIsNone(registry.resolved_repo)
        self.assertEqual((registry.rel, registry.line), ("main.tf", 22))

    def test_a_local_path_is_no_reference(self):
        """Break caught: `../modules/local-thing` reported as a dependency. It is
        inside the repository, so it is not a cross-repository reference at all."""
        refs = relationships.scan(ESTATE / "svc-a", _kind("terraform_module"))
        self.assertEqual([ref for ref in refs if "local-thing" in ref.target], [])

    def test_the_terraform_cache_is_ignored(self):
        """Break caught: `.terraform/` read as the repository's own files. It holds
        copies of upstream modules, whose sources would read as this repository's
        dependencies."""
        refs = relationships.scan(ESTATE / "svc-a", _kind("terraform_module"))
        self.assertNotIn("tf-cached-only", {ref.target for ref in refs})
        self.assertEqual({ref.rel for ref in refs}, {"main.tf"})


class TerraformScopeTest(SettingsIsolated):
    FILES = {
        "main.tf": (
            'terraform {\n  required_providers {\n    aws = {\n      source = "hashicorp/aws"\n    }\n  }\n}\n\n'
            'resource "aws_s3_object" "page" {\n  source = "files/app.zip"\n}\n\n'
            'resource "aws_s3_object" "tpl" {\n  source = "${path.module}/files/index.html"\n}\n\n'
            'resource "aws_s3_object" "gh" {\n  source = "github.com/example-org/not-a-module"\n}\n\n'
            'module "interpolated" {\n  source = "${path.module}/modules/x"\n}\n\n'
            '# module "old" {\n#   source = "hashicorp/old/aws"\n# }\n\n'
            'module "real" {\n  providers = {\n    source = "ignored-nested"\n  }\n'
            '  xsource = "ignored-suffix"\n  source = "hashicorp/consul/aws"\n}\n'
        )
    }

    def test_only_a_module_blocks_source_is_a_reference(self):
        """Break caught: every `source = "..."` in a .tf file reported as a module
        dependency - an `aws_s3_object` file argument, an interpolated path, a
        provider address, a commented-out block - which the store would then list
        as dependencies it does not hold."""
        with _clone(self.FILES) as root:
            refs = relationships.scan(Path(root), _kind("terraform_module"))
        self.assertEqual([(r.target, r.line) for r in refs], [("hashicorp/consul/aws", 34)])

    def test_an_interpolated_module_source_is_no_reference(self):
        """Break caught: `${path.module}/modules/x` inside a real module block read
        as a registry address. It is a local path the guard cannot see by prefix."""
        with _clone(self.FILES) as root:
            refs = relationships.scan(Path(root), _kind("terraform_module"))
        self.assertNotIn("interpolated", " ".join(r.text for r in refs))

    def _scan(self, text: str) -> list[relationships.Reference]:
        with _clone({"a.tf": text}) as root:
            return relationships.scan(Path(root), _kind("terraform_module"))

    def test_a_heredoc_body_is_not_structure(self):
        """Break caught: a `}` in a heredoc body ending the module block, so the
        `source` after it is dropped as outside a module."""
        refs = self._scan('module "a" {\n x = <<EOF\n}\nEOF\n source = "r/x"\n}\n')
        self.assertEqual([(r.target, r.line) for r in refs], [("r/x", 5)])

    def test_an_unbalanced_quote_and_brace_in_a_heredoc_are_text(self):
        """Break caught: `} "unbalanced {` in an indented heredoc read as brace and
        string, which swallows or ends the block and loses the source."""
        text = (
            'module "a" {\n x = <<-EOT\n  } "unbalanced {\n  EOT\n'
            ' source = "git::https://git.example.test/t/m.git?ref=v1"\n}\n'
        )
        refs = self._scan(text)
        self.assertEqual(
            [(r.target, r.ref) for r in refs], [("git::https://git.example.test/t/m.git", "v1")]
        )

    def test_an_interpolation_holding_a_quote_and_a_brace_does_not_end_the_block(self):
        """Break caught: the string ended at the quote inside `${ ... }`, so the `}`
        in it closed the module block early."""
        text = 'module "a" {\n d = "${var.x == "}" ? 1 : 2}"\n source = "r/x"\n}\n'
        refs = self._scan(text)
        self.assertEqual([(r.target, r.line) for r in refs], [("r/x", 3)])

    def test_only_pin_parameters_leave_a_targets_query(self):
        """Break caught: the whole query dropped, so `?archive=zip&id=1` and `id=2`
        collapse into one target - or a pin left in, so two pins read as two."""
        tf = 'module "m" {{\n  source = "{}"\n}}\n'
        with _clone(
            {
                "1.tf": tf.format("https://h.example.test/x?archive=zip&id=1"),
                "2.tf": tf.format("https://h.example.test/x?archive=zip&id=2"),
                "3.tf": tf.format("https://h.example.test/x?ref=v2&depth=1"),
                "4.tf": tf.format("https://h.example.test/x?id=3&ref=v2&archive=zip"),
            }
        ) as root:
            refs = relationships.scan(Path(root), _kind("terraform_module"))
        self.assertEqual(
            [(r.target, r.ref) for r in refs],
            [
                ("https://h.example.test/x?archive=zip&id=1", ""),
                ("https://h.example.test/x?archive=zip&id=2", ""),
                ("https://h.example.test/x", "v2"),
                ("https://h.example.test/x?id=3&archive=zip", "v2"),
            ],
        )

    def test_a_non_github_git_source_keeps_its_pin_and_drops_it_from_the_target(self):
        """Break caught: `?ref=v2` on a non-GitHub source reported as floating, or
        left in the target so two pins of one module read as two modules."""
        files = {
            "a.tf": 'module "m" {\n  source = "git::https://git.example.test/team/mod.git?ref=v2"\n}\n'
        }
        with _clone(files) as root:
            (ref,) = relationships.scan(Path(root), _kind("terraform_module"))
        self.assertEqual(
            (ref.target, ref.ref), ("git::https://git.example.test/team/mod.git", "v2")
        )


class CredentialTest(SettingsIsolated):
    TOKEN = "xxxxx-not-a-real-token"

    def test_a_credential_in_a_url_reaches_no_field_of_any_kind(self):
        """Break caught: `user:token@` in a module source or an npm spec carried into
        `target`, `ref` or `text`. A target becomes a key of the unresolved report,
        so the token would be written unmasked."""
        url = f"git::https://user:{self.TOKEN}@git.example.test/team/mod.git?ref=v1"
        files = {
            "main.tf": f'module "m" {{\n  source = "{url}"\n}}\n',
            "package.json": (
                '{\n  "dependencies": {\n'
                f'    "private": "git+https://user:{self.TOKEN}@git.example.test/p.git#v2"\n'
                "  }\n}\n"
            ),
        }
        refs = []
        with _clone(files) as root:
            for name in ("terraform_module", "npm_package"):
                refs.extend(relationships.scan(Path(root), _kind(name)))
        self.assertEqual(len(refs), 2)
        for ref in refs:
            with self.subTest(kind=ref.kind):
                self.assertNotIn(self.TOKEN, repr(ref))
        self.assertEqual(refs[0].target, "git::https://git.example.test/team/mod.git")
        self.assertEqual(refs[0].ref, "v1")

    def test_a_credentialed_github_source_names_its_repository(self):
        """Break caught: the GitHub form matched before the credential is stripped,
        so `https://user:token@github.com/org/repo` names no repository and is
        reported as an address the estate does not hold."""
        url = f"git::https://user:{self.TOKEN}@github.com/example-org/tf-network.git?ref=v1"
        text = f'module "m" {{\n  source = "{url}"\n}}\n'
        refs = _kind("terraform_module").references(Path(), "main.tf", text)
        self.assertEqual(
            [(ref.org, ref.target, ref.resolved_repo, ref.ref, ref.line) for ref in refs],
            [("example-org", "tf-network", "tf-network", "v1", 2)],
        )
        self.assertNotIn(self.TOKEN, repr(refs))

    def test_an_ssh_source_with_any_login_or_a_credential_names_its_repository(self):
        """Break caught: only `ssh://git@github.com` read as GitHub. Stripping
        `user:token@` leaves `ssh://github.com`, and another login name leaves
        `ssh://deploy@github.com`; either then names no repository and is reported
        as an address the estate does not hold."""
        for url in (
            f"git::ssh://user:{self.TOKEN}@github.com/example-org/tf-network.git?ref=v1",
            "ssh://deploy@github.com/example-org/tf-network.git?ref=v1",
            "ssh://github.com/example-org/tf-network.git?ref=v1",
        ):
            with self.subTest(url=url):
                text = f'module "m" {{\n  source = "{url}"\n}}\n'
                refs = _kind("terraform_module").references(Path(), "main.tf", text)
                self.assertEqual(
                    [(ref.org, ref.target, ref.resolved_repo, ref.ref) for ref in refs],
                    [("example-org", "tf-network", "tf-network", "v1")],
                )
                self.assertNotIn(self.TOKEN, repr(refs))


class UserinfoTest(SettingsIsolated):
    def test_an_ssh_login_name_is_kept_and_a_credential_is_not(self):
        """Break caught: `git@` stripped from an ssh URL (the target no longer names
        the address that was written), or `user:pass@` / a bare http token kept."""
        for text, want in [
            (
                "git::ssh://git@gitlab.example.test/o/r.git",
                "git::ssh://git@gitlab.example.test/o/r.git",
            ),
            ("ssh://user@host.example.test/x", "ssh://user@host.example.test/x"),
            ("git+ssh://git@h.example.test/x", "git+ssh://git@h.example.test/x"),
            ("ssh://user:pw@host.example.test/x", "ssh://host.example.test/x"),
            ("ftp://user:pw@host.example.test/x", "ftp://host.example.test/x"),
            ("https://tok@h.example.test/x", "https://h.example.test/x"),
            ("git::http://tok@h.example.test/x", "git::http://h.example.test/x"),
            ("git+https://tok@h.example.test/x", "git+https://h.example.test/x"),
            ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(relationships.strip_userinfo(text), want)


class LocateTest(SettingsIsolated):
    def test_two_chart_dependencies_of_one_name_get_their_own_lines(self):
        """Break caught: an aliased dependency (`redis` twice) reporting the first
        one's line for both, so the second example cites the wrong place."""
        text = (
            "name: c\ndependencies:\n"
            "  - name: redis\n    version: 1.0.0\n    alias: one\n"
            "  - name: redis\n    version: 2.0.0\n    alias: two\n"
        )
        with _clone({"Chart.yaml": text}) as root:
            refs = relationships.scan(Path(root), _kind("helm_chart"))
        self.assertEqual([(r.ref, r.line) for r in refs], [("1.0.0", 3), ("2.0.0", 6)])

    def test_a_non_ascii_npm_name_is_found_on_its_own_line(self):
        """Break caught: the name searched for in its `\\u` escaped form, which the
        file does not contain, so the reference silently cites the section line."""
        # The blank line separates the fallback position (the line after the previous
        # hit) from the right one, so a name that is not found cannot pass by luck.
        text = '{\n  "dependencies": {\n    "a": "1",\n\n    "caf\u00e9": "^1.0.0"\n  }\n}\n'
        with _clone({"package.json": text}) as root:
            refs = relationships.scan(Path(root), _kind("npm_package"))
        self.assertEqual([(r.target, r.line) for r in refs], [("a", 3), ("caf\u00e9", 5)])


class ChartYamlTest(SettingsIsolated):
    ADVERSARIAL = {
        "comments": (
            "# top comment\n"
            "name: commented   # trailing\n"
            "dependencies:   # deps\n"
            "# a comment line\n"
            "- name: a  # c\n"
            "  version: 1.0.0 # c\n"
            '  repository: "https://x.example/#frag"\n'
            "  # between\n"
            "- name: b\n"
            '  version: "2"\n'
            "appVersion: 9\n"
        ),
        "quoted": (
            'name: "quoted-name"\n'
            "dependencies:\n"
            "  - name: 'single'\n"
            "    version: '1.0'\n"
            '    repository: "oci://r.example/a"\n'
            '  - name: "dbl"\n'
            "    version: ^2.0.0\n"
            "    condition: dbl.enabled\n"
            '  - name: "esc"\n'
            '    version: "1\\"#2"\n'
            "    repository: 'it''s # here'\n"
        ),
        "flow": (
            "name: flowed\n"
            'dependencies: [{name: a, version: "1.0", repository: "https://x.example"},\n'
            "  {name: b, version: ^2.0.0}]\n"
            "keywords: [one, two]\n"
        ),
    }

    @staticmethod
    def _reference(text: str) -> tuple[str, list[dict]]:
        # `str()` normalises PyYAML's typed scalars, and is only safe for fixtures
        # whose values are strings there: an unquoted `version: 1.10` loads as the
        # float 1.1 and would fail here although the reader's "1.10" is the right one.
        document = yaml.safe_load(text)
        keep = ("name", "version", "repository")
        return str(document["name"]), [
            {key: str(dep[key]) for key in keep if key in dep}
            for dep in document.get("dependencies", [])
        ]

    @needs_yaml
    def test_the_stdlib_reader_equals_pyyaml_on_every_fixture_chart(self):
        """Break caught: the hand-written reader diverging from a real YAML parser
        on a chart the estate actually holds."""
        charts = sorted(ESTATE.glob("*/Chart.yaml"))
        self.assertTrue(charts)
        for path in charts:
            with self.subTest(chart=path.parent.name):
                text = path.read_text(encoding="utf-8")
                self.assertEqual(relationships.chart_yaml(text), self._reference(text))

    @needs_yaml
    def test_the_stdlib_reader_equals_pyyaml_on_comments_quotes_and_flow_lists(self):
        """Break caught: a `#` inside a quoted URL read as a comment, a comment line
        between list items ending the block, quotes kept in a value, or a
        flow-style list read as nothing."""
        for label, text in self.ADVERSARIAL.items():
            with self.subTest(case=label):
                self.assertEqual(relationships.chart_yaml(text), self._reference(text))

    def test_the_stdlib_reader_matches_hand_derived_values(self):
        """Break caught: the same divergence, without PyYAML in the way - this
        runs on the default install, where the comparison above skips."""
        self.assertEqual(
            relationships.chart_yaml(_text("chart-web", "Chart.yaml")),
            (
                "chart-web",
                [
                    {
                        "name": "common",
                        "version": "1.2.3",
                        "repository": "https://charts.example.test/stable",
                    },
                    {
                        "name": "redis",
                        "version": "~17.0.0",
                        "repository": "oci://registry.example.test/charts",
                    },
                ],
            ),
        )
        self.assertEqual(
            relationships.chart_yaml(self.ADVERSARIAL["comments"]),
            (
                "commented",
                [
                    {"name": "a", "version": "1.0.0", "repository": "https://x.example/#frag"},
                    {"name": "b", "version": "2"},
                ],
            ),
        )

    def test_a_flow_list_is_read_without_pyyaml(self):
        """Break caught: flow-style `dependencies: [...]` read as an empty list."""
        self.assertEqual(
            relationships.chart_yaml(self.ADVERSARIAL["flow"]),
            (
                "flowed",
                [
                    {"name": "a", "version": "1.0", "repository": "https://x.example"},
                    {"name": "b", "version": "^2.0.0"},
                ],
            ),
        )

    def test_a_chart_dependency_is_a_helm_chart_reference(self):
        """Break caught: Chart.yaml dependencies not reaching the kind, or the
        chart name, version spec and line lost on the way."""
        refs = relationships.scan(ESTATE / "chart-web", _kind("helm_chart"))
        self.assertEqual(
            [(r.target, r.chart, r.ref, r.rel, r.line) for r in refs],
            [
                ("common", "common", "1.2.3", "Chart.yaml", 5),
                ("redis", "redis", "~17.0.0", "Chart.yaml", 8),
            ],
        )
        self.assertEqual(refs[0].text, "- name: common")
        self.assertTrue(all(r.kind == "helm_chart" for r in refs))

    def test_the_chart_a_repository_publishes_is_its_chart_yaml_name(self):
        """Break caught: publishers read from the directory name, not the chart."""
        self.assertEqual(
            _kind("helm_chart").publishes(ESTATE / "chart-web"),
            [("chart-web", "Chart.yaml")],
        )


@needs_yaml
class HelmReleaseTest(SettingsIsolated):
    def test_a_helm_release_is_read_through_the_flux_reader(self):
        """Break caught: a second, divergent reading of HelmRelease. The release's
        chart, version and sourceRef name come from `deploy_flux.chart_of`."""
        document = yaml.safe_load(_text("svc-b", "flux/helmrelease.yaml"))
        chart = deploy_flux.chart_of(document)
        assert chart is not None
        self.assertEqual(
            (chart.name, chart.version, chart.source), ("chart-web", "1.4.0", "chart-web")
        )

    def test_the_kind_reads_a_release_the_way_chart_of_does_and_no_other_way(self):
        """Break caught: the kind reimplementing the read instead of going through
        `deploy_flux.chart_of`. Three things only `chart_of` decides: a `sourceRef`
        of a kind it does not accept gives no source, a numeric version is
        stringified, and a document with no chart block (a patch) is no reference."""
        text = (
            "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\nmetadata:\n  name: a\n"
            "spec:\n  chart:\n    spec:\n      chart: odd\n      version: 1.4\n"
            "      sourceRef:\n        kind: ExternalArtifact\n        name: not-a-chart-source\n"
            "---\napiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\nmetadata:\n"
            "  name: a\nspec:\n  values:\n    replicaCount: 2\n"
        )
        with _clone({"r.yaml": text}) as root:
            (ref,) = relationships.scan(Path(root), _kind("helm_chart"))
        self.assertEqual((ref.target, ref.ref, ref.source, ref.line), ("odd", "1.4", "", 8))

    def test_a_release_whose_values_hold_a_bare_equals_sign_is_read(self):
        """Break caught: PyYAML's safe loader refusing `op: =` - it resolves a bare
        `=` to YAML 1.1's `value` type and has no constructor for it - so the file
        reads as not YAML and the release's chart reference above the values is
        lost. A policy rule in a release's values is where `=` is written."""
        text = (
            "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\nmetadata:\n  name: a\n"
            "spec:\n  chart:\n    spec:\n      chart: xchart\n      version: 1.2.3\n"
            "      sourceRef:\n        kind: HelmRepository\n        name: some-helm-repo\n"
            "  values:\n    criteria:\n      - key: namespace\n        op: =\n"
            "        value: '*'\n"
        )
        with _clone({"r.yaml": text}) as root:
            refs = relationships.scan(Path(root), _kind("helm_chart"))
        self.assertEqual(
            [(r.target, r.ref, r.source, r.line) for r in refs],
            [("xchart", "1.2.3", "some-helm-repo", 8)],
        )

    def test_a_release_that_will_not_parse_is_unreadable_not_empty(self):
        """Break caught: a file holding a `HelmRelease` that does not parse read as
        holding none, so the layer cannot count what it lost."""
        text = "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\nspec: {unclosed: [\n"
        with _clone({"r.yaml": text}) as root, self.assertRaises(ValueError):
            relationships.scan(Path(root), _kind("helm_chart"))

    def test_the_kind_reports_the_release_as_a_chart_reference(self):
        """Break caught: the release read but not turned into a reference, or its
        sourceRef dropped - the resolver falls back on it for a chart no clone
        defines."""
        refs = relationships.scan(ESTATE / "svc-b", _kind("helm_chart"))
        self.assertEqual(len(refs), 1)
        ref = refs[0]
        self.assertEqual(
            (ref.kind, ref.target, ref.chart, ref.ref, ref.rel, ref.source),
            ("helm_chart", "chart-web", "chart-web", "1.4.0", "flux/helmrelease.yaml", "chart-web"),
        )
        self.assertIsNone(ref.resolved_repo)


class PomTest(SettingsIsolated):
    def test_a_namespaced_pom_is_read(self):
        """Break caught: matching `project` only without its namespace, which reads
        every namespaced POM - nearly all of them - as empty."""
        pom = relationships.pom(_text("svc-b", "pom.xml"))
        self.assertEqual((pom.group, pom.artifact, pom.version), ("com.example", "svc-b", "2.0.0"))
        self.assertEqual(pom.dependencies, [("com.example", "lib-a", "1.2.0", 12)])

    def test_a_plain_pom_is_read_the_same(self):
        """Break caught: handling only the namespaced form."""
        pom = relationships.pom(_text("libs-java", "pom.xml"))
        self.assertEqual((pom.group, pom.artifact, pom.version), ("com.example", "lib-a", "1.2.0"))
        self.assertIsNone(pom.parent)

    def test_dependency_management_and_plugins_are_not_read(self):
        """Break caught: `<dependencyManagement>`, which declares a version without
        using the artifact, or a plugin's own dependencies counted as the
        project's. Both would invent reuse."""
        pom = relationships.pom(_text("libs-java", "pom.xml"))
        artifacts = {dep[1] for dep in pom.dependencies}
        self.assertNotIn("managed-only", artifacts)
        self.assertNotIn("plugin-only", artifacts)
        self.assertNotIn("some-plugin", artifacts)

    def test_profile_dependencies_are_read(self):
        """Break caught: dependencies declared only under a profile vanishing."""
        pom = relationships.pom(_text("libs-java", "pom.xml"))
        self.assertIn(("org.third", "profile-dep", "3.0", 42), pom.dependencies)

    def test_expressions_resolve_within_the_pom(self):
        """Break caught: `${project.groupId}`, `${project.version}` or a
        `<properties>` value left literal, which makes a coordinate that cannot
        match its publisher and a version that reads as unpinned."""
        pom = relationships.pom(_text("libs-java", "pom.xml"))
        self.assertEqual(pom.properties["lib.v"], "4.1")
        self.assertIn(("com.example", "lib-core", "1.2.0", 22), pom.dependencies)
        self.assertIn(("org.third", "widget", "4.1", 27), pom.dependencies)

    def test_an_unknown_property_stays_literal(self):
        """Break caught: an unresolvable `${example.lib.version}` replaced by an empty
        string, which reads as unpinned-by-omission rather than by property."""
        pom = relationships.pom(_text("libs-java", "pom.xml"))
        self.assertIn(("org.third", "gadget", "${example.lib.version}", 32), pom.dependencies)

    def test_a_child_inherits_its_parents_coordinates(self):
        """Break caught: the `<parent>` ignored, which leaves a child with no
        groupId (so no publisher) and `${project.parent.version}` literal."""
        pom = relationships.pom(_text("libs-java/lib-b", "pom.xml"))
        self.assertEqual(pom.parent, ("com.example", "lib-a", "1.2.0"))
        self.assertEqual((pom.group, pom.artifact, pom.version), ("com.example", "lib-b", "1.2.0"))
        self.assertEqual(pom.properties["own.v"], "1.2.0-b")
        self.assertEqual(pom.dependencies, [("com.example", "lib-core", "1.2.0-b", 16)])

    def test_a_malformed_pom_raises_rather_than_reading_as_empty(self):
        """Break caught: a file that will not parse read as a POM with no
        dependencies. The caller counts it as unreadable and names it."""
        with self.assertRaises(ValueError):
            relationships.pom("<project><dependencies></project>")

    def test_the_kind_reports_group_artifact_and_version_spec(self):
        """Break caught: the kind reading a different quantity than `pom()` - the
        target must be `group:artifact` and the version spec the `ref`."""
        refs = relationships.scan(ESTATE / "libs-java", _kind("maven_artifact"))
        got = sorted((r.rel, r.line, r.target, r.ref) for r in refs)
        self.assertEqual(
            got,
            [
                ("lib-b/pom.xml", 16, "com.example:lib-core", "1.2.0-b"),
                ("pom.xml", 22, "com.example:lib-core", "1.2.0"),
                ("pom.xml", 27, "org.third:widget", "4.1"),
                ("pom.xml", 32, "org.third:gadget", "${example.lib.version}"),
                ("pom.xml", 42, "org.third:profile-dep", "3.0"),
            ],
        )
        self.assertEqual(refs[0].text, "<artifactId>lib-core</artifactId>")

    def test_each_pom_publishes_its_own_coordinate(self):
        """Break caught: a child POM publishing nothing because it names no
        groupId, or publishing its parent's artifact."""
        self.assertEqual(
            _kind("maven_artifact").publishes(ESTATE / "libs-java"),
            [("com.example:lib-b", "lib-b/pom.xml"), ("com.example:lib-a", "pom.xml")],
        )


class NpmTest(SettingsIsolated):
    def test_all_three_dependency_sections_are_read_with_their_specs(self):
        """Break caught: `peerDependencies` or `devDependencies` skipped, or the
        version spec lost - it is what the pin summary is built from."""
        refs = relationships.scan(ESTATE / "svc-b", _kind("npm_package"))
        self.assertEqual(
            [(r.target, r.ref, r.rel, r.line) for r in refs],
            [
                ("@example/ui", "^2.1.0", "package.json", 4),
                ("typescript", "~5.4.0", "package.json", 7),
                ("react", ">=18", "package.json", 10),
            ],
        )
        self.assertEqual(refs[0].text, '"@example/ui": "^2.1.0"')

    def test_a_package_publishes_its_name_and_manifest(self):
        """Break caught: the publisher map missing the scoped name consumers use."""
        self.assertEqual(
            _kind("npm_package").publishes(ESTATE / "ui-kit"), [("@example/ui", "package.json")]
        )


if __name__ == "__main__":
    unittest.main()
