"""What the shipped documents claim about flags and defaults, held against the code.

Every other documentation gate here compares names: `test_documented_stages.py`
holds each `knowledgestore <stage>` to be a stage, and says nothing about what
follows it. A document can write `--prune` when no stage accepts it, or
`--top 50` when the default is 10, and every gate stays green. This is the gate
that reads meaning rather than names. It covers two claims:

- **Flags.** A `--flag` named in prose or a fenced block must be one the stage it
  is attributed to declares in an `add_argument` call.
- **Defaults.** "`--flag` (default X)", "`--flag`, default X" and "the default
  `--flag X`" must agree with the `default=` of the matching `add_argument`.

Out of scope, deliberately: exit codes (no uniform declaration to read them from),
defaults phrased without naming the flag next to the word "default" ("the default
plans ..."), short options, and a default that is computed rather than written
down - those are listed as "could not compare" rather than passed.

**How a flag is attributed to a stage.** In this order, first rule that applies:

1. *Command line.* The flag sits in a fenced command line (backslash continuations
   joined) or an inline code span, and the last command word before it is
   `knowledgestore <stage>`: that stage. If the command word is another tool
   (`pip`, `git`, `uv`, `graphify` and the like), the flag belongs to that tool and
   is not checked - which is what keeps `--extra-index-url`, `--only-binary` and
   `--depth` out of the report.
2. *Global.* `--root`, `--version` and `--help` are accepted by every invocation.
3. *Declared third-party.* A flag in `docs/third-party-flags.txt`, for the prose
   that names a pip, git or graphify flag with no command beside it. An entry that
   a stage also declares is itself reported, because it would hide a defect.
4. *Paragraph.* The paragraph, list item or table row naming exactly one stage
   (`knowledgestore <stage>` or a backticked stage name) attributes its flags to
   it. Several stages: the flag must belong to at least one of them. A stage named
   and a flag it does not declare is a defect.
5. *Nothing names a stage.* The flag must still be declared by some stage - one no
   stage declares is a defect wherever it appears. One that exists but cannot be
   tied to a stage is listed as **unattributed**: reported in every run, never
   dropped, and never claimed as checked against a stage.

Several stages reuse a flag name (`--strict`, `--sample`), so a default claim is
compared against every candidate stage's default and passes if it matches one.

The gate reports what it read, treats zero as a failure, and proves its own
sensitivity on every run over forged documents and forged source - a document
naming a flag that does not exist, one naming a wrong default, a correct one, and
one naming a third-party flag. The last two are what stop it answering "does this
mention a flag", a neighbouring question with the same answer today.

Run: python3 tests/documented_flags.py
"""

from __future__ import annotations

import ast
import math
import re
import sys
import warnings
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from docs_integrity import documents, fence_state  # noqa: E402

THIRD_PARTY = Path("docs/third-party-flags.txt")
SEPARATOR = " :: "

# Accepted by `knowledgestore` itself, whichever stage follows.
GLOBAL_FLAGS = frozenset({"--root", "--version", "--help"})

FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9]*(?:-[a-z0-9]+)*)")
CODE_SPAN = re.compile(r"`([^`]+)`")
# The last of these before a flag names the command the flag belongs to. A path
# (`graphify-out/`) or a filename (`.git`) is not a command, hence the bounds.
COMMAND = re.compile(
    r"(?<![\w./=-])(?:knowledgestore(?:[ \t]+(?P<stage>[a-z][a-z0-9-]*))?"
    r"|(?P<tool>pip3?|git|uv|graphify|npm|npx|node|python3?|gh|jq|curl|claude))(?![\w.-])"
)
STAGE_INVOCATION = re.compile(r"(?<!from )\bknowledgestore[ \t]+([a-z][a-z0-9-]*)")
# "`--timeout` (default 600 seconds" / "`--floor`, default 10" / "default: 5"
DEFAULT_AFTER = re.compile(
    r"`(?P<flag>--[a-z][a-z0-9-]*)`[\s,(]+default:?\s+`?(?P<value>[^\s`,;)]+)"
)
# "Under the default `--carry exact`"
DEFAULT_BEFORE = re.compile(r"\bdefault\s+`(?P<flag>--[a-z][a-z0-9-]*)\s+(?P<value>[^\s`]+)`")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


