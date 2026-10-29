"""A repository `discover` drops from `config/repositories.txt` stops being part of the estate.

The store is built through the real stages, over real git repositories: two origins,
`alpha` and `beta`, each with one commit naming a ticket. `sync`, `export-history` and
`intent` run on it as the operator runs them, then `beta` is removed from the
declaration (the case `discover` produces when a repository is archived) and each stage
runs again. Assertions land on what the stages wrote and printed, not on calls.
"""

from __future__ import annotations

import contextlib
import gzip
import io as _io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from test_drift import Estate, _commit  # noqa: E402

from knowledgestore import cli, config, drift, export_git_history

SOURCE = Path(__file__).resolve().parent.parent / "src"
GRAPH_BYTES = b'{"nodes": [], "note": "untracked per-repository graph"}\n'


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
    )


def _origin(root: Path, name: str, subject: str) -> Path:
    origin = root / "origins" / name
    origin.mkdir(parents=True)
    _git(origin, "init", "-q", "-b", "main")
    (origin / "src").mkdir()
    (origin / "src" / f"{name}.ts").write_text(f"export const {name} = 1;\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", subject)
    return origin


class TwoRepositoryStore(SettingsIsolated):
    """alpha and beta, synced, exported and indexed; `drop_beta` then undeclares beta.

    The store is built once per class, through the real stages in child processes, and
    copied for each test: building real git origins and running three stages per test
    made this module cost minutes, and every test needs only its own copy of the result.
    """

    template: Path

    @classmethod
    def setUpClass(cls):
        cls.template = Path(tempfile.mkdtemp()).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.template, True)
        (cls.template / "config").mkdir()
        cls.origins = {
            "alpha": _origin(cls.template, "alpha", "DEMO-1: tidy alpha"),
            "beta": _origin(cls.template, "beta", "DEMO-2: retire beta"),
        }
        (cls.template / "config" / "repositories.txt").write_text(
            "".join(f"{name}|{origin}|main\n" for name, origin in cls.origins.items()),
            encoding="utf-8",
        )
        env = {**os.environ, "PYTHONPATH": str(SOURCE)}
        for stage in ("sync", "export-history", "intent"):
            done = subprocess.run(
                [sys.executable, "-B", "-m", "knowledgestore", "--root", str(cls.template), stage],
                capture_output=True,
                text=True,
                env=env,
            )
            assert done.returncode == 0, (stage, done.stdout, done.stderr)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / "store"
        shutil.copytree(self.template, self.root, symlinks=True)
        # Work a clone holds that nothing else does (CLAUDE.md 7).
        graph = self.root / "repositories" / "beta" / "graphify-out" / "graph.json"
        graph.parent.mkdir()
        graph.write_bytes(GRAPH_BYTES)

    def declare(self, *names: str) -> None:
        (self.root / "config" / "repositories.txt").write_text(
            "".join(f"{name}|{self.origins[name]}|main\n" for name in names), encoding="utf-8"
        )

    def run_stage(self, stage: str) -> tuple[int, str]:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["--root", str(self.root), stage])
        return code, out.getvalue()

    def intent_layer(self) -> dict:
        with gzip.open(self.root / "knowledge" / "intent" / "file-tickets.json.gz", "rt") as f:
            return json.load(f)

    def drop_beta(self) -> None:
        self.declare("alpha")


class SyncTest(TwoRepositoryStore):
    def test_sync_names_an_undeclared_clone_and_leaves_it_untouched(self):
        """Catches: sync deleting the clone, taking its untracked graph with it
        (CLAUDE.md 7), or saying nothing so the clone lingers unseen."""
        self.drop_beta()
        code, output = self.run_stage("sync")
        self.assertEqual(code, 0)
        self.assertIn("1 clone(s) not declared: beta", output)
        graph = self.root / "repositories" / "beta" / "graphify-out" / "graph.json"
        self.assertEqual(graph.read_bytes(), GRAPH_BYTES)
        self.assertTrue((self.root / "repositories" / "beta" / ".git").is_dir())

    def test_sync_reports_nothing_while_every_clone_is_declared(self):
        """Catches: the report firing on a healthy estate, which would teach
        operators to ignore it."""
        _, output = self.run_stage("sync")
        self.assertNotIn("not declared", output)


