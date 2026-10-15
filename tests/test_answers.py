"""The answers stage: what each answer in a session cost, and how it cited.

Every fixture is forged in the shape of real Claude Code transcript lines and carries
no real content: the questions, answers, paths and ids are invented, and the field
names, nesting and line order are copied from real transcripts. Two shapes are
inferred rather than copied, and say so where they are built: the resumed session
(`test_a_resumed_session_counts_each_repeated_turn_once`) and the `!` shell line
(`Transcript.shell`). Three fields are read
for a meaning the transcript does not state, and each was confirmed against real
sessions before it was relied on:

- `origin.kind` separates a person's prompt (`human`) from lines the harness injects
  as user lines (`task-notification`, `peer`, `auto-continuation`); confirmed by the
  text each kind carried across two real sessions.
- `toolUseResult.agentId` names the subagent transcript an `Agent` call started, at
  `<session>/subagents/agent-<agentId>.jsonl`; confirmed by every one of nine
  dispatches in one real session resolving to a file there.
- `isSidechain` is true on every line of a subagent transcript, including the prompt
  the subagent was given; confirmed on the same files.

Expected values are worked out by hand in each fixture's comment, never with the code
under test.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import os
import tempfile
from unittest import mock
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import answers, check_citations, cli

SHA = "0123456789abcdef0123456789abcdef01234567"


def _usage(uncached: int, write: int, read: int, output: int) -> dict:
    return {
        "input_tokens": uncached,
        "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
        "output_tokens": output,
        "service_tier": "standard",
        "cache_creation": {"ephemeral_1h_input_tokens": write, "ephemeral_5m_input_tokens": 0},
    }


class Transcript:
    """One transcript's lines, in the real shape, with forged content."""

    def __init__(self, session_id: str, cwd: str = "/forged/cwd", agent_id: str = ""):
        self.session_id = session_id
        self.cwd = cwd
        self.agent_id = agent_id
        self.lines: list[dict] = []
        self.parent: str | None = None

    def _line(self, kind: str) -> dict:
        uuid = (
            f"{self.session_id[:8]}-{self.agent_id[:4] or '0000'}-4000-8000-{len(self.lines):012d}"
        )
        line: dict = {"parentUuid": self.parent, "isSidechain": bool(self.agent_id)}
        if self.agent_id:
            line["agentId"] = self.agent_id
        line.update(
            {
                "type": kind,
                "uuid": uuid,
                "timestamp": "2026-10-15T09:00:00.000Z",
                "userType": "external",
                "entrypoint": "cli",
                "cwd": self.cwd,
                "sessionId": self.session_id,
                "version": "2.1.293",
                "gitBranch": "main",
            }
        )
        self.parent = uuid
        self.lines.append(line)
        return line

    def enqueue(self, text: str) -> None:
        """A queue-operation line: no uuid, no message - read past, never an answer."""
        self.lines.append(
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "timestamp": "2026-10-15T09:00:00.000Z",
                "sessionId": self.session_id,
                "content": text,
            }
        )

    def prompt(self, text: str) -> None:
        line = self._line("user")
        line["promptId"] = "forged-prompt"
        line["message"] = {"role": "user", "content": text}
        if not self.agent_id:
            line.update(
                {"permissionMode": "default", "promptSource": "typed", "origin": {"kind": "human"}}
            )

    def interrupt(self) -> None:
        """What the harness writes when the person stops a tool call: a user line of
        text blocks with no `origin` and no `isMeta`. Shape confirmed against the one
        such line found in real transcripts on the machine this was written on."""
        line = self._line("user")
        line["promptId"] = "forged-prompt"
        line["message"] = {
            "role": "user",
            "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}],
        }

    def shell(self, text: str) -> None:
        """A `!` shell line typed into the prompt. INFERRED: no real transcript on the
        machine this was written on held one, so the shape is a prompt's, which is the
        worst case - it carries nothing that marks it as anything else."""
        line = self._line("user")
        line["promptId"] = "forged-prompt"
        line["message"] = {"role": "user", "content": text}

    def notification(self, text: str) -> None:
        """What the harness injects when a background agent finishes."""
        line = self._line("user")
        line["promptId"] = "forged-prompt"
        line["message"] = {"role": "user", "content": text}
        line.update(
            {
                "promptSource": "system",
                "turnOrigin": "task_notification",
                "origin": {"kind": "task-notification"},
            }
        )

    def _assistant(self, message_id: str, usage: dict, block: dict) -> None:
        line = self._line("assistant")
        line["message"] = {
            "model": "forged-model",
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [block],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": usage,
        }
        line["requestId"] = "req_forged"

    def thinking(self, message_id: str, usage: dict) -> None:
        self._assistant(message_id, usage, {"type": "thinking", "thinking": "", "signature": "x"})

    def text(self, message_id: str, usage: dict, text: str) -> None:
        self._assistant(message_id, usage, {"type": "text", "text": text})

    def tool(self, message_id: str, usage: dict, tool_id: str, name: str, given: dict) -> None:
        self._assistant(
            message_id,
            usage,
            {
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": given,
                "caller": {"type": "direct"},
            },
        )

    def result(self, tool_id: str, text: str, outcome: dict | None = None) -> None:
        """A tool's result, which comes back to the session as a user-role line."""
        source = self.parent
        line = self._line("user")
        line["promptId"] = "forged-prompt"
        line["message"] = {
            "role": "user",
            "content": [
                {
                    "tool_use_id": tool_id,
                    "type": "tool_result",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        }
        line["toolUseResult"] = outcome if outcome is not None else {"type": "text"}
        line["sourceToolAssistantUUID"] = source

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in self.lines), encoding="utf-8")
        return path