class _Unresolved:
    """A default the source states but this gate cannot evaluate."""

    def __repr__(self) -> str:
        return "UNRESOLVED"


UNRESOLVED = _Unresolved()
Default = object  # a constant, None for "declares none", or UNRESOLVED


def declared_arguments(source: str) -> dict[str, list[Default]]:
    """Each `--flag` an `add_argument` call declares, with the defaults it gives.

    A flag reused across sub-parsers lists one default per declaration. A constant
    named in `default=` is followed through module-level assignments; anything
    else is UNRESOLVED rather than guessed. `store_true` declares False.
    """
    with warnings.catch_warnings():
        # A stage's own regexes can carry escapes this gate has no business warning about.
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(source)
    constants: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                constants[target.id] = node.value

    def evaluate(node: ast.expr, depth: int = 0) -> Default:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and depth < 5 and node.id in constants:
            return evaluate(constants[node.id], depth + 1)
        return UNRESOLVED

    found: dict[str, list[Default]] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            continue
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        action = keywords.get("action")
        if "default" in keywords:
            default = evaluate(keywords["default"])
        elif isinstance(action, ast.Constant) and action.value == "store_true":
            default = False
        else:
            default = None
        for argument in node.args:
            if (
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and argument.value.startswith("--")
            ):
                found.setdefault(argument.value, []).append(default)
    return found


def stage_arguments(
    root: Path, stages: Mapping[str, tuple[str, str]]
) -> dict[str, dict[str, list[Default]]]:
    """Stage name to the flags its module declares, read from source under `root`."""
    declared: dict[str, dict[str, list[Default]]] = {}
    for stage, (module, _help) in stages.items():
        path = root / "src" / "knowledgestore" / f"{module}.py"
        declared[stage] = declared_arguments(path.read_text(encoding="utf-8"))
    return declared


def third_party_flags(root: Path) -> dict[str, str]:
    """Flag to the tool that owns it, from the committed declaration."""
    path = root / THIRD_PARTY
    entries: dict[str, str] = {}
    if not path.is_file():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and SEPARATOR in line:
            flag, tool = line.split(SEPARATOR, 1)
            entries[flag.strip()] = tool.strip()
    return entries


@dataclass(frozen=True)
class Unit:
    """One paragraph, list item, table row or fence: the span a stage is named in."""

    line: int
    text: str
    fenced: bool


def units(text: str) -> list[Unit]:
    """Split a document into the units attribution works over.

    Fences come from `docs_integrity.fence_state`, so there is one fence rule
    across the gates rather than a second copy to drift.
    """
    found: list[Unit] = []
    current: list[str] = []
    start = 0
    in_fence = False

    def flush(fenced: bool) -> None:
        if current:
            found.append(Unit(start, "\n".join(current), fenced))
            current.clear()

    for number, line, fenced in fence_state(text):
        if fenced != in_fence:
            flush(in_fence)
            in_fence = fenced
        if fenced:
            if not current:
                start = number
            current.append(line)
            continue
        starts_new = line.startswith("|") or bool(LIST_ITEM.match(line)) or line.startswith("#")
        if not line.strip():
            flush(False)
        elif starts_new:
            flush(False)
            start = number
            current.append(line)
            if line.startswith("|") or line.startswith("#"):
                flush(False)
        else:
            if not current:
                start = number
            current.append(line)
    flush(in_fence)
    return found


def logical_lines(block: str) -> Iterator[str]:
    """A fenced block's command lines, backslash continuations joined."""
    pending = ""
    for line in block.splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        yield pending + line
        pending = ""
    if pending:
        yield pending


def command_before(segment: str, position: int) -> tuple[str, str] | None:
    """The command a flag at `position` belongs to: ("stage", s), ("global", ""),
    ("foreign", tool), ("unknown-stage", word) - or None when no command precedes."""
    last = None
    for match in COMMAND.finditer(segment, 0, position):
        last = match
    if last is None:
        return None
    if last.group("tool"):
        return ("foreign", last.group("tool"))
    stage = last.group("stage")
    if stage is None:
        return ("global", "")
    return ("stage", stage)


