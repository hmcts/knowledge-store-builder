"""The citation gate, driven with citation shapes as published answers write them.

The store's own version of this gate was written against imagined shapes and broke
on the first real one that carried an argument (#357). Every span below is a shape
a published answer carries, and every expected verdict was derived by hand from the
graph in `GRAPH`, not by running the code under test.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import (
    build_community_summaries,
    check_citations,
    cli,
    config,
    provenance,
    source_urls,
    sync_repositories,
)

FIELD_HOLDER = "src/main/java/uk/example/form/FieldHolder.java"
REPOSITORY = "src/main/java/uk/example/repo/CaseRepository.java"

# Labels as the AST layer writes them: methods carry a leading dot and trailing
# brackets, classes and files are bare.
GRAPH = {
    "nodes": [
        {"id": "a", "label": "FieldHolder", "source_file": FIELD_HOLDER},
        {"id": "b", "label": ".setValue()", "source_file": FIELD_HOLDER},
        {"id": "c", "label": "CaseRepository", "source_file": REPOSITORY},
        {"id": "d", "label": ".findOne()", "source_file": REPOSITORY},
        {"id": "e", "label": "CaseRepository.java", "source_file": REPOSITORY},
        # A structural node: no label and no source file. Must not break the walk.
        {"id": "f"},
    ],
    "links": [],
}

ANSWER = """# Case lookup

The holder is updated with `setValue(null)`, which clears the field, and read back
through `FieldHolder.setValue()`. Lookups go through `CaseRepository.findOne(id)`
and are defined in `repo/CaseRepository.java`.

The optional is unwrapped by `ofNullable(...).ifPresent(...)`. A typo reads
`setValu(null)`. The path `epo/CaseRepository.java` is a suffix of a real path
by characters but not by segments. The store keeps `case_record.created_date`.

Words such as `null` and `retry` are not code.