S1 = "11111111-aaaa-4bbb-8ccc-000000000001"
S2 = "22222222-aaaa-4bbb-8ccc-000000000002"
S3 = "33333333-aaaa-4bbb-8ccc-000000000003"
S4 = "44444444-aaaa-4bbb-8ccc-000000000004"
AGENT = "a0f1e2d3c4b5a6978"

Q_SUBMIT = "Where is an order submitted?"
Q_RETRY = "What retry limit does checkout use?"
Q_CANCEL = "And how is an order cancelled?"
Q_GATEWAY = "Which services call the payment gateway?"


def first_answer_lines(t: Transcript) -> None:
    """Q_SUBMIT, answered in two turns with a tool call between them.

    msg_01 is one turn written as three lines (thinking, text, tool call), each
    carrying the turn's usage: 3 + 1,000 + 5,000 = 6,003 input; output 2 -> 2 -> 40.
    msg_02 is one line: 5 + 200 + 6,000 = 6,205 input; output 10.
    Answer input 6,003 + 6,205 = 12,208. Summing every line would read
    3 x 6,003 + 6,205 = 24,214. cache_read 11,000, cache_write 1,200, output floor
    40 + 10 = 50, turns 2, one Read.
    """
    t.prompt(Q_SUBMIT)
    t.thinking("msg_01", _usage(3, 1000, 5000, 2))
    t.text("msg_01", _usage(3, 1000, 5000, 2), "Let me look.")
    t.tool(
        "msg_01",
        _usage(3, 1000, 5000, 40),
        "toolu_01",
        "Read",
        {"file_path": "/forged/elsewhere/a.md"},
    )
    t.result("toolu_01", "forged file body")
    t.text("msg_02", _usage(5, 200, 6000, 10), "Orders are submitted by `OrderService`.")


def session_one() -> Transcript:
    """Two answers. The second: msg_03, 4 + 0 + 6,200 = 6,204 input, output 7."""
    t = Transcript(S1)
    t.enqueue(Q_SUBMIT)
    first_answer_lines(t)
    t.prompt(Q_RETRY)
    t.text("msg_03", _usage(4, 0, 6200, 7), "There is no retry limit in the store.")
    return t


class AnswersStage(SettingsIsolated):
    def _dir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name).resolve()

    def _store(self, nodes: list[dict] | None = None) -> Path:
        store = self._dir() / "store"
        graph = store / "graphify-out" / "graph.json"
        graph.parent.mkdir(parents=True)
        graph.write_text(json.dumps({"nodes": nodes or [], "links": []}), encoding="utf-8")
        return store

    def _records(self, paths: list[Path], store: Path) -> list[dict]:
        vocabulary = check_citations.load_vocabulary(store / "graphify-out" / "graph.json")
        return answers.measure(paths, store, vocabulary).records()

    def _run(self, *arguments: str) -> tuple[int, str]:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["answers", *arguments])
        return code, out.getvalue()


