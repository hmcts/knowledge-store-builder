"""What each answer in a session cost, and how it cited. Reads transcripts after the fact.

    knowledgestore answers <transcript or directory>... [--store <root>] [--out answers.jsonl]
    knowledgestore answers --compare <before.jsonl> <after.jsonl>

A store needs to know whether a change made its answers cheaper or better cited, and a
full evaluation is too expensive to run after every change. This stage reads the Claude
Code transcripts of sessions that already happened - no hook, nothing running while
anyone asks - and writes one record per answer, so the same questions run before and
after a change can be compared.

## What an answer is

An answer runs from one prompt to the next. Three kinds of user-role line are not
prompts: a tool's result, which comes back to the session as a user line; a line the
harness injects (`isMeta`, a compaction summary, an `origin` whose kind is not
`human` such as a background agent's completion notice, or text the harness writes in
the person's voice - see `HARNESS_TEXT`); and any line of a subagent's
transcript (`isSidechain`). The answer's text is the last assistant text block before
the next prompt, from the session itself and never from a subagent.

A subagent the answer dispatched is part of what it cost. Its transcript, at
`<session>/subagents/agent-<agentId>.jsonl` and named by the `agentId` on the `Agent`
call's result, is read into the answer that dispatched it - usage, turns, tool calls and
files - and a linked transcript that is missing is counted, so a dispatch whose record
is gone does not read as a cheap answer.

## Counting usage

Usage is counted once per message id with `cost`'s ledger, shared across every file
read - the same code, so the two stages cannot disagree about what one turn is.
`input` is everything the model read, as `cost` defines it (uncached input, cache
writes and cache reads); `cache_read` and `cache_write` break it down. `output_floor`
is the largest `output_tokens` any line of a turn carried, summed over turns: transcript
lines under-report output, so it is a floor, never the output.

A line already read in an earlier file - same `uuid` - is skipped, and a prompt read
before opens no new answer, so a session that carries earlier lines over cannot answer
one question twice. That shape is INFERRED, not observed: `cost` documents copied,
resumed and forked sessions repeating message ids, but no transcript on the machine
this was written on repeated lines across files (a headless `--resume` appended to the
same file). If a fork re-mints uuids, a carried-over prompt opens a second answer with
no usage, since message-id de-duplication still holds its tokens at zero.

No price is applied. `cost_usd` appears only when a headless result
(`claude -p --output-format json`) is among the inputs, names the session, and the
session holds exactly one answer - a result prices the whole run, so it cannot be
divided between several.

## Files and citations

`store_files` lists the files under `--store` that a `Read` opened or that a `Grep`,
`Glob` or `Bash` call named, with each file's size now. A Bash command is split into
shell words, so a path inside an interpreter's quoted script is not counted. A path is
resolved before it is tested, so `<store>/../elsewhere` is outside the store, and a
relative path is read from the line's working directory.

Each citation in the answer - a backticked span, a URL, or a bare `repositories/...`
path - gets one class:

    resolves     check_citations.resolve finds it in the store graph
    pinned_url   a blob URL at a 40-hex commit, so it still says what it said
    corpus_path  a repositories/... path: the sync tree, which no reader can open
    unresolved   code-shaped, and the graph does not hold it
    column       a table.column citation; the graph holds code, not schemas
    skipped      no code shape, or a URL that is not pinned

`absence_claim` is whether a sentence states that something is absent or not found,
with words inside code spans ignored, and `absence_sentence` quotes the first such
sentence so a reader can overrule it. Absence is the claim most likely to be an
unsearched corner rather than a fact.

## Comparing

`--compare` pairs answers by the SHA-256 of the trimmed question, never by similarity:
a fuzzy pairing can match two different questions without saying so. A question asked
more than once in one file is compared as the median of its repetitions, and the
repetitions are listed. A question present in only one file is listed under that file.

Every run ends on its limit: these measure cost and citation hygiene, not correctness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import statistics
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, graph_files, io
from .check_citations import FENCE, SPAN, Vocabulary, citations, load_vocabulary, resolve
from .cost import (
    RESULT_SUFFIXES,
    TRANSCRIPT_SUFFIXES,
    MessageLedger,
    files_under,
    headless_result,
    input_tokens,
)

LIMIT = (
    "These are measures of cost and citation hygiene, not of correctness; "
    "correctness still needs judging."
)

# User lines the harness writes in the person's voice: local command output, the
# interrupt marker (confirmed on a real transcript: text blocks, no origin, no
# isMeta), and `!` shell input and output (prefix only; no real sample was found).
HARNESS_TEXT = (
    "<local-command-",
    "[Request interrupted",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)

TOKEN_FIELDS = ("input", "cache_read", "cache_write", "output_floor")
CLASSES = ("resolves", "pinned_url", "corpus_path", "unresolved", "column", "skipped")
_VERDICT_CLASS = {
    "resolved": "resolves",
    "unresolved": "unresolved",
    "column": "column",
    "skipped": "skipped",
}

_URL = re.compile(r"https?://[^\s<>()\[\]`]+")
_PINNED = re.compile(r"/blob/[0-9a-f]{40}/")
_CORPUS = re.compile(r"(?<![\w/.-])(?:\./)?repositories/[^\s`<>()\[\]]+")
_TRAILING = ".,;:!?'\""
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_ABSENCE = re.compile(
    r"\b(?:"
    r"not\s+(?:found|present|defined|referenced|implemented|configured|mentioned)"
    r"|(?:does|do|did)\s*(?:not|n't)\s+(?:exist|appear|contain|define|reference|mention)"
    r"|there\s+(?:is|are|was|were)\s+no"
    r"|no\s+(?:mention|reference|evidence|trace|record|definition)s?\s+of"
    r"|(?:could|can)\s*(?:not|n't)\s+find|cannot\s+find"
    r"|nowhere|absent"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class Answer:
    """One prompt and everything that followed it until the next."""

    session: str
    index: int
    question: str
    text: str = ""
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    message_ids: list[str] = field(default_factory=list)
    unidentified_output: int = 0
    turns: int = 0
    tool_calls: Counter[str] = field(default_factory=Counter)
    files: set[Path] = field(default_factory=set)
    subagents: int = 0
    subagents_missing: int = 0
    cost_usd: float | None = None


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def is_prompt(record: dict[str, Any]) -> bool:
    """Whether a line is a person's prompt, which opens an answer."""
    message = record.get("message")
    if record.get("type") != "user" or not isinstance(message, dict):
        return False
    if record.get("isSidechain") or record.get("isMeta") or record.get("isCompactSummary"):
        return False
    content = message.get("content")
    if isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    ):
        return False
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") != "human":
        return False
    text = _message_text(content).lstrip()
    return not text.startswith(HARNESS_TEXT)


