"""The `source-urls` stage gives every estate node in the graph a `url`.

Driven through `main()` against a real store layout: a `repositories.txt` in the
format `discover` writes, provenance written by `provenance.write`, and graph
nodes in the shape a merged graphify graph carries - `repo`, a repo-relative
`source_file`, and the `source_location` forms graphify emits. Assertions land on
the files the stage wrote. Each test names the defect it catches.
"""

from __future__ import annotations

import contextlib
import gzip
import io as stdio
import json
import tempfile
from pathlib import Path

from settings_isolation import SettingsIsolated
from knowledgestore import build_source_urls, config, provenance

SHA = "0123456789abcdef0123456789abcdef01234567"
BASE = f"https://github.com/example-org/example-repo/blob/{SHA}/"
SINGLE_ROOT_BASE = f"https://github.com/example-org/single-root-repo/blob/{SHA}/"

REPOSITORIES_TXT = """\
# Generated from the example-org organisation repository listing
# Filtered by config/repository-filters.txt (archived always excluded)
# Format: name|clone-url|default-branch
example-repo|git@github.com:example-org/example-repo.git|main
single-root-repo|git@github.com:example-org/single-root-repo.git|main
hosted-elsewhere|git@gitlab.example.com:example-org/hosted-elsewhere.git|main
"""


def _code_node(local_id, label, source_file, source_location, repo="example-repo"):
    """A node as `graphify merge-graphs` leaves it, keys in graphify's order."""
    return {
        "label": label,
        "file_type": "code",
        "source_file": source_file,
        "source_location": source_location,
        "_origin": "ast",
        "repo": repo,
        "local_id": local_id,
        "id": f"{repo}::{local_id}",
        "community": 7,
        "norm_label": label.lower(),
    }


def _graph():
    return {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            _code_node("src_case_caseservice", "CaseService", "src/case/CaseService.java", "L12"),
            _code_node("charts_values", "values.yaml", "charts/values.yaml", "values.yaml:149"),
            _code_node("readme", "README.md", "README.md", None),
            # A per-repository repository that really holds a top-level
            # `repositories/` directory. Its other nodes are repo-relative, so these
            # paths are files in the repository, not a corpus prefix.
            _code_node(
                "repositories_other_repo_notes",
                "notes.md",
                "repositories/other-repo/notes.md",
                None,
            ),
            _code_node(
                "repositories_example_repo_index",
                "index.md",
                "repositories/example-repo/index.md",
                None,
            ),
            # A repository built by the single-root route: every file-bearing node
            # carries its own corpus prefix.
            _code_node(
                "src_case_caseservice_open",
                "open()",
                "repositories/single-root-repo/src/case/CaseService.java",
                "L30-L41",
                repo="single-root-repo",
            ),
            _code_node(
                "readme",
                "README.md",
                "repositories/single-root-repo/README.md",
                None,
                repo="single-root-repo",
            ),
            _code_node("ci", "ci.yml", ".gitlab-ci.yml", "L1", repo="hosted-elsewhere"),
            # A Java package-hierarchy node: neither label nor source_file (CLAUDE.md 6.4).
            {"id": "example-repo::uk_example", "repo": "example-repo", "community": 7},
            # A Terraform module referenced from outside the estate: `repo` is "".
            {
                "id": "shared-modules::module",
                "label": "shared-modules",
                "source_file": None,
                "source_location": None,
                "repo": "",
                "_origin": "packages",
            },
            # A semantic node from a graph assembled without `merge-graphs`: a file,
            # but no repository to link it in.
            {"id": "intro_doc", "label": "Introduction", "source_file": "docs/intro.md"},
            # A node carrying a link from an earlier build whose repository has since
            # left the configuration: the stale link must not survive.
            dict(
                _code_node("gone", "Gone", "src/Gone.java", "L1", repo="left-the-estate"),
                url="https://github.com/example-org/left-the-estate/blob/"
                + "f" * 40
                + "/src/Gone.java#L1",
            ),
        ],
        "links": [],
    }


