"""What the shipped skills tell people to run must exist in the library shipping them.

The skills and the library install separately - the skills through the plugin cache, the
library through pip - so on a user's machine the two versions can differ, and the symptom
is a stage the instructions document reported as `unknown stage`. That is diagnosable now
(`knowledgestore --version`), but the cheaper win is upstream: guarantee that any single
release is internally consistent, so a user whose plugin and library are the same version
never meets the failure at all.

This is the direction that can actually be checked here. Drift is a property of two
installs, which a test in one repository cannot see; internal consistency of one release
is a property of this commit, which it can.

Historical plans under `docs/superpowers/plans/` are excluded deliberately: they record
what was planned at a date, not what to run today, and one of them still names a stage
that was renamed afterwards. Freezing a record of a decision is the point of it.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from knowledgestore import cli

ROOT = Path(__file__).resolve().parent.parent
INVOCATION = re.compile(r"\bknowledgestore\s+([a-z][a-z0-9-]*)")


def shipped_documentation() -> list[Path]:
    """Every file that tells a reader to run something, plans excluded."""
    skills = sorted(ROOT.joinpath("skills").rglob("SKILL.md"))
    docs = [
        p
        for p in sorted(ROOT.joinpath("docs").rglob("*.md"))
        if "superpowers" not in p.relative_to(ROOT).parts
    ]
    return skills + docs


class DocumentedStagesExist(unittest.TestCase):
    def test_the_scan_finds_anything_at_all(self):
        """A scan that silently matches nothing would pass the test below vacuously.

        This is the guard on the instrument rather than on the code: a later change to
        the regex, the glob or the directory layout would otherwise turn this whole file
        into a green check of an empty set.
        """
        files = shipped_documentation()
        self.assertGreater(len(files), 3, "no shipped documentation was found to scan")
        mentioned = {
            m.group(1) for f in files for m in INVOCATION.finditer(f.read_text(encoding="utf-8"))
        }
        self.assertGreater(len(mentioned), 10, f"only {len(mentioned)} invocations found")

    def test_every_documented_stage_is_a_real_stage(self):
        for path in shipped_documentation():
            text = path.read_text(encoding="utf-8")
            for match in INVOCATION.finditer(text):
                stage = match.group(1)
                with self.subTest(file=str(path.relative_to(ROOT)), stage=stage):
                    self.assertIn(
                        stage,
                        cli.STAGES,
                        f"{path.relative_to(ROOT)} documents `knowledgestore {stage}`, "
                        "which is not a stage in this release - a reader following it "
                        "gets `unknown stage`",
                    )


class ThePluginIsIdentifiable(unittest.TestCase):
    """A plugin with no version cannot be told apart from any other copy of itself.

    Which is what made version drift hard to even discuss: asked what they had installed,
    a user could answer for the library (with the flag added alongside this) but had
    nothing to read for the skills.
    """

    def test_the_plugin_manifest_carries_a_version(self):
        manifest = json.loads(
            ROOT.joinpath(".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertIn("version", manifest, "plugin.json carries no version")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")

    def test_the_build_skill_declares_a_library_minimum(self):
        """The skill is the newer artefact when the two drift, so it is the one that has
        to state what it needs. A library cannot describe stages it does not have.
        """
        skill = ROOT.joinpath("skills/knowledge-store-build/SKILL.md").read_text(encoding="utf-8")
        declared = re.search(r"assumes knowledge-store-builder (\d+\.\d+\.\d+) or newer", skill)
        self.assertIsNotNone(declared, "the build skill declares no library minimum")
        assert declared is not None  # for the type checker

        from importlib.metadata import version

        def triple(v: str) -> tuple[int, int, int]:
            parts = re.match(r"(\d+)\.(\d+)\.(\d+)", v)
            assert parts is not None, f"unparseable version {v!r}"
            return (int(parts[1]), int(parts[2]), int(parts[3]))

        installed = version("hmcts-knowledge-store-builder")
        self.assertLessEqual(
            triple(declared.group(1)),
            triple(installed),
            f"the skill demands {declared.group(1)} but this library is {installed}, so "
            "the skill shipped in this release cannot be followed with it",
        )


if __name__ == "__main__":
    unittest.main()