class ExportHistoryTest(TwoRepositoryStore):
    def history(self, name: str) -> Path:
        return self.root / "knowledge" / "git-history" / name

    def snapshot(self, name: str) -> dict[str, bytes]:
        base = self.history(name)
        return {
            str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()
        }

    def test_export_history_removes_the_undeclared_directory_and_keeps_the_declared(self):
        """Catches: a dropped repository's dataset surviving (so its tickets stay
        indexed), or the removal reaching a declared repository's dataset."""
        before = self.snapshot("alpha")
        self.assertTrue(before)
        self.drop_beta()
        code, output = self.run_stage("export-history")
        self.assertEqual(code, 0)
        self.assertIn("1 history directory(ies) for undeclared repositories removed: beta", output)
        self.assertFalse(self.history("beta").exists())
        self.assertEqual(self.snapshot("alpha"), before)
        # Clones are never touched.
        self.assertEqual(
            (self.root / "repositories" / "beta" / "graphify-out" / "graph.json").read_bytes(),
            GRAPH_BYTES,
        )

    def test_a_second_run_removes_nothing_more(self):
        """Catches: the report firing again for a directory already gone."""
        self.drop_beta()
        self.run_stage("export-history")
        _, output = self.run_stage("export-history")
        self.assertNotIn("removed", output)

    def test_a_symlinked_entry_is_not_followed_out_of_the_history_root(self):
        """Catches: rmtree of an undeclared entry that is a symlink, deleting what
        it points at, which sits outside knowledge/git-history/."""
        outside = self.root / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
        (self.history("escape")).symlink_to(outside, target_is_directory=True)
        self.drop_beta()
        code, _ = self.run_stage("export-history")
        self.assertEqual(code, 0)
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertFalse(self.history("beta").exists())

    def test_removals_are_named_with_the_list_capped(self):
        """Catches: hundreds of removed names printed in full."""
        for number in range(7):
            self.history(f"gone-{number}").mkdir()
        _, output = self.run_stage("export-history")
        self.assertIn(
            "7 history directory(ies) for undeclared repositories removed: "
            "gone-0, gone-1, gone-2, gone-3, gone-4 and 2 more",
            output,
        )

    def snapshot_all(self) -> dict[str, bytes]:
        base = self.root / "knowledge" / "git-history"
        return {
            str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()
        }

    def tree(self, base: Path) -> dict[str, bytes]:
        return {
            str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()
        }

    def export_to(self, *extra: str) -> tuple[int, str]:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["--root", str(self.root), "export-history", *extra])
        return code, out.getvalue()

    def assert_nothing_outside_the_rule_is_touched(self, before: dict[str, bytes]) -> None:
        after = self.tree(self.root)
        for path, data in before.items():
            self.assertIn(path, after, f"{path} was deleted")
            self.assertEqual(after[path], data, f"{path} changed")

    def test_an_output_directory_that_is_not_the_history_root_sweeps_nothing(self):
        """Catches: `--output <store root>` (or `<root>/knowledge`) making config/,
        knowledge/, repositories/ with its untracked graphs and .git count as
        undeclared history directories and be deleted."""
        _git(self.root, "init", "-q")
        (self.root / ".git" / "marker").write_text("keep\n", encoding="utf-8")
        for label, output in (("root", self.root), ("knowledge", self.root / "knowledge")):
            with self.subTest(output=label):
                self.drop_beta()
                before = self.tree(self.root)
                code, text = self.export_to("--output", str(output))
                self.assertEqual(code, 0, text)
                self.assertIn("is not this store's history root", text)
                self.assertNotIn("removed:", text)
                self.assert_nothing_outside_the_rule_is_touched(before)
                for name in ("config", "knowledge", "repositories", ".git"):
                    self.assertTrue((self.root / name).is_dir(), name)

    def test_a_whitespace_only_declaration_removes_nothing(self):
        """Catches: a declaration of blank lines parsing to nothing and reading as
        "every repository is undeclared"."""
        before = self.snapshot_all()
        (self.root / "config" / "repositories.txt").write_text("  \n\t\n\n", encoding="utf-8")
        self.run_stage("export-history")
        self.assertEqual(self.snapshot_all(), before)

    def test_a_plain_file_under_the_history_root_survives(self):
        """Catches: the sweep removing anything but a directory."""
        note = self.history("notes.txt")
        note.write_text("keep\n", encoding="utf-8")
        self.drop_beta()
        self.run_stage("export-history")
        self.assertEqual(note.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse(self.history("beta").exists())

    def test_a_dot_directory_under_the_history_root_survives(self):
        """Catches: the sweep removing a `.`-prefixed entry such as `.git`."""
        hidden = self.history(".keep")
        hidden.mkdir()
        (hidden / "x").write_text("keep\n", encoding="utf-8")
        self.drop_beta()
        self.run_stage("export-history")
        self.assertEqual((hidden / "x").read_text(encoding="utf-8"), "keep\n")

    def test_a_declared_name_that_climbs_out_is_refused_and_nothing_is_touched(self):
        """Catches: a declared `../escape` writing outside the history root, or
        steering the sweep there."""
        (self.root / "config" / "repositories.txt").write_text(
            f"../escape|{self.origins['alpha']}|main\n", encoding="utf-8"
        )
        before = self.tree(self.root)
        code, text = self.run_stage("export-history")
        self.assertEqual(code, 1, text)
        self.assertFalse((self.root / "knowledge" / "escape").exists())
        self.assert_nothing_outside_the_rule_is_touched(before)

    def test_a_symlinked_history_root_is_swept_through_its_target(self):
        """Catches: the history root being a symlink making the sweep refuse, or
        judge every entry outside the root and delete or keep the wrong ones."""
        history = self.history("")
        target = self.root.parent / "elsewhere"
        shutil.move(str(history), str(target))
        history.symlink_to(target, target_is_directory=True)
        alpha_before = {k: v for k, v in self.tree(target).items() if k.startswith("alpha/")}
        self.drop_beta()
        code, text = self.run_stage("export-history")
        self.assertEqual(code, 0, text)
        self.assertFalse((target / "beta").exists())
        self.assertEqual(
            {k: v for k, v in self.tree(target).items() if k.startswith("alpha/")}, alpha_before
        )

    def test_a_declaration_that_names_nothing_removes_nothing(self):
        """Catches: an empty or comment-only repositories.txt reading as "every
        repository is undeclared" and deleting every history directory; and a
        missing or unparseable one doing the same before it was read."""
        before = self.snapshot_all()
        self.assertTrue(before)
        declaration = self.root / "config" / "repositories.txt"
        cases = {
            "empty": "",
            "comment-only": "# discover found nothing\n\n",
            "unparseable": "alpha|only-two-fields\n",
            "missing": None,
        }
        for label, text in cases.items():
            with self.subTest(declaration=label):
                if text is None:
                    declaration.unlink()
                else:
                    declaration.write_text(text, encoding="utf-8")
                _, output = self.run_stage("export-history")
                self.assertNotIn("removed:", output)
                self.assertEqual(self.snapshot_all(), before)
        self.declare("alpha", "beta")
        declaration.write_text("# nothing\n", encoding="utf-8")
        _, output = self.run_stage("export-history")
        self.assertIn("no history directory removed", output)

    def test_a_declaration_from_outside_the_store_removes_nothing(self):
        """Catches: --config naming another store's declaration, which lists none of
        this store's repositories, deleting this store's history."""
        before = self.snapshot_all()
        elsewhere = self.root.parent / "other-store" / "repositories.txt"
        elsewhere.parent.mkdir()
        elsewhere.write_text(f"alpha|{self.origins['alpha']}|main\n", encoding="utf-8")
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            cli.main(["--root", str(self.root), "export-history", "--config", str(elsewhere)])
        self.assertIn("may belong to another store", out.getvalue())
        self.assertEqual(self.snapshot_all(), before)

    def test_an_org_qualified_name_is_not_removed_straight_after_it_is_written(self):
        """Catches: a declared `org/alpha`, whose dataset is written under
        history/org/alpha, being judged undeclared because `org` is not a name."""
        clone = self.root / "repositories" / "org" / "alpha"
        shutil.copytree(self.root / "repositories" / "alpha", clone)
        declaration = self.root / "config" / "repositories.txt"
        declaration.write_text(
            f"org/alpha|{self.origins['alpha']}|main\nbeta|{self.origins['beta']}|main\n",
            encoding="utf-8",
        )
        code, output = self.run_stage("export-history")
        self.assertEqual(code, 0, output)
        written = self.root / "knowledge" / "git-history" / "org" / "alpha" / "commits.ndjson"
        self.assertTrue(written.is_file())
        # Only the old top-level `alpha`, which is no longer declared, goes; `org` stays.
        self.assertIn("removed: alpha\n", output)
        self.assertTrue(written.parent.parent.is_dir())

    def test_a_directory_differing_only_in_case_from_a_declared_name_is_kept(self):
        """Catches: a declared `Alpha` with an existing `alpha` directory being judged
        undeclared. On a case-insensitive filesystem they are one directory and the
        just-exported data would be deleted; the rule is asserted here by name so it
        holds on a case-sensitive one too."""
        base = self.root / "knowledge" / "git-history"
        self.assertEqual(export_git_history.undeclared_directories(base, ["Alpha"]), ["beta"])
        self.assertEqual(export_git_history.undeclared_directories(base, ["ALPHA", "Beta"]), [])


class IntentTest(TwoRepositoryStore):
    def test_before_the_drop_both_repositories_are_indexed(self):
        """Control: the instrument sees beta while it is declared, so its absence
        below is the reconciliation and not a blind read."""
        self.assertEqual(sorted(self.intent_layer()), ["alpha", "beta"])

    def test_intent_excludes_an_undeclared_repository_from_the_index_and_tickets(self):
        """Catches: intent indexing every knowledge/git-history/<repo> directory, so
        a dropped repository's tickets stay in file-tickets and in the estate."""
        self.drop_beta()
        code, output = self.run_stage("intent")
        self.assertEqual(code, 0)
        self.assertIn("1 history directory(ies) not declared, skipped: beta", output)
        self.assertIn("across 1 repos", output)
        layer = self.intent_layer()
        self.assertEqual(sorted(layer), ["alpha"])
        self.assertNotIn("DEMO-2", json.dumps(layer))
        with gzip.open(
            self.root / "knowledge" / "intent" / "ticket-descriptions.json.gz", "rt"
        ) as f:
            descriptions = json.load(f)
        self.assertEqual(sorted(descriptions), ["DEMO-1"])

    def test_a_store_declaring_nothing_still_indexes_every_history_directory(self):
        """Catches: the reconciliation treating a missing declaration as an empty
        one and indexing nothing, which switches the stage off for such stores."""
        (self.root / "config" / "repositories.txt").unlink()
        code, output = self.run_stage("intent")
        self.assertEqual(code, 0, output)
        self.assertEqual(sorted(self.intent_layer()), ["alpha", "beta"])

    def test_the_undeclared_names_are_capped(self):
        """Catches: a store that dropped hundreds of repositories printing them all."""
        for number in range(7):
            extra = self.root / "knowledge" / "git-history" / f"gone-{number}"
            extra.mkdir()
        _, output = self.run_stage("intent")
        self.assertIn(
            "7 history directory(ies) not declared, skipped: "
            "gone-0, gone-1, gone-2, gone-3, gone-4 and 2 more",
            output,
        )


class ServerTest(TwoRepositoryStore):
    """The server reads the ticket layer `intent` wrote: a repository it names is a member."""

    def _session(self):
        from knowledgestore.server import store

        manifest = {
            "format": {"major": 1, "minor": 4},
            "store": {"id": "removed-repository-store"},
            "layers": {
                "graph": {"present": True, "paths": ["graphify-out/graph.json"]},
                "intent": {
                    "present": True,
                    "paths": ["knowledge/intent/file-tickets.json.gz"],
                },
            },
        }
        (self.root / "graphify-out").mkdir(exist_ok=True)
        (self.root / "graphify-out" / "graph.json").write_text(
            json.dumps({"nodes": [], "links": []}), encoding="utf-8"
        )
        (self.root / "knowledge" / "store-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        cache = self.root / "cache"
        opened = []

        def factory():
            loaded = store.open_store(self.root, cache)
            opened.append(loaded)
            return loaded

        self.addCleanup(lambda: [loaded.close() for loaded in opened])
        return store.Session(factory, lambda: None)  # type: ignore[arg-type]

    def test_before_the_drop_the_server_counts_beta_as_in_the_estate(self):
        """Control: the server really does take a ticket-layer repository as a member."""
        from knowledgestore.server import tools_graph

        found = tools_graph.find_repo(self._session(), "beta")
        self.assertEqual(found["answer"], "in the estate")

    def test_after_the_drop_the_server_no_longer_names_the_repository(self):
        """Catches: intent's ticket layer still naming the dropped repository, so the
        server reports an archived repository as a member of the estate."""
        from knowledgestore.server import tools_graph

        self.drop_beta()
        self.run_stage("intent")
        found = tools_graph.find_repo(self._session(), "beta")
        self.assertNotEqual(found["answer"], "in the estate")


class DriftTest(SettingsIsolated):
    def test_a_repository_only_in_before_is_deleted_with_all_its_planned_files(self):
        """Catches: a repository discover dropped being reported as "not measured",
        so a delta-driven prune keeps its semantic nodes."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        config.configure(root=str(root))
        estate = Estate(root)
        shas = {}
        for name in ("alpha", "beta"):
            repo = estate.clone(name)
            (repo / "a.yaml").write_text("a: 1\n", encoding="utf-8")
            (repo / "b.yaml").write_text("b: 1\n", encoding="utf-8")
            shas[name] = _commit(repo, "one")
        estate.before = {name: {"sha": sha} for name, sha in shas.items()}
        estate.after = {"alpha": {"sha": shas["alpha"]}}
        estate.plan = [
            "repositories/alpha/a.yaml",
            "repositories/beta/a.yaml",
            "repositories/beta/b.yaml",
        ]
        before, _ = estate.write()
        out = root / "drift.json"
        err = _io.StringIO()
        with contextlib.redirect_stdout(err), contextlib.redirect_stderr(err):
            code = drift.main(["--before", str(before), "--out", str(out)])
        result = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(
            result["deleted"], ["repositories/beta/a.yaml", "repositories/beta/b.yaml"]
        )
        self.assertEqual(result["stale"], result["deleted"])
        self.assertIn("recorded at --before are gone from --after", err.getvalue())
        self.assertIn("beta", err.getvalue())
        self.assertNotIn("not measured", err.getvalue())

    def test_a_repository_only_in_after_lists_its_tracked_files_under_new(self):
        """Catches: a repository that joined the estate being reported as "not
        measured", so none of its files reach the delta; the clone's
        `.graphifyignore` being disregarded, extracting vendored code a full build
        would skip; or an extension the plan does not hold being admitted."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        config.configure(root=str(root))
        estate = Estate(root)
        alpha = estate.clone("alpha")
        (alpha / "a.yaml").write_text("a: 1\n", encoding="utf-8")
        sha = _commit(alpha, "one")
        gamma = estate.clone("gamma")
        (gamma / "vendor").mkdir()
        for name in ("a.yaml", "docs a é.yaml", "vendor/c.yaml", "notes.txt"):
            (gamma / name).write_text("x: 1\n", encoding="utf-8")
        joined = _commit(gamma, "one")
        # Written by `apply-ignores` into the clone, never committed.
        (gamma / ".graphifyignore").write_text("vendor/\n", encoding="utf-8")
        estate.before = {"alpha": {"sha": sha}}
        estate.after = {"alpha": {"sha": sha}, "gamma": {"sha": joined}}
        estate.plan = ["repositories/alpha/a.yaml"]
        before, _ = estate.write()
        out = root / "drift.json"
        text = _io.StringIO()
        with contextlib.redirect_stdout(text), contextlib.redirect_stderr(text):
            code = drift.main(["--before", str(before), "--out", str(out)])
        result = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(code, 0, text.getvalue())
        self.assertEqual(
            result["new"], ["repositories/gamma/a.yaml", "repositories/gamma/docs a é.yaml"]
        )
        self.assertEqual(result["stale"], result["new"])
        self.assertIn("new to --after", text.getvalue())
        self.assertNotIn("not measured", text.getvalue())


if __name__ == "__main__":
    unittest.main()