```java
fakeThing(x);   // inside a fence, never a citation
`alsoFake()`
```
"""


class Bed(SettingsIsolated):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config.configure(root=self.tmp)
        self.answers = self.tmp / "docs" / "topics"
        self.answers.mkdir(parents=True)
        (self.answers / "case-lookup.md").write_text(ANSWER)
        self.graph = self.tmp / "graph.json"
        self.graph.write_text(json.dumps(GRAPH))

    def run_stage(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check_citations.main(list(args))
        return code, out.getvalue(), err.getvalue()


class VerdictsOnRealCitationShapes(Bed):
    def setUp(self) -> None:
        super().setUp()
        self.vocabulary = check_citations.load_vocabulary(self.graph)

    def kind(self, span: str) -> str:
        return check_citations.resolve(span, self.vocabulary).kind

    def test_a_method_cited_with_its_argument_resolves(self):
        """The defect: `rstrip("()")` looked this up as `setvalue(null` and failed a
        method that exists."""
        self.assertEqual(self.kind("setValue(null)"), "resolved")

    def test_a_type_and_method_resolve_as_parts(self):
        self.assertEqual(self.kind("FieldHolder.setValue()"), "resolved")
        self.assertEqual(self.kind("CaseRepository.findOne(id)"), "resolved")

    def test_a_chain_of_calls_the_graph_never_held_does_not_resolve(self):
        """The flip side: `split("(")[0]` left an empty member, and the empty string
        matches everything, so this used to pass."""
        self.assertEqual(self.kind("ofNullable(...).ifPresent(...)"), "unresolved")

    def test_a_partial_path_resolves_on_whole_segments(self):
        self.assertEqual(self.kind("repo/CaseRepository.java"), "resolved")
        self.assertEqual(self.kind("CaseRepository.java"), "resolved")

    def test_a_path_that_is_only_a_character_suffix_does_not_resolve(self):
        self.assertEqual(self.kind("epo/CaseRepository.java"), "unresolved")

    def test_a_path_with_the_wrong_directory_does_not_resolve(self):
        self.assertEqual(self.kind("form/CaseRepository.java"), "unresolved")

    def test_a_dotted_schema_name_is_neither_passed_nor_failed(self):
        self.assertEqual(self.kind("case_record.created_date"), "column")

    def test_a_dotted_name_with_a_call_is_never_a_column(self):
        self.assertEqual(self.kind("case_record.created_date()"), "unresolved")

    def test_a_column_shaped_name_whose_parts_both_resolve_is_an_ordinary_pass(self):
        # `holder.value` cannot be told from a column by shape, so it is decided by
        # the graph first: neither part is a label here, so it is a column ...
        self.assertEqual(self.kind("holder.value"), "column")
        # ... and once both parts are labels it resolves.
        self.vocabulary.add(".holder()", "x/Y.java")
        self.vocabulary.add(".value()", "x/Y.java")
        self.assertEqual(self.kind("holder.value"), "resolved")

    def test_plain_words_are_skipped_not_checked(self):
        self.assertEqual(self.kind("null"), "skipped")
        self.assertEqual(self.kind("retry"), "skipped")
        self.assertEqual(self.kind("GET /api/cases"), "skipped")

    def test_the_nearest_identifier_is_named(self):
        verdict = check_citations.resolve("setValu(null)", self.vocabulary)
        self.assertEqual((verdict.kind, verdict.nearest), ("unresolved", ".setValue()"))

    def test_the_nearest_for_a_wrong_directory_is_the_real_path(self):
        verdict = check_citations.resolve("form/CaseRepository.java", self.vocabulary)
        self.assertEqual(verdict.nearest, REPOSITORY)

    def test_a_citation_nothing_resembles_has_no_nearest(self):
        verdict = check_citations.resolve("ofNullable(...).ifPresent(...)", self.vocabulary)
        self.assertEqual(verdict.nearest, "")


class TheSpansOfAnAnswer(unittest.TestCase):
    def test_fenced_blocks_are_not_read_and_spans_are_not_repeated(self):
        text = "`a1B()` and `a1B()` again\n```\n`inFence()`\n```\nthen `c2D()`\n"
        self.assertEqual(check_citations.citations(text), ["a1B()", "c2D()"])


class TheSupportedName(unittest.TestCase):
    def test_normalise_is_the_function_the_summaries_stage_uses(self):
        """One rule: a second copy is how the two checks come to disagree."""
        self.assertIs(check_citations.normalise, build_community_summaries._normalise)
        self.assertEqual(check_citations.normalise(".setValue()"), "setvalue")

    def test_the_stage_is_registered_and_parses_its_own_arguments(self):
        self.assertIn("check-citations", cli.STAGES)
        self.assertIn("check-citations", cli.SELF_PARSING)


class TheStage(Bed):
    def test_it_fails_and_names_what_is_missing_with_the_nearest(self):
        code, out, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual(code, 1)
        # setValue(null), FieldHolder.setValue(), CaseRepository.findOne(id),
        # repo/CaseRepository.java resolve; three do not.
        self.assertIn("7 citations checked, 4 resolve, 3 do not", out)
        self.assertIn("1 database-column citations reported separately", out)
        self.assertIn("2 spans skipped", out)
        self.assertIn("`ofNullable(...).ifPresent(...)`  no near match", err)
        self.assertIn("`setValu(null)`  nearest: .setValue()", err)
        self.assertIn("`epo/CaseRepository.java`", err)
        self.assertNotIn("fakeThing", err)
        self.assertNotIn("alsoFake", err)

    def test_columns_are_named_and_do_not_fail_the_run(self):
        (self.answers / "case-lookup.md").write_text("Stored in `case_record.created_date`.\n")
        code, out, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("`case_record.created_date`", out)
        self.assertIn("0 citations checked", out)

    def test_a_clean_answer_passes(self):
        (self.answers / "case-lookup.md").write_text("Use `setValue(null)`.\n")
        code, _, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual((code, err), (0, ""))

    def test_generated_evidence_beside_the_answers_is_not_read(self):
        (self.answers / "case-lookup.md").write_text("Use `setValue(null)`.\n")
        (self.answers / "topics-input.json").write_text('{"x": "`inventedThing()`"}')
        code, _, _ = self.run_stage("--graph", str(self.graph))
        self.assertEqual(code, 0)

    def test_the_committed_archive_is_read_when_the_plain_graph_is_absent(self):
        archive = self.tmp / "graphify-out" / "graph.json.gz"
        archive.parent.mkdir(parents=True)
        with gzip.open(archive, "wt") as handle:
            json.dump(GRAPH, handle)
        (self.answers / "case-lookup.md").write_text("Use `setValue(null)`.\n")
        code, out, _ = self.run_stage()
        self.assertEqual(code, 0)
        self.assertIn("graph.json.gz", out)

    def test_no_answers_asserts_nothing_and_does_not_pass(self):
        """Breaks if a run that read no answer exits 0 by default: a check that read
        nothing must not report a pass (CLAUDE.md 2.10)."""
        (self.answers / "case-lookup.md").unlink()
        code, _, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual(code, 2)
        self.assertIn("nothing was asserted", err)

    def test_allow_none_lets_a_store_with_no_answers_pass_on_purpose(self):
        """Breaks if `--allow-none` is ignored, so a store that has published no
        answers yet reads as a failed gate chain - or if it passes silently, without
        the message saying nothing was asserted."""
        (self.answers / "case-lookup.md").unlink()
        code, _, err = self.run_stage("--graph", str(self.graph), "--allow-none")
        self.assertEqual(code, 0)
        self.assertIn("No published answers to check under", err)
        self.assertIn("nothing was asserted", err)

    def test_allow_none_does_not_excuse_an_answer_that_fails(self):
        """Breaks if `--allow-none` turns into a general pass: it excuses having no
        answers, never an answer citing code the graph does not hold."""
        code, _, _ = self.run_stage("--graph", str(self.graph), "--allow-none")
        self.assertEqual(code, 1)

    def test_named_paths_replace_the_default_locations(self):
        other = self.tmp / "other.md"
        other.write_text("Use `inventedThing()`.\n")
        code, _, err = self.run_stage("--graph", str(self.graph), str(other))
        self.assertEqual(code, 1)
        self.assertIn("other.md", err)
        self.assertNotIn("case-lookup.md", err)


# A store whose answers cite source links. The graph, repository list and
# provenance are in the shapes their stages write: nodes carry `repo`,
# `source_file`, `source_location` and the `url` `source-urls` gave them; the
# repository list is `discover`'s `name|clone-url|branch`; provenance is `sync`'s.
BUILD_SHA = "0123456789abcdef0123456789abcdef01234567"
RENAMED_SHA = "fedcba9876543210fedcba9876543210fedcba98"
STALE_SHA = "1111111111111111111111111111111111111111"
CASE_SERVICE = "src/main/java/uk/example/CaseService.java"
ONLY_ELSEWHERE = "src/main/java/uk/example/OnlyElsewhere.java"
BASE = f"https://github.com/example-org/example-repo/blob/{BUILD_SHA}/"


def _node(node_id: str, label: str, repo: str, source_file: str, url: str = "") -> dict:
    node = {
        "id": node_id,
        "label": label,
        "repo": repo,
        "source_file": source_file,
        "source_location": "L12",
    }
    if url:
        node["url"] = url
    return node


LINKED_GRAPH = {
    "nodes": [
        _node("a", "CaseService", "example-repo", CASE_SERVICE, BASE + CASE_SERVICE + "#L12"),
        _node("b", ".findCase()", "example-repo", CASE_SERVICE, BASE + CASE_SERVICE + "#L12"),
        _node("c", "OnlyElsewhere", "other-repo", ONLY_ELSEWHERE),
        # The single-root route: every file of the repository carries its own prefix.
        _node("d", "values.yaml", "flat-repo", "repositories/flat-repo/charts/values.yaml"),
        _node("e", "README.md", "renamed-locally", "README.md"),
        {"id": "f", "label": "uk.example"},
    ],
    "links": [],
}

REPOSITORY_LIST = """\
# Format: name|clone-url|default-branch
example-repo|git@github.com:example-org/example-repo.git|main
other-repo|git@github.com:example-org/other-repo.git|main
flat-repo|git@github.com:example-org/flat-repo.git|main
renamed-locally|https://github.com/example-org/upstream-name.git|master
"""


def _git(repo: Path, *args: str) -> str:
    """Real git, with an identity and no signing, so a commit works on any machine."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Example",
        "GIT_AUTHOR_EMAIL": "example@example.invalid",
        "GIT_COMMITTER_NAME": "Example",
        "GIT_COMMITTER_EMAIL": "example@example.invalid",
    }
    completed = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env=env,
    )
    return completed.stdout.strip()


