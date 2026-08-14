"""Tests for knowledgestore/build_topic_briefs.py (GraphRAG phase 3)."""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path


from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import build_topic_briefs as briefs  # noqa: E402


def write_topics(tmp: Path, content: str) -> Path:
    path = tmp / "topics.txt"
    path.write_text(content, encoding="utf-8")
    return path


class ReadTopicsTest(SettingsIsolated):
    def test_parses_slug_title_and_keywords(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_topics(
                Path(tmp),
                ("# comment\n\nwelsh-language | Welsh language | welsh, cymraeg , Bilingual\n"),
            )
            topics = briefs.read_topics(path)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0].slug, "welsh-language")
        self.assertEqual(topics[0].title, "Welsh language")
        self.assertEqual(topics[0].keywords, ["welsh", "cymraeg", "bilingual"])

    def test_rejects_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_topics(Path(tmp), "welsh-language | missing keywords\n")
            with self.assertRaises(ValueError):
                briefs.read_topics(path)

    def test_rejects_empty_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_topics(Path(tmp), "# only comments\n")
            with self.assertRaises(ValueError):
                briefs.read_topics(path)


class DossierTest(SettingsIsolated):
    def setUp(self):
        self.topic = briefs.Topic(
            slug="welsh-language",
            title="Welsh language",
            keywords=["welsh", "cymraeg"],
        )
        self.graph = {
            "nodes": [
                {
                    "id": "n1",
                    "label": "WelshTranslationService",
                    "repo": "svc-ui-alpha",
                    "source_file": "src/welsh.ts",
                    "metadata": {"kind": "class"},
                },
                {
                    "id": "n2",
                    "label": "renderer",
                    "repo": "svc-ui-alpha",
                    "source_file": "src/welsh-toggle.ts",
                    "metadata": {"kind": "function"},
                },
                {
                    "id": "n3",
                    "label": "Publish Welsh notice",
                    "repo": "svc-ui-alpha",
                    "source_file": "features/welsh.feature",
                    "metadata": {"kind": "gherkin_feature"},
                },
                {
                    "id": "t1",
                    "label": "DD-100",
                    "repo": "svc-ui-alpha",
                    "source_file": None,
                    "metadata": {"kind": "jira_ticket"},
                },
                {
                    "id": "t2",
                    "label": "DD-200",
                    "repo": "svc-ui-beta",
                    "source_file": None,
                    "metadata": {"kind": "jira_ticket"},
                },
                {
                    "id": "n4",
                    "label": "PaymentService",
                    "repo": "svc-ui-beta",
                    "source_file": "src/pay.ts",
                    "metadata": {"kind": "class"},
                },
            ],
            "links": [
                {"source": "n1", "target": "t1"},  # welsh node -> its ticket
                {"source": "t2", "target": "n4"},  # payment ticket, not welsh
                {"source": "n1", "target": "n2"},
            ],
        }
        self.summaries = {
            "42": "Community around Welsh language toggles and notices.",
            "43": "Community about payments.",
        }
        self.descriptions = {
            "DD-100": {
                "d": ["Add Welsh translation for SJP notices"],
                "first": "2021-01-01",
                "last": "2021-06-01",
                "repos": ["svc-ui-alpha"],
                "n": 4,
            },
            "DD-200": {
                "d": ["Fix payment retries"],
                "first": "2021-01-01",
                "last": "2021-01-02",
                "repos": ["svc-ui-beta"],
                "n": 1,
            },
        }

    def test_dossier_gathers_only_matching_evidence(self):
        dossier = briefs.topic_dossier(self.topic, self.graph, self.summaries, self.descriptions)
        self.assertEqual(list(dossier["nodes_by_repo"]), ["svc-ui-alpha"])
        self.assertEqual(len(dossier["nodes_by_repo"]["svc-ui-alpha"]), 2)
        self.assertEqual(dossier["business_features"], ["Publish Welsh notice"])
        self.assertEqual(dossier["ticket_nodes"], ["DD-100"])
        self.assertEqual([t["ticket"] for t in dossier["described_tickets"]], ["DD-100"])
        self.assertEqual([s["community"] for s in dossier["matched_summaries"]], ["42"])

    def test_source_file_match_counts(self):
        # "welsh" appears only in the source path for the renderer node
        dossier = briefs.topic_dossier(self.topic, self.graph, self.summaries, self.descriptions)
        joined = " ".join(dossier["nodes_by_repo"]["svc-ui-alpha"])
        self.assertIn("welsh-toggle.ts", joined)