class SplittingASession(AnswersStage):
    def test_a_tool_result_does_not_open_an_answer(self):
        """Break: reading a tool result's user-role line as a prompt. The session
        then holds three answers, and the first stops at its tool call."""
        root = self._dir()
        session_one().write(root / f"{S1}.jsonl")
        records = self._records([root], self._store())
        self.assertEqual([r["question"] for r in records], [Q_SUBMIT, Q_RETRY])
        self.assertEqual([r["index"] for r in records], [0, 1])
        self.assertEqual(records[0]["session"], S1)
        self.assertEqual(records[0]["turns"], 2)
        self.assertEqual(records[0]["tool_calls"], {"Read": 1})

    def test_a_turn_split_over_lines_is_counted_once(self):
        """Break: summing usage per line rather than once per message id. The first
        answer's input then reads 24,214 rather than 12,208."""
        root = self._dir()
        session_one().write(root / f"{S1}.jsonl")
        first, second = self._records([root], self._store())
        self.assertEqual(first["input"], 12_208)
        self.assertEqual(first["cache_read"], 11_000)
        self.assertEqual(first["cache_write"], 1_200)
        self.assertEqual(first["output_floor"], 50)
        self.assertEqual(second["input"], 6_204)
        self.assertEqual(second["output_floor"], 7)

    def test_the_question_hash_ignores_surrounding_whitespace(self):
        """Break: hashing the untrimmed prompt, so one question asked with a trailing
        newline in one run fails to pair with itself in another."""
        root = self._dir()
        t = Transcript(S1)
        t.prompt(Q_RETRY + "\n")
        t.text("msg_03", _usage(4, 0, 6200, 7), "Forged.")
        t.write(root / f"{S1}.jsonl")
        (record,) = self._records([root], self._store())
        # sha256 of "What retry limit does checkout use?", computed by hand with
        # `printf %s "What retry limit does checkout use?" | shasum -a 256`.
        self.assertEqual(
            record["question_sha"],
            "cd1c8c7978de31dd0b5567b024a160ba99ffd490a5a54d227b0370144866d3f8",
        )

    def test_the_answer_text_is_the_last_text_before_the_next_prompt(self):
        """Break: taking the first text block, which is the preamble ("Let me look.")
        rather than the answer, so its citations are never read."""
        root = self._dir()
        session_one().write(root / f"{S1}.jsonl")
        first, _ = self._records([root], self._store([{"id": "n", "label": "OrderService"}]))
        self.assertEqual(first["citations"], [{"span": "OrderService", "class": "resolves"}])

    def test_a_resumed_session_counts_each_repeated_turn_once(self):
        """Break: reading a carried-over prompt as a new question. A resumed session
        repeats the earlier lines, uuids and message ids included, before its own;
        read naively, Q_SUBMIT is answered twice and the resumed copy has no usage.

        INFERRED shape: `cost` documents carried-over sessions repeating message ids,
        but no real transcript found repeated lines across files - a headless
        `--resume` appended to its own file - so that uuids are kept is an assumption.

        S3 carries Q_SUBMIT over and then asks Q_CANCEL: msg_04, 2 + 100 + 7,000 =
        7,102 input.
        """
        root = self._dir()
        session_one().write(root / f"{S1}.jsonl")
        resumed = Transcript(S1)
        resumed.enqueue(Q_SUBMIT)
        first_answer_lines(resumed)
        resumed.session_id = S3
        resumed.prompt(Q_CANCEL)
        resumed.text("msg_04", _usage(2, 100, 7000, 12), "Forged.")
        resumed.write(root / f"{S3}.jsonl")
        records = self._records([root], self._store())
        self.assertEqual(
            [(r["session"], r["question"]) for r in records],
            [(S1, Q_SUBMIT), (S1, Q_RETRY), (S3, Q_CANCEL)],
        )
        self.assertEqual([r["input"] for r in records], [12_208, 6_204, 7_102])

    def test_injected_user_lines_are_not_prompts(self):
        """Break: reading a background agent's completion notice as a person's
        question, which splits one answer into two and pairs nothing."""
        root = self._dir()
        t = Transcript(S1)
        t.prompt(Q_RETRY)
        t.text("msg_03", _usage(4, 0, 6200, 7), "Started a search.")
        t.notification("<task-notification>forged</task-notification>")
        t.text("msg_05", _usage(1, 0, 6300, 3), "There is no retry limit.")
        t.write(root / f"{S1}.jsonl")
        records = self._records([root], self._store())
        self.assertEqual([r["question"] for r in records], [Q_RETRY])
        self.assertEqual(records[0]["turns"], 2)

    def test_an_interrupt_marker_is_not_a_prompt(self):
        """Break: reading the harness's interrupt marker as a question. Every
        interrupted answer then becomes two, and the empty second one's question
        pairs with every other run's interrupt."""
        root = self._dir()
        t = Transcript(S1)
        t.prompt(Q_RETRY)
        t.tool("msg_03", _usage(4, 0, 6200, 7), "toolu_03", "Bash", {"command": "sleep 99"})
        t.result("toolu_03", "forged")
        t.interrupt()
        t.text("msg_05", _usage(1, 0, 6300, 3), "Stopped.")
        t.write(root / f"{S1}.jsonl")
        records = self._records([root], self._store())
        self.assertEqual([r["question"] for r in records], [Q_RETRY])
        self.assertEqual(records[0]["turns"], 2)

    def test_a_shell_line_is_not_a_prompt(self):
        """Break: reading a `!` shell command and its output as two questions."""
        root = self._dir()
        t = Transcript(S1)
        t.prompt(Q_RETRY)
        t.text("msg_03", _usage(4, 0, 6200, 7), "Forged.")
        t.shell("<bash-input>ls</bash-input>")
        t.shell("<bash-stdout>forged</bash-stdout><bash-stderr></bash-stderr>")
        t.write(root / f"{S1}.jsonl")
        records = self._records([root], self._store())
        self.assertEqual([r["question"] for r in records], [Q_RETRY])