def stages_named(text: str, stages: Mapping[str, object]) -> set[str]:
    """The stages a unit names, by invocation or by a backticked stage name."""
    named = {m.group(1) for m in STAGE_INVOCATION.finditer(text) if m.group(1) in stages}
    named |= {m.group(1) for m in CODE_SPAN.finditer(text) if m.group(1) in stages}
    return named


@dataclass
class Findings:
    """What one run read and found. `read` is the count a zero-read refusal uses."""

    documents: int = 0
    mentions: int = 0
    attributed: int = 0
    global_flags: int = 0
    third_party: int = 0
    by_name: int = 0
    unattributed: list[str] = field(default_factory=list)
    defaults_compared: int = 0
    uncomparable: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def absorb(self, other: Findings) -> None:
        self.documents += other.documents
        self.mentions += other.mentions
        self.attributed += other.attributed
        self.global_flags += other.global_flags
        self.third_party += other.third_party
        self.by_name += other.by_name
        self.unattributed += other.unattributed
        self.defaults_compared += other.defaults_compared
        self.uncomparable += other.uncomparable
        self.problems += other.problems


def default_matches(claimed: str, actual: object) -> bool:
    """Whether a documented default states the value the code declares.

    A percentage matches either scale, because the documents write 20% for 0.2.
    """
    claimed = claimed.strip(".:").lower()
    if isinstance(actual, bool):
        return claimed in (("true", "on") if actual else ("false", "off"))
    if isinstance(actual, str):
        return claimed == actual.lower()
    if isinstance(actual, (int, float)):
        percent = claimed.endswith("%")
        try:
            number = float(claimed.rstrip("%"))
        except ValueError:
            return False
        if percent:
            return math.isclose(number, actual * 100) or math.isclose(number, actual)
        return math.isclose(number, actual)
    return False


def read_document(
    text: str,
    name: str,
    declared: Mapping[str, Mapping[str, list[Default]]],
    stages: Mapping[str, object],
    third_party: Mapping[str, str],
) -> Findings:
    """Everything one document claims about flags and defaults, checked."""
    result = Findings(documents=1)
    for unit in units(text):
        named = stages_named(unit.text, stages)
        for flag, command in unit_mentions(unit):
            result.mentions += 1
            _classify(
                result,
                f"{name}:{unit.line}: `{flag}`",
                flag,
                command,
                named,
                declared,
                stages,
                third_party,
            )
        for pattern in (DEFAULT_AFTER, DEFAULT_BEFORE):
            for match in pattern.finditer(unit.text):
                _compare_default(
                    result, f"{name}:{unit.line}", match["flag"], match["value"], named, declared
                )
    return result


def unit_mentions(unit: Unit) -> list[tuple[str, tuple[str, str] | None]]:
    """Each flag in a unit with the command it follows, or None when none precedes it.

    A fence is read as command lines. Prose is read as its code spans, each a
    command context of its own, plus the flags written outside any span.
    """
    if unit.fenced:
        segments = list(logical_lines(unit.text))
        outside = ""
    else:
        segments = [m.group(1) for m in CODE_SPAN.finditer(unit.text)]
        outside = CODE_SPAN.sub(lambda m: " " * len(m.group(0)), unit.text)
    found: list[tuple[str, tuple[str, str] | None]] = []
    for segment in segments:
        for match in FLAG.finditer(segment):
            found.append((match.group(1), command_before(segment, match.start())))
    found += [(m.group(1), None) for m in FLAG.finditer(outside)]
    return found