class DossierRankingTest(SettingsIsolated):
    """The per-repository cap keeps the STRONGEST matches, not the first.

    The old behaviour kept the first twelve in node-iteration order, so a
    well-connected match listed late in the file was silently dropped in
    favour of leaf nodes listed early - arbitrary evidence for the author.
    """

    def test_high_degree_match_listed_last_survives_the_cap(self):
        from knowledgestore.build_topic_briefs import MAX_NODES_PER_REPO, Topic, topic_dossier

        # cap + 1 matching leaf nodes first, then one hub matching node last
        leaves = [
            {
                "id": f"n{i}",
                "label": f"welsh leaf {i:02d}",
                "repo": "app",
                "source_file": f"src/{i}.ts",
                "metadata": {"kind": "function"},
            }
            for i in range(MAX_NODES_PER_REPO + 1)
        ]
        hub = {
            "id": "hub",
            "label": "welsh hub service",
            "repo": "app",
            "source_file": "src/hub.ts",
            "metadata": {"kind": "class"},
        }
        links = [{"source": "hub", "target": f"n{i}"} for i in range(5)]
        graph = {"nodes": leaves + [hub], "links": links}
        topic = Topic(slug="welsh", title="Welsh", keywords=["welsh"])
        dossier = topic_dossier(topic, graph, {}, {})
        joined = " ".join(dossier["nodes_by_repo"]["app"])
        self.assertIn("welsh hub service", joined, "the best-connected match must survive")
        self.assertEqual(len(dossier["nodes_by_repo"]["app"]), MAX_NODES_PER_REPO)


class MarkdownTest(SettingsIsolated):
    def test_headings_paragraphs_and_inline(self):
        html = briefs.markdown_to_html(
            "# Title\n\nSome **bold** and `code` text\nsame paragraph.\n"
        )
        self.assertIn("<h2>Title</h2>", html)
        self.assertIn("<p>Some <b>bold</b> and <code>code</code> text same paragraph.</p>", html)

    def test_lists_and_tables(self):
        html = briefs.markdown_to_html(
            "- first\n- second\n\n| Repo | Role |\n|---|---|\n| a | b |\n"
        )
        self.assertIn("<ul><li>first</li><li>second</li></ul>", html)
        self.assertIn("<tr><th>Repo</th><th>Role</th></tr>", html)
        self.assertIn("<tr><td>a</td><td>b</td></tr>", html)

    def test_blocks_that_abut_without_a_blank_line(self):
        """Each block consumer returns the line it stopped at, and the next starts
        there. An off-by-one in that handoff swallows or repeats a line, and only
        shows where two blocks touch - which real briefs do constantly."""
        html = briefs.markdown_to_html("- item\n| H |\n|---|\n| c |\n# Heading\npara text\n")
        self.assertIn("<ul><li>item</li></ul>", html)
        self.assertIn("<th>H</th>", html)
        self.assertIn("<h2>Heading</h2>", html)
        self.assertIn("<p>para text</p>", html)
        self.assertNotIn("<li>| H |</li>", html, "the list consumed the table's first row")

    def test_a_paragraph_is_terminated_by_the_next_block(self):
        html = briefs.markdown_to_html("first line\nstill the same\n- a list\n")
        self.assertIn("<p>first line still the same</p>", html)
        self.assertIn("<ul><li>a list</li></ul>", html)

    def test_a_block_running_to_the_end_of_input_terminates(self):
        """Each consumer's while loop is bounded by len(lines); a block with no
        trailing newline or blank line must still close."""
        for markdown, wanted in (
            ("- only item", "<ul><li>only item</li></ul>"),
            ("| H |\n|---|", "<th>H</th>"),
            ("# Just a heading", "<h2>Just a heading</h2>"),
            ("bare paragraph", "<p>bare paragraph</p>"),
        ):
            with self.subTest(markdown=markdown):
                self.assertIn(wanted, briefs.markdown_to_html(markdown))

    def test_empty_and_blank_only_input_render_to_nothing(self):
        for markdown in ("", "\n", "\n\n\n", "   \n  \n"):
            with self.subTest(markdown=repr(markdown)):
                self.assertEqual(briefs.markdown_to_html(markdown), "")

    def test_escapes_html(self):
        html = briefs.markdown_to_html("A <script> tag & more\n")
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_heading_depth_capped(self):
        html = briefs.markdown_to_html("##### Deep\n")
        self.assertIn("<h5>Deep</h5>", html)


class MergeTest(SettingsIsolated):
    def test_merge_validates_and_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            topics_config = write_topics(
                root,
                (
                    "present | Present topic | word\n"
                    "absent | Absent topic | other\n"
                    "short | Short topic | tiny\n"
                ),
            )
            docs = root / "docs"
            docs.mkdir()
            (docs / "present.md").write_text(
                "# Present topic\n\n" + ("Evidence sentence. " * 60), encoding="utf-8"
            )
            (docs / "short.md").write_text("# Too short\n", encoding="utf-8")
            out = root / "briefs.json"

            config.configure(
                TOPICS_CONFIG_PATH=topics_config,
                TOPICS_DOCS_DIR=docs,
                TOPICS_BRIEFS_PATH=out,
            )
            exit_code = briefs.merge()

            self.assertEqual(exit_code, 1)  # two topics missing/short
            import json

            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(list(written), ["present"])
            self.assertIn("<h2>Present topic</h2>", written["present"]["html"])
            self.assertEqual(written["present"]["source"], "docs/topics/present.md")