def _commit(repo: Path, path: str) -> str:
    """Add one file and commit it; the new commit's sha."""
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{path}\n", encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", f"add {path}")
    return _git(repo, "rev-parse", "HEAD")


class SourceLinkBed(Bed):
    def setUp(self) -> None:
        super().setUp()
        self.graph.write_text(json.dumps(LINKED_GRAPH))
        # example-repo's clone is present and holds no commit, so a path the graph
        # does not hold is looked for in real git and not found there.
        self.clone = config.REPOSITORIES_DIR / "example-repo"
        self.clone.mkdir(parents=True)
        _git(self.clone, "init", "-q")
        config.REPOSITORIES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        config.REPOSITORIES_CONFIG.write_text(REPOSITORY_LIST, encoding="utf-8")
        provenance.write(
            {
                name: {"sha": sha, "branch": "main", "committed": "2026-01-02T00:00:00Z"}
                for name, sha in (
                    ("example-repo", BUILD_SHA),
                    ("other-repo", BUILD_SHA),
                    ("flat-repo", BUILD_SHA),
                    ("renamed-locally", RENAMED_SHA),
                )
            }
        )

    def _vocabulary(self) -> check_citations.Vocabulary:
        return check_citations.load_vocabulary(
            self.graph, source_urls.SourceUrls.from_store(), config.REPOSITORIES_DIR
        )