def question_sha(question: str) -> str:
    return hashlib.sha256(question.strip().encode("utf-8")).hexdigest()


def _named_paths(name: str, given: dict[str, Any]) -> list[str]:
    if name == "Read":
        return [str(given.get("file_path") or "")]
    if name in ("Grep", "Glob"):
        return [str(given.get("path") or "")]
    if name == "Bash":
        command = str(given.get("command") or "")
        # Shell punctuation splits words: `cat a.md; echo` names `a.md`, not `a.md;`.
        # A path inside an interpreter's script (`python -c "open('a.md')"`) is one
        # quoted word and is not read - a known undercount, not a guess.
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            return list(lexer)
        except ValueError:
            return command.split()
    return []


class Reader:
    """Reads sessions into answers, counting every line and turn once across files."""

    def __init__(self, store: Path) -> None:
        self.store = store.resolve()
        self.ledger = MessageLedger()
        self.seen_lines: set[str] = set()
        self.folded: set[Path] = set()

    def first_reading(self, record: dict[str, Any]) -> bool:
        """Whether this line is read here for the first time; marks it read.

        A line already read in an earlier file - same `uuid` - is a copy carried over,
        and was counted where it was first read.
        """
        uuid = record.get("uuid")
        if not isinstance(uuid, str):
            return True
        if uuid in self.seen_lines:
            return False
        self.seen_lines.add(uuid)
        return True

    def session(self, path: Path) -> list[Answer]:
        answers: list[Answer] = []
        current: Answer | None = None
        for record in _lines(path):
            prompt = is_prompt(record)
            if not prompt and current is None:
                continue
            if not self.first_reading(record):
                if prompt:
                    # Carried over from a session already read: it was answered there.
                    current = None
                continue
            if prompt:
                text = _message_text((record.get("message") or {}).get("content"))
                current = Answer(path.stem, len(answers), text)
                answers.append(current)
            elif current is not None:
                self.fold(record, current, path)
        return answers

    def fold(self, record: dict[str, Any], answer: Answer, session: Path) -> None:
        """Count one line into the answer it belongs to."""
        message = record.get("message")
        if not isinstance(message, dict):
            return
        if message.get("role") == "assistant":
            self._assistant(record, message, answer)
            return
        outcome = record.get("toolUseResult")
        agent = outcome.get("agentId") if isinstance(outcome, dict) else None
        if isinstance(agent, str) and agent:
            transcript = session.parent / session.stem / "subagents" / f"agent-{agent}.jsonl"
            self._subagent(transcript, answer, session)

    def _assistant(self, record: dict[str, Any], message: dict[str, Any], answer: Answer) -> None:
        usage = message.get("usage")
        if isinstance(usage, dict) and self.ledger.sight(message):
            _count_turn(message, usage, answer)
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and not record.get("isSidechain"):
                answer.text = str(block.get("text") or "")
            elif block.get("type") == "tool_use":
                self._tool_use(block, record, answer)

    def _tool_use(self, block: dict[str, Any], record: dict[str, Any], answer: Answer) -> None:
        name = str(block.get("name") or "")
        answer.tool_calls[name] += 1
        given = block.get("input")
        if isinstance(given, dict):
            self._files(name, given, str(record.get("cwd") or ""), answer)

    def _files(self, name: str, given: dict[str, Any], cwd: str, answer: Answer) -> None:
        base = Path(cwd) if cwd else self.store
        for named in _named_paths(name, given):
            if not named or "\0" in named:
                continue
            try:
                # Only `~` and `~/...`: `~name` expands another user's home, and
                # raises when there is no such user.
                word = Path(named).expanduser() if named == "~" or named.startswith("~/") else named
                candidate = (base / word).resolve()
                if candidate.is_relative_to(self.store) and candidate.is_file():
                    answer.files.add(candidate)
            except (OSError, ValueError):
                continue

    def _subagent(self, path: Path, answer: Answer, session: Path) -> None:
        if not path.is_file():
            answer.subagents_missing += 1
            return
        resolved = path.resolve()
        if resolved in self.folded:
            return
        self.folded.add(resolved)
        answer.subagents += 1
        for record in _lines(path):
            if self.first_reading(record):
                self.fold(record, answer, session)


