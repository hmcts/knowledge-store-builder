"""Every remedy a stage prints must name something an operator can actually run.

The break this catches: a refusal telling an operator to run a step that does not
exist. It shipped. Three stages read `config.DETECT_PATH`, and all three told the
reader to run graphify's "detect step", "detect pass" or "detection" first —
graphify's CLI has no `detect` subcommand, `graphify update` at the store root
exits 0 without writing the file, and no document named the call that does write
it (#236). A refusal whose remedy cannot be performed is worse than a warning: it
sends a reader looking for a command, and the search ends in nothing.

Two halves, because they fail differently:

- **The sweep** reads every operator-facing message in `src/knowledgestore`, so a
  new message repeating the mistake is caught wherever it is written. It reads
  literals, so it also resolves a message assembled from a module-level constant —
  otherwise moving the text into a constant would make the sweep silently blind.
- **The driven stages** run the three real refusals and check the message an
  operator is actually shown, which is the artefact that can be wrong. A literal
  in the source is not what reaches a terminal; the composed string is.

A remedy resolves when it names a `knowledgestore` stage in `cli.STAGES`, a real
`graphify` subcommand, a flag on the command in hand, the command in hand itself
("run it again"), or a self-contained interpreter call shown in full. Anything
else is a step name, and a step name has to exist.

Both halves assert their own sensitivity in the same run: the sweep is re-run over
the wording that shipped, and over a module that hides the same wording behind a
constant, and must report both.
"""

from __future__ import annotations

import ast
import contextlib
import io as _io
import re
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import build_chunk_plan, build_content_set, cli, config, extract_ast

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "knowledgestore"

# The wording that shipped, kept verbatim as the sensitivity fixture: the gate has
# to report this, or it is decoration.
SHIPPED_WORDING = (
    "No detection results at /s/graphify-out/.graphify_detect.json. graphify writes "
    "this when it scans the corpus, so run its detect step first - a plan invented "
    "without it would name files nobody has confirmed are there."
)

# "run X" where X is what the operator is told to run: a backticked command, or the
# next word or two. Only the imperative sense counts, so the match must open a
# sentence or follow a clause break - "a run with broader permissions" and "the run
# recorded in <file>" are nouns, and a gate that read them as commands would force
# unrelated messages to be reworded.
IMPERATIVE = re.compile(
    r"(?:^|(?<=[.;:)-])\s|(?<=\bso)\s|(?<=\bthen)\s|(?<=\band)\s|(?<=\bor)\s)"
    r"(?:re-)?run\s+(?:its\s+|the\s+|graphify's\s+)?(`[^`]*`|[^.,;\n]{0,45})",
    re.I,
)

# "run it again", "re-run this stage": the command in hand, which exists by the
# fact that the operator just ran it.
POINTERS = frozenset({"this", "that", "it", "again", "them", "one"})


@dataclass(frozen=True)
class Message:
    """One string an operator is shown, and where it is written."""

    module: str
    line: int
    text: str


def _flatten(node: ast.AST, constants: dict[str, str]) -> str | None:
    """The literal text of a message expression, or None if it is not a string.

    `constants` maps `NAME` and `module.NAME` to the text of module-level string
    constants, so a message assembled from one is read rather than skipped. Without
    that, moving wording into a constant would take it out of this gate's sight -
    which is exactly what the fix for #236 does.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(_flatten(value, constants) or "{}" for value in node.values)
    if isinstance(node, ast.FormattedValue):
        # An interpolated value: the shared remedy reaches its messages this way,
        # and a path or a count resolves to nothing and reads as "{}".
        return _flatten(node.value, constants)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _flatten(node.left, constants), _flatten(node.right, constants)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return constants.get(f"{node.value.id}.{node.attr}")
    return None


def module_constants() -> dict[str, str]:
    """Module-level string constants across the package, keyed both ways.

    Keyed by bare `NAME` for a reference inside its own module and by
    `module.NAME` for one from another, which is how a stage reads a shared
    remedy.
    """
    found: dict[str, str] = {}
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            text = _flatten(node.value, {})
            if text is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    found[target.id] = text
                    found[f"{path.stem}.{target.id}"] = text
    return found


def operator_messages(path: Path, constants: dict[str, str]) -> list[Message]:
    """Everything printed or raised at an operator from one module.

    Docstrings and comments are excluded deliberately: they are for whoever
    changes the code, and they use "run" as a noun freely.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    messages: list[Message] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if name != "print" and not name.endswith("Refused") and not name.endswith("Error"):
            continue
        for argument in node.args:
            text = _flatten(argument, constants)
            if text:
                messages.append(Message(path.name, node.lineno, " ".join(text.split())))
    return messages