class VerdictsOnSourceLinks(SourceLinkBed):
    def setUp(self) -> None:
        super().setUp()
        self.loaded = self._vocabulary()

    def verdict(self, span: str) -> check_citations.Verdict:
        return check_citations.resolve(span, self.loaded)

    def test_a_link_to_a_real_file_at_the_build_commit_resolves(self):
        """Breaks if a correct link fails: the path compared still encoded, the
        single-root prefix left on the graph's side, or the repository looked up by
        its local name rather than its name on the host."""
        for span in (
            BASE + CASE_SERVICE + "#L12",
            BASE + CASE_SERVICE,
            f"https://github.com/example-org/flat-repo/blob/{BUILD_SHA}/charts/values.yaml#L3",
            f"https://github.com/example-org/upstream-name/blob/{RENAMED_SHA}/README.md",
        ):
            with self.subTest(span=span):
                self.assertEqual(self.verdict(span).kind, "resolved")

    def test_a_link_at_another_commit_fails_and_says_it_is_the_sha(self):
        """Breaks if the sha is not checked: a link pinned to a commit the store never
        read - or to a branch, which drifts - would pass as a citation of this build."""
        for ref in (STALE_SHA, "main", RENAMED_SHA):
            with self.subTest(ref=ref):
                got = self.verdict(
                    f"https://github.com/example-org/example-repo/blob/{ref}/{CASE_SERVICE}#L12"
                )
                self.assertEqual(got.kind, "unresolved")
                self.assertEqual(
                    got.reason, f"{check_citations.DIFFERENT_SHA}: the store read {BUILD_SHA}"
                )

    def test_a_link_to_a_file_its_repository_does_not_hold_fails_and_says_so(self):
        """Breaks if the path is not resolved, or is resolved across the estate: a file
        that exists only in another repository is still a link to nothing."""
        for path in (
            "src/main/java/uk/example/CaseServise.java",
            ONLY_ELSEWHERE,
            "CaseService.java",
        ):
            with self.subTest(path=path):
                got = self.verdict(BASE + path + "#L1")
                self.assertEqual(got.kind, "unresolved")
                self.assertEqual(got.reason, f"{check_citations.UNKNOWN_FILE} in example-repo")
        self.assertEqual(
            self.verdict(BASE + "src/main/java/uk/example/CaseServise.java").nearest, CASE_SERVICE
        )

    def test_an_unpinned_link_reports_the_sha_alone(self):
        """Breaks if a slashed branch (`blob/feature/x/a.py`, read as ref `feature`
        and path `x/a.py`) also reports `unknown file`, sending the reader to fix a
        path that was never the problem."""
        for ref in ("feature/x", "main", "0123abc"):
            with self.subTest(ref=ref):
                got = self.verdict(
                    f"https://github.com/example-org/example-repo/blob/{ref}/src/Invented.java"
                )
                self.assertEqual(
                    got.reason, f"{check_citations.DIFFERENT_SHA}: the store read {BUILD_SHA}"
                )

    def test_a_link_wrong_in_both_ways_names_both(self):
        """Breaks if one reason hides the other, so fixing the one named still fails."""
        got = self.verdict(f"https://github.com/example-org/example-repo/blob/{STALE_SHA}/nope.py")
        self.assertEqual(
            got.reason,
            f"{check_citations.UNKNOWN_FILE} in example-repo; "
            f"{check_citations.DIFFERENT_SHA}: the store read {BUILD_SHA}",
        )

    def test_a_link_to_a_repository_the_store_does_not_build_is_neither_passed_nor_failed(self):
        """Breaks if a link outside the estate fails the gate (a store citing a
        third-party library could never go green) or passes it silently."""
        span = f"https://github.com/another-org/library/blob/{BUILD_SHA}/src/Lib.java"
        self.assertEqual(self.verdict(span).kind, "unchecked")

    def test_without_the_store_files_a_link_is_unchecked_not_passed(self):
        """Breaks if a vocabulary loaded with no repository list or provenance lets a
        link through as resolved: nothing was read that could resolve it."""
        bare = check_citations.load_vocabulary(self.graph)
        self.assertEqual(check_citations.resolve(BASE + CASE_SERVICE, bare).kind, "unchecked")

    def test_any_other_url_is_skipped_as_before(self):
        """Breaks if the parser's scope widens to links the graph cannot answer for."""
        for span in (
            "https://example.com/docs/a.html",
            "https://github.com/example-org/example-repo/tree/main/src",
            "https://github.com/example-org/example-repo/pull/12",
        ):
            with self.subTest(span=span):
                self.assertEqual(self.verdict(span).kind, "skipped")