def _count_turn(message: dict[str, Any], usage: dict[str, Any], answer: Answer) -> None:
    """Add one turn's usage to its answer, the first time its message is sighted."""
    answer.turns += 1
    answer.input += input_tokens(usage)
    answer.cache_read += int(usage.get("cache_read_input_tokens") or 0)
    answer.cache_write += int(usage.get("cache_creation_input_tokens") or 0)
    message_id = message.get("id")
    if isinstance(message_id, str):
        answer.message_ids.append(message_id)
    else:
        answer.unidentified_output += int(usage.get("output_tokens") or 0)


def classify(span: str, vocabulary: Vocabulary) -> str:
    """One citation's class. See the module docstring for what each means."""
    text = span.strip()
    if _URL.fullmatch(text):
        return "pinned_url" if _PINNED.search(text) else "skipped"
    if text.removeprefix("./").startswith("repositories/"):
        return "corpus_path"
    return _VERDICT_CLASS[resolve(text, vocabulary).kind]


def cited(text: str) -> list[str]:
    """Backticked spans, then URLs and bare corpus paths outside them, de-duplicated."""
    found: dict[str, None] = dict.fromkeys(citations(text))
    prose = SPAN.sub(" ", FENCE.sub("", text))
    for pattern in (_URL, _CORPUS):
        for match in pattern.finditer(prose):
            found.setdefault(match.group(0).rstrip(_TRAILING), None)
    return list(found)


