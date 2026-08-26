"""The fan-out dispatch instructions must survive a rewrite of the skill (#131).

The semantic fan-out hands work to a crowd of agents, and three of its properties
are invisible from the code, cost a day each when rediscovered, and live nowhere
but prose:

- **Past roughly 64k output tokens an agent that batched its writes dies with
  everything it produced lost.** One line in the dispatch prompt prevents it. Two
  agents died before it was added; afterwards a session limit killed ten
  simultaneously and cost one chunk each rather than twenty-odd each.
- **The concurrent-agent ceiling rejects rather than queues**, so a chunk can be
  recorded as dispatched and never launched, and nothing will ever produce it.
- **A dispatch log is a cache of intent, not a record of fact.** A coverage gap of
  ninety-odd chunks was announced from a log without intersecting disk, and a
  redundant round of agents was launched for it.

`CLAUDE.md` puts the reason this file exists as: a correction ships the check that
makes it durable. The correction here is prose in the skill an agent executes, and
prose has no gate unless one is written. So this pins the operative content rather
than the wording around it - the section can be rewritten freely and cannot be
gutted.

The last test **runs the command the skill documents**, against a store built here.
A test asserting a copy of the command would pass while the skill said something
else, and the skill is what an agent executes.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "knowledge-store-build" / "SKILL.md"
HEADING = "### Dispatching the semantic fan-out"

# The instruction itself, verbatim. Pinned exactly because it is quoted into a
# dispatch prompt: a paraphrase that drops "IMMEDIATELY" or the second sentence
# leaves an agent free to do what it does by default, which is batch and lose.
WRITE_PER_CHUNK = (
    "Write each chunk to disk IMMEDIATELY after producing it. Do NOT accumulate\n"
    "> results in your context and write at the end."
)

# Each entry is a distinct fact an operator has no other way of learning, and the
# cost of its absence. Dropping any one of them leaves a real failure unexplained.
#
# Matched against the section with whitespace collapsed, deliberately: the section
# must stay free to be rewrapped, and a gate that fails on reflow gets deleted the
# first time someone reformats the file.
REQUIRED_FACTS = {
    "the output limit destroys the batch": (
        "64k output tokens it dies with everything it produced lost"
    ),
    "the ceiling rejects": "rejects rather than queues",
    "a rejected chunk never arrives": "never launched",
    "the two causes are distinct": '"No output on disk" has two causes',
    "capacity from a completed count is optimistic": "systematically optimistic",
    "progress comes from the artefacts": "consults no log",
    "the log is intent, not fact": "cache of intent, not a record of fact",
    "a code-only corpus is unaffected": "code-only corpus never reaches this section",
}


def collapsed(text: str) -> str:
    """One space between words, so the section can be rewrapped freely."""
    return " ".join(text.split())


def section(text: str) -> str:
    """The dispatch section, or "" when the heading is gone.

    Extracted rather than searched for across the document, so a phrase appearing
    elsewhere cannot stand in for the section actually being present.
    """
    if HEADING not in text:
        return ""
    after = text.split(HEADING, 1)[1]
    following = after.find("\n### ")
    return after[:following] if following != -1 else after


def documented_command(text: str) -> str:
    """The `chunk-status` invocation as the skill ships it."""
    match = re.search(r"^knowledgestore chunk-status[^\n]*$", text, re.MULTILINE)
    return match.group(0) if match else ""


class FanoutDispatchGuidanceTest(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text(encoding="utf-8")
        self.section = section(self.text)

    def test_the_section_is_present(self):
        """Breaks if the heading is renamed or removed, which would leave every
        assertion below passing over an empty string."""
        self.assertTrue(self.section, f"no dispatch guidance found in {SKILL.name}")
        self.assertGreater(
            len(self.section.strip().splitlines()),
            20,
            "the section is present but too short to carry the guidance",
        )

    def test_the_write_per_chunk_instruction_is_quotable_and_verbatim(self):
        """Breaks if the instruction is paraphrased or summarised away.

        It is copied into a dispatch prompt, so it has to survive as text an agent
        can be handed. "Write results incrementally" is not the same instruction: the
        failure mode is an agent deciding that the end of its batch counts as
        incremental.
        """
        self.assertIn(WRITE_PER_CHUNK, self.section)
        self.assertIn("> ", self.section, "the instruction is no longer a quotable block")

    def test_every_fact_an_operator_cannot_infer_is_stated(self):
        """Breaks if any one of them is dropped in a rewrite.

        Each is a separate failure that has happened. None is visible from the code,
        and an operator meeting the missing one has no way to diagnose it.
        """
        flat = collapsed(self.section)
        for name, phrase in REQUIRED_FACTS.items():
            with self.subTest(fact=name):
                self.assertIn(phrase, flat)

    def test_the_evidence_for_the_instruction_is_kept_with_it(self):
        """Breaks if the counter-evidence is trimmed as anecdote.

        An instruction with no cost attached is the first thing removed from a
        dispatch prompt that is already long. The ten-agents-one-chunk-each figure is
        what makes it obviously worth its line.
        """
        flat = collapsed(self.section)
        self.assertIn("ten agents", flat)
        self.assertIn("rather than the twenty-odd each had completed", flat)

    def test_the_stage_it_names_exists_in_this_library(self):
        """Breaks if the skill documents a stage the shipped library does not have.

        The skills and the library install separately, so the symptom on a user's
        machine is `unknown stage` at the point they most need the tool.
        """
        sys.path.insert(0, str(ROOT / "src"))
        from knowledgestore import cli

        self.assertIn("chunk-status", cli.STAGES)

    def test_the_command_the_skill_documents_runs_and_reports(self):
        """Breaks if the documented invocation is wrong, or the stage is unwired.

        Run rather than compared, and through the CLI rather than the module, because
        a stage reached only through its own `main()` is the most repeated escape in
        this repository. The store here is built to the state the guidance is about:
        one chunk extracted, one dispatched with nothing back, one never sent.
        """
        command = documented_command(self.section)
        self.assertTrue(command, "the skill no longer documents a chunk-status command")
        self.assertIn("--dispatched", command, "the documented command passes no dispatch log")

        with tempfile.TemporaryDirectory() as raw:
            store = Path(raw)
            (store / "graphify-out").mkdir()
            (store / "graphify-out" / ".graphify_chunk_plan.json").write_text(
                json.dumps(
                    {i: [f"repositories/demo/doc-{i}.md"] for i in ("0001", "0002", "0003")}
                ),
                encoding="utf-8",
            )
            (store / "graphify-out" / ".graphify_chunk_0001.json").write_text(
                json.dumps({"nodes": [{"id": "n1"}], "edges": []}), encoding="utf-8"
            )
            arguments = command.split()[1:]
            for name in [a for a in arguments if a.endswith(".txt")]:
                (store / name).write_text("0001\n0002\n", encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from knowledgestore.cli import main; raise SystemExit(main())",
                    *arguments,
                ],
                cwd=store,
                capture_output=True,
                text=True,
                env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("NEVER SENT  1 planned chunk(s)", completed.stdout)
        self.assertIn("0003", completed.stdout)
        self.assertIn("in flight   1 dispatched", completed.stdout)
        self.assertIn("done        1 of 3", completed.stdout)

    def test_this_gate_notices_a_dropped_fact(self):
        """The sensitivity check, in the same run.

        Removes one required fact from the real section text and asserts the check
        reports exactly that one. If this ever passes, the assertions above are
        measuring the presence of a document rather than its content.
        """
        dropped = REQUIRED_FACTS["the ceiling rejects"]
        forged = collapsed(self.section).replace(dropped, "")
        missing = [name for name, phrase in REQUIRED_FACTS.items() if phrase not in forged]
        self.assertEqual(
            missing,
            ["the ceiling rejects"],
            "removing a fact was not detected, so this gate is vacuous",
        )


if __name__ == "__main__":
    unittest.main()
