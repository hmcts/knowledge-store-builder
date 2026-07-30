"""Tests for knowledgestore/generate_repository_list.py, sync_repositories.py and
build_graph.py - the (former bash) plumbing, now testable Python."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


from knowledgestore import generate_repository_list as repo_list  # noqa: E402
from knowledgestore import sync_repositories as sync  # noqa: E402
from knowledgestore import export_git_history as export  # noqa: E402
from knowledgestore import cli  # noqa: E402


class FiltersTest(unittest.TestCase):
    def _filters(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repository-filters.txt"
            path.write_text(text, encoding="utf-8")
            return repo_list.read_filters(path)

    def test_prefix_repo_and_exclude_rules(self):
        filters = self._filters(
            "# comment\nprefix cpp-context-\nrepo odd-one\nexclude cpp-context-skip\n"
        )
        self.assertTrue(filters.matches("cpp-context-hearing"))
        self.assertTrue(filters.matches("odd-one"))
        self.assertFalse(filters.matches("cpp-context-skip"))  # exclude wins
        self.assertFalse(filters.matches("unrelated"))

    def test_rejects_unknown_kind_and_empty_rules(self):
        with self.assertRaises(ValueError):
            self._filters("wildcard cpp-*\n")
        with self.assertRaises(ValueError):
            self._filters("# only comments\n")


class DiscoverTest(unittest.TestCase):
    def test_discovers_filters_and_sorts(self):
        calls = []

        def fake_runner(args):
            calls.append(args)
            return (
                '{"name":"cpp-context-b","defaultBranch":"main"}\n'
                '{"name":"cpp-context-a","defaultBranch":"master"}\n'
                '{"name":"infra-thing","defaultBranch":"main"}\n'
            )

        filters = repo_list.Filters(prefixes=["cpp-context-"])
        repos = repo_list.discover(filters, runner=fake_runner)
        self.assertEqual([r["name"] for r in repos], ["cpp-context-a", "cpp-context-b"])
        self.assertIn("--paginate", calls[0])

    def test_render_config_pipe_format(self):
        self.addCleanup(setattr, repo_list, "GITHUB_ORG", repo_list.GITHUB_ORG)
        repo_list.GITHUB_ORG = "myorg"
        content = repo_list.render_config(
            [
                {"name": "a-repo", "defaultBranch": "main"},
            ]
        )
        lines = [line for line in content.splitlines() if line and not line.startswith("#")]
        self.assertEqual(lines, ["a-repo|git@github.com:myorg/a-repo.git|main"])
        self.assertIn("repository-filters.txt", content)


class SyncRepositoryTest(unittest.TestCase):
    def _repo(self):
        return export.RepositoryConfig(
            name="repo-a", clone_url="git@example.com:o/repo-a.git", default_branch="main"
        )

    def test_clones_when_missing_and_syncs(self):
        commands = []

        def fake_git(args):
            commands.append(args)
            return "42\n" if args[-2:] == ["--all", "--count"] else ""

        with tempfile.TemporaryDirectory() as tmp:
            count = sync.sync_repository(self._repo(), Path(tmp), run=fake_git)
        self.assertEqual(count, 42)
        self.assertEqual(commands[0][0], "clone")
        flat = [" ".join(c) for c in commands]
        self.assertTrue(any("fetch origin --prune" in c for c in flat))
        self.assertTrue(any("reset --hard origin/main" in c for c in flat))

    def test_clean_preserves_per_repo_graph(self):
        # Regression: `git clean -fd` without the exclusion deleted every
        # untracked repositories/<repo>/graphify-out/ on re-sync, destroying
        # the per-repo graphs the estate merge is built from.
        commands = []

        def fake_git(args):
            commands.append(args)
            return "1\n" if args[-2:] == ["--all", "--count"] else ""

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "repo-a" / ".git").mkdir(parents=True)
            sync.sync_repository(self._repo(), Path(tmp), run=fake_git)
        cleans = [c for c in commands if "clean" in c]
        self.assertEqual(len(cleans), 1)
        clean = " ".join(cleans[0])
        self.assertIn("-e graphify-out", clean)

    def test_skips_clone_when_repo_exists(self):
        commands = []

        def fake_git(args):
            commands.append(args)
            return "7\n" if args[-2:] == ["--all", "--count"] else ""

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "repo-a" / ".git").mkdir(parents=True)
            sync.sync_repository(self._repo(), Path(tmp), run=fake_git)
        self.assertNotEqual(commands[0][0], "clone")

    def test_missing_remote_branch_raises(self):
        def fake_git(args):
            if args[-3:-1] == ["--verify", "--quiet"] or "--verify" in args:
                raise subprocess.CalledProcessError(1, args)
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "repo-a" / ".git").mkdir(parents=True)
            repo, target = self._repo(), Path(tmp)
            with self.assertRaises(RuntimeError):
                sync.sync_repository(repo, target, run=fake_git)


class CliTest(unittest.TestCase):
    def test_stages_are_listed_in_pipeline_order(self):
        self.assertEqual(
            list(cli.STAGES),
            [
                "discover",
                "sync",
                "export-history",
                "context",
                "intent",
                "ticket-titles",
                "gherkin",
                "summaries",
                "semantic",
                "topics",
                "explorer",
            ],
        )

    def test_every_stage_maps_to_a_module_with_a_main(self):
        for stage, (module_name, help_text) in cli.STAGES.items():
            module = __import__(f"knowledgestore.{module_name}", fromlist=["main"])
            self.assertTrue(callable(module.main), stage)
            self.assertTrue(help_text and help_text[0].islower(), stage)

    def test_no_arguments_prints_usage_and_fails(self):
        self.assertEqual(cli.main([]), 1)

    def test_help_succeeds_and_lists_every_stage(self):
        usage = cli.usage()
        for stage in cli.STAGES:
            self.assertIn(stage, usage)
        self.assertEqual(cli.main(["--help"]), 0)

    def test_unknown_stage_fails(self):
        self.assertEqual(cli.main(["nonsense"]), 1)

    def test_root_option_repoints_every_derived_path(self):
        from knowledgestore import config

        original = config.ROOT
        self.addCleanup(config.configure, original)
        with tempfile.TemporaryDirectory() as tmp:
            cli.main(["--root", tmp])
            self.assertEqual(config.GRAPH_PATH.parent.parent, Path(tmp).resolve())


class SemanticVocabularyTest(unittest.TestCase):
    def test_vocabulary_filters_short_stop_and_rare_tokens(self):
        from knowledgestore import build_semantic_index as semantic

        with tempfile.TemporaryDirectory() as tmp:
            graph = Path(tmp) / "graph.json"
            import json

            graph.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {"label": "address validation address validation address validation"},
                            {"label": "there there there"},  # stopword
                            {"label": "ab ab ab"},  # too short
                            {"label": "rareword"},  # below MIN_DF
                        ],
                        "links": [],
                    }
                ),
                encoding="utf-8",
            )
            semantic.GRAPH_PATH = graph
            semantic.LABELS_PATH = Path(tmp) / "missing.json"
            semantic.SUMMARIES_PATH = Path(tmp) / "missing2.json"
            vocab = semantic.collect_vocabulary()
        self.assertIn("address", vocab)
        self.assertIn("validation", vocab)
        self.assertNotIn("there", vocab)
        self.assertNotIn("rareword", vocab)


class FakeRow:
    """Just enough of a numpy row for nearest_neighbours: indexing + argsort."""

    def __init__(self, values):
        self.values = values

    def __getitem__(self, i):
        return self.values[i]

    def argsort(self):
        order = sorted(range(len(self.values)), key=lambda i: self.values[i])
        return _Sliceable(order)


class _Sliceable(list):
    def __getitem__(self, item):
        result = super().__getitem__(item)
        return _Sliceable(result) if isinstance(item, slice) else result


class NearestNeighboursTest(unittest.TestCase):
    def test_keeps_similar_tokens_and_skips_shared_stems(self):
        from knowledgestore import build_semantic_index as semantic

        vocab = ["outcome", "result", "results", "banana"]
        #        self       strong    shared-stem-with-result  weak
        row = FakeRow([1.0, 0.7, 0.69, 0.1])
        near = semantic.nearest_neighbours(vocab, row, "outcome")
        names = [n for n, _ in near]
        self.assertIn("result", names)
        self.assertNotIn("banana", names)  # below MIN_SIMILARITY

    def test_shared_stem_pairs_are_skipped(self):
        from knowledgestore import build_semantic_index as semantic

        vocab = ["result", "results", "verdict"]
        row = FakeRow([1.0, 0.9, 0.6])
        near = semantic.nearest_neighbours(vocab, row, "result")
        names = [n for n, _ in near]
        self.assertNotIn("results", names)  # results/result share a stem
        self.assertIn("verdict", names)


class IntentSummariseTest(unittest.TestCase):
    def test_reports_counts(self):
        import contextlib
        import gzip as _gzip
        import io
        import json as _json
        from knowledgestore import build_intent_index as intent

        with tempfile.TemporaryDirectory() as tmp:
            intent.OUTPUT = Path(tmp) / "file-tickets.json.gz"
            with _gzip.open(intent.OUTPUT, "wt", encoding="utf-8") as out:
                _json.dump({}, out)
            index = {"repo-a": {"a.ts": {"tickets": {"DD-1": 2}}}}
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                intent.summarise(index, 2)
        printed = buffer.getvalue()
        self.assertIn("1 files", printed.replace(",", ""))
        self.assertIn("1 distinct tickets", printed.replace(",", ""))


if __name__ == "__main__":
    unittest.main()