def _classify(
    result: Findings,
    where: str,
    flag: str,
    command: tuple[str, str] | None,
    named: set[str],
    declared: Mapping[str, Mapping[str, list[Default]]],
    stages: Mapping[str, object],
    third_party: Mapping[str, str],
) -> None:
    """Apply the attribution rules in the module docstring, in order."""
    kind = command[0] if command else ""
    if kind == "foreign":
        result.third_party += 1
    elif flag in GLOBAL_FLAGS:
        result.global_flags += 1
    elif kind == "stage":
        _classify_by_command(result, where, flag, command[1] if command else "", declared, stages)
    elif flag in third_party:
        result.third_party += 1
    elif named:
        if any(flag in declared.get(s, {}) for s in named):
            result.attributed += 1
        else:
            result.problems.append(
                f"{where} is not an argument of {_list(sorted(named))}, the stage(s)"
                " this passage names"
            )
    else:
        owners = sorted(s for s in declared if flag in declared[s])
        if not owners:
            result.problems.append(
                f"{where} is not an argument of any stage, and is not declared third-party"
                f" in {THIRD_PARTY}"
            )
        elif len(owners) == 1:
            # Exists, and exactly one stage could mean it. Counted, not listed: the flag
            # is real but nothing here names its stage, so no stage-specific claim has
            # been checked.
            result.by_name += 1
        else:
            result.unattributed.append(f"{where} names no stage here (declared by {_list(owners)})")


def _classify_by_command(
    result: Findings,
    where: str,
    flag: str,
    stage: str,
    declared: Mapping[str, Mapping[str, list[Default]]],
    stages: Mapping[str, object],
) -> None:
    if stage not in stages:
        result.unattributed.append(
            f"{where} follows `knowledgestore {stage}`, which is not a stage"
        )
    elif flag in declared.get(stage, {}):
        result.attributed += 1
    else:
        result.problems.append(
            f"{where} is not an argument of `{stage}` ({_declares(declared.get(stage, {}))})"
        )


def _list(items: list[str]) -> str:
    return ", ".join(f"`{item}`" for item in items) or "none"


def _declares(flags: Mapping[str, object]) -> str:
    return f"it declares {_list(sorted(flags))}" if flags else "it declares no flags"


def _compare_default(
    result: Findings,
    where: str,
    flag: str,
    claimed: str,
    named: set[str],
    declared: Mapping[str, Mapping[str, list[Default]]],
) -> None:
    candidates = [s for s in sorted(named) if flag in declared.get(s, {})] or [
        s for s in sorted(declared) if flag in declared[s]
    ]
    actual = [d for s in candidates for d in declared[s][flag]]
    claim = f"{where}: `{flag}` documented default {claimed}"
    if not candidates:
        return  # an unknown flag is reported by the flag check, once
    comparable = [d for d in actual if d is not None and d is not UNRESOLVED]
    if not comparable:
        result.uncomparable.append(
            f"{claim}, but {_list(candidates)} declares no literal default to compare"
        )
        return
    result.defaults_compared += 1
    if not any(default_matches(claimed, d) for d in comparable):
        shown = ", ".join(repr(d) for d in comparable)
        result.problems.append(f"{claim}; {_list(candidates)} declares {shown}")


def third_party_problems(
    root: Path, declared: Mapping[str, Mapping[str, list[Default]]], texts: Mapping[str, str]
) -> list[str]:
    """The declaration's own consistency: no entry a stage declares, none unused.

    An entry a stage also declares would silence a real defect for that flag, and
    an entry no document uses is a list that has stopped being read.
    """
    problems: list[str] = []
    everywhere = {flag for flags in declared.values() for flag in flags}
    joined = "\n".join(texts.values())
    for flag, tool in sorted(third_party_flags(root).items()):
        if flag in everywhere:
            problems.append(
                f"{THIRD_PARTY}: `{flag}` ({tool}) is also declared by a stage, so listing it"
                " as third-party would hide a wrong use of it"
            )
        if flag not in joined:
            problems.append(f"{THIRD_PARTY}: `{flag}` ({tool}) is named by no shipped document")
    return problems


def read_tree(root: Path, stages: Mapping[str, tuple[str, str]]) -> Findings:
    declared = stage_arguments(root, stages)
    third_party = third_party_flags(root)
    total = Findings()
    texts: dict[str, str] = {}
    for path in documents(root):
        relative = path.relative_to(root)
        if "superpowers" in relative.parts:
            continue
        text = path.read_text(encoding="utf-8")
        texts[str(relative)] = text
        total.absorb(read_document(text, str(relative), declared, stages, third_party))
    total.problems += third_party_problems(root, declared, texts)
    return total


