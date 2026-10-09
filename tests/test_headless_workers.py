"""The headless worker core, stubbed only at the `run=` process boundary.

Everything downstream of the stub is real: permission rules, argv assembly,
result files on disk, the gate callable, wave control and the report.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated

from knowledgestore import headless_workers as hw
from knowledgestore.headless_workers import Job, Outcome, Report


def result_json(session="sess-1", is_error=False, denials=0, inp=10, out=5, usd=0.5):
    return json.dumps(
        {
            "session_id": session,
            "is_error": is_error,
            "num_turns": 2,
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 1000,
                "output_tokens": out,
            },
            "total_cost_usd": usd,
            "permission_denials": [{"tool_name": "Read"}] * denials,
        }
    )


def proc(stdout, code=0):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")


class Stub:
    """A `claude` stand-in: records argv, writes `out` on chosen rounds."""

    def __init__(self, out: Path, write_on=(1, 2, 3, 4), stdout=None, code=0):
        self.out, self.write_on = out, write_on
        self.stdout, self.code = stdout, code
        self.calls: list[list[str]] = []
        self.lock = threading.Lock()

    def __call__(self, argv, cwd, timeout):
        with self.lock:
            self.calls.append(argv)
            round_no = sum(1 for _ in self.calls)
        if "--resume" not in argv:
            round_no = 1
        if round_no in self.write_on:
            self.out.write_text(json.dumps({"round": round_no}))
        return proc(self.stdout if self.stdout is not None else result_json(), self.code)


class Base(SettingsIsolated):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.runs = self.root / "runs"
        self.src = self.root / "chunk.md"
        self.src.write_text("x")

    def job(self, key="1", complete=False, gate=None):
        out = self.root / f"out-{key}.json"
        return Job(
            key=key,
            prompt="do it",
            reads=(str(self.src),),
            out=out,
            complete=lambda: complete,
            gate=gate or (lambda: []),
        )

    def opts(self, run, repair_rounds=2):
        return dict(
            runs=self.runs,
            kind="extract",
            model="sonnet",
            repair_rounds=repair_rounds,
            timeout=5,
            claude_bin="claude",
            run=run,
        )


class OutcomeTests(Base):
    def test_clean_exit_without_output_is_no_output(self):
        # Break: treating exit 0 as done.
        job = self.job()
        stub = Stub(job.out, write_on=())
        report = hw.run_job(job, **self.opts(stub))
        self.assertEqual(report.outcome, Outcome.NO_OUTPUT)
        self.assertEqual(len(stub.calls), 1)

    def test_unparseable_output_is_no_output(self):
        # Break: an unreadable file read as an empty extraction.
        job = self.job()
        stub = Stub(job.out, write_on=())
        job.out.write_text("{not json")
        report = hw.run_job(job, **self.opts(stub))
        self.assertEqual(report.outcome, Outcome.NO_OUTPUT)

    def test_gate_fail_then_pass_resumes_the_session(self):
        # Break: the repair round is not wired.
        verdicts = [["bad id foo"], []]
        job = self.job(gate=lambda: verdicts.pop(0))
        stub = Stub(job.out, stdout=result_json(session="abc"))
        report = hw.run_job(job, **self.opts(stub))
        self.assertEqual(report.outcome, Outcome.DONE)
        self.assertEqual(report.rounds, 2)
        second = stub.calls[1]
        self.assertEqual(second[second.index("--resume") + 1], "abc")
        self.assertIn("bad id foo", second[2])
        self.assertNotIn("--resume", stub.calls[0])

    def test_gate_failing_every_round_stops_after_repair_rounds(self):
        # Break: an off-by-one, or looping forever.
        job = self.job(gate=lambda: ["still bad"])
        stub = Stub(job.out)
        report = hw.run_job(job, **self.opts(stub, repair_rounds=2))
        self.assertEqual(report.outcome, Outcome.GATE_FAILED)
        self.assertEqual(report.rounds, 3)
        self.assertEqual(len(stub.calls), 3)
        self.assertEqual(report.violations, ["still bad"])

    def test_is_error_is_api_error(self):
        job = self.job()
        stub = Stub(job.out, stdout=result_json(is_error=True))
        self.assertEqual(hw.run_job(job, **self.opts(stub)).outcome, Outcome.API_ERROR)

    def test_non_json_stdout_is_api_error_and_saved_raw(self):
        # Break: a crash on a CLI warning.
        job = self.job()
        stub = Stub(job.out, write_on=(), stdout="Error: overloaded")
        report = hw.run_job(job, **self.opts(stub))
        self.assertEqual(report.outcome, Outcome.API_ERROR)
        self.assertEqual([p.suffix for p in report.results], [".txt"])
        self.assertEqual(report.results[0].read_text(), "Error: overloaded")

    def test_nonzero_exit_is_api_error(self):
        job = self.job()
        stub = Stub(job.out, code=1)
        self.assertEqual(hw.run_job(job, **self.opts(stub)).outcome, Outcome.API_ERROR)

    def test_warning_before_json_still_parses(self):
        # Break: the stdin warning seen in practice.
        self.assertEqual(
            hw.parse_result("Warning: no stdin\n" + result_json())["session_id"], "sess-1"
        )
        self.assertIsNone(hw.parse_result("no json here"))
        self.assertIsNone(hw.parse_result("[1, 2]"))

    def test_complete_job_is_skipped_without_spawning(self):
        # Break: re-spending on resume.
        job = self.job(complete=True)
        stub = Stub(job.out)
        report = hw.run_job(job, **self.opts(stub))
        self.assertEqual(report.outcome, Outcome.SKIPPED)
        self.assertEqual(stub.calls, [])
        self.assertFalse(self.runs.exists())

    def test_unsafe_path_is_refused_not_run(self):
        # Break: a rule granting more than the file.
        job = Job(
            "1", "p", (str(self.root / "a(b).md"),), self.root / "o.json", lambda: False, lambda: []
        )
        stub = Stub(job.out)
        self.assertEqual(hw.run_job(job, **self.opts(stub)).outcome, Outcome.UNSAFE_PATH)
        self.assertEqual(stub.calls, [])

    def test_timeout_is_timed_out(self):
        # Break: a hung worker kills the wave.
        def run(argv, cwd, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        self.assertEqual(hw.run_job(self.job(), **self.opts(run)).outcome, Outcome.TIMED_OUT)


class PermissionTests(unittest.TestCase):
    def test_rules_are_spelled_exactly(self):
        rules = hw.permission_rules(["/a/b.md", "/c/d.md"], Path("/o/out.json"))
        self.assertEqual(rules, ["Read(//a/b.md)", "Read(//c/d.md)", "Edit(//o/out.json)"])

    def test_unsafe_characters_and_relative_paths_are_refused(self):
        for bad in "()*?[]{}\\!":
            self.assertIsNone(hw.permission_rules([f"/a/x{bad}y"], Path("/o/out.json")), bad)
            self.assertIsNone(hw.permission_rules(["/a/x"], Path(f"/o/{bad}")), bad)
        self.assertIsNone(hw.permission_rules(["rel/x"], Path("/o/out.json")))
        self.assertIsNone(hw.permission_rules(["/a/x"], Path("o/out.json")))

    def test_command_argv_is_exact(self):
        # Break: widening the worker's permissions.
        argv = hw.claude_command(
            "P", model="m", rules=["Read(//a)", "Edit(//o)"], claude_bin="cl", resume="S"
        )
        self.assertEqual(
            argv,
            [
                "cl", "-p", "P", "--model", "m", "--output-format", "json",
                "--setting-sources=", "--strict-mcp-config", "--mcp-config",
                '{"mcpServers":{}}', "--exclude-dynamic-system-prompt-sections",
                "--system-prompt", hw.SYSTEM_PROMPT, "--tools", "Read,Write",
                "--permission-mode", "dontAsk", "--allowedTools", "Read(//a)", "Edit(//o)",
                "--resume", "S",
            ],
        )  # fmt: skip
        self.assertFalse(any("Bash" in a for a in argv))


class WaveTests(Base):
    def test_api_error_stops_the_wave(self):
        # Break: the wave keeps going into a rate limit.
        jobs = [self.job(k) for k in "123"]
        stub = Stub(jobs[0].out, write_on=(), stdout=result_json(is_error=True))
        reports, not_started = hw.run_wave(jobs, parallel=1, **self.opts(stub))
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual([r.outcome for r in reports], [Outcome.API_ERROR])
        self.assertEqual(not_started, 2)
        self.assertEqual(hw.exit_code(reports), 2)

    def test_timeout_does_not_stop_the_wave(self):
        jobs = [self.job(k) for k in "12"]
        seen = []

        def run(argv, cwd, timeout):
            seen.append(cwd.name)
            if cwd.name.endswith("-1"):
                raise subprocess.TimeoutExpired(argv, timeout)
            jobs[1].out.write_text("{}")
            return proc(result_json())

        reports, not_started = hw.run_wave(jobs, parallel=1, **self.opts(run))
        self.assertEqual([r.outcome for r in reports], [Outcome.TIMED_OUT, Outcome.DONE])
        self.assertEqual(not_started, 0)

    def test_reports_are_in_key_order_whatever_finishes_first(self):
        # Break: nondeterministic report order.
        jobs = [self.job("1"), self.job("2")]

        def run(argv, cwd, timeout):
            if cwd.name.endswith("-1"):
                time.sleep(0.2)
            (jobs[0] if cwd.name.endswith("-1") else jobs[1]).out.write_text("{}")
            return proc(result_json())

        reports, _ = hw.run_wave(jobs, parallel=2, **self.opts(run))
        self.assertEqual([r.key for r in reports], ["1", "2"])

    def test_report_totals_are_the_hand_summed_figures(self):
        jobs = [self.job("1"), self.job("2")]
        stdouts = {
            "1": result_json(denials=1, inp=10, out=5, usd=0.25),
            "2": result_json(denials=2, inp=20, out=7, usd=0.5),
        }

        def run(argv, cwd, timeout):
            key = cwd.name.split("-")[-1]
            jobs[int(key) - 1].out.write_text("{}")
            return proc(stdouts[key])

        reports, ns = hw.run_wave(jobs, parallel=2, **self.opts(run))
        lines = hw.report_lines(reports, self.runs / "results", ns)
        self.assertEqual(lines[0], "done 1: rounds=1 denials=1")
        self.assertEqual(lines[1], "done 2: rounds=1 denials=2")
        self.assertEqual(lines[2], "summary: done 2")
        # input = (10+100+1000) + (20+100+1000); output 5+7; cost 0.25+0.5
        self.assertEqual(lines[3], "usage: runs=2 input=2230 output=12 cost_usd=0.7500")
        self.assertTrue(lines[4].startswith(f"results: {self.runs / 'results'} "))

    def test_report_lines_show_five_violations_and_not_started(self):
        report = Report("9", Outcome.GATE_FAILED, 3, [f"v{i}" for i in range(8)], 0, [])
        lines = hw.report_lines([report, Report("1", Outcome.SKIPPED)], Path("r"), 4)
        self.assertEqual(lines[0], "gate-failed 9: rounds=3 denials=0")
        self.assertEqual(lines[1:6], [f"    v{i}" for i in range(5)])
        self.assertEqual(lines[6], "summary: skipped 1, gate-failed 1, not started 4")


class ExitCodeTests(unittest.TestCase):
    def test_exit_code_table(self):
        Out = Outcome
        cases = [
            ([], 0),
            ([Out.DONE, Out.SKIPPED], 0),
            ([Out.DONE, Out.GATE_FAILED], 3),
            ([Out.NO_OUTPUT], 3),
            ([Out.TIMED_OUT], 3),
            ([Out.UNSAFE_PATH], 3),
            ([Out.API_ERROR], 2),
            ([Out.GATE_FAILED, Out.API_ERROR, Out.DONE], 2),
        ]
        for outcomes, want in cases:
            with self.subTest(outcomes=outcomes):
                self.assertEqual(hw.exit_code([Report("k", o) for o in outcomes]), want)


if __name__ == "__main__":
    unittest.main()