def gateway_session(root: Path, store: Path) -> None:
    """Q_GATEWAY, which dispatches one subagent.

    Parent: msg_10 (Agent call) 2 + 300 + 4,000 = 4,302; msg_11 1 + 10 + 4,300 = 4,311;
    msg_12 1 + 50 + 4,400 = 4,451. Parent alone: 13,064.
    Subagent: msg_s1 (Grep) 10 + 8,000 + 0 = 8,010; msg_s2 1 + 100 + 8,000 = 8,101.
    With the subagent folded in: 13,064 + 16,111 = 29,175 input over five turns.
    Output floor: 50 + 5 + 30 + 20 + 60 = 165.
    """
    t = Transcript(S2)
    t.prompt(Q_GATEWAY)
    t.tool(
        "msg_10",
        _usage(2, 300, 4000, 50),
        "toolu_10",
        "Agent",
        {"description": "forged", "prompt": "forged", "subagent_type": "Explore"},
    )
    t.result(
        "toolu_10",
        "Async agent launched successfully.",
        {
            "isAsync": True,
            "status": "async_launched",
            "agentId": AGENT,
            "description": "forged",
            "prompt": "forged",
            "outputFile": "/forged/tasks/a.output",
            "canReadOutputFile": True,
        },
    )
    t.text("msg_11", _usage(1, 10, 4300, 5), "Dispatched a search.")
    t.notification("<task-notification>forged</task-notification>")
    t.text("msg_12", _usage(1, 50, 4400, 30), "The gateway is called by two services.")
    t.write(root / f"{S2}.jsonl")

    sub = Transcript(S2, agent_id=AGENT)
    sub.prompt("Find the callers of the payment gateway.")
    sub.tool("msg_s1", _usage(10, 8000, 0, 20), "toolu_s1", "Grep", {"pattern": "gateway"})
    sub.result("toolu_s1", "forged matches")
    sub.text("msg_s2", _usage(1, 100, 8000, 60), "Found two `PaymentGateway` callers.")
    sub.write(root / S2 / "subagents" / f"agent-{AGENT}.jsonl")


