"""Tests for knowledgestore/generate_repository_list.py, sync_repositories.py and
build_graph.py - the (former bash) plumbing, now testable Python."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import redirect_stdout
from pathlib import Path


from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import generate_repository_list as repo_list  # noqa: E402
from knowledgestore import sync_repositories as sync  # noqa: E402
from knowledgestore import export_git_history as export  # noqa: E402
from knowledgestore import cli  # noqa: E402


class FiltersTest(SettingsIsolated):
    def _filters(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repository-filters.txt"
            path.write_text(text, encoding="utf-8")
            return repo_list.read_filters(path)

    def test_prefix_repo_and_exclude_rules(self):
        filters = self._filters(
            "# comment\nprefix svc-context-\nrepo odd-one\nexclude svc-context-skip\n"
        )
        self.assertTrue(filters.matches("svc-context-hearing"))
        self.assertTrue(filters.matches("odd-one"))
        self.assertFalse(filters.matches("svc-context-skip"))  # exclude wins
        self.assertFalse(filters.matches("unrelated"))

    def test_rejects_unknown_kind_and_empty_rules(self):
        with self.assertRaises(ValueError):
            self._filters("wildcard svc-*\n")
        with self.assertRaises(ValueError):
            self._filters("# only comments\n")


class DiscoverTest(SettingsIsolated):
    def test_discovers_filters_and_sorts(self):
        calls = []

        def fake_runner(args):
            calls.append(args)
            return (
                '{"name":"svc-context-b","defaultBranch":"main"}\n'
                '{"name":"svc-context-a","defaultBranch":"master"}\n'
                '{"name":"infra-thing","defaultBranch":"main"}\n'
            )

        filters = repo_list.Filters(prefixes=["svc-context-"])
        repos = repo_list.discover(filters, runner=fake_runner)
        self.assertEqual([r["name"] for r in repos], ["svc-context-a", "svc-context-b"])
        self.assertIn("--paginate", calls[0])

    def test_render_config_pipe_format(self):
        self.addCleanup(setattr, repo_list, "GITHUB_ORG", config.GITHUB_ORG)
        config.configure(GITHUB_ORG="myorg")
        content = repo_list.render_config(
            [
                {"name": "a-repo", "defaultBranch": "main"},
            ]
        )
        lines = [line for line in content.splitlines() if line and not line.startswith("#")]
        self.assertEqual(lines, ["a-repo|git@github.com:myorg/a-repo.git|main"])
        self.assertIn("repository-filters.txt", content)


class TeamDiscoveryTest(SettingsIsolated):
    """The `team <slug>` rule: an estate defined by ownership, not naming.

    Teams without naming conventions (and the infrastructure estates being
    added next) cannot be expressed as prefixes; a team's repository listing
    is the source instead. Excludes must still win, and the union with
    name-based rules must be deduplicated.
    """

    def _filters(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repository-filters.txt"
            path.write_text(text, encoding="utf-8")
            return repo_list.read_filters(path)

    @staticmethod
    def _runner(org_repos, team_repos):
        def fake_gh(arguments):
            joined = " ".join(arguments)
            if "/teams/" in joined:
                slug = joined.split("/teams/")[1].split("/")[0]
                rows = team_repos.get(slug, [])
            else:
                rows = org_repos
            import json

            return "\n".join(json.dumps({"name": n, "defaultBranch": "main"}) for n in rows)

        return fake_gh

    def test_team_rule_parses(self):
        filters = self._filters("team platform-team\n")
        self.assertEqual(filters.teams, ["platform-team"])

    def test_a_team_only_config_is_valid(self):
        # no prefix or repo rule needed when a team defines the estate
        filters = self._filters("team platform-team\n")
        self.assertEqual((filters.prefixes, filters.includes), ([], set()))

    def test_team_membership_includes_a_repo_no_name_rule_matches(self):
        filters = self._filters("prefix svc-\nteam platform-team\n")
        runner = self._runner(
            org_repos=["svc-a", "oddly-named"],
            team_repos={"platform-team": ["oddly-named"]},
        )
        repos = repo_list.discover(filters, runner=runner)
        self.assertEqual([r["name"] for r in repos], ["oddly-named", "svc-a"])
        # sync clones the branch this row names; a team-sourced repo must carry it
        self.assertEqual(repos[0]["defaultBranch"], "main")

    def test_exclude_beats_team_membership(self):
        filters = self._filters("team platform-team\nexclude retired-thing\n")
        runner = self._runner(
            org_repos=["retired-thing", "kept-thing"],
            team_repos={"platform-team": ["retired-thing", "kept-thing"]},
        )
        repos = repo_list.discover(filters, runner=runner)
        self.assertEqual([r["name"] for r in repos], ["kept-thing"])

    def test_union_is_deduplicated_when_rules_overlap(self):
        # a repo matched by prefix AND owned by the team appears once
        filters = self._filters("prefix svc-\nteam platform-team\n")
        runner = self._runner(
            org_repos=["svc-a"],
            team_repos={"platform-team": ["svc-a"]},
        )
        repos = repo_list.discover(filters, runner=runner)
        self.assertEqual([r["name"] for r in repos], ["svc-a"])

    def test_two_teams_both_contribute(self):
        filters = self._filters("team one\nteam two\n")
        runner = self._runner(
            org_repos=[],
            team_repos={"one": ["alpha"], "two": ["beta"]},
        )
        repos = repo_list.discover(filters, runner=runner)
        self.assertEqual([r["name"] for r in repos], ["alpha", "beta"])


class SyncRepositoryTest(SettingsIsolated):
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


class MainProvenanceTest(SettingsIsolated):
    def test_sync_records_provenance_for_every_configured_repository(self):
        """Design promise: after `knowledgestore sync`, provenance.json
        describes every configured repository with its own sha/branch/
        committed - the durable artefact every staleness and deep-dive check
        downstream reads via provenance.read(). Only the true IO boundaries
        are stubbed (git clone/fetch behind sync_repository; git
        rev-parse/log behind head_info) - main()'s own wiring (config
        parsing, provenance.write) runs for real."""
        from knowledgestore import provenance

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "repositories.txt"
            config_path.write_text(
                "repo-a|git@example.com:o/repo-a.git|main\n"
                "repo-b|git@example.com:o/repo-b.git|main\n",
                encoding="utf-8",
            )

            self.addCleanup(setattr, sync, "CONFIG", config.REPOSITORIES_CONFIG)
            config.configure(REPOSITORIES_CONFIG=config_path)
            self.addCleanup(setattr, sync, "REPOSITORIES", config.REPOSITORIES_DIR)
            config.configure(REPOSITORIES_DIR=root / "repositories")
            # true IO boundary: no real git clone/fetch/reset
            self.addCleanup(setattr, sync, "sync_repository", sync.sync_repository)
            sync.sync_repository = lambda repo, repositories_dir, run=None: 5

            # true IO boundary: no real git rev-parse/log. The sha is
            # derived from which repository is being asked about, so a
            # wiring bug (e.g. both repos ending up with the same entry,
            # or one repo missing) fails the per-repo assertions below.
            def fake_head_info(repo_dir: Path, branch: str, run=None) -> dict:
                return {
                    "sha": (repo_dir.name * 40)[:40],
                    "branch": branch,
                    "committed": "2026-07-01T00:00:00+00:00",
                }

            self.addCleanup(setattr, provenance, "head_info", provenance.head_info)
            provenance.head_info = fake_head_info

            self.addCleanup(setattr, provenance, "PROVENANCE_PATH", config.PROVENANCE_PATH)
            config.configure(PROVENANCE_PATH=root / "provenance.json")

            result = sync.main()
            recorded = provenance.read()  # the artefact downstream stages consume

        self.assertEqual(result, 0)
        self.assertEqual(set(recorded), {"repo-a", "repo-b"})
        self.assertEqual(
            recorded["repo-a"],
            {
                "sha": ("repo-a" * 40)[:40],
                "branch": "main",
                "committed": "2026-07-01T00:00:00+00:00",
            },
        )
        self.assertEqual(
            recorded["repo-b"],
            {
                "sha": ("repo-b" * 40)[:40],
                "branch": "main",
                "committed": "2026-07-01T00:00:00+00:00",
            },
        )


class SyncFailureIsolationTest(SettingsIsolated):
    """One repository's failure must not cost the whole estate.

    A single `git fetch` exiting non-zero used to abort the run: repositories
    after it were never attempted, and because provenance is written after the
    loop, the ones that had succeeded lost their record too — so the run
    produced nothing at all. On a large estate one failure left dozens of
    repositories unsynced, and the traceback named only the repository that
    failed, never what had been skipped.
    """

    def _setup(self, root, failing: set[str]):
        from knowledgestore import provenance

        config_path = root / "repositories.txt"
        config_path.write_text(
            "".join(f"repo-{n}|git@example.com:o/repo-{n}.git|main\n" for n in "abc"),
            encoding="utf-8",
        )
        self.addCleanup(setattr, sync, "CONFIG", config.REPOSITORIES_CONFIG)
        config.configure(REPOSITORIES_CONFIG=config_path)
        self.addCleanup(setattr, sync, "REPOSITORIES", config.REPOSITORIES_DIR)
        config.configure(REPOSITORIES_DIR=root / "repositories")

        self.attempted: list[str] = []

        def fake_sync(repo, repositories_dir, run=None):
            self.attempted.append(repo.name)
            if repo.name in failing:
                raise subprocess.CalledProcessError(1, ["git", "fetch"])
            return 5

        self.addCleanup(setattr, sync, "sync_repository", sync.sync_repository)
        sync.sync_repository = fake_sync

        self.addCleanup(setattr, provenance, "head_info", provenance.head_info)
        provenance.head_info = lambda repo_dir, branch, run=None: {
            "sha": (repo_dir.name * 40)[:40],
            "branch": branch,
            "committed": "2026-07-01T00:00:00+00:00",
        }
        self.addCleanup(setattr, provenance, "PROVENANCE_PATH", config.PROVENANCE_PATH)
        config.configure(PROVENANCE_PATH=root / "provenance.json")
        return provenance

    def test_a_failing_repository_does_not_skip_the_ones_after_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            provenance = self._setup(Path(tmp), failing={"repo-a"})
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = sync.main()
            recorded = provenance.read()
            output = buffer.getvalue()

        self.assertEqual(self.attempted, ["repo-a", "repo-b", "repo-c"], "every repo attempted")
        self.assertEqual(set(recorded), {"repo-b", "repo-c"}, "successes keep their provenance")
        self.assertIn("repo-a", output, "the failure is named")
        self.assertEqual(code, 1, "a partial sync must not report success")

    def test_a_clean_run_still_succeeds_and_records_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            provenance = self._setup(Path(tmp), failing=set())
            with redirect_stdout(io.StringIO()):
                code = sync.main()
            recorded = provenance.read()

        self.assertEqual(code, 0)
        self.assertEqual(set(recorded), {"repo-a", "repo-b", "repo-c"})


class UnmatchedRuleWarningTest(SettingsIsolated):
    """A rule that selects nothing must say so.

    Rules take the whole rest of the line, so `repo name  # note` becomes a
    rule for a repository called "name  # note" and can never match. Discovery
    printed its usual count and exited 0, so three of four intended additions
    were silently dropped during an estate expansion and only found by diffing
    the selected count against the previous run. The same silence covers a
    renamed repository and a mistyped team slug.
    """

    def _filters(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repository-filters.txt"
            path.write_text(text, encoding="utf-8")
            return repo_list.read_filters(path), path

    @staticmethod
    def _runner(org_repos, team_repos=None):
        def fake_gh(arguments):
            import json

            joined = " ".join(arguments)
            if "/teams/" in joined:
                slug = joined.split("/teams/")[1].split("/")[0]
                rows = (team_repos or {}).get(slug, [])
            else:
                rows = org_repos
            return "\n".join(json.dumps({"name": n, "defaultBranch": "main"}) for n in rows)

        return fake_gh

    def test_repo_rule_matching_nothing_is_reported_with_its_line(self):
        filters, path = self._filters("prefix svc-\nrepo does-not-exist\n")
        runner = self._runner(["svc-a"])
        problems = repo_list.unmatched_rules(
            filters, repo_list.discover(filters, runner=runner), runner=runner
        )
        self.assertEqual(problems, [(2, "repo does-not-exist")])

    def test_a_trailing_comment_makes_the_rule_unmatchable_and_is_reported(self):
        # the exact shape that cost three repositories
        filters, path = self._filters("repo svc-a  # the important one\n")
        runner = self._runner(["svc-a"])
        problems = repo_list.unmatched_rules(
            filters, repo_list.discover(filters, runner=runner), runner=runner
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("# the important one", problems[0][1])

    def test_rules_that_did_match_are_not_reported(self):
        filters, path = self._filters("prefix svc-\nrepo other\nteam platform\n")
        runner = self._runner(["svc-a", "other", "owned"], {"platform": ["owned"]})
        selected = repo_list.discover(filters, runner=runner)
        self.assertEqual(repo_list.unmatched_rules(filters, selected, runner=runner), [])

    def test_empty_team_is_reported(self):
        filters, path = self._filters("prefix svc-\nteam ghost-team\n")
        runner = self._runner(["svc-a"], {"ghost-team": []})
        selected = repo_list.discover(filters, runner=runner)
        self.assertEqual(
            repo_list.unmatched_rules(filters, selected, runner=runner),
            [(2, "team ghost-team")],
        )

    def test_strict_turns_an_unmatched_rule_into_a_non_zero_exit(self):
        # interactively a bad rule warns and still produces an estate; in CI it
        # must fail the build, or nothing enforces the config being correct
        from knowledgestore import config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "repository-filters.txt").write_text(
                "prefix svc-\nrepo does-not-exist\n", encoding="utf-8"
            )
            old_root = config.ROOT
            try:
                config.configure(root=str(root))
                config.configure(GITHUB_ORG="example-org")
                runner = self._runner(["svc-a"])
                lenient = repo_list.main([], runner=runner)
                strict = repo_list.main(["--strict"], runner=runner)
            finally:
                config.configure(root=str(old_root))

        self.assertEqual(lenient, 0, "a warning must not break an interactive run")
        self.assertEqual(strict, 1, "--strict must fail when a rule selected nothing")

    def test_a_stale_exclude_is_not_reported(self):
        # excludes legitimately outlive the repository they excluded
        filters, path = self._filters("prefix svc-\nexclude long-gone\n")
        runner = self._runner(["svc-a"])
        selected = repo_list.discover(filters, runner=runner)
        self.assertEqual(repo_list.unmatched_rules(filters, selected, runner=runner), [])


class ExportHistoryRootTest(SettingsIsolated):
    """The CLI --root contract: a stage operates on the configured store root.

    export-history ignored config.ROOT in favour of a package-relative
    default, so the first estate rebuild from the installed wheel looked for
    config/repositories.txt inside site-packages, exported nothing, and the
    downstream stages silently rebuilt from stale history.
    """

    def test_cli_root_reaches_export_history(self):
        from knowledgestore import config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "config").mkdir()
            repo_dir = root / "repositories" / "demo"
            repo_dir.mkdir(parents=True)
            git = ["git", "-C", str(repo_dir), "-c", "user.email=t@t", "-c", "user.name=t"]
            subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
            (repo_dir / "a.txt").write_text("hello\n", encoding="utf-8")
            subprocess.run([*git, "add", "."], check=True)
            subprocess.run([*git, "commit", "-qm", "CCT-1 first"], check=True)
            (root / "config" / "repositories.txt").write_text(
                "demo|git@github.com:example/demo.git|main\n", encoding="utf-8"
            )

            old_root, old_argv = config.ROOT, list(sys.argv)
            try:
                exit_code = cli.main(["--root", str(root), "export-history"])
            finally:
                config.configure(root=str(old_root))
                sys.argv = old_argv

            self.assertEqual(exit_code, 0)
            dataset = root / "knowledge" / "git-history" / "demo" / "commits.ndjson"
            self.assertTrue(dataset.exists(), "history dataset must land under the store root")
            self.assertIn("CCT-1", dataset.read_text(encoding="utf-8"))


class CliTest(SettingsIsolated):
    def test_stages_are_listed_in_pipeline_order(self):
        self.assertEqual(
            list(cli.STAGES),
            [
                "discover",
                "sync",
                "convert",
                "export-history",
                "context",
                "intent",
                "ticket-titles",
                # the other route to the same enrichment: ask the tracker
                # rather than import an export of it
                "fetch-tickets",
                "gherkin",
                "packages",
                # before clustering, so deployment nodes join communities
                "deployments",
                "summaries",
                "semantic",
                "topics",
                "deepdive",
                "explorer",
                "status",
                # not part of a build: gates a store runs in CI
                "check-install-docs",
                "check-corpus",
                "check-evidence",
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


class SemanticVocabularyTest(SettingsIsolated):
    def test_vocabulary_filters_short_stop_and_rare_tokens(self):
        from knowledgestore import build_semantic_index as semantic

        with tempfile.TemporaryDirectory() as tmp:
            graph = Path(tmp) / "graph.json"
            import json

            graph.write_text(
                json.dumps(
                    {
                        "nodes": [
                            # df = 3: once in each of three texts
                            {"label": "address validation service"},
                            {"label": "address validation pipe"},
                            {"label": "address validation form"},
                            {"label": "there there there"},  # stopword
                            {"label": "ab ab ab"},  # too short
                            {"label": "rareword"},  # below MIN_DF
                            # MIN_DF means DOCUMENT frequency: repetition inside a
                            # single text must not qualify a token. The old
                            # implementation counted occurrences, and this test
                            # used to codify that by repeating a phrase three
                            # times in one label.
                            {"label": "shouted shouted shouted"},
                        ],
                        "links": [],
                    }
                ),
                encoding="utf-8",
            )
            config.configure(GRAPH_PATH=graph)
            config.configure(LABELS_PATH=Path(tmp) / "missing.json")
            config.configure(SUMMARIES_PATH=Path(tmp) / "missing2.json")
            vocab = semantic.collect_vocabulary()
        self.assertIn("address", vocab)
        self.assertIn("validation", vocab)
        self.assertNotIn("there", vocab)
        self.assertNotIn("rareword", vocab)
        self.assertNotIn("shouted", vocab, "occurrences in one text are not document frequency")

    def test_manifest_records_what_decided_the_artefact_and_is_deterministic(self):
        import json

        from knowledgestore import build_semantic_index as semantic

        with tempfile.TemporaryDirectory() as tmp:
            config.configure(SYNONYMS_PATH=Path(tmp) / "token-neighbours.json.gz")
            semantic.write_manifest(["alpha", "beta"], dimensions=384)
            first = (Path(tmp) / "manifest.json").read_bytes()
            semantic.write_manifest(["alpha", "beta"], dimensions=384)
            second = (Path(tmp) / "manifest.json").read_bytes()
            manifest = json.loads(first)
        self.assertEqual(first, second, "no timestamps - as deterministic as the artefact")
        for key in ("model", "dimensions", "min_df", "min_similarity", "vocabulary_sha256"):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["vocabulary_size"], 2)


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


class NearestNeighboursTest(SettingsIsolated):
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


class IntentSummariseTest(SettingsIsolated):
    def test_reports_counts(self):
        import contextlib
        import gzip as _gzip
        import io
        import json as _json
        from knowledgestore import build_intent_index as intent

        with tempfile.TemporaryDirectory() as tmp:
            config.configure(INTENT_INDEX_PATH=Path(tmp) / "file-tickets.json.gz")
            with _gzip.open(config.INTENT_INDEX_PATH, "wt", encoding="utf-8") as out:
                _json.dump({}, out)
            index = {"repo-a": {"a.ts": {"tickets": {"DD-1": 2}}}}
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                intent.summarise(index, 2)
        printed = buffer.getvalue()
        self.assertIn("1 files", printed.replace(",", ""))
        self.assertIn("1 distinct tickets", printed.replace(",", ""))


class FailedSyncKeepsProvenanceTest(SettingsIsolated):
    """A repository that fails to sync must keep its previous record.

    `entries` starts empty each run and `provenance.write` replaces the file, so
    a failure used to DELETE the repository's record - the manifest then declared
    163 repositories while provenance held 162, and a reconciliation that
    iterates provenance could not see the missing one at all. A check keyed on
    the record cannot see a record that was removed.
    """

    def _run(self, failing: str) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        manifest = root / "repositories.txt"
        manifest.write_text(
            "repo-a | https://example.invalid/repo-a.git | main\n"
            "repo-b | https://example.invalid/repo-b.git | main\n",
            encoding="utf-8",
        )
        config.configure(
            ROOT=root,
            REPOSITORIES_CONFIG=manifest,
            REPOSITORIES_DIR=root / "repositories",
            EXTERNAL_CONFIG=root / "external.txt",
            EXTERNAL_DIR=root / "external",
            PROVENANCE_PATH=root / "provenance.json",
        )
        from knowledgestore import provenance

        provenance.write({"repo-a": {"sha": "old-a"}, "repo-b": {"sha": "old-b"}}, {})

        self.addCleanup(setattr, sync, "sync_repository", sync.sync_repository)
        self.addCleanup(setattr, provenance, "head_info", provenance.head_info)
        provenance.head_info = lambda *a, **k: {"sha": "new"}

        def fake(repo, repositories_dir, run=None):
            if repo.name == failing:
                raise RuntimeError("would clobber existing tag")
            return 1

        sync.sync_repository = fake
        printed = io.StringIO()
        with redirect_stdout(printed), contextlib.redirect_stderr(io.StringIO()):
            sync.main()
        # Kept, not discarded: one of these tests asserts on the reported
        # denominator, which is where the retention fix broke the arithmetic.
        self.printed = printed.getvalue()
        return provenance.read()

    def test_the_failed_repository_keeps_its_previous_commit(self):
        recorded = self._run("repo-b")
        self.assertIn(
            "repo-b",
            recorded,
            "dropping it makes the repository invisible to any provenance-keyed check",
        )
        self.assertEqual(recorded["repo-b"]["sha"], "old-b")
        self.assertIn("clobber", recorded["repo-b"]["sync_failed"])

    def test_the_successful_repository_is_updated(self):
        recorded = self._run("repo-b")
        self.assertEqual(recorded["repo-a"]["sha"], "new")
        self.assertNotIn("sync_failed", recorded["repo-a"])

    def test_the_failure_count_does_not_double_count_a_retained_record(self):
        """The retention fix broke the arithmetic of the message reporting it.

        `total` was `len(entries) + len(failures)`, and retaining a failed
        repository puts its name in BOTH - so it was counted twice. Only on the
        retention path: without a previous record the sum was right, which is why
        a test had to pre-seed provenance AND assert on the printed denominator
        to see it. On a 361-repository sync it read "12 of 373", sending an
        operator to look for twelve missing clones in a list of 361.
        """
        self._run("repo-b")
        line = next(x for x in self.printed.splitlines() if "failed to sync" in x)
        self.assertIn(
            "1 of 2",
            line,
            f"the manifest declares two repositories: {line!r}",
        )

    def test_membership_still_matches_the_manifest(self):
        """The invariant: after a sync, every declared repository has a record."""
        recorded = self._run("repo-b")
        self.assertEqual(set(recorded), {"repo-a", "repo-b"})


class FetchShapeTest(unittest.TestCase):
    """A moved tag must not fail the whole fetch.

    The refspec's leading + force-updates heads and says nothing about tags, so
    a repository that re-points a release tag - which is routine - rejects with
    "would clobber existing tag" and becomes mysteriously unsyncable.
    """

    def test_the_fetch_forces_tags(self):
        calls = []
        repo = SimpleNamespace(
            name="repo-a", clone_url="https://example.invalid/repo-a.git", default_branch="main"
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "repo-a" / ".git").mkdir(parents=True)

        def run(args):
            calls.append(args)
            return "1"

        sync.sync_repository(repo, Path(tmp.name), run=run)
        fetch = next(c for c in calls if "fetch" in c)
        self.assertIn("--force", fetch, f"a moved tag fails this fetch: {fetch}")
        self.assertIn("--tags", fetch)


if __name__ == "__main__":
    unittest.main()


class FetchOnlyRuleTest(SettingsIsolated):
    """`fetch` means clone it, never extract it.

    The rule exists because an estate that needed a repository on disk but not in
    the graph had no supported way to say so, and met the need with a clone kept by
    hand, which drifted and was easy to be unaware of. Ingesting it instead leaves
    the graph describing the same estate twice, from two sources that then diverge.

    The guarantee is structural rather than procedural - a fetch-only repository is
    written to a different manifest and cloned to a different directory, and the
    extraction pass only ever walks repositories/. These tests hold that line.
    """

    def _filters(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repository-filters.txt"
            path.write_text(text, encoding="utf-8")
            return repo_list.read_filters(path)

    def test_a_fetch_rule_selects_the_repository_like_any_include(self):
        # It has to be selected: the clone URL and default branch come from the
        # organisation listing exactly as they do for an estate repository.
        filters = self._filters("prefix svc-\nfetch outside-thing\n")
        self.assertTrue(filters.matches("outside-thing"))
        self.assertEqual(filters.fetch_only, {"outside-thing"})

    def test_the_same_name_cannot_be_both_estate_and_fetch_only(self):
        with self.assertRaises(ValueError) as caught:
            self._filters("repo both-ways\nfetch both-ways\n")
        self.assertIn("both-ways", str(caught.exception))

    def test_exclude_still_beats_fetch(self):
        filters = self._filters("prefix svc-\nfetch gone-away\nexclude gone-away\n")
        self.assertFalse(filters.matches("gone-away"))

    def test_fetch_only_repositories_are_kept_out_of_the_estate_manifest(self):
        """The one that matters: a prefix must not drag a fetch-only repo in.

        `prefix svc-` matches `svc-notes`, so without fetch taking precedence the
        repository would land in the estate manifest and be extracted - the outcome
        the rule exists to avoid.
        """

        def fake_runner(args):
            return (
                '{"name":"svc-a","defaultBranch":"main"}\n'
                '{"name":"svc-notes","defaultBranch":"main"}\n'
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "repository-filters.txt").write_text(
                "prefix svc-\nfetch svc-notes\n", encoding="utf-8"
            )
            config.configure(root=root, GITHUB_ORG="myorg")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = repo_list.main([], runner=fake_runner)
            estate = (root / "config" / "repositories.txt").read_text(encoding="utf-8")
            external = (root / "config" / "repositories-external.txt").read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertIn("svc-a|", estate)
        self.assertNotIn("svc-notes", estate, "a fetch-only repo must never reach the estate")
        self.assertIn("svc-notes|", external)
        self.assertNotIn("svc-a", external)
        self.assertIn("not extracted", external, "the file says what it is")
        self.assertIn("not part of the estate", external, "and what it is not")

    def test_the_external_manifest_is_cleared_when_the_last_rule_goes(self):
        # Otherwise removing a `fetch` rule leaves a stale file that `sync` acts
        # on, and the repository keeps being fetched with nothing declaring it.
        def fake_runner(args):
            return '{"name":"svc-a","defaultBranch":"main"}\n'

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            stale = root / "config" / "repositories-external.txt"
            stale.write_text("old-thing|git@example.com:o/old-thing.git|main\n", encoding="utf-8")
            (root / "config" / "repository-filters.txt").write_text(
                "prefix svc-\n", encoding="utf-8"
            )
            config.configure(root=root, GITHUB_ORG="myorg")
            with redirect_stdout(io.StringIO()):
                repo_list.main([], runner=fake_runner)
            self.assertNotIn("old-thing", stale.read_text(encoding="utf-8"))


class SyncExternalTest(SettingsIsolated):
    """Fetch-only repositories are cloned somewhere the extraction pass never looks."""

    def _setup(self, root):
        from knowledgestore import provenance

        (root / "config").mkdir()
        (root / "config" / "repositories.txt").write_text(
            "repo-a|git@example.com:o/repo-a.git|main\n", encoding="utf-8"
        )
        (root / "config" / "repositories-external.txt").write_text(
            "outside-thing|git@example.com:o/outside-thing.git|main\n", encoding="utf-8"
        )
        config.configure(root=root)

        self.where: dict[str, Path] = {}

        def fake_sync(repo, repositories_dir, run=None):
            self.where[repo.name] = repositories_dir
            return 5

        self.addCleanup(setattr, sync, "sync_repository", sync.sync_repository)
        sync.sync_repository = fake_sync
        self.addCleanup(setattr, provenance, "head_info", provenance.head_info)
        provenance.head_info = lambda repo_dir, branch, run=None: {
            "sha": "a" * 40,
            "branch": branch,
            "committed": "2026-07-01T00:00:00+00:00",
        }
        return provenance

    def test_external_repositories_land_outside_the_extraction_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provenance = self._setup(root)
            with redirect_stdout(io.StringIO()):
                code = sync.main()
            recorded = provenance.read()
            external = provenance.read_external()
            repositories_dir, external_dir = config.REPOSITORIES_DIR, config.EXTERNAL_DIR

        self.assertEqual(code, 0)
        self.assertEqual(self.where["repo-a"], repositories_dir)
        self.assertEqual(
            self.where["outside-thing"],
            external_dir,
            "a fetch-only repo must not be cloned into repositories/",
        )
        self.assertNotEqual(repositories_dir, external_dir)
        self.assertEqual(
            set(recorded), {"repo-a"}, "the estate count must not include fetch-only sources"
        )
        self.assertEqual(
            set(external),
            {"outside-thing"},
            "but its commit is still recorded, so a finding can cite it",
        )

    def test_no_external_config_is_normal_and_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provenance = self._setup(root)
            (root / "config" / "repositories-external.txt").unlink()
            with redirect_stdout(io.StringIO()):
                code = sync.main()
            self.assertEqual(code, 0)
            self.assertEqual(provenance.read_external(), {})


class FetchOnlyRuleReportingTest(SettingsIsolated):
    """What `unmatched_rules` and the include check make of a `fetch` rule.

    These pin decisions that are easy to reverse by accident while editing the rule
    dispatch, and that nothing else would notice.
    """

    def _filters(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repository-filters.txt"
            path.write_text(text, encoding="utf-8")
            return repo_list.read_filters(path)

    @staticmethod
    def _runner(names):
        import json

        def fake_gh(arguments):
            return "\n".join(json.dumps({"name": n, "defaultBranch": "main"}) for n in names)

        return fake_gh

    def test_a_fetch_rule_that_selects_nothing_is_reported(self):
        # A `fetch` naming a repository that does not exist is as much a config
        # defect as a `repo` that names one - it is tracked in `origins` for that
        # reason, and --strict has to be able to fail on it.
        filters = self._filters("prefix svc-\nfetch gone-from-the-org\n")
        runner = self._runner(["svc-a"])
        selected = repo_list.discover(filters, runner=runner)
        problems = repo_list.unmatched_rules(filters, selected, runner=runner)
        self.assertEqual(problems, [(2, "fetch gone-from-the-org")])

    def test_an_exclude_is_not_tracked_as_a_selecting_rule(self):
        """An exclude legitimately outlives what it excluded, so it is not tracked.

        Asserted on `origins` rather than on the reported problems. Two independent
        things currently keep a stale exclusion from being reported - the dispatch
        not tracking it, and `unmatched_rules` skipping the kind - so an
        outcome-only assertion passes even when one of them is removed. Mutation
        testing showed exactly that: dropping the early return changed no test
        result. This pins the mechanism the docstring in `Filters` describes.
        """
        filters = self._filters("prefix svc-\nexclude retired-years-ago\n")
        self.assertEqual(
            [rule for _, rule in filters.origins],
            ["prefix svc-"],
            "an exclude must not be recorded as a rule that has to select something",
        )
        # And the outcome it exists for, which is what an operator sees.
        runner = self._runner(["svc-a"])
        selected = repo_list.discover(filters, runner=runner)
        self.assertEqual(repo_list.unmatched_rules(filters, selected, runner=runner), [])

    # Named to avoid being exactly 40 characters. The secret scanner's Lob
    # detector matches `test_` followed by 35 word characters - the shape of a Lob
    # test key - and reports it as a verified secret, failing the build. Three
    # pre-existing names in this file have the same length and will do the same to
    # whoever next edits their lines; raised as #97.
    def test_fetch_alone_does_not_define_an_estate(self):
        # A store whose only rules are `fetch` has nothing to extract. Better to
        # say so than to build an empty graph and leave someone wondering.
        with self.assertRaises(ValueError) as caught:
            self._filters("fetch outside-thing\n")
        self.assertIn("No include rules", str(caught.exception))

    def test_a_team_owned_repository_can_still_be_fetch_only(self):
        """Team rules never consult `matches()`, so only the partition catches this.

        `discover` walks a team's own listing and filters it by `excludes` alone -
        a fetch-only name is invisible to that loop. The split in `main()` is the
        one thing keeping such a repository out of the estate manifest.
        """
        import json

        def fake_runner(arguments):
            joined = " ".join(arguments)
            names = ["svc-a", "team-notes"] if "/teams/" in joined else []
            return "\n".join(json.dumps({"name": n, "defaultBranch": "main"}) for n in names)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "repository-filters.txt").write_text(
                "team platform-team\nfetch team-notes\n", encoding="utf-8"
            )
            config.configure(root=root, GITHUB_ORG="myorg")
            with redirect_stdout(io.StringIO()):
                code = repo_list.main([], runner=fake_runner)
            estate = (root / "config" / "repositories.txt").read_text(encoding="utf-8")
            external = (root / "config" / "repositories-external.txt").read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertIn("svc-a|", estate)
        self.assertNotIn("team-notes", estate, "team ownership must not override `fetch`")
        self.assertIn("team-notes|", external)


class SyncExternalFailureTest(SettingsIsolated):
    """A fetch-only repository failing to sync must not cost the estate.

    The estate loop learned this the hard way - one unreachable remote used to
    abort the run and discard the provenance of everything already synced. The
    external loop is a second copy of that shape, so it needs the same guarantee
    held down rather than assumed from the first.
    """

    def _setup(self, root, failing):
        from knowledgestore import provenance

        (root / "config").mkdir()
        (root / "config" / "repositories.txt").write_text(
            "repo-a|git@example.com:o/repo-a.git|main\n", encoding="utf-8"
        )
        (root / "config" / "repositories-external.txt").write_text(
            "outside-a|git@example.com:o/outside-a.git|main\n"
            "outside-b|git@example.com:o/outside-b.git|main\n",
            encoding="utf-8",
        )
        config.configure(root=root)

        self.attempted = []

        def fake_sync(repo, repositories_dir, run=None):
            self.attempted.append(repo.name)
            if repo.name in failing:
                raise subprocess.CalledProcessError(1, ["git", "fetch"])
            return 5

        self.addCleanup(setattr, sync, "sync_repository", sync.sync_repository)
        sync.sync_repository = fake_sync
        self.addCleanup(setattr, provenance, "head_info", provenance.head_info)
        provenance.head_info = lambda repo_dir, branch, run=None: {
            "sha": "a" * 40,
            "branch": branch,
            "committed": "2026-07-01T00:00:00+00:00",
        }
        return provenance

    def test_one_failing_external_repository_does_not_stop_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provenance = self._setup(root, failing={"outside-a"})
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = sync.main()
            recorded, external = provenance.read(), provenance.read_external()
            output = buffer.getvalue()

        self.assertEqual(self.attempted, ["repo-a", "outside-a", "outside-b"])
        self.assertEqual(set(recorded), {"repo-a"}, "the estate keeps its provenance")
        self.assertEqual(set(external), {"outside-b"}, "the one that worked keeps its record")
        self.assertIn("outside-a", output, "the failure is named")
        self.assertEqual(code, 1, "a partial sync must not report success")
