"""Tests for knowledgestore/build_topic_briefs.py (GraphRAG phase 3)."""

from __future__ import annotations

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
                    "repo": "cpp-ui-alpha",
                    "source_file": "src/welsh.ts",
                    "metadata": {"kind": "class"},
                },
                {
                    "id": "n2",
                    "label": "renderer",
                    "repo": "cpp-ui-alpha",
                    "source_file": "src/welsh-toggle.ts",
                    "metadata": {"kind": "function"},
                },
                {
                    "id": "n3",
                    "label": "Publish Welsh notice",
                    "repo": "cpp-ui-alpha",
                    "source_file": "features/welsh.feature",
                    "metadata": {"kind": "gherkin_feature"},
                },
                {
                    "id": "t1",
                    "label": "DD-100",
                    "repo": "cpp-ui-alpha",
                    "source_file": None,
                    "metadata": {"kind": "jira_ticket"},
                },
                {
                    "id": "t2",
                    "label": "DD-200",
                    "repo": "cpp-ui-beta",
                    "source_file": None,
                    "metadata": {"kind": "jira_ticket"},
                },
                {
                    "id": "n4",
                    "label": "PaymentService",
                    "repo": "cpp-ui-beta",
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
                "repos": ["cpp-ui-alpha"],
                "n": 4,
            },
            "DD-200": {
                "d": ["Fix payment retries"],
                "first": "2021-01-01",
                "last": "2021-01-02",
                "repos": ["cpp-ui-beta"],
                "n": 1,
            },
        }

    def test_dossier_gathers_only_matching_evidence(self):
        dossier = briefs.topic_dossier(self.topic, self.graph, self.summaries, self.descriptions)
        self.assertEqual(list(dossier["nodes_by_repo"]), ["cpp-ui-alpha"])
        self.assertEqual(len(dossier["nodes_by_repo"]["cpp-ui-alpha"]), 2)
        self.assertEqual(dossier["business_features"], ["Publish Welsh notice"])
        self.assertEqual(dossier["ticket_nodes"], ["DD-100"])
        self.assertEqual([t["ticket"] for t in dossier["described_tickets"]], ["DD-100"])
        self.assertEqual([s["community"] for s in dossier["matched_summaries"]], ["42"])

    def test_source_file_match_counts(self):
        # "welsh" appears only in the source path for the renderer node
        dossier = briefs.topic_dossier(self.topic, self.graph, self.summaries, self.descriptions)
        joined = " ".join(dossier["nodes_by_repo"]["cpp-ui-alpha"])
        self.assertIn("welsh-toggle.ts", joined)


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