class SubagentsAreFolded(AnswersStage):
    def test_a_subagent_s_usage_is_part_of_the_answer_that_dispatched_it(self):
        """Break: dropping the subagent transcript, so the answer reads 13,064 input
        rather than 29,175 - the cheapest-looking answers become the ones that
        delegated the most."""
        root = self._dir()
        store = self._store()
        gateway_session(root, store)
        (record,) = self._records([root], store)
        self.assertEqual(record["input"], 29_175)
        self.assertEqual(record["output_floor"], 165)
        self.assertEqual(record["turns"], 5)
        self.assertEqual(record["tool_calls"], {"Agent": 1, "Grep": 1})
        self.assertEqual(record["subagents"], 1)
        self.assertEqual(record["subagents_missing"], 0)

    def test_the_subagent_s_text_is_not_the_answer(self):
        """Break: taking the subagent's last text as the answer the person read.

        The session ends on the dispatch, as a real one did when its background agent
        was still running, so the subagent's text is the last text read. Its report
        cites `PaymentGateway`; the answer the person read cites nothing.
        """
        root = self._dir()
        store = self._store([{"id": "n", "label": "PaymentGateway"}])
        gateway_session(root, store)
        session = root / f"{S2}.jsonl"
        lines = session.read_text(encoding="utf-8").splitlines(keepends=True)
        # Keep the prompt, the dispatch and its result: everything up to the result.
        cut = next(i for i, line in enumerate(lines) if "async_launched" in line)
        session.write_text("".join(lines[: cut + 1]), encoding="utf-8")
        (record,) = self._records([root], store)
        self.assertEqual(record["subagents"], 1)
        self.assertEqual(record["citations"], [])

    def test_a_linked_subagent_that_is_missing_is_counted(self):
        """Break: a dispatch whose transcript is gone reading as a cheap answer."""
        root = self._dir()
        store = self._store()
        gateway_session(root, store)
        (root / S2 / "subagents" / f"agent-{AGENT}.jsonl").unlink()
        (record,) = self._records([root], store)
        self.assertEqual(record["input"], 13_064)
        self.assertEqual(record["subagents_missing"], 1)


class StoreFiles(AnswersStage):
    def test_only_files_under_the_store_are_counted(self):
        """Break: counting a file outside --store. The planted outside file is named
        by a Read through `..`, and by absolute path inside a Bash command."""
        root = self._dir()
        store = self._store()
        topics = store / "docs" / "topics"
        topics.mkdir(parents=True)
        (topics / "orders.md").write_text("twelve bytes", encoding="utf-8")  # 12 bytes
        (topics / "retries.md").write_text("seven b", encoding="utf-8")  # 7 bytes
        outside = store.parent / "outside"
        outside.mkdir()
        planted = outside / "planted.md"
        planted.write_text("not the store", encoding="utf-8")

        t = Transcript(S4, cwd=str(store))
        t.prompt(Q_RETRY)
        u = _usage(1, 0, 100, 1)
        t.tool("m1", u, "t1", "Read", {"file_path": str(topics / "orders.md")})
        t.tool("m1", u, "t2", "Read", {"file_path": f"{store}/../outside/planted.md"})
        t.tool(
            "m1",
            u,
            "t3",
            "Bash",
            {"command": f"grep -n retry docs/topics/retries.md; cat {planted}", "description": "x"},
        )
        t.tool("m1", u, "t4", "Grep", {"pattern": "retry", "path": str(store / "docs")})
        t.tool("m1", u, "t5", "Glob", {"pattern": "**/*.md", "path": str(topics / "gone.md")})
        t.text("m2", u, "Forged.")
        t.write(root / f"{S4}.jsonl")

        (record,) = self._records([root], store)
        self.assertEqual(
            record["store_files"],
            [
                {"path": "docs/topics/orders.md", "bytes": 12},
                {"path": "docs/topics/retries.md", "bytes": 7},
            ],
        )
        self.assertEqual(record["tool_calls"], {"Bash": 1, "Glob": 1, "Grep": 1, "Read": 2})

    def _one_bash(self, store: Path, command: str, cwd: str = "") -> dict:
        root = self._dir()
        t = Transcript(S4, cwd=cwd or str(store))
        t.prompt(Q_RETRY)
        t.tool("m1", _usage(1, 0, 100, 1), "t1", "Bash", {"command": command})
        t.write(root / f"{S4}.jsonl")
        (record,) = self._records([root], store)
        return record

    def test_a_word_naming_another_user_s_home_does_not_stop_the_run(self):
        """Break: expanding `~nosuchuser/x`, which raises RuntimeError and loses every
        record in the run over one unusual word in one session."""
        store = self._store()
        (store / "notes.md").write_text("four", encoding="utf-8")
        record = self._one_bash(store, "ls ~nosuchuser/x; cat notes.md")
        self.assertEqual(record["store_files"], [{"path": "notes.md", "bytes": 4}])

    def test_a_path_under_the_home_directory_is_expanded(self):
        """Break: dropping `~/` expansion with the fix above, so a store under the
        home directory named as `~/...` is never counted."""
        store = self._store()
        (store / "notes.md").write_text("four", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(store)}):
            # A cwd outside the store, so only the expansion can reach the file.
            record = self._one_bash(store, "cat ~/notes.md", cwd="/forged/cwd")
        self.assertEqual(record["store_files"], [{"path": "notes.md", "bytes": 4}])