class TheLinksOfAnAnswer(unittest.TestCase):
    def test_a_link_is_a_citation_however_it_is_written(self):
        """Breaks if only backticked spans are read: answers cite a link as a
        markdown link or bare, and a made-up one there would never be looked at."""
        good = BASE + CASE_SERVICE + "#L12"
        text = (
            f"See [CaseService]({good}) and {BASE}a.py. "
            f"Also <{BASE}b.py>, `{BASE}c.py` and https://example.com/x.\n"
            f"```\n{BASE}fenced.py\n```\n"
        )
        self.assertEqual(
            check_citations.citations(text),
            [good, BASE + "a.py", BASE + "b.py", BASE + "c.py"],
        )

    def test_a_bare_link_ends_where_github_ends_it(self):
        """Breaks if emphasis or a curly apostrophe stays on the link: `**<link>**`
        would cite `a.py**`, a file nobody holds, and fail an honest answer."""
        for written in (
            f"**{BASE}a.py**",
            f"*{BASE}a.py*",
            f"_{BASE}a.py_",
            f"{BASE}a.py~",
            f"{BASE}a.py\u2019s",
            f"{BASE}a.py.",
        ):
            with self.subTest(written=written):
                self.assertEqual(
                    check_citations.citations(f"See {written} here.\n"), [BASE + "a.py"]
                )