if __name__ == "__main__":
    unittest.main()


class ProseLinkingTest(SettingsIsolated):
    """Ticket ids and repository names in brief prose become links.

    Briefs and deep dives are the answers people actually read, and every
    identifier in them was unclickable: one estate's Welsh-language brief cited
    16 ticket ids and rendered zero anchors. The constrained markdown subset
    excludes link syntax on purpose - an author should not be hand-writing URLs -
    so the linking belongs in the renderer, where the tracker URL and the
    organisation are already configuration.
    """

    def _store(self, stack, repositories="", **settings):
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        (root / "config").mkdir(parents=True)
        (root / "config" / "repositories.txt").write_text(repositories, encoding="utf-8")
        config.configure(root=str(root), **settings)
        return root

    def test_ticket_ids_become_tracker_links(self):
        with contextlib.ExitStack() as stack:
            self._store(stack, TICKET_BROWSE_URL="https://tracker/browse/")
            html = briefs.markdown_to_html("Ticket ABC-890 changed the flow.")
        self.assertIn('<a href="https://tracker/browse/ABC-890"', html)
        self.assertIn(">ABC-890</a>", html)

    def test_ticket_ids_stay_plain_with_no_tracker_configured(self):
        with contextlib.ExitStack() as stack:
            self._store(stack, TICKET_BROWSE_URL="")
            html = briefs.markdown_to_html("Ticket ABC-890 changed the flow.")
        self.assertNotIn("<a href", html)
        self.assertIn("ABC-890", html)

    def test_known_repository_names_link_to_the_organisation(self):
        with contextlib.ExitStack() as stack:
            self._store(stack, repositories="svc-context-sjp\nsvc-ui-hearing\n", GITHUB_ORG="hmcts")
            html = briefs.markdown_to_html("Handled in `svc-context-sjp` today.")
        self.assertIn('<a href="https://github.com/hmcts/svc-context-sjp"', html)

    def test_only_real_repositories_are_linked(self):
        """`app-commons` looks like a repository and is a module inside one.

        Linking anything hyphenated would send readers to 404s, so the renderer
        links only names the estate's own repository list contains.
        """
        with contextlib.ExitStack() as stack:
            self._store(stack, repositories="svc-legacy-portal\n", GITHUB_ORG="hmcts")
            html = briefs.markdown_to_html("The `app-commons` module of `svc-legacy-portal`.")
        self.assertIn('href="https://github.com/hmcts/svc-legacy-portal"', html)
        self.assertNotIn("github.com/hmcts/app-commons", html)

    def test_repository_list_entries_carry_a_git_suffix_and_branch(self):
        """The real file stores `name.git|main`, not a bare name.

        Written because the first implementation split only on "/" and produced
        `svc-context-sjp.git|main`, which matches nothing. Every unit test used
        idealised bare names, so the feature passed its tests and did nothing at
        all on the estate it was built for.
        """
        with contextlib.ExitStack() as stack:
            self._store(
                stack,
                repositories="svc-context-sjp.git|main\nhmcts/svc-ui-hearing.git|develop\n",
                GITHUB_ORG="hmcts",
            )
            html = briefs.markdown_to_html("`svc-context-sjp` and `svc-ui-hearing`.")
        self.assertIn("github.com/hmcts/svc-context-sjp", html)
        self.assertIn("github.com/hmcts/svc-ui-hearing", html)
        self.assertNotIn(".git", html)

    def test_an_identifier_is_never_linked_twice(self):
        """A second pass must not rewrite an href the first pass inserted."""
        with contextlib.ExitStack() as stack:
            self._store(
                stack,
                repositories="svc-context-sjp\n",
                GITHUB_ORG="hmcts",
                TICKET_BROWSE_URL="https://tracker/browse/",
            )
            html = briefs.markdown_to_html("ABC-890 in `svc-context-sjp`.")
        self.assertEqual(html.count("<a href"), 2)
        self.assertNotIn("browse/https", html)
        self.assertNotIn("github.com/hmcts/https", html)

    def test_table_cells_are_linked_too(self):
        """Where it lives and Change history tables are where these live."""
        with contextlib.ExitStack() as stack:
            self._store(
                stack,
                repositories="svc-context-sjp\n",
                GITHUB_ORG="hmcts",
                TICKET_BROWSE_URL="https://tracker/browse/",
            )
            html = briefs.markdown_to_html(
                "| repo | ticket |\n|---|---|\n| `svc-context-sjp` | ABC-890 |\n"
            )
        self.assertIn("github.com/hmcts/svc-context-sjp", html)
        self.assertIn("tracker/browse/ABC-890", html)