CITING = (
    "`OrderService.submitOrder()` validates the basket "
    f"([source](https://github.com/example/shop-api/blob/{SHA}/src/OrderService.java#L12)). "
    "It is defined in `repositories/shop-api/src/OrderService.java`. "
    "`OrderService.cancelEverything()` handles refunds, "
    "see https://github.com/example/shop-api/blob/main/src/OrderService.java. "
    "Returns `null` when empty, and reads `orders.order_id`."
)


class Citations(AnswersStage):
    def _citing_store(self) -> Path:
        source = "repositories/shop-api/src/OrderService.java"
        return self._store(
            [
                {"id": "a", "label": "OrderService", "source_file": source},
                {"id": "b", "label": ".submitOrder()", "source_file": source},
            ]
        )

    def test_each_citation_is_classed_once(self):
        """Break: classing a `repositories/...` path by whether the graph resolves it.
        A reader cannot open a corpus path, so it must be corpus_path even when, as
        here, the graph holds that exact source file - otherwise corpus_path is 0."""
        root = self._dir()
        t = Transcript(S1)
        t.prompt(Q_SUBMIT)
        t.text("m1", _usage(1, 0, 10, 1), CITING)
        t.write(root / f"{S1}.jsonl")
        (record,) = self._records([root], self._citing_store())
        self.assertEqual(
            record["citations"],
            [
                {"span": "OrderService.submitOrder()", "class": "resolves"},
                {"span": "repositories/shop-api/src/OrderService.java", "class": "corpus_path"},
                {"span": "OrderService.cancelEverything()", "class": "unresolved"},
                {"span": "null", "class": "skipped"},
                {"span": "orders.order_id", "class": "column"},
                {
                    "span": f"https://github.com/example/shop-api/blob/{SHA}/src/OrderService.java#L12",
                    "class": "pinned_url",
                },
                {
                    "span": "https://github.com/example/shop-api/blob/main/src/OrderService.java",
                    "class": "skipped",
                },
            ],
        )