class TheStageOnSourceLinks(SourceLinkBed):
    def test_it_fails_on_a_bad_link_and_says_which_of_the_two_it_is(self):
        """Breaks if a made-up link passes the publish gate, or the report does not say
        whether the file or the commit is wrong, which decides what to fix."""
        (self.answers / "case-lookup.md").write_text(
            f"Found in [CaseService]({BASE}{CASE_SERVICE}#L12), which "
            f"`CaseService` defines.\n"
            f"Pinned wrong: https://github.com/example-org/example-repo/blob/{STALE_SHA}/"
            f"{CASE_SERVICE}#L12\n"
            f"Invented: {BASE}src/Invented.java#L4\n"
            f"Elsewhere: https://github.com/another-org/library/blob/main/Lib.java\n"
        )
        code, out, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual(code, 1)
        self.assertIn("4 citations checked, 2 resolve, 2 do not", out)
        self.assertIn("1 source links this store cannot decide", out)
        self.assertIn("https://github.com/another-org/library/blob/main/Lib.java", out)
        self.assertIn(
            f"/{STALE_SHA}/{CASE_SERVICE}#L12`  {check_citations.DIFFERENT_SHA}: "
            f"the store read {BUILD_SHA}",
            err,
        )
        self.assertIn(f"src/Invented.java#L4`  {check_citations.UNKNOWN_FILE} in example-repo", err)

    def test_a_clean_answer_citing_links_passes(self):
        (self.answers / "case-lookup.md").write_text(f"See {BASE}{CASE_SERVICE}#L12.\n")
        code, out, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 citations checked, 1 resolve", out)

    def test_an_unreadable_repository_list_stops_the_gate(self):
        """Breaks if a malformed list is read as an empty estate, which turns every
        link into an unchecked one and lets the gate pass over them."""
        config.REPOSITORIES_CONFIG.write_text("example-repo|only-two-fields\n", encoding="utf-8")
        code, _, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual(code, 2)
        self.assertIn("repositories.txt", err)

    def test_links_with_no_repository_list_stop_the_gate(self):
        """Breaks if a missing list - the stage run from outside the store root - lets
        every link through as "a repository this store does not build", exit 0: a
        silence about a file the gate never read."""
        config.REPOSITORIES_CONFIG.unlink()
        (self.answers / "case-lookup.md").write_text(f"See {BASE}src/Invented.java.\n")
        code, out, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual((code, out), (2, ""))
        self.assertIn(f"No {config.REPOSITORIES_CONFIG}", err)

    def test_no_repository_list_is_fine_when_no_link_is_cited(self):
        """Breaks if the new refusal reaches stores whose answers cite no link, which
        had no reason to keep a repository list beside them."""
        config.REPOSITORIES_CONFIG.unlink()
        (self.answers / "case-lookup.md").write_text("Use `CaseService`.\n")
        code, _, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual((code, err), (0, ""))