def graphify_subcommands() -> frozenset[str]:
    """graphify's real subcommands, or empty when the peer CLI is not installed.

    graphify is an optional extra, and CI runs the suite once without it. Empty
    means a `graphify <name>` remedy cannot be checked here; the run that installs
    the extra checks it.
    """
    if not shutil.which("graphify"):
        return frozenset()
    try:
        help_text = subprocess.run(
            ["graphify", "--help"], capture_output=True, text=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - peer CLI absent
        return frozenset()
    return frozenset(
        match.group(1) for match in re.finditer(r"^  ([a-z][a-z-]*)\s", help_text, re.M)
    )


def named_command(phrase: str) -> list[str]:
    """The words naming the command, from a backticked span or the bare phrase."""
    if phrase.startswith("`"):
        return phrase.strip("`").split()
    return phrase.split()[:2]


def unresolved_remedies(message: str, subcommands: frozenset[str] = frozenset()) -> list[str]:
    """Every remedy in one message that names nothing runnable."""
    problems: list[str] = []
    for match in IMPERATIVE.finditer(message):
        words = named_command(match.group(1))
        if not words:
            problems.append("tells an operator to run nothing at all")
            continue
        first = words[0].strip("`'\",")
        second = words[1].strip("`'\",") if len(words) > 1 else ""
        if first == "knowledgestore":
            if second and second not in cli.STAGES:
                problems.append(f"names `knowledgestore {second}`, which is not a stage")
        elif first in cli.STAGES:
            continue
        elif first == "graphify":
            if subcommands and second not in subcommands:
                problems.append(f"names `graphify {second}`, which is not a subcommand")
        elif first.startswith("--") or (first == "with" and second.startswith("--")):
            continue
        elif first.lower() in POINTERS:
            continue
        elif first in {"python3", "python"}:
            continue
        else:
            problems.append(f"names {' '.join(words)!r}, which is not a command anything provides")
    return problems


class TheSweepOverEveryOperatorMessage(unittest.TestCase):
    """The gate. Reads the package's messages and resolves every remedy in them."""

    @classmethod
    def setUpClass(cls):
        cls.constants = module_constants()
        cls.subcommands = graphify_subcommands()
        cls.messages = [
            message
            for path in sorted(SRC.glob("*.py"))
            for message in operator_messages(path, cls.constants)
        ]

    def test_no_message_names_a_step_nothing_provides(self):
        """Breaks if a remedy names a step that does not exist.

        The state of the code when this was written: three stages told an operator
        to run graphify's detect step, and there is no such command anywhere.
        """
        findings = [
            f"{message.module}:{message.line}: {problem}"
            for message in self.messages
            for problem in unresolved_remedies(message.text, self.subcommands)
        ]
        self.assertEqual(
            findings,
            [],
            "a printed remedy names a step nobody can run:\n  " + "\n  ".join(findings),
        )

    def test_the_sweep_reads_the_messages_it_claims_to_read(self):
        """Breaks if the extractor goes blind, which would make the gate above pass
        over nothing.

        A gate that can only pass or fail cannot report that it has become
        vacuous, so it says here how much it read and from where: every module
        holding a detect refusal must be represented.
        """
        self.assertGreater(len(self.messages), 100, "the sweep found almost no messages")
        modules = {message.module for message in self.messages}
        for module in ("build_chunk_plan.py", "build_content_set.py", "extract_ast.py"):
            self.assertIn(module, modules, f"the sweep read no message from {module}")

    def test_the_sweep_reports_the_wording_that_shipped(self):
        """The sensitivity check, in the same run: the defect of record, restated."""
        self.assertEqual(
            unresolved_remedies(SHIPPED_WORDING, self.subcommands),
            ["names 'detect step', which is not a command anything provides"],
        )

    def test_the_sweep_sees_through_a_shared_constant(self):
        """The other way this gate could go vacuous, and the likely one.

        The fix moves the remedy into one shared constant. If the extractor could
        not follow a constant reference, every message built from one would read as
        having no remedy at all and the gate would pass by knowing nothing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp) / "forged.py"
            module.write_text(
                'REMEDY = "so run its detect step first"\n'
                'print(f"No detection results at {path}. {REMEDY}.")\n',
                encoding="utf-8",
            )
            constants = {"REMEDY": "so run its detect step first"}
            messages = operator_messages(module, constants)
            self.assertEqual(len(messages), 1, "the forged message was not read")
            self.assertEqual(
                unresolved_remedies(messages[0].text, self.subcommands),
                ["names 'detect step', which is not a command anything provides"],
            )

    def test_a_peer_command_is_checked_against_the_peer_and_not_a_local_list(self):
        """Breaks if the graphify branch stops resolving against the real CLI.

        This is the branch that had no observer while nothing named a graphify
        subcommand, and the one a future remedy is most likely to use. It resolves
        against `graphify --help`, so a second local model of graphify's commands
        cannot drift from it - and `detect`, the command the wrong remedies implied,
        is not in it.
        """
        if not self.subcommands:
            self.skipTest("needs graphify (a peer CLI, not a dependency) to check against")
        self.assertIn("update", self.subcommands)
        self.assertNotIn("detect", self.subcommands)
        self.assertEqual(
            unresolved_remedies("Run `graphify update .` first.", self.subcommands), []
        )
        self.assertEqual(
            unresolved_remedies("Run `graphify detect .` first.", self.subcommands),
            ["names `graphify detect`, which is not a subcommand"],
        )

    def test_a_resolvable_remedy_is_not_reported(self):
        """Breaks if the resolver rejects everything, which would fail the sweep for
        reasons unrelated to the defect and be "fixed" by deleting the gate."""
        for remedy in (
            "Re-run `knowledgestore sync`.",
            "Re-run `summaries snapshot` against a clustered graph.",
            "Nothing was written; run it again once sync has finished.",
            "Run with --estate to check the graph instead.",
            'From the store root, run `python3 -c "import json"` and redirect it.',
        ):
            with self.subTest(remedy=remedy):
                self.assertEqual(unresolved_remedies(remedy, self.subcommands), [])

    def test_a_noun_is_not_read_as_an_imperative(self):
        """Breaks if the extractor widens to every "run" in the language.

        These are real messages in the package. Reading them as commands would
        force unrelated wording to change to satisfy a gate about something else,
        which is how a gate gets removed.
        """
        for sentence in (
            "A run with broader permissions will retry them.",
            "The run recorded in provenance.json is older than the graph.",
            "The run of each compares against it.",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(unresolved_remedies(sentence, self.subcommands), [])


class TheRefusalsAnOperatorActuallySees(SettingsIsolated):
    """The three stages that read the detect result, driven with it absent.

    The message that reaches a terminal is assembled at run time from a shared
    constant, so the source literal is not the artefact that can be wrong. These
    drive the real stages over a real empty store root and read what was printed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        config.configure(root=str(self.root))
        self.assertFalse(config.DETECT_PATH.exists(), "the store root under test is not empty")

    def refusals(self) -> dict[str, tuple[int, str]]:
        """Each stage's exit code and message, from a store with no detect result."""
        captured: dict[str, tuple[int, str]] = {}
        for stage, entry_point in (
            ("content-set", build_content_set.main),
            ("chunk-plan", build_chunk_plan.main),
            ("extract-ast", extract_ast.main),
        ):
            out, err = _io.StringIO(), _io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = entry_point([])
            captured[stage] = (code, " ".join((out.getvalue() + err.getvalue()).split()))
        return captured

    def test_every_stage_refuses_rather_than_writing_something(self):
        """Breaks if a stage starts inventing a content set from a missing input.
        The refusals are the reason the messages matter at all."""
        for stage, (code, text) in self.refusals().items():
            with self.subTest(stage=stage):
                self.assertNotEqual(code, 0, f"{stage} did not refuse: {text}")

    def test_every_refusal_names_the_call_that_produces_the_file(self):
        """Breaks if a refusal drops the producer and goes back to naming a step.

        The three elements are the whole remedy: the callable, the argument that
        makes a store's corpus visible to it, and where the result has to land. A
        message with any one of them missing cannot be acted on.
        """
        for stage, (_code, text) in self.refusals().items():
            for element in ("graphify.detect", "gitignore=False", ".graphify_detect.json"):
                with self.subTest(stage=stage, element=element):
                    self.assertIn(element, text, f"{stage}'s refusal no longer names {element}")

    def test_every_refusal_resolves_under_the_sweep(self):
        """Breaks if a refusal keeps the producer and adds an unrunnable step next
        to it, which the element check above would not notice."""
        subcommands = graphify_subcommands()
        for stage, (_code, text) in self.refusals().items():
            with self.subTest(stage=stage):
                self.assertEqual(unresolved_remedies(text, subcommands), [])


if __name__ == "__main__":
    unittest.main()