class AbsenceClaims(AnswersStage):
    def _claim(self, text: str) -> dict:
        root = self._dir()
        t = Transcript(S1)
        t.prompt(Q_RETRY)
        t.text("m1", _usage(1, 0, 10, 1), text)
        t.write(root / f"{S1}.jsonl")
        (record,) = self._records([root], self._store())
        return record

    def test_a_sentence_stating_absence_is_quoted(self):
        """Break: the rule matching nothing, so a "there is no X" answer - the claim
        most likely to be an unsearched corner - is never flagged."""
        record = self._claim("Checkout calls the basket. There is no retry limit in the store.")
        self.assertTrue(record["absence_claim"])
        self.assertEqual(record["absence_sentence"], "There is no retry limit in the store.")

    def test_absence_words_inside_a_code_span_are_not_a_claim(self):
        """Break: matching "is not found" inside a backticked log message, which is
        the answer quoting code rather than stating that anything is absent."""
        record = self._claim("Checkout logs `order is not found` and retries three times.")
        self.assertFalse(record["absence_claim"])
        self.assertIsNone(record["absence_sentence"])

    def test_a_code_span_holding_a_sentence_break_is_not_a_claim(self):
        """Break: splitting sentences before code spans are masked. The span is cut at
        its full stop, neither half is a span any more, and the quoted log message
        reads as a claim that something is not found."""
        record = self._claim("It logs `fail. not found here` and moves on.")
        self.assertFalse(record["absence_claim"])
        self.assertIsNone(record["absence_sentence"])


class HeadlessCost(AnswersStage):
    def test_dollars_come_only_from_a_matching_headless_result(self):
        """Break: attaching a run's total_cost_usd to an answer it did not price, or
        inventing a price for transcript usage. One session holds one answer and
        has a result; the other holds two, and its result prices neither."""
        root = self._dir()
        session_one().write(root / f"{S1}.jsonl")
        t = Transcript(S4)
        t.prompt(Q_CANCEL)
        t.text("m1", _usage(1, 0, 10, 1), "Forged.")
        t.write(root / f"{S4}.jsonl")
        (root / "run.json").write_text(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 1,
                    "result": "Forged.",
                    "session_id": S4,
                    "total_cost_usd": 0.42,
                    "usage": _usage(1, 0, 10, 1),
                }
            ),
            encoding="utf-8",
        )
        # A result for S1 too, which holds two answers: it prices the whole run, so
        # it cannot be put on either of them.
        (root / "other.json").write_text(
            json.dumps({"type": "result", "session_id": S1, "total_cost_usd": 1.5}),
            encoding="utf-8",
        )
        vocabulary = check_citations.load_vocabulary(self._store() / "graphify-out" / "graph.json")
        run = answers.measure([root], self._store(), vocabulary)
        records = run.records()
        self.assertEqual([r.get("cost_usd") for r in records], [None, None, 0.42])
        self.assertNotIn("cost_usd", records[0])
        self.assertEqual(run.results_unmatched, 1)


class CommandLine(AnswersStage):
    def test_without_out_it_prints_a_summary_and_writes_nothing(self):
        """Break: writing a default file the operator did not name, or a summary
        that does not end on what it cannot measure."""
        root = self._dir()
        store = self._store()
        session_one().write(root / f"{S1}.jsonl")
        before = sorted(p for p in root.rglob("*"))
        code, out = self._run(str(root), "--store", str(store))
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p for p in root.rglob("*")), before)
        self.assertIn("2 answers", out)
        self.assertIn("input", out)
        self.assertIn("18,412", out)  # 12,208 + 6,204
        self.assertTrue(out.rstrip().endswith(answers.LIMIT), out)

    def test_out_writes_one_record_per_answer(self):
        """Break: --out writing the summary rather than the records."""
        root = self._dir()
        store = self._store()
        session_one().write(root / f"{S1}.jsonl")
        target = root / "out" / "answers.jsonl"
        code, out = self._run(
            str(root / f"{S1}.jsonl"), "--store", str(store), "--out", str(target)
        )
        self.assertEqual(code, 0, out)
        written = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["question"] for r in written], [Q_SUBMIT, Q_RETRY])

    def test_each_summary_note_sits_on_its_own_quantity(self):
        """Break: attaching the notes by line position, so reordering the token fields
        puts "a floor" on a quantity that is exact and leaves output unqualified."""
        root = self._dir()
        session_one().write(root / f"{S1}.jsonl")
        code, out = self._run(str(root), "--store", str(self._store()))
        self.assertEqual(code, 0, out)
        lines = {line.split()[0]: line for line in out.splitlines() if line.startswith("  ")}
        self.assertIn("a floor", lines["output_floor"])
        self.assertIn("cache reads", lines["input"])
        for name in ("cache_read", "cache_write", "turns", "tool_calls"):
            self.assertNotIn("a floor", lines[name])
            self.assertNotIn("cache reads", lines[name])

    def test_reading_no_answer_exits_two(self):
        """Break: reporting zeroes as a measurement when no session was read."""
        root = self._dir()
        (root / "notes.txt").write_text("forged", encoding="utf-8")
        code, out = self._run(str(root), "--store", str(self._store()))
        self.assertEqual(code, 2, out)

    def test_a_store_with_no_graph_exits_two(self):
        """Break: classing every citation unresolved against an empty graph."""
        root = self._dir()
        session_one().write(root / f"{S1}.jsonl")
        code, out = self._run(str(root), "--store", str(self._dir()))
        self.assertEqual(code, 2, out)
        self.assertIn("graph", out)


