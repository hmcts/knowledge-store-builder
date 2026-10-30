"""Nodes with no community are not a community, and get no prose.

Nodes added after clustering - a `<repo>::repository` node, a chart-source node -
carry no `community`, or carry `-1`. `extract` used to file them all under digest
id `-1`, `batches` then asked an author for prose about "a cluster" nothing can
reach, and `merge` accepted it. Every test here drives the real stage over a real
store layout and asserts on the files it wrote or the output it printed; each names
the production change that should make it fail.
"""

from __future__ import annotations

import contextlib
import io as stdio
import json
import tempfile
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import cli, config, status  # noqa: E402

PROSE = (
    "Helm values for widget-ui in svc-charts, setting listenPort for the web tier. "
    "It holds the base chart configuration."
)


def _node(node_id: str, **extra) -> dict:
    return {
        "id": node_id,
        "label": node_id,
        "repo": "repo-a",
        "source_file": "x.ts",
        "metadata": {},
        **extra,
    }


def _graph() -> dict:
    """One real community, and three groups of community-less nodes.

    Each group is large enough to clear the significance threshold on its own, so
    any one of them being bucketed would show up as a digest.
    """
    size = config.MIN_COMMUNITY_SIZE
    nodes = [_node(f"real{i}", community=1) for i in range(size)]
    nodes += [_node(f"absent{i}") for i in range(size)]
    nodes += [_node(f"minus{i}", community=-1) for i in range(size)]
    nodes += [_node(f"none{i}", community=None) for i in range(size)]
    return {"nodes": nodes, "links": []}


class NoCommunityTest(SettingsIsolated):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        config.configure(root=str(self.root))
        config.GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.GRAPH_PATH.write_text(json.dumps(_graph()), encoding="utf-8")
        config.LABELS_PATH.write_text(json.dumps({"1": "Real Area"}), encoding="utf-8")

    def run_cli(self, *arguments: str) -> tuple[int, str]:
        buffer = stdio.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["--root", str(self.root), "summaries", *arguments])
        return code, buffer.getvalue()

    def write_digests(self, ids: list) -> None:
        digests = [{"id": cid, "label": f"Area {cid}", "size": 40} for cid in ids]
        config.SUMMARIES_INPUT_PATH.write_text(json.dumps(digests), encoding="utf-8")

    def write_batch(self, mapping: dict) -> str:
        path = self.root / "authored.json"
        path.write_text(json.dumps(mapping), encoding="utf-8")
        return str(path)

    def test_extract_writes_no_digest_for_nodes_with_no_community(self):
        # breaks if extract buckets an absent, `-1` or None community as a cluster
        code, _ = self.run_cli("extract")
        digests = json.loads(config.SUMMARIES_INPUT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual([d["id"] for d in digests], [1])

    def test_batches_cut_nothing_for_a_digest_with_no_community(self):
        # breaks if a stale digest file holding a `-1` entry is still cut into a
        # batch, asking an author for prose about a cluster nothing can reach
        self.write_digests([1, -1, None])
        out_dir = self.root / "batches"
        code, _ = self.run_cli("batches", "--out-dir", str(out_dir))
        batches = [json.loads(p.read_text()) for p in sorted(out_dir.glob("batch_*.json"))]
        self.assertEqual(code, 0)
        self.assertEqual([[d["id"] for d in b["digests"]] for b in batches], [[1]])

    def test_merge_refuses_prose_keyed_to_no_community_and_names_it(self):
        # breaks if merge accepts a `-1` entry because a (stale) digest lists it
        self.write_digests([1, -1])
        code, output = self.run_cli("merge", self.write_batch({"1": PROSE, "-1": PROSE}))
        merged = json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertIn("-1: no community", output)
        self.assertEqual(sorted(k for k in merged if not k.startswith("_")), ["1"])

    def test_merge_of_only_a_no_community_entry_writes_nothing(self):
        # breaks if the refusal of `-1` still lets the run count it as merged
        self.write_digests([-1])
        code, output = self.run_cli("merge", self.write_batch({"-1": PROSE}))
        self.assertEqual(code, 1)
        self.assertIn("-1: no community", output)
        self.assertFalse(config.SUMMARIES_PATH.exists())

    def test_merge_does_not_report_no_community_as_a_summary_still_owed(self):
        # breaks if a `-1` digest is counted among the significant communities, so
        # every merge ends "1 significant communities still lack a summary" for a
        # community nothing can write about
        self.write_digests([1, -1])
        code, output = self.run_cli("merge", self.write_batch({"1": PROSE}))
        self.assertEqual(code, 0)
        self.assertNotIn("still lack", output)

    def test_status_does_not_expect_prose_for_no_community(self):
        # breaks if the coverage denominator counts a `-1` digest as a summary to write
        self.write_digests([1, 2, -1])
        self.assertEqual(status.layer_coverage()["summaries_expected"], 2)

    def test_the_clustering_fingerprint_ignores_nodes_with_no_community(self):
        # breaks if the fingerprint buckets `-1` while extract does not, so verify
        # reads every refresh as a different clustering
        with_none = summaries._clustering_of_nodes(_graph()["nodes"])
        only_real = summaries._clustering_of_nodes(
            [n for n in _graph()["nodes"] if n.get("community") == 1]
        )
        self.assertNotEqual(only_real, "")
        self.assertEqual(with_none, only_real)