def absence(text: str) -> str | None:
    """The first sentence stating that something is absent, ignoring code spans."""
    prose = FENCE.sub("", text)
    # Mask spans before splitting: a span holding ". " would otherwise be cut in two,
    # and neither half is a span any more. Same length, so offsets carry across.
    masked = SPAN.sub(lambda match: " " * len(match.group(0)), prose)
    edges = [0]
    for gap in _SENTENCE.finditer(masked):
        edges += [gap.start(), gap.end()]
    edges.append(len(masked))
    for start, end in zip(edges[::2], edges[1::2]):
        if _ABSENCE.search(masked[start:end]):
            return prose[start:end].strip()
    return None


@dataclass
class Run:
    answers: list[Answer] = field(default_factory=list)
    vocabulary: Vocabulary = field(default_factory=Vocabulary)
    sessions: int = 0
    results: int = 0
    results_unmatched: int = 0
    store: Path = Path()
    ledger: MessageLedger = field(default_factory=MessageLedger)

    def records(self) -> list[dict[str, Any]]:
        return [record(answer, self) for answer in self.answers]


def record(answer: Answer, run: Run) -> dict[str, Any]:
    largest = run.ledger.largest_output
    output_floor = sum(largest[m] for m in answer.message_ids) + answer.unidentified_output
    files = sorted(
        (path.relative_to(run.store).as_posix(), path.stat().st_size)
        for path in answer.files
        if path.is_file()
    )
    sentence = absence(answer.text)
    row: dict[str, Any] = {
        "session": answer.session,
        "index": answer.index,
        "question": answer.question,
        "question_sha": question_sha(answer.question),
        "input": answer.input,
        "cache_read": answer.cache_read,
        "cache_write": answer.cache_write,
        "output_floor": output_floor,
    }
    if answer.cost_usd is not None:
        row["cost_usd"] = answer.cost_usd
    row.update(
        {
            "turns": answer.turns,
            "tool_calls": dict(sorted(answer.tool_calls.items())),
            "store_files": [{"path": path, "bytes": size} for path, size in files],
            "citations": [
                {"span": span, "class": classify(span, run.vocabulary)}
                for span in cited(answer.text)
            ],
            "absence_claim": sentence is not None,
            "absence_sentence": sentence,
            "subagents": answer.subagents,
            "subagents_missing": answer.subagents_missing,
        }
    )
    return row


def measure(paths: Iterable[Path], store: Path, vocabulary: Vocabulary) -> Run:
    """Every answer in the sessions named or under a named directory."""
    reader = Reader(store)
    run = Run(vocabulary=vocabulary, store=reader.store, ledger=reader.ledger)
    results: dict[str, list[float]] = {}
    for path in files_under(paths):
        if path.suffix in TRANSCRIPT_SUFFIXES:
            found = reader.session(path)
            if found:
                run.sessions += 1
                run.answers.extend(found)
        elif path.suffix in RESULT_SUFFIXES:
            _read_result(path, run, results)
    _price(run, results)
    return run


def _read_result(path: Path, run: Run, results: dict[str, list[float]]) -> None:
    """Keep a headless result's cost under the session it names."""
    result = headless_result(path)
    if result is not None:
        run.results += 1
        session = str(result.get("session_id") or "")
        results.setdefault(session, []).append(float(result.get("total_cost_usd") or 0.0))


def _price(run: Run, results: dict[str, list[float]]) -> None:
    """Put each result's cost on its session's answer, where there is exactly one."""
    by_session: dict[str, list[Answer]] = {}
    for answer in run.answers:
        by_session.setdefault(answer.session, []).append(answer)
    for session, dollars in results.items():
        owned = by_session.get(session, [])
        if len(owned) == 1 and len(dollars) == 1:
            owned[0].cost_usd = dollars[0]
        else:
            run.results_unmatched += len(dollars)