class Comparing(AnswersStage):
    def _write(self, name: str, sessions: list[list[tuple[str, int]]]) -> Path:
        """Run the real stage over forged sessions and keep its --out file.

        Each session is a list of (question, cache read); each answer is one turn of
        1 + 0 + read input, so its input is read + 1.
        """
        root = self._dir()
        for s, questions in enumerate(sessions):
            sid = f"{s:08d}-{name[:4]:x<4}-4bbb-8ccc-000000000000"
            t = Transcript(sid)
            for q, (question, read) in enumerate(questions):
                t.prompt(question)
                t.text(f"msg_{name}_{s}_{q}", _usage(1, 0, read, 1), "Forged.")
            t.write(root / f"{sid}.jsonl")
        target = root / f"{name}.jsonl"
        code, out = self._run(str(root), "--store", str(self._store()), "--out", str(target))
        self.assertEqual(code, 0, out)
        return target

    def test_an_unpaired_question_is_listed_on_its_own_side(self):
        """Break: comparing only the paired questions and saying nothing of the rest,
        so a question dropped from the second run vanishes from the comparison."""
        before = self._write("before", [[(Q_SUBMIT, 99), (Q_RETRY, 9)]])
        after = self._write("after", [[(Q_SUBMIT, 199), (Q_CANCEL, 9)]])
        comparison = answers.compare(answers.load(before), answers.load(after))
        self.assertEqual(comparison.only_before, [Q_RETRY])
        self.assertEqual(comparison.only_after, [Q_CANCEL])
        code, out = self._run("--compare", str(before), str(after))
        self.assertEqual(code, 0, out)
        self.assertIn(Q_RETRY, out)
        self.assertIn(Q_CANCEL, out)
        self.assertTrue(out.rstrip().endswith(answers.LIMIT), out)

    def test_a_malformed_record_file_is_refused_cleanly(self):
        """Break: a record file missing a field raising a traceback out of --compare
        instead of a refusal naming the file, with exit 2."""
        before = self._write("before", [[(Q_SUBMIT, 99)]])
        broken = self._dir() / "broken.jsonl"
        broken.write_text(json.dumps({"question": Q_SUBMIT}) + "\n", encoding="utf-8")
        code, out = self._run("--compare", str(before), str(broken))
        self.assertEqual(code, 2, out)
        self.assertIn("broken.jsonl", out)
        self.assertNotIn("Traceback", out)

    def test_a_repeated_question_is_compared_as_its_median(self):
        """Break: comparing the first repetition only, or summing them.

        Before: Q_SUBMIT once at 100 input. After: three times, at 300, 500 and 1,000
        input - median 500, so the change is +400 (first-only would give +200, a sum
        +1,700). Q_RETRY is asked once each side, 10 -> 30, a change of +20.
        Median of the two changes: 210. Total: 420.
        """
        before = self._write("before", [[(Q_SUBMIT, 99), (Q_RETRY, 9)]])
        after = self._write(
            "after", [[(Q_SUBMIT, 299), (Q_RETRY, 29)], [(Q_SUBMIT, 499)], [(Q_SUBMIT, 999)]]
        )
        comparison = answers.compare(answers.load(before), answers.load(after))
        self.assertEqual(comparison.changes["input"], (210, 420))
        self.assertEqual(comparison.repeated, [(Q_SUBMIT, 1, 3)])