class LinksToFilesNoNodeNames(SourceLinkBed):
    """A corpus-search match: a file the repository holds that no graph node names.
    The clone under `repositories/<name>/` answers for it at the build commit."""

    def setUp(self) -> None:
        super().setUp()
        self.build = _commit(self.clone, "docs/RUNBOOK.md")
        # Committed after the build: in the clone's working tree, not at the build sha.
        _commit(self.clone, "docs/LATER.md")
        provenance.write(
            {
                "example-repo": {"sha": self.build, "branch": "main", "committed": ""},
                "other-repo": {"sha": BUILD_SHA, "branch": "main", "committed": ""},
            }
        )
        self.loaded = self._vocabulary()

    def link(self, path: str, sha: str = "", repo: str = "example-repo") -> str:
        return f"https://github.com/example-org/{repo}/blob/{sha or self.build}/{path}#L1"

    def test_a_file_the_clone_holds_at_the_build_commit_resolves(self):
        """Breaks if a link to a real file that no node names fails the gate, which
        is every link the skill tells an agent to build for a corpus-search match."""
        verdict = check_citations.resolve(self.link("docs/RUNBOOK.md"), self.loaded)
        self.assertEqual(verdict.kind, "resolved")

    def test_a_file_the_clone_does_not_hold_at_the_build_commit_fails(self):
        """Breaks if the clone is asked about its working tree or its head rather than
        the build commit - a file added later would pass - or if the clone's answer
        is not taken at all and an invented path passes."""
        for path in ("docs/LATER.md", "docs/NEVER.md"):
            with self.subTest(path=path):
                verdict = check_citations.resolve(self.link(path), self.loaded)
                self.assertEqual(verdict.kind, "unresolved")
                self.assertEqual(verdict.reason, f"{check_citations.UNKNOWN_FILE} in example-repo")

    def test_without_a_clone_a_file_no_node_names_is_unchecked_not_failed(self):
        """Breaks if a store without its corpus - every machine that did not build
        it - fails a link nothing it holds can decide, or passes it silently."""
        verdict = check_citations.resolve(
            self.link("docs/RUNBOOK.md", BUILD_SHA, "other-repo"), self.loaded
        )
        self.assertEqual(verdict.kind, "unchecked")

    def test_without_a_clone_a_wrong_sha_still_fails(self):
        """Breaks if the missing clone hides the claim that can still be decided: the
        sha is checked against provenance, which needs no clone."""
        verdict = check_citations.resolve(
            self.link("docs/RUNBOOK.md", STALE_SHA, "other-repo"), self.loaded
        )
        self.assertEqual(verdict.kind, "unresolved")
        self.assertEqual(
            verdict.reason, f"{check_citations.DIFFERENT_SHA}: the store read {BUILD_SHA}"
        )

    def test_the_stage_reads_the_clones_under_the_store(self):
        """Breaks if the stage does not hand the store's `repositories/` to the
        check, so every corpus-search link is unchecked and an invented one passes."""
        (self.answers / "case-lookup.md").write_text(
            f"See {self.link('docs/RUNBOOK.md')} and {self.link('docs/NEVER.md')}.\n"
        )
        code, out, err = self.run_stage("--graph", str(self.graph))
        self.assertEqual(code, 1)
        self.assertIn("2 citations checked, 1 resolve, 1 do not", out)
        self.assertIn(f"docs/NEVER.md#L1`  {check_citations.UNKNOWN_FILE} in example-repo", err)

    def test_a_link_no_clone_can_decide_is_listed_as_undecided(self):
        """Breaks if the unchecked list describes every entry as outside the estate:
        a corpus-search link with no clone here is inside it, and a reader told
        otherwise checks the wrong thing."""
        (self.answers / "case-lookup.md").write_text(
            f"See {self.link('docs/RUNBOOK.md', BUILD_SHA, 'other-repo')}.\n"
        )
        code, out, _ = self.run_stage("--graph", str(self.graph))
        self.assertEqual(code, 0)
        self.assertIn("1 source links this store cannot decide", out)
        self.assertIn("no clone under repositories/ to look in", out)


