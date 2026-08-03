"""The version has one source of truth: the installed distribution metadata.

The distribution version is derived from the git tag at build time
(hatch-vcs), so a hand-maintained copy in source is the defect this guards
against — two of the three copies had already drifted when this test was
written.
"""

from importlib.metadata import version
from settings_isolation import SettingsIsolated  # noqa: E402


class VersionTest(SettingsIsolated):
    def test_dunder_version_is_the_distribution_version(self):
        import knowledgestore

        self.assertEqual(knowledgestore.__version__, version("hmcts-knowledge-store-builder"))