# --- sensitivity -------------------------------------------------------------

_FORGED_STAGES = {"sync": ("sync_module", "x"), "check": ("check_module", "x")}
_FORGED_MODULES = {
    "sync": (
        "DEFAULT_TIMEOUT = 600\n"
        "parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)\n"
        "parser.add_argument('--strict', action='store_true')\n"
    ),
    "check": "parser.add_argument('--strict', action='store_true')\n",
}


def _forged_read(document: str, third_party: Mapping[str, str] | None = None) -> Findings:
    declared = {s: declared_arguments(src) for s, src in _FORGED_MODULES.items()}
    return read_document(document, "forged.md", declared, _FORGED_STAGES, third_party or {})


def sensitivity() -> list[str]:
    """Failures of the gate's own discrimination, over forged text and forged source.

    Four documents, each of which the gate must treat differently, so it cannot be
    answering "does this mention a flag". Covered by `TheGateCanStillTell` in
    `test_documented_flags.py`, which drives the same cases one at a time.
    """
    failures: list[str] = []
    missing = _forged_read("```bash\nknowledgestore sync --prune\n```\n")
    if not any("--prune" in p for p in missing.problems):
        failures.append("a flag no stage declares was not reported")
    wrong = _forged_read("Run `knowledgestore sync`; `--timeout` (default 30 seconds) bounds it.")
    if not any("30" in p and "600" in p for p in wrong.problems):
        failures.append("a wrong default was not reported")
    right = _forged_read(
        "Run `knowledgestore sync --timeout 5`; `--timeout` (default 600 seconds) bounds it."
    )
    if right.problems or right.attributed < 1 or right.defaults_compared < 1:
        failures.append("a correct flag and default was reported, or went unread")
    foreign = _forged_read("```bash\npip install --upgrade --extra-index-url URL pkg\n```\n")
    if foreign.problems or foreign.third_party != 2 or foreign.attributed:
        failures.append("a third-party flag was reported, or attributed to a stage")
    loose = _forged_read("Add `--strict` to fail the build.")
    if loose.problems or len(loose.unattributed) != 1:
        failures.append("a flag several stages declare was dropped instead of listed")
    named = _forged_read("Add `--timeout` to bound it.")
    if named.problems or named.by_name != 1 or named.unattributed:
        failures.append("a flag one stage declares was misfiled")
    return failures


def main(root: Path = ROOT) -> int:
    from knowledgestore import cli

    failures = sensitivity()
    if failures:
        for failure in failures:
            print(f"documented-flags: sensitivity check failed: {failure}", file=sys.stderr)
        print("\nThe gate can no longer tell these cases apart, so a clean run means nothing.")
        return 1

    found = read_tree(root, cli.STAGES)
    if found.mentions == 0 or found.documents == 0:
        print(
            "documented-flags: read no flag mentions at all, so it has nothing to report on"
            "\n  - a check that looked at nothing is not a check that passed.",
            file=sys.stderr,
        )
        return 1
    print(
        f"documented-flags: checked {found.mentions} flag mention(s) in {found.documents}"
        f" document(s): {found.attributed} against a stage, {found.global_flags} global,"
        f" {found.third_party} third-party, {found.by_name} real but naming no stage (one"
        f" stage declares each), {len(found.unattributed)} unattributed (several stages declare"
        f" it, listed below);"
        f" {found.defaults_compared} default claim(s) compared"
    )
    for line in found.unattributed:
        print(f"  unattributed: {line}")
    for line in found.uncomparable:
        print(f"  could not compare: {line}")
    if found.problems:
        print(f"\ndocumented-flags: {len(found.problems)} problem(s):\n", file=sys.stderr)
        for problem in found.problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nCorrect the document to what the stage declares. If the flag belongs to pip, git"
            f"\nor graphify and no command sits beside it, declare it in {THIRD_PARTY}.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