class AskingTheClone(LinksToFilesNoNodeNames):
    """What reaches git, read at the seam: the argv of every call, run for real."""

    def _recorded(self) -> tuple[list[list[str]], check_citations.Vocabulary]:
        calls: list[list[str]] = []

        def run(arguments: list[str]) -> str:
            calls.append(list(arguments))
            return sync_repositories.run_git(arguments, quiet=True)

        loaded = check_citations.load_vocabulary(
            self.graph, source_urls.SourceUrls.from_store(), config.REPOSITORIES_DIR, run
        )
        return calls, loaded

    def test_the_question_carries_end_of_options_where_git_honours_it(self):
        """Breaks if the revision is passed without `--end-of-options` on a git that
        honours it, which is every git CI runs (the clone here has a HEAD)."""
        calls, loaded = self._recorded()
        check_citations.resolve(self.link("docs/RUNBOOK.md"), loaded)
        self.assertIn(
            ["--end-of-options", f"{self.build}:docs/RUNBOOK.md"], [c[-2:] for c in calls]
        )

    def test_git_is_never_handed_a_ref_the_link_supplied(self):
        """Breaks if, with no build sha recorded, the link's own ref is asked of git:
        text from a published answer would reach a subprocess's argv for nothing,
        since a link with no build sha to match fails on the sha anyway."""
        provenance.write({"other-repo": {"sha": BUILD_SHA, "branch": "main", "committed": ""}})
        calls, loaded = self._recorded()
        for ref in (self.build, "--help"):
            with self.subTest(ref=ref):
                got = check_citations.resolve(self.link("docs/RUNBOOK.md", ref), loaded)
                self.assertEqual(got.kind, "unresolved")
                self.assertNotIn(check_citations.UNKNOWN_FILE, got.reason)
        self.assertEqual(
            [c for c in calls if any(ref in a for a in c for ref in (self.build, "--help"))], []
        )
        # Nor anything else that does not begin with a full sha: with no build sha,
        # `:<path>` would be asked, which git reads as a lookup in the clone's index.
        self.assertEqual(
            [c[-1] for c in calls if c[-1] != "HEAD" and not re.match(r"[0-9a-f]{40}:", c[-1])],
            [],
        )

    def test_a_path_climbing_with_dot_dot_is_never_asked(self):
        """Breaks if a `..` path reaches git, which prints `fatal: ... outside
        repository` for it; the path names no file in a repository either way."""
        calls, loaded = self._recorded()
        for path in ("../secret.txt", "docs/../docs/RUNBOOK.md"):
            with self.subTest(path=path):
                got = check_citations.resolve(self.link(path), loaded)
                self.assertEqual(got.reason, f"{check_citations.UNKNOWN_FILE} in example-repo")
        self.assertEqual([c for c in calls if any(".." in a for a in c)], [])

    def test_a_git_that_cannot_be_asked_leaves_the_link_unchecked(self):
        """Breaks if a missing git binary, or an argument git cannot be handed, ends
        the gate with a traceback instead of a report."""
        for error in (FileNotFoundError("git"), ValueError("embedded null byte")):
            with self.subTest(error=type(error).__name__):

                def run(arguments: list[str], error: Exception = error) -> str:
                    raise error

                loaded = check_citations.load_vocabulary(
                    self.graph, source_urls.SourceUrls.from_store(), config.REPOSITORIES_DIR, run
                )
                got = check_citations.resolve(self.link("docs/RUNBOOK.md"), loaded)
                self.assertEqual(
                    (got.kind, got.reason), ("unchecked", check_citations.CLONE_UNASKED)
                )

    def test_a_nul_in_a_link_is_not_a_link(self):
        """Breaks if `%00` decodes into a path handed to git, where subprocess raises
        on the NUL and the stage dies without a report."""
        (self.answers / "case-lookup.md").write_text(f"See {self.link('docs/a%00b.md')}.\n")
        code, out, _ = self.run_stage("--graph", str(self.graph))
        self.assertEqual(code, 0)
        self.assertIn("0 citations checked", out)


class TheQuietRunner(unittest.TestCase):
    def test_a_quiet_git_call_prints_nothing_when_git_refuses(self):
        """Breaks if `quiet` stops discarding stderr: git's `fatal:` line for a
        refused question would land in the middle of the gate's report. Read from a
        real child process, because a child's stderr bypasses `sys.stderr`."""
        script = (
            "import subprocess, sys\n"
            "from knowledgestore.sync_repositories import run_git\n"
            "try:\n"
            "    run_git(['rev-parse', '--verify', 'HEAD:../outside'], quiet=True)\n"
            "except subprocess.CalledProcessError:\n"
            "    print('refused')\n"
        )
        src = str(Path(__file__).resolve().parent.parent / "src")
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": src},
            check=False,
        )
        self.assertEqual((completed.stdout, completed.stderr), ("refused\n", ""))


class EveryVerdictKindIsNamed(unittest.TestCase):
    def test_kinds_lists_what_resolve_returns(self):
        """Breaks if a kind is dropped from KINDS, which `answers` is held to."""
        self.assertEqual(
            set(check_citations.KINDS),
            {"resolved", "unresolved", "column", "unchecked", "skipped"},
        )


if __name__ == "__main__":
    unittest.main()
