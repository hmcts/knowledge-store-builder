"""Run semantic work as headless `claude -p` workers, one per chunk or batch.

A worker's cost is the context it re-reads every turn, so each one gets only
`Read` and `Write`, no settings and no MCP servers. Its permissions are exact
rules: `Read` on the files it was given, `Edit` on its one output path, and
everything else denied without prompting. The library applies the shipped gate
itself once the worker exits and resumes the session with the findings.

The core is spawning, permissions, repair rounds, wave control and reporting.
The `extract` and `summaries` sub-commands build `Job`s on top of it:

    knowledgestore workers extract   [--plan PATH] [--chunks ID[,ID...]] ...
    knowledgestore workers summaries --batches DIR ...
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.resources
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import check_chunk, config, cost, io, store_paths, summary_batches

PACKAGE = "knowledgestore"

SYSTEM_PROMPT = (
    "You are a worker for a knowledge store. Use only the tools you have; "
    "everything else is denied."
)

# A character that would change what a permission rule matches. A path holding
# one cannot be written as a rule that grants exactly that file, so it is refused
# rather than run under a rule that grants more.
RULE_UNSAFE = re.compile(r"[()*?\[\]{}\\!]")


class Outcome(str, Enum):
    SKIPPED = "skipped"  # already complete before spawning; claude not run
    DONE = "done"  # gate passed
    GATE_FAILED = "gate-failed"  # output exists, violations remain after the last round
    NO_OUTPUT = "no-output"  # clean finish, output missing or unreadable
    TIMED_OUT = "timed-out"
    UNSAFE_PATH = "unsafe-path"  # a path cannot be an exact permission rule; not run
    API_ERROR = "api-error"  # non-zero exit, unparseable result or is_error; stops the wave


_NOT_DONE = (Outcome.GATE_FAILED, Outcome.NO_OUTPUT, Outcome.TIMED_OUT, Outcome.UNSAFE_PATH)

Runner = Callable[[list[str], Path, float], subprocess.CompletedProcess]


@dataclass(frozen=True)
class Job:
    key: str  # chunk id or batch number as a string; the sort key for reports
    prompt: str
    reads: tuple[str, ...]  # absolute paths the worker may Read
    out: Path  # the one path the worker may write
    complete: Callable[[], bool]  # already done? checked before spawning
    gate: Callable[[], list[str]]  # violations in `out` now; [] means pass


@dataclass
class Report:
    key: str
    outcome: Outcome
    rounds: int = 0  # claude invocations made
    violations: list[str] = field(default_factory=list)
    denials: int = 0  # permission denials summed over the job's runs
    results: list[Path] = field(default_factory=list)


def permission_rules(reads: Sequence[str], out: Path) -> list[str] | None:
    """Exact allow rules, or None when a path cannot be written as one."""
    paths = [*reads, str(out)]
    if any(RULE_UNSAFE.search(p) or not p.startswith("/") for p in paths):
        return None
    # An absolute path in a rule takes a leading `//`; a write rule is `Edit`.
    return ["Read(/" + p + ")" for p in reads] + ["Edit(/" + str(out) + ")"]


def claude_command(
    prompt: str,
    *,
    model: str,
    rules: list[str],
    claude_bin: str,
    resume: str | None = None,
) -> list[str]:
    # The prompt precedes `--allowedTools` because that flag is variadic.
    argv = [
        claude_bin,
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "json",
        "--setting-sources=",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--exclude-dynamic-system-prompt-sections",
        "--system-prompt",
        SYSTEM_PROMPT,
        "--tools",
        "Read,Write",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        *rules,
    ]
    if resume:
        argv += ["--resume", resume]
    return argv


def parse_result(stdout: str) -> dict | None:
    """The result JSON, skipping anything printed before it (a stdin warning)."""
    start = stdout.find("{")
    if start < 0:
        return None
    try:
        value = json.loads(stdout[start:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def default_runner(argv: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )


def _output_parses(out: Path) -> bool:
    try:
        return io.read_json(out) is not None
    except (ValueError, OSError):
        return False


def _record(report: Report, runs: Path, kind: str, proc, parsed: dict | None) -> None:
    stem = runs / "results" / f"{kind}-{report.key}-{report.rounds}"
    if parsed is not None:
        path = stem.with_suffix(".json")
        io.write_json(path, parsed)
    else:
        path = stem.with_suffix(".txt")
        io.checked_write_target(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(proc.stdout or "", encoding="utf-8")
    report.results.append(path)


def run_job(
    job: Job,
    *,
    runs: Path,
    kind: str,
    model: str,
    repair_rounds: int,
    timeout: float,
    claude_bin: str,
    run: Runner,
) -> Report:
    report = Report(job.key, Outcome.NO_OUTPUT)
    rules = permission_rules(job.reads, job.out)
    if rules is None:
        report.outcome = Outcome.UNSAFE_PATH
        return report
    if job.complete():
        report.outcome = Outcome.SKIPPED
        return report

    work = runs / "work" / f"{kind}-{job.key}"
    io.checked_write_target(work)
    work.mkdir(parents=True, exist_ok=True)

    prompt = job.prompt
    resume: str | None = None
    for _ in range(repair_rounds + 1):
        report.rounds += 1
        argv = claude_command(
            prompt, model=model, rules=rules, claude_bin=claude_bin, resume=resume
        )
        try:
            proc = run(argv, work, timeout)
        except subprocess.TimeoutExpired:
            report.outcome = Outcome.TIMED_OUT
            return report
        parsed = parse_result(proc.stdout or "")
        _record(report, runs, kind, proc, parsed)
        if parsed is None or proc.returncode != 0 or parsed.get("is_error"):
            report.outcome = Outcome.API_ERROR
            return report
        report.denials += len(parsed.get("permission_denials") or [])
        # A missing file after a clean exit means the worker gave up, usually on a
        # denial. Resuming would ask it the same question again.
        if not _output_parses(job.out):
            report.outcome = Outcome.NO_OUTPUT
            return report
        report.violations = job.gate()
        if not report.violations:
            report.outcome = Outcome.DONE
            return report
        resume = parsed.get("session_id")
        prompt = (
            f"The gate found these problems in {job.out}:\n"
            + "\n".join(report.violations[:30])
            + f"\nFix them and write {job.out} again with the Write tool. "
            "Do not reply with the JSON."
        )
    report.outcome = Outcome.GATE_FAILED
    return report


def run_wave(jobs: Sequence[Job], *, parallel: int, **job_options) -> tuple[list[Report], int]:
    """Run jobs in sorted key order, at most `parallel` at once.

    Returns the reports sorted by key and the count of jobs never started
    because an API error stopped the wave. Workers already running finish.
    """
    pending = sorted(jobs, key=lambda j: j.key)
    reports: list[Report] = []
    stopped = False
    in_flight: set[Future[Report]] = set()
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        while pending or in_flight:
            while pending and not stopped and len(in_flight) < max(1, parallel):
                in_flight.add(pool.submit(run_job, pending.pop(0), **job_options))
            if not in_flight:
                break
            finished, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in finished:
                report = future.result()
                reports.append(report)
                if report.outcome is Outcome.API_ERROR:
                    stopped = True
    not_started = len(pending)
    return sorted(reports, key=lambda r: r.key), not_started


def exit_code(reports: Sequence[Report]) -> int:
    outcomes = {r.outcome for r in reports}
    if Outcome.API_ERROR in outcomes:
        return 2
    if outcomes & set(_NOT_DONE):
        return 3
    return 0


def _usage_totals(reports: Sequence[Report]) -> tuple[int, int, int, float]:
    runs = inp = out = 0
    spent = 0.0
    for report in reports:
        for path in report.results:
            runs += 1
            if path.suffix != ".json":
                continue
            data = io.read_json_dict(path)
            usage = data.get("usage") or {}
            inp += cost.input_tokens(usage)
            out += int(usage.get("output_tokens") or 0)
            spent += float(data.get("total_cost_usd") or 0)
    return runs, inp, out, spent


def report_lines(reports: Sequence[Report], results_dir: Path, not_started: int = 0) -> list[str]:
    lines: list[str] = []
    for report in sorted(reports, key=lambda r: r.key):
        if report.outcome is Outcome.SKIPPED:
            continue
        lines.append(
            f"{report.outcome.value} {report.key}: rounds={report.rounds} denials={report.denials}"
        )
        lines += [f"    {v}" for v in report.violations[:5]]
    counts = Counter(r.outcome for r in reports)
    parts = [f"{o.value} {counts[o]}" for o in Outcome if counts[o]]
    if not_started:
        parts.append(f"not started {not_started}")
    lines.append("summary: " + (", ".join(parts) or "nothing to do"))
    runs, inp, out, spent = _usage_totals(reports)
    lines.append(f"usage: runs={runs} input={inp} output={out} cost_usd={spent:.4f}")
    lines.append(f"results: {results_dir} (knowledgestore cost {results_dir} re-reads them)")
    return lines


# --- the sub-commands -----------------------------------------------------

_SPEC_PARTS = ("skills", "claude", "references", "extraction-spec.md")
_SPEC_HEADING = "# graphify reference"
_FENCED = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_PLACEHOLDER = re.compile(r"^(FILE_LIST|CHUNK_PATH)$|\b(CHUNK_NUM|TOTAL_CHUNKS|DEEP_MODE)\b", re.M)
CROSS_CHUNK_NOTE = (
    "cross-chunk hyperedge ids are checked by merge-chunks / check-chunk over the wave, "
    "not per worker"
)


class UsageError(Exception):
    """A refusal that ends the run with exit 1 and this message."""


def _asset(name: str) -> str:
    return (importlib.resources.files(PACKAGE) / "assets" / name).read_text(encoding="utf-8")


def extraction_spec_prompt() -> str:
    """graphify's own extraction prompt block, which owns the schema.

    Never replaced by a prompt this library wrote: if the spec or its block is
    gone, the schema has moved and the right answer is to say so.
    """
    spec = importlib.resources.files("graphify")
    for part in _SPEC_PARTS:
        spec = spec / part
    try:
        version = importlib.metadata.version("graphifyy")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    where = f"{spec} (graphify {version})"
    try:
        text = spec.read_text(encoding="utf-8")
    except OSError as error:
        raise UsageError(f"workers extract needs graphify's extraction spec at {where}") from error
    start = text.find(_SPEC_HEADING)
    block = _FENCED.search(text, start) if start >= 0 else None
    if block is None:
        raise UsageError(f"no fenced prompt block after {_SPEC_HEADING!r} in {where}")
    return block.group(1)


def fill_prompt(template: str, files: Sequence[str], number: str, total: int, out: Path) -> str:
    """Substitute the spec's placeholders in one pass.

    One pass, so a path that happens to contain a placeholder's name is not
    substituted again. `FILE_LIST` and `CHUNK_PATH` are replaced only where the
    spec uses them as a whole line: elsewhere they sit inside the schema's
    `<FILE_LIST path verbatim>` and must stay as the words they are.
    """
    values = {
        "FILE_LIST": "\n".join(files),
        "CHUNK_PATH": str(out),
        "CHUNK_NUM": number,
        "TOTAL_CHUNKS": str(total),
        "DEEP_MODE": "DEEP_MODE=false",
    }
    return _PLACEHOLDER.sub(lambda m: values[m.group(1) or m.group(2)], template)


def _out_name(key: str) -> str:
    # The name `chunk-status` and `merge-chunks` read, as `chunk-plan` writes it.
    return f".graphify_chunk_{key}.json"


def extract_jobs(
    plan: dict[str, list[str]],
    directory: Path,
    normalise: Callable[[str], str],
    template: str,
    total: int,
) -> list[Job]:
    addendum = _asset("extraction_worker_prompt.md")
    jobs = []
    for key in sorted(plan):
        files = tuple(plan[key])
        out = directory / _out_name(key)

        def gate(key=key, files=files, out=out) -> list[str]:
            # A fresh dict per chunk: a hyperedge id reused by another chunk is
            # `check-chunk --batch`'s job over the whole wave, not a worker's.
            found, _ = check_chunk.check_chunk(int(key), out, files, {}, normalise)
            return [str(v) for v in found]

        def complete(out=out, gate=gate) -> bool:
            return _output_parses(out) and not gate()

        prompt = fill_prompt(template, files, key, total, out) + "\n" + addendum
        jobs.append(Job(key, prompt, files, out, complete, gate))
    return jobs


def summary_jobs(batch_files: Sequence[Path]) -> list[Job]:
    template = _asset("summary_worker_prompt.md")
    jobs = []
    for path in batch_files:
        batch = io.read_json_dict(path)
        out = Path(str(batch["out"]))
        # The number as the file spells it, zero-padded, so keys sort in batch order.
        key = path.stem.removeprefix(summary_batches.BATCH_PREFIX)
        prompt = (
            template.replace("{batch}", key)
            .replace("{batch_file}", str(path))
            .replace("{out}", str(out))
        )

        def gate(path=path) -> list[str]:
            return summary_batches.check_batch(str(path))[0]

        def complete(out=out, gate=gate) -> bool:
            return out.is_file() and not gate()

        jobs.append(Job(key, prompt, (str(path),), out, complete, gate))
    return jobs


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore workers",
        description="Run extraction or summary authoring as headless claude workers, one "
        "per chunk or batch, each limited to Read on its inputs and Write on its output, "
        "with the shipped gate applied after it finishes.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--runs", type=Path, help="work and result files (default depends on stage)")
        p.add_argument("--parallel", type=int, default=3, help="workers at once (default 3)")
        p.add_argument("--model", default="sonnet", help="model name (default sonnet)")
        p.add_argument(
            "--repair-rounds",
            type=int,
            default=2,
            help="times to send the gate's findings back to the same session (default 2)",
        )
        p.add_argument("--timeout", type=float, default=30, help="minutes per run (default 30)")
        p.add_argument("--claude", default="claude", help="the claude executable (default claude)")

    extract = sub.add_parser("extract", help="one worker per chunk of the chunk plan")
    extract.add_argument("--plan", type=Path, help=f"default: {config.CHUNK_PLAN_PATH.name}")
    extract.add_argument("--chunks", help="only these chunk ids, comma separated")
    common(extract)
    summaries = sub.add_parser("summaries", help="one worker per summary batch")
    summaries.add_argument("--batches", type=Path, required=True, help="directory of batch files")
    common(summaries)
    return parser.parse_args(argv)


def _select_chunks(plan: dict[str, list[str]], wanted: str | None) -> dict[str, list[str]]:
    if not wanted:
        return plan
    by_number = {int(k): k for k in plan if k.isdigit()}
    chosen: dict[str, list[str]] = {}
    for token in (t.strip() for t in wanted.split(",") if t.strip()):
        key = token if token in plan else None
        if key is None and token.isdigit():
            key = by_number.get(int(token))
        if key is None:
            raise UsageError(f"chunk {token} is not in the plan")
        chosen[key] = plan[key]
    return chosen


def _extract_setup(arguments: argparse.Namespace) -> tuple[list[Job], Path]:
    try:
        from graphify.ids import normalize_id
    except ImportError as error:
        raise UsageError(
            "workers extract compares every id with graphify's own normalize_id, and graphify "
            "is not installed. Install it with `pip install 'hmcts-knowledge-store-builder[ast]'` "
            "and re-run."
        ) from error
    plan_path = arguments.plan or config.CHUNK_PLAN_PATH
    if not plan_path.is_file():
        raise UsageError(f"no chunk plan at {plan_path}: run knowledgestore chunk-plan first")
    whole = store_paths.load_plan(plan_path)
    plan = _select_chunks(whole, arguments.chunks)
    if any(not key.isdigit() for key in plan):
        raise UsageError(f"chunk ids in {plan_path} must be numbers")
    directory = config.CHUNK_PLAN_PATH.parent.absolute()
    template = extraction_spec_prompt()
    runs = arguments.runs or directory / ".workers"
    return extract_jobs(plan, directory, normalize_id, template, len(whole)), runs


def _summaries_setup(arguments: argparse.Namespace) -> tuple[list[Job], Path]:
    if not arguments.batches.is_dir():
        raise UsageError(f"no batch directory at {arguments.batches}: run summaries batches first")
    found = sorted(arguments.batches.glob(f"{summary_batches.BATCH_PREFIX}*.json"))
    if not found:
        raise UsageError(f"no {summary_batches.BATCH_PREFIX}*.json files in {arguments.batches}")
    return summary_jobs([p.absolute() for p in found]), arguments.runs or arguments.batches / "runs"


def main(argv: list[str] | None = None, run: Runner | None = None) -> int:
    arguments = parse_args(argv)
    try:
        if arguments.command == "extract":
            jobs, runs = _extract_setup(arguments)
        else:
            jobs, runs = _summaries_setup(arguments)
    except UsageError as error:
        print(str(error), file=sys.stderr)
        return 1
    reports, not_started = run_wave(
        jobs,
        parallel=arguments.parallel,
        runs=runs,
        kind=arguments.command,
        model=arguments.model,
        repair_rounds=arguments.repair_rounds,
        timeout=arguments.timeout * 60,
        claude_bin=arguments.claude,
        run=run or default_runner,
    )
    for line in report_lines(reports, runs / "results", not_started):
        print(line)
    if arguments.command == "extract":
        print(CROSS_CHUNK_NOTE)
    return exit_code(reports)


__all__ = [
    "Job",
    "Outcome",
    "Report",
    "RULE_UNSAFE",
    "SYSTEM_PROMPT",
    "claude_command",
    "default_runner",
    "exit_code",
    "extract_jobs",
    "main",
    "parse_args",
    "parse_result",
    "permission_rules",
    "report_lines",
    "run_job",
    "run_wave",
    "summary_jobs",
]
