"""A deployment node publishes its config, so a credential in it must be withheld (#541).

`deployments` copied every config value, up to 200 characters, into the graph,
and from there into the explorer page and the MCP server's answers. A credential
typed literally into an overlay was published with it, and nothing downstream
read the graph for one. These tests run the real stage over real files on disk
and assert on the graph it wrote, every file it wrote, and everything it printed.

Every value here is invented. Each is shaped to trip a rule and is obviously
fake; the one with a vendor prefix is assembled from two literals so the
repository's own secret scanner does not read the source as a leak.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path

# Importing this first puts the working tree ahead of any installed copy.
from settings_isolation import SettingsIsolated
from knowledgestore import build_deployments as deployments
from collections import Counter

from knowledgestore import check_evidence, config, deploy_secrets, secret_mask
from test_flux_kustomize_deployments import REPO, _deploy_repo, _facts, _graph, _write, needs_yaml

PASSWORD = "fake-overlay-pw-0123"
INSTRUMENTATION_KEY = "00000000-1111-2222-3333-444444444444"
VENDOR_KEY = "sk" + "_live_" + "fake0123456789"
STORE_VALUE = "REC/4821"
STORE_RULE = {"record-reference": r"\bREC/[0-9]{4}\b"}
SECRETS = (PASSWORD, INSTRUMENTATION_KEY, VENDOR_KEY)

BASE = """\
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
    image: registry.example/alpha:1.4.0
    appInsights:
      instrumentationKey: {key}
    database:
      dbPassword: {password}
    notifier:
      endpoint: https://hooks.example/alpha
      header: {vendor}
    note: {store_value}
""".format(key=INSTRUMENTATION_KEY, password=PASSWORD, vendor=VENDOR_KEY, store_value=STORE_VALUE)

CLUSTER = """\
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: apps
spec:
  path: ./apps/environments/staging
"""

PATCH = """\
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: alpha
spec:
  values:
    replicaCount: 3
