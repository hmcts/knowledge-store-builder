"""A community with no label in the labels file should still get a name.

`community_digest` took its label from `.graphify_labels.json` and fell back to
the ordinal `Community 40862`. Anything not clustered by graphify is absent from
that file, so every such community got an ordinal — and a store that adds nodes
through its own extractor hit exactly that: eleven communities, every one
unnamed (issue #110).

It costs twice. The author of a summary has only the top nodes to infer identity
from and nothing to check the inference against; and the reader is shown
`Community 297`, which is not a handle anyone can hold.

The label is derived from evidence already in the digest — the dominant
repository and the highest-degree node — so it stays checkable against the graph
rather than being invented. The ordinal remains for a community with no labelled
node and no repository, because a wrong name is worse than an honest number.
"""

from __future__ import annotations

import unittest

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402


def _nodes():
    """Degree is what `community_digest` sorts by, so it decides the label."""
    return [
        {"id": "a", "label": "MIReportService", "repo": "alpha-portal", "source_file": "a.java"},
        {"id": "b", "label": "ReportRow", "repo": "alpha-portal", "source_file": "b.java"},
        {"id": "c", "label": "Helper", "repo": "other-repo", "source_file": "c.java"},
    ]


DEGREE = {"a": 9, "b": 4, "c": 1}


class DerivedLabelTest(SettingsIsolated):
    def _digest(self, labels, nodes=None):
        return summaries.community_digest(
            42, nodes if nodes is not None else _nodes(), labels, {}, DEGREE
        )

    def test_a_community_absent_from_the_labels_file_is_named_from_evidence(self):
        digest = self._digest({})
        self.assertNotEqual(digest["label"], "Community 42")
        self.assertIn("MIReportService", digest["label"])
        self.assertIn("alpha-portal", digest["label"])

    def test_the_labels_file_still_wins_where_it_has_one(self):
        """Communities graphify did cluster must be unaffected."""
        digest = self._digest({"42": "hearing scheduling"})
        self.assertEqual(digest["label"], "hearing scheduling")

    def test_the_highest_degree_node_names_it(self):
        """Deterministic, and checkable against the graph."""
        digest = self._digest({})
        self.assertIn("MIReportService", digest["label"])
        self.assertNotIn("Helper", digest["label"])

    def test_the_dominant_repository_is_used_not_a_minority_one(self):
        digest = self._digest({})
        self.assertIn("alpha-portal", digest["label"])
        self.assertNotIn("other-repo", digest["label"])

    def test_an_ordinal_remains_when_there_is_no_evidence(self):
        """A wrong name is worse than an honest number."""
        nodes = [{"id": "a", "label": "", "repo": "", "source_file": None}]
        digest = summaries.community_digest(42, nodes, {}, {}, {"a": 1})
        self.assertEqual(digest["label"], "Community 42")

    def test_a_labelless_node_does_not_produce_a_dangling_separator(self):
        """Repository but no labelled node: name it by repository, not 'repo: '."""
        nodes = [{"id": "a", "label": "", "repo": "alpha-portal", "source_file": None}]
        digest = summaries.community_digest(42, nodes, {}, {}, {"a": 1})
        self.assertEqual(digest["label"], "alpha-portal")


if __name__ == "__main__":
    unittest.main()