def _number(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def _signed(value: float) -> str:
    return ("+" if value > 0 else "") + _number(value)


def _quantity(row: dict[str, Any], name: str) -> float | None:
    if name == "tool_calls":
        return float(sum((row.get("tool_calls") or {}).values()))
    if name == "store_files":
        return float(len(row.get("store_files") or []))
    value = row.get(name)
    return float(value) if isinstance(value, (int, float)) else None


QUANTITIES = (*TOKEN_FIELDS, "turns", "tool_calls", "store_files", "cost_usd")


def _shares(rows: Iterable[dict[str, Any]]) -> tuple[int, dict[str, float]]:
    counts = Counter(c["class"] for row in rows for c in row.get("citations") or [])
    total = sum(counts.values())
    return total, {name: (counts[name] / total if total else 0.0) for name in CLASSES}


SUMMARY_NOTES = {
    "input": " tokens (uncached + cache writes + cache reads)",
    "output_floor": " - a floor: transcript lines under-report output",
}


def summarise(run: Run, rows: list[dict[str, Any]]) -> list[str]:
    folded = sum(row["subagents"] for row in rows)
    missing = sum(row["subagents_missing"] for row in rows)
    head = f"{len(rows):,} answers from {run.sessions:,} sessions; {folded:,} subagent transcripts folded in"
    if missing:
        head += f", {missing:,} linked but missing (those answers read low)"
    lines = [head]
    for name in (*TOKEN_FIELDS, "turns", "tool_calls"):
        values = [_quantity(row, name) or 0.0 for row in rows]
        lines.append(
            f"  {name:<13} median {_number(statistics.median(values))}, "
            f"total {_number(sum(values))}{SUMMARY_NOTES.get(name, '')}"
        )
    distinct = {f["path"]: f["bytes"] for row in rows for f in row["store_files"]}
    opened = [len(row["store_files"]) for row in rows]
    lines.append(
        f"  {'store_files':<13} median {_number(statistics.median(opened))} per answer, "
        f"{len(distinct):,} distinct, {sum(distinct.values()):,} bytes now"
    )
    priced = [row["cost_usd"] for row in rows if "cost_usd" in row]
    if priced:
        lines.append(
            f"  cost          ${sum(priced):,.2f} over {len(priced):,} answers (total_cost_usd)"
        )
    else:
        lines.append(
            "  no cost: transcripts carry no price, and no headless result priced an answer"
        )
    if run.results_unmatched:
        lines.append(
            f"  {run.results_unmatched:,} headless results were not matched to a session "
            "holding exactly one answer, and priced nothing"
        )
    total, shares = _shares(rows)
    lines.append(
        f"Citations: {total:,} - " + ", ".join(f"{name} {shares[name]:.1%}" for name in CLASSES)
    )
    claims = sum(1 for row in rows if row["absence_claim"])
    lines.append(
        f"Absence claims: {claims:,} of {len(rows):,} answers - each sentence is quoted in the records"
    )
    return lines


def load(path: Path) -> list[dict[str, Any]]:
    """The records an earlier run wrote with --out."""
    rows = []
    # Sonar S8707, on the grounds stated in `build_community_summaries.merge`:
    # reading the record file the operator named with --compare is the flag's purpose.
    for line in path.read_text(encoding="utf-8").splitlines():  # NOSONAR(S8707)
        if line.strip():
            rows.append(json.loads(line))
    return rows


@dataclass
class Comparison:
    paired: int = 0
    # quantity -> (median change, total change), over paired questions
    changes: dict[str, tuple[float, float]] = field(default_factory=dict)
    # class -> (share before, share after), over paired questions
    shares: dict[str, tuple[float, float]] = field(default_factory=dict)
    citations: tuple[int, int] = (0, 0)
    repeated: list[tuple[str, int, int]] = field(default_factory=list)
    only_before: list[str] = field(default_factory=list)
    only_after: list[str] = field(default_factory=list)


def _by_question(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["question_sha"], []).append(row)
    return grouped


def compare(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> Comparison:
    """Pair answers by question hash; list what does not pair."""
    old, new = _by_question(before), _by_question(after)
    paired = [sha for sha in old if sha in new]
    comparison = Comparison(paired=len(paired))
    comparison.only_before = [rows[0]["question"] for sha, rows in old.items() if sha not in new]
    comparison.only_after = [rows[0]["question"] for sha, rows in new.items() if sha not in old]
    for sha in [*old, *(s for s in new if s not in old)]:
        counts = (len(old.get(sha, [])), len(new.get(sha, [])))
        if max(counts) > 1:
            question = (old.get(sha) or new[sha])[0]["question"]
            comparison.repeated.append((question, *counts))
    for name in QUANTITIES:
        deltas = []
        for sha in paired:
            was = [v for v in (_quantity(r, name) for r in old[sha]) if v is not None]
            now = [v for v in (_quantity(r, name) for r in new[sha]) if v is not None]
            if was and now:
                deltas.append(statistics.median(now) - statistics.median(was))
        if deltas:
            comparison.changes[name] = (statistics.median(deltas), sum(deltas))
    was_total, was_shares = _shares(r for sha in paired for r in old[sha])
    now_total, now_shares = _shares(r for sha in paired for r in new[sha])
    comparison.citations = (was_total, now_total)
    comparison.shares = {name: (was_shares[name], now_shares[name]) for name in CLASSES}
    return comparison


def report_comparison(comparison: Comparison, before: Path, after: Path) -> list[str]:
    lines = [f"{comparison.paired:,} questions in both {before.name} and {after.name}"]
    for name, (median, total) in comparison.changes.items():
        lines.append(f"  {name:<13} median change {_signed(median)}, total change {_signed(total)}")
    was, now = comparison.citations
    lines.append(f"Citation shares over the paired questions ({was:,} citations -> {now:,}):")
    for name, (old, new) in comparison.shares.items():
        lines.append(f"  {name:<13} {old:.1%} -> {new:.1%} ({(new - old) * 100:+.1f} points)")
    if comparison.repeated:
        lines.append("Asked more than once, compared as the median of the repetitions:")
        lines += [f"  {b}x before, {a}x after: {q}" for q, b, a in comparison.repeated]
    for label, questions in (
        (f"Only in {before.name}", comparison.only_before),
        (f"Only in {after.name}", comparison.only_after),
    ):
        lines.append(f"{label}: {len(questions):,}")
        lines += [f"  {q}" for q in questions]
    return lines


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore answers",
        description="Report what each answer in a session cost and how it cited, from "
        "Claude Code transcripts; or compare two runs' records by question.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="session transcripts, headless result files, or directories holding them",
    )
    parser.add_argument(
        "--store",
        type=Path,
        help="the store whose files and graph answers are read against (default: KSB_ROOT)",
    )
    parser.add_argument(
        "--out", type=Path, help="write one JSON record per answer here (default: write nothing)"
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        type=Path,
        metavar=("BEFORE", "AFTER"),
        help="compare two --out files by question instead of reading transcripts",
    )
    return parser.parse_args(argv)


def _main_compare(before: Path, after: Path) -> int:
    try:
        old, new = load(before), load(after)
    except (OSError, ValueError) as error:
        print(f"Cannot read the records to compare: {error}", file=sys.stderr)
        return 2
    try:
        lines = report_comparison(compare(old, new), before, after)
    except (KeyError, TypeError, AttributeError) as error:
        print(
            f"Cannot compare {before} with {after}: a record is not one `answers --out` "
            f"wrote (missing or malformed {error}).",
            file=sys.stderr,
        )
        return 2
    for line in lines:
        print(line)
    print(LIMIT)
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    if arguments.compare:
        if arguments.paths:
            print("--compare reads two record files and nothing else.", file=sys.stderr)
            return 2
        return _main_compare(*arguments.compare)
    if not arguments.paths:
        print(
            "Name transcripts or directories to read, or --compare two record files.",
            file=sys.stderr,
        )
        return 2
    store = (arguments.store or config.ROOT).expanduser().resolve()
    graph = graph_files.graph_to_read(store / config.GRAPH_PATH.relative_to(config.ROOT))
    if graph is None or not graph.is_file():
        print(
            f"No graph under {store} - citations are classed against the store graph, "
            "so pass the store with --store (or KSB_ROOT).",
            file=sys.stderr,
        )
        return 2
    run = measure(arguments.paths, store, load_vocabulary(graph))
    if not run.answers:
        print(
            "No session with a prompt was read, so there is nothing to report - a zero "
            "here would be a measurement of nothing.",
            file=sys.stderr,
        )
        return 2
    rows = run.records()
    if arguments.out:
        try:
            io.checked_write_target(arguments.out)
        except ValueError as error:
            print(f"Not writing --out: {error}", file=sys.stderr)
            return 2
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(  # NOSONAR(S2083, S8707) - validated by checked_write_target
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )
        print(f"Wrote {len(rows):,} records to {arguments.out}")
    for line in summarise(run, rows):
        print(line)
    print(LIMIT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