class SourceUrlsStageTest(SettingsIsolated):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        config.configure(root=str(root))
        config.REPOSITORIES_CONFIG.parent.mkdir(parents=True)
        config.REPOSITORIES_CONFIG.write_text(REPOSITORIES_TXT, encoding="utf-8")
        provenance.write(
            {
                "example-repo": {"sha": SHA, "branch": "main", "committed": "2026-01-02T03:04:05Z"},
                "single-root-repo": {"sha": SHA, "branch": "main", "committed": ""},
                "hosted-elsewhere": {"sha": SHA, "branch": "main", "committed": ""},
            }
        )
        config.GRAPH_PATH.parent.mkdir(parents=True)

    def _write_graph(self, graph):
        config.GRAPH_PATH.write_text(json.dumps(graph), encoding="utf-8")

    def _run(self):
        out = stdio.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = build_source_urls.main()
        return code, out.getvalue()

    def _nodes(self, path=None):
        path = path or config.GRAPH_PATH
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            return {node["id"]: node for node in json.load(handle)["nodes"]}

    def test_each_estate_node_links_to_its_line_at_the_build_commit(self):
        """Breaks if the stage writes no link, the wrong link, or loses the anchor."""
        self._write_graph(_graph())
        code, _ = self._run()
        self.assertEqual(code, 0)
        nodes = self._nodes()
        self.assertEqual(
            nodes["example-repo::src_case_caseservice"]["url"],
            BASE + "src/case/CaseService.java#L12",
        )
        self.assertEqual(
            nodes["example-repo::charts_values"]["url"], BASE + "charts/values.yaml#L149"
        )
        self.assertEqual(nodes["example-repo::readme"]["url"], BASE + "README.md")

    def test_the_single_root_route_links_the_same_file(self):
        """Breaks if a `repositories/<repo>/` source_file is linked with its corpus
        prefix, which names a path that exists in no repository."""
        self._write_graph(_graph())
        self._run()
        nodes = self._nodes()
        self.assertEqual(
            nodes["single-root-repo::src_case_caseservice_open"]["url"],
            SINGLE_ROOT_BASE + "src/case/CaseService.java#L30-L41",
        )
        self.assertEqual(nodes["single-root-repo::readme"]["url"], SINGLE_ROOT_BASE + "README.md")

    def test_a_real_repositories_directory_is_not_read_as_the_corpus_prefix(self):
        """Breaks if the route is decided per path. A repository whose files are
        repo-relative and which holds a `repositories/` directory would have its own
        `repositories/example-repo/...` stripped to a different file, and its
        `repositories/other-repo/...` refused."""
        self._write_graph(_graph())
        self._run()
        nodes = self._nodes()
        self.assertEqual(
            nodes["example-repo::repositories_other_repo_notes"]["url"],
            BASE + "repositories/other-repo/notes.md",
        )
        self.assertEqual(
            nodes["example-repo::repositories_example_repo_index"]["url"],
            BASE + "repositories/example-repo/index.md",
        )

    def test_a_node_that_cannot_be_linked_correctly_has_no_url(self):
        """Breaks if a node without a file, without a repository, on another host or
        outside the configuration is given a link, or keeps one from an earlier build."""
        self._write_graph(_graph())
        self._run()
        nodes = self._nodes()
        for node_id in (
            "example-repo::uk_example",
            "shared-modules::module",
            "intro_doc",
            "hosted-elsewhere::ci",
            "left-the-estate::gone",
        ):
            with self.subTest(node=node_id):
                self.assertNotIn("url", nodes[node_id])

    def test_both_graph_files_are_written_and_agree(self):
        """Breaks if only one file is rewritten: the committed archive is what a store
        ships, and a link written only into the gitignored plain file is lost."""
        self._write_graph(_graph())
        self._run()
        self.assertEqual(self._nodes(), self._nodes(config.GRAPH_PATH.with_name("graph.json.gz")))

    def test_the_report_counts_what_was_linked_and_why_the_rest_was_not(self):
        """Breaks if the report drops a reason or miscounts. Every node is accounted
        for: 7 linked, and 5 skipped across four named reasons; and the one link an
        earlier build wrote that this run could not rebuild is counted as removed."""
        self._write_graph(_graph())
        _, report = self._run()
        self.assertIn("Source URLs: 7 of 12 nodes linked (3 with a line anchor)", report)
        self.assertIn("removed 1 links an earlier build wrote", report)
        self.assertIn("2 no source_file", report)
        self.assertIn("1 no repo", report)
        self.assertIn("1 clone URL is not on github.com", report)
        self.assertIn("1 repository not in config/repositories.txt", report)

    def test_a_rerun_is_byte_identical(self):
        """Breaks if a second run reorders keys or rewrites links differently - two
        runs on the same inputs must produce the same bytes (CLAUDE.md 1.3)."""
        archive = config.GRAPH_PATH.with_name("graph.json.gz")
        self._write_graph(_graph())
        self._run()
        first, first_archive = config.GRAPH_PATH.read_bytes(), archive.read_bytes()
        code, _ = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(config.GRAPH_PATH.read_bytes(), first)
        # The archive is what a store commits, so it is the copy that must not churn.
        self.assertEqual(archive.read_bytes(), first_archive)

    def test_a_missing_graph_is_refused(self):
        """Breaks if the stage reports success with no graph to annotate."""
        code, report = self._run()
        self.assertEqual(code, 1)
        self.assertIn("Graph not found", report)

    def test_a_malformed_repository_list_is_refused_and_nothing_is_written(self):
        """Breaks if a list the stage cannot read is treated as an estate of nothing.

        The graph carries no link here on purpose. With one, the stage's "would link
        none" refusal also fires and this test could not tell the two guards apart;
        without one, a stage reading the list as empty proceeds to rewrite both files
        and exits 0, so this refusal is the only thing that stops it."""
        graph = _graph()
        graph["nodes"] = [node for node in graph["nodes"] if "url" not in node]
        self._write_graph(graph)
        before = config.GRAPH_PATH.read_bytes()
        config.REPOSITORIES_CONFIG.write_text("example-repo|only-two-fields\n", encoding="utf-8")
        code, report = self._run()
        self.assertEqual(code, 1)
        self.assertIn("repositories.txt", report)
        self.assertEqual(config.GRAPH_PATH.read_bytes(), before)

    # --- refusing to strip an estate's links --------------------------------

    def _write_linked_pair(self):
        """A graph that already carries links, in both files, as a store commits it."""
        graph = _graph()
        graph["nodes"][0]["url"] = BASE + "src/case/CaseService.java#L12"
        serialised = json.dumps(graph)
        config.GRAPH_PATH.write_text(serialised, encoding="utf-8")
        with gzip.open(config.GRAPH_PATH.with_name("graph.json.gz"), "wt", encoding="utf-8") as f:
            f.write(serialised)
        return config.GRAPH_PATH.read_bytes(), config.GRAPH_PATH.with_name(
            "graph.json.gz"
        ).read_bytes()

    def _assert_nothing_written(self, before):
        after = (
            config.GRAPH_PATH.read_bytes(),
            config.GRAPH_PATH.with_name("graph.json.gz").read_bytes(),
        )
        self.assertEqual(after, before, "a refusal must leave both graph files as they were")

    def test_missing_provenance_is_refused_and_nothing_is_written(self):
        """Breaks if a machine without `knowledge/provenance.json` strips every
        published link and exits 0: every node would fall to "no provenance sha"."""
        before = self._write_linked_pair()
        config.PROVENANCE_PATH.unlink()
        code, report = self._run()
        self.assertEqual(code, 1)
        self.assertIn("provenance.json is missing or records no repository", report)
        self._assert_nothing_written(before)

    def test_provenance_recording_no_repository_is_refused(self):
        """Breaks if an empty provenance file is read as permission to unlink everything."""
        before = self._write_linked_pair()
        config.PROVENANCE_PATH.write_text(json.dumps({"repositories": {}}), encoding="utf-8")
        code, _ = self._run()
        self.assertEqual(code, 1)
        self._assert_nothing_written(before)

    def test_a_run_that_would_remove_every_link_is_refused(self):
        """Breaks if a store whose repository list is missing - provenance present -
        loses every link it published. Nothing can be linked, the graph carried links,
        so the stage refuses and says why rather than writing."""
        before = self._write_linked_pair()
        config.REPOSITORIES_CONFIG.unlink()
        code, report = self._run()
        self.assertEqual(code, 1)
        self.assertIn("would link none", report)
        self.assertIn("repository not in config/repositories.txt", report)
        self._assert_nothing_written(before)