"""

LAYOUT = {
    "apps/base/services/alpha/helmrelease.yaml": BASE,
    "apps/environments/staging/services/alpha.yaml": PATCH,
    "clusters/staging/cluster-a/apps.yaml": CLUSTER,
}


def _run(tmp: Path) -> tuple[dict, str, str]:
    """The stage's written graph, its stdout and its stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = deployments.main()
    if code != 0:
        raise AssertionError(f"the stage returned {code}: {out.getvalue()}{err.getvalue()}")
    graph = json.loads((tmp / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    return graph, out.getvalue(), err.getvalue()


def _written_text(tmp: Path) -> dict[str, str]:
    """Every file the stage leaves under the store's graphify-out, decompressed."""
    texts: dict[str, str] = {}
    for path in sorted((tmp / "graphify-out").rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                texts[path.name] = handle.read()
        else:
            texts[path.name] = path.read_text(encoding="utf-8")
    return texts


@needs_yaml
class AFlaggedValueIsWithheldFromTheNode(SettingsIsolated):
    """Break it catches: a value that matches a secret rule written to the node as
    it was read, which publishes a literal credential to the graph, the page and
    the MCP answers. And the converse: a rule so eager that it takes ordinary
    configuration with it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        _graph(self.root)
        _deploy_repo(self.root, LAYOUT)
        config.configure(root=self.root, DEPLOY_REPOS={REPO}, SENSITIVE_PATTERNS=STORE_RULE)
        self.graph, self.stdout, self.stderr = _run(self.root)
        self.node = _facts(self.graph)["alpha (staging)"]["metadata"]

    def test_a_value_under_a_secret_named_key_is_withheld(self):
        self.assertEqual(self.node["config"]["database.dbPassword"], "[key name withheld]")

    def test_an_instrumentation_key_is_withheld(self):
        """The finding that raised this: a real key in a non-production overlay."""
        self.assertEqual(
            self.node["config"]["appInsights.instrumentationKey"], "[key name withheld]"
        )

    def test_a_value_shaped_like_a_vendor_token_is_withheld_under_a_harmless_key(self):
        self.assertEqual(self.node["config"]["notifier.header"], "[sk key withheld]")

    def test_a_store_rule_is_applied(self):
        """`KSB_SENSITIVE_PATTERNS` is the store's own definition of what must not be
        published; a stage that read only the built-in rules ignores it."""
        self.assertEqual(self.node["config"]["note"], "[record reference withheld]")

    def test_the_withheld_keys_are_listed_sorted(self):
        self.assertEqual(
            self.node["config_withheld"],
            [
                "appInsights.instrumentationKey",
                "database.dbPassword",
                "note",
                "notifier.header",
            ],
        )

    def test_a_value_that_matches_nothing_is_kept_unchanged(self):
        self.assertEqual(self.node["config"]["replicaCount"], "3")
        self.assertEqual(self.node["config"]["image"], "registry.example/alpha:1.4.0")
        self.assertEqual(self.node["config"]["notifier.endpoint"], "https://hooks.example/alpha")

    def test_the_base_fact_is_masked_as_well(self):
        base = _facts(self.graph)[f"alpha ({config.DEPLOY_BASE_ENV})"]["metadata"]
        self.assertEqual(base["config"]["database.dbPassword"], "[key name withheld]")

    def test_a_node_with_nothing_withheld_carries_no_marker(self):
        """Absent rather than empty, as `config_truncated` is, so a graph built
        before the marker differs only where it applies."""
        _write(
            self.root / "repositories" / REPO,
            {"apps/base/services/alpha/helmrelease.yaml": BASE.split("    appInsights:")[0]},
        )
        graph, _, _ = _run(self.root)
        for node in _facts(graph).values():
            self.assertNotIn("config_withheld", node["metadata"])

    def test_the_value_is_in_no_output_and_no_file_the_stage_wrote(self):
        """Every sink, not the graph alone: the report, stderr, the compressed copy
        and the labels file each republish a value that reaches them."""
        sinks = {"stdout": self.stdout, "stderr": self.stderr, **_written_text(self.root)}
        # The sinks this loop covers must exist, or it passes over nothing.
        self.assertIn("graph.json", sinks)
        self.assertIn("graph.json.gz", sinks)
        for name, text in sinks.items():
            for secret in (*SECRETS, STORE_VALUE):
                with self.subTest(sink=name, value=secret[:6]):
                    self.assertNotIn(secret, text)

    def test_the_report_names_repository_file_key_and_rule(self):
        self.assertIn("withheld", self.stdout)
        self.assertIn(
            f"{REPO}: apps/base/services/alpha/helmrelease.yaml  "
            "key database.dbPassword  rule key-name",
            self.stdout,
        )
        self.assertIn("rotate", self.stdout)


@needs_yaml
class ReportingIsCappedAndNeverQuotes(SettingsIsolated):
    """Break it catches: a report that grows with the number of findings until it
    buries the run, and one that quotes a value to be helpful."""

    def test_a_long_list_is_capped_and_the_total_stays_whole(self):
        keys = "".join(f"    credential{n:02d}Password: {PASSWORD}-{n}\n" for n in range(13))
        base = BASE.split("    appInsights:")[0] + keys
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _graph(root)
            _deploy_repo(root, {**LAYOUT, "apps/base/services/alpha/helmrelease.yaml": base})
            config.configure(root=root, DEPLOY_REPOS={REPO})
            _, stdout, stderr = _run(root)
        # 13 keys, in each of the two facts (the base and staging) that carry them.
        self.assertIn("26 config value(s) withheld", stdout)
        self.assertIn("... and 16 more", stdout)
        self.assertNotIn(PASSWORD, stdout + stderr)


@needs_yaml
class TheValuesRouteIsMaskedToo(SettingsIsolated):
    """Break it catches: masking wired to one route only. The existing layout's
    output is the one already committed in consumer repositories."""

    def test_a_values_file_value_is_withheld(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _graph(root)
            repo = root / "repositories" / REPO
            (repo / ".git").mkdir(parents=True)
            _write(
                repo,
                {
                    "ansible/group_vars/prod/alpha_values.yaml.j2": (
                        f"replicas: 2\ndbPassword: {PASSWORD}\n"
                    )
                },
            )
            config.configure(root=root, DEPLOY_REPOS={REPO})
            graph, stdout, stderr = _run(root)
            written = _written_text(root)
        node = _facts(graph)["alpha (prod)"]["metadata"]
        self.assertEqual(node["route"], "values")
        self.assertEqual(node["config"], {"dbPassword": "[key name withheld]", "replicas": "2"})
        self.assertEqual(node["config_withheld"], ["dbPassword"])
        for text in (stdout, stderr, *written.values()):
            self.assertNotIn(PASSWORD, text)


@needs_yaml
class ASecondRunIsByteIdentical(SettingsIsolated):
    """Break it catches: an unordered walk, or a re-run that withholds twice
    (`[key name withheld]` read as a value and wrapped again)."""

    def test_two_runs_write_the_same_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _graph(root)
            _deploy_repo(root, LAYOUT)
            config.configure(root=root, DEPLOY_REPOS={REPO}, SENSITIVE_PATTERNS=STORE_RULE)
            _run(root)
            first = {k: v for k, v in _written_text(root).items()}
            _run(root)
            second = _written_text(root)
        self.assertEqual(sorted(first), sorted(second))
        for name in first:
            with self.subTest(file=name):
                self.assertEqual(first[name], second[name])


def _graph_holding(config_values: dict[str, str]) -> dict:
    return {
        "nodes": [
            {"id": "ignored::node", "metadata": {"kind": "feature", "config": {"p": PASSWORD}}},
            {
                "id": f"{REPO}::deploy:prod:alpha",
                "metadata": {"kind": "deployment", "config": config_values},
            },
        ],
        "links": [],
    }


class CheckEvidenceReadsDeploymentConfig(SettingsIsolated):
    """Break it catches: a gate that reads only mined commit text, so a graph built
    before the stage withheld values, or before a rule was added, passes."""

    def _check(self, graph: dict) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "graph.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as out:
                json.dump(graph, out)
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
                code = check_evidence.main([str(path)])
        return code, printed.getvalue()

    def test_a_graph_still_holding_a_matching_value_fails_and_names_the_node(self):
        code, output = self._check(_graph_holding({"dbPassword": PASSWORD, "replicas": "2"}))
        self.assertEqual(code, 1)
        self.assertIn(f"{REPO}::deploy:prod:alpha", output)
        self.assertIn("1 value(s)", output)
        self.assertIn("key-name", output)

    def test_no_value_is_printed(self):
        config.configure(SENSITIVE_PATTERNS=STORE_RULE)
        code, output = self._check(
            _graph_holding({"dbPassword": PASSWORD, "header": VENDOR_KEY, "note": STORE_VALUE})
        )
        # Fails first if nothing was reported, or the loop below reads an empty page.
        self.assertEqual(code, 1)
        self.assertIn(f"{REPO}::deploy:prod:alpha", output)
        for secret in (PASSWORD, VENDOR_KEY, STORE_VALUE):
            self.assertNotIn(secret, output)
        self.assertNotIn("dbPassword", output)

    def test_a_rule_added_later_is_noticed(self):
        graph = _graph_holding({"note": STORE_VALUE})
        self.assertEqual(self._check(graph)[0], 0)
        config.configure(SENSITIVE_PATTERNS=STORE_RULE)
        self.assertEqual(self._check(graph)[0], 1)

    def test_what_the_stage_withheld_is_not_flagged_again(self):
        code, output = self._check(
            _graph_holding({"dbPassword": "[key name withheld]", "replicas": "2"})
        )
        self.assertEqual(code, 0)
        self.assertIn("none matches a rule", output)

    def test_only_deployment_nodes_are_read(self):
        """The decoy in `_graph_holding` is a non-deployment node holding a matching
        value under `config`; a gate reading every node would fail on it."""
        self.assertEqual(self._check(_graph_holding({"replicas": "2"}))[0], 0)

    def test_the_node_list_is_capped(self):
        graph = {
            "nodes": [
                {
                    "id": f"{REPO}::deploy:prod:svc{n:02d}",
                    "metadata": {"kind": "deployment", "config": {"dbPassword": PASSWORD}},
                }
                for n in range(25)
            ]
        }
        code, output = self._check(graph)
        self.assertEqual(code, 1)
        self.assertIn("25 of 25", output)
        self.assertIn("... and 5 more node(s)", output)

    def test_the_rules_the_stage_applies_are_the_rules_the_gate_reads(self):
        """One predicate for both ends, so a value the stage keeps is one the gate
        passes and a value it withholds is one the gate would flag."""
        for key, value in (("replicas", "2"), ("dbPassword", PASSWORD), ("h", VENDOR_KEY)):
            with self.subTest(key=key):
                flat = deploy_secrets.flatten({key: value}, 10, 200)
                self.assertEqual(bool(flat.withheld), bool(deploy_secrets.rule_for(key, value)))
                self.assertEqual(deploy_secrets.findings(_graph_holding(dict(flat))["nodes"]), [])


class ThePlaceholderPatternsAreNamedAndTested(SettingsIsolated):
    """Break it catches: a templating placeholder withheld because its key is named
    for a secret, which loses the fact that the service takes the value at deploy
    time; and the converse, a placeholder exemption wide enough to let a literal
    credential through."""

    def test_a_value_wholly_a_placeholder_is_recognised(self):
        for value in ("<set-at-deploy-time>", "{{ vault.dbPassword }}", "${DB_PASSWORD}", "$${X}"):
            with self.subTest(value=value):
                self.assertTrue(deploy_secrets.is_placeholder(value))

    def test_a_literal_is_not_a_placeholder_even_when_it_contains_one(self):
        for value in (PASSWORD, f"{PASSWORD}${{X}}", "prefix-{{ x }}", ""):
            with self.subTest(value=value):
                self.assertFalse(deploy_secrets.is_placeholder(value))

    def test_a_placeholder_under_a_secret_named_key_is_kept(self):
        for value in ("<set-at-deploy-time>", "{{ vault.dbPassword }}", "${DB_PASSWORD}"):
            with self.subTest(value=value):
                self.assertEqual(deploy_secrets.rule_for("database.dbPassword", value), "")

    def test_a_placeholder_that_also_matches_a_value_shape_is_withheld(self):
        self.assertEqual(deploy_secrets.rule_for("header", "{{ " + VENDOR_KEY + " }}"), "sk-key")


class MaskLeavesNoZeroEntries(SettingsIsolated):
    """Break it catches: `secret_mask.mask` inserting a zero for every rule it tried,
    so `if counts:` over text it never changed is true. One store reported 13,029
    locations for 951 matches."""

    def test_text_that_matches_nothing_leaves_the_counter_empty(self):
        counts: Counter[str] = Counter()
        secret_mask.mask("replicas: 3\nimage: registry.example/alpha:1.4.0\n", counts)
        self.assertEqual(dict(counts), {})
        self.assertFalse(counts)

    def test_a_match_is_counted_under_its_rule_alone(self):
        counts: Counter[str] = Counter()
        secret_mask.mask(f"dbPassword: {PASSWORD}\n", counts)
        self.assertEqual(dict(counts), {"key-name": 1})


PEM_KIND = "RSA PRIVATE" + " KEY"
# Header, 300 characters of body, footer: it matches `pem-private-key` whole and
# matches nothing once cut at 200, because the cut leaves no footer.
PEM = "-----BEGIN " + PEM_KIND + "-----\n" + "A" * 300 + "\n-----END " + PEM_KIND + "-----"
# A token whose first character is the 191st, so the cap would keep its first ten.
LATE_TOKEN = "build-log " + "w" * 180 + " " + "gh" + "p_" + "x" * 36
CAP = 200


def _stage_over(**values: str) -> tuple[dict, str, str, dict[str, str]]:
    """Run the stage over a base overlay holding `values` (YAML text, by key)."""
    lines = "".join(f"    {key}: {text}\n" for key, text in values.items())
    base = BASE.split("    appInsights:")[0] + lines
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _graph(root)
        _deploy_repo(root, {**LAYOUT, "apps/base/services/alpha/helmrelease.yaml": base})
        config.configure(root=root, DEPLOY_REPOS={REPO})
        graph, stdout, stderr = _run(root)
        return graph, stdout, stderr, _written_text(root)


@needs_yaml
class TheCapNeverRunsBeforeMasking(SettingsIsolated):
    """Break it catches: cutting a value to 200 characters before the rules read it.
    A rule that needs the whole value (a private-key block, whose footer the cut
    removes) or a token that straddles the cap matches nothing once cut, and the
    first 200 characters are published. A prefix-matching rule cannot show this:
    it matches the cut value as well, which is why the earlier version of this test
    passed over it."""

    def _assert_withheld_everywhere(self, secret_text: str, key: str, marker: str, run):
        graph, stdout, stderr, written = run
        cfg = _facts(graph)["alpha (staging)"]["metadata"]["config"]
        self.assertEqual(cfg[key], marker)
        for fragment in (secret_text[:60], secret_text[-60:]):
            for text in (stdout, stderr, *written.values()):
                self.assertNotIn(fragment, text)
                self.assertNotIn(json.dumps(fragment)[1:-1], text)

    def test_a_private_key_block_longer_than_the_cap_is_withheld(self):
        self.assertGreater(len(PEM), CAP)
        self.assertEqual(deploy_secrets.rule_for("tlsKey", PEM[:CAP]), "")
        self.assertNotEqual(deploy_secrets.rule_for("tlsKey", PEM), "")
        run = _stage_over(tlsKey=json.dumps(PEM))
        self._assert_withheld_everywhere(PEM, "tlsKey", "[pem private key withheld]", run)
        node = _facts(run[0])["alpha (staging)"]["metadata"]
        self.assertIn("tlsKey", node["config_withheld"])

    def test_a_token_that_starts_inside_the_cap_and_ends_beyond_it_is_withheld(self):
        self.assertEqual(deploy_secrets.rule_for("note", LATE_TOKEN[:CAP]), "")
        rule = deploy_secrets.rule_for("note", LATE_TOKEN)
        self.assertNotEqual(rule, "")
        run = _stage_over(note=json.dumps(LATE_TOKEN))
        self._assert_withheld_everywhere(
            LATE_TOKEN[190:], "note", f"[{rule.replace('-', ' ')} withheld]", run
        )

    def test_a_long_value_that_matches_nothing_is_still_capped(self):
        """The cap still applies to what is kept, so the change cannot grow a node."""
        long_value = "z" * 500
        graph, *_ = _stage_over(banner=long_value)
        cfg = _facts(graph)["alpha (staging)"]["metadata"]["config"]
        self.assertEqual(cfg["banner"], "z" * CAP)
        self.assertNotIn(
            "banner", _facts(graph)["alpha (staging)"]["metadata"].get("config_withheld", [])
        )

    def test_the_values_route_masks_before_the_cap_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _graph(root)
            repo = root / "repositories" / REPO
            (repo / ".git").mkdir(parents=True)
            _write(
                repo,
                {
                    "ansible/group_vars/prod/alpha_values.yaml.j2": (
                        f"tlsKey: {json.dumps(PEM)}\nnote: {json.dumps(LATE_TOKEN)}\n"
                    )
                },
            )
            config.configure(root=root, DEPLOY_REPOS={REPO})
            graph, stdout, stderr = _run(root)
            written = _written_text(root)
        node = _facts(graph)["alpha (prod)"]["metadata"]
        self.assertEqual(node["config_withheld"], ["note", "tlsKey"])
        for text in (stdout, stderr, *written.values()):
            self.assertNotIn("A" * 60, text)
            self.assertNotIn("w" * 60, text)


if __name__ == "__main__":
    unittest.main()
