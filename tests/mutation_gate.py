"""Assert that the suite fails when the product is broken.

A test suite reports two things with the same output: that the product works,
and that nothing is checking it. `0 errors` is printed either way. Every gap
found in this library over one week was of the second kind — the behaviour was
tested and the *call site* was not, or the default that ships was never
exercised — and every one was caught by hand, after the code was written.

This gate closes that by construction. Each entry below is a **real** defect
that was written, passed review, and was caught late. The value is entirely in
that: an invented mutation proves a test can fail, a real one proves the suite
would have stopped the thing that actually happened.

    python3 tests/mutation_gate.py                   # all mutations
    python3 tests/mutation_gate.py --list            # names, targets, observers
    python3 tests/mutation_gate.py --only recovery   # entries whose name matches
    python3 tests/mutation_gate.py --derive-mapping  # the observers, from a full run
    python3 tests/mutation_gate.py --verify-mapping  # check every entry's observers

Each entry names the test modules that observe it, and a run applies the
mutation and runs only those. The whole suite per entry cost 8.7 seconds against
0.6 for a module, which is the difference between 26 minutes and 2 - but the
mapping is worth more than the time. An entry that names its observer can be
falsified by a later reader; a gate that merely claims to be sensitive cannot.

The two slow modes are what keep the mapping honest, and neither runs in the
`tests` job: `--derive-mapping` fills in a new entry from a full suite run, and
`--verify-mapping` applies every entry against the whole suite and refuses a
named set that does not match the modules that failed. The `tests` workflow runs
the second one on its schedule and on `workflow_dispatch`, in the same job that
runs the fast mode on a pull request.

Every conclusion here rests on the suite reading this tree, and it is asserted
rather than assumed. The suite runs in `tests/`, so a relative `PYTHONPATH=src`
resolves to `tests/src` in the child, which does not exist, and the child imports
whatever `knowledgestore` is installed instead - after which no mutation of this
tree is visible to any run. A run therefore starts by asking a child where it
imports the package from, and refuses if the answer is not this tree's `src/`.

A run that is killed can leave a mutation applied, because SIGKILL cannot be
handled: `apply` therefore records the original bytes in
`tests/.mutation-gate-recovery` before touching a file, and the next run restores
from that record before it does anything else.

A surviving mutation is a failure of this gate, not a curiosity: it means the
behaviour it describes could be removed today and the suite would stay green.

There are two mutation gates, and this is the Python one. The answer gate that
ships to store operators has its own, `tests/explorer/answer-regression-mutations.mjs`,
because its defects live in the shipped `.mjs` runner and are only observable by
breaking a built page. Three successive versions of that runner's ticket predicate
passed every assertion here and in the page regression while failing to notice a
dead file-to-ticket join. Neither gate covers the other.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "knowledgestore"

# What a killed run leaves behind: the module it had mutated and the original
# bytes, written before the mutation and deleted after the restore. SIGKILL
# cannot be handled, so nothing in this process can put the file back - only
# something already on disk can.
RECOVERY_PATH = ROOT / "tests" / ".mutation-gate-recovery"

# The signals that end the process without unwinding, so a `finally` never
# runs: a timeout, a job kill and a cancelled CI step all send SIGTERM. SIGINT
# is absent because CPython already raises KeyboardInterrupt for it.
TERMINATION_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
)


@dataclass(frozen=True)
class Mutation:
    """One real defect, the escape it represents, and what observes it.

    `observers` names the test modules that fail when this entry is applied, and
    they are the only ones the gate runs for it. Two things follow, and the
    second is the reason the field exists rather than a consequence of it:

    - a run costs one module rather than the whole suite, which took 8.7 seconds
      per entry and 26 minutes over the table;
    - the entry becomes falsifiable by a later reader. "This gate is
      sensitivity-checked" cannot be checked; "this behaviour is observed by
      `test_x`" can, by `--verify-mapping`, which runs the whole suite and
      refuses a set that does not match.

    Derived rather than typed: `--derive-mapping` prints what actually failed.
    A hand-written name that no test module holds, or holds without observing,
    is invisible from a passing run - the gate would still print `caught`.
    """

    name: str
    module: str
    find: str
    replace: str
    escaped_as: str
    # Defaulted, not required, though every shipped entry must name observers and
    # `check_mapping` refuses one that does not. A required argument turns an entry
    # written against the older five-field form - which is what every branch opened
    # before this change still carries - into a TypeError at import, which takes
    # five unrelated test modules down with it and says nothing about the cause.
    # Defaulted, the same entry imports and fails one named check that says which
    # entry is unmapped and which command fills it in. The refusal is the same
    # strictness, delivered where it can be acted on.
    observers: tuple[str, ...] = ()


# Ordered by the escape they represent rather than by module, because the
# categories are the finding: wiring never asserted, and shipped defaults never
# exercised. Both classes passed review repeatedly.
MUTATIONS = (
    Mutation(
        "gzipped graph unreadable again",
        "io.py",
        'if path.suffix == ".gz":',
        "if False:",
        "shipped in v0.12.0 and found by an operator: `record-clustering --graph "
        "graph.json.gz` died on the gzip magic byte, so the escape hatch the stage's "
        "own warning names was unavailable for the only artefact that store ships",
        ("test_config_and_io", "test_read_path_policy"),
    ),
    Mutation(
        "graph loader stops reading the archive",
        "io.py",
        '    """\n    return _read_json_file(path)\n',
        '    """\n    return json.loads(path.read_text(encoding="utf-8"))  # NOSONAR(S8707)\n',
        "the same defect one function along, and it shipped because the first fix "
        "looked complete: the dispatch went into `read_json` while `load_graph` read "
        "the same artefact for the explorer, deep dives, topic briefs, community "
        "summaries and `status --verify-graph`, so a store holding only its committed "
        "archive could not be read by any of them and no call site said why. The "
        "replacement is the code that was there, so this is the shipped defect rather "
        "than an invented one",
    ),
    Mutation(
        "stale counterpart no longer named",
        "record_clustering.py",
        "        disagreement = counterpart_disagreement(config.GRAPH_PATH, (communities, clustered))",
        '        disagreement = ""',
        "the same operator had to diff two graph files by hand to find that the "
        "record described a stale leftover; every count in it reconciled",
        ("test_clustering_partitioner",),
    ),
    Mutation(
        "summaries snapshot no longer names a stale graph",
        "build_community_summaries.py",
        '    print(_graph_disagreement(members), end="", file=sys.stderr)',
        "    pass",
        "the same stale-graph class that `record-clustering` already warned about "
        "reached `summaries snapshot`, where a snapshot keyed to a leftover "
        "graph.json mis-keys the entire remap carry; reported by a store operator, "
        "and invisible to `_remap_refusal` because the snapshot and the stale file "
        "share every node id",
        ("test_stale_remedy_directions", "test_summaries_graph_ambiguity"),
    ),
    Mutation(
        "summaries remap no longer names a stale graph",
        "build_community_summaries.py",
        '    print(_graph_disagreement(_membership({"nodes": nodes})), end="", file=sys.stderr)',
        "    pass",
        "the second call site of the same warning, and the one that rewrites the "
        "committed summaries file - the snapshot half being covered is exactly how "
        "a half-wired check reads as done",
        ("test_summaries_graph_ambiguity",),
    ),
    Mutation(
        "explorer built from a stale graph in silence",
        "build_explorer.py",
        '    print(\n        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "explorer.html"),\n        end="",\n        file=sys.stderr,\n    )',
        "    pass",
        "the page is tracked and ships to readers who have no graph and no CLI to "
        "check it against; an operator hit the same shape when a refresh embedded a "
        "stale semantic layer beside a newly built graph and every gate passed",
        ("test_summaries_graph_ambiguity",),
    ),
    Mutation(
        "deep dives built from a stale graph in silence",
        "build_deep_dives.py",
        '    print(\n        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "dives.json"),\n        end="",\n        file=sys.stderr,\n    )',
        "    pass",
        "dives.json is a committed artefact, so a stale read is published rather than merely reported",
        ("test_summaries_graph_ambiguity",),
    ),
    Mutation(
        "semantic layer built from a stale graph in silence",
        "build_semantic_index.py",
        '    print(\n        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "the semantic layer"),\n        end="",\n        file=sys.stderr,\n    )',
        "    pass",
        "the layer is committed and the explorer page embeds it, so the stale read reaches the page twice over",
        ("test_summaries_graph_ambiguity",),
    ),
    Mutation(
        "the estate check runs against a stale graph in silence",
        "build_community_summaries.py",
        '    print(\n        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "the estate check"),\n        end="",\n        file=sys.stderr,\n    )',
        "    pass",
        "the strongest form of the class: a truthfulness gate reading the wrong artefact passes on the wrong data, and its silence then licenses a claim about something it never looked at",
        ("test_summaries_graph_ambiguity",),
    ),
    Mutation(
        "upward write paths accepted again",
        "io.py",
        '    if any(part == ".." for part in Path(path).parts):',
        "    if False:",
        "reported by SonarCloud as pythonsecurity:S8707 on write_json: a path "
        "assembled from CLI arguments reached write_text unvalidated, so whatever "
        "built those arguments chose where the process wrote",
        ("test_io_write_targets", "test_read_path_policy"),
    ),
    Mutation(
        "the write guard applied to the reads",
        "io.py",
        "    if not path.exists():\n        return default\n    return _read_json_file(path)",
        "    checked_write_target(path)\n    if not path.exists():\n"
        "        return default\n    return _read_json_file(path)",
        "the one-line answer to the same rule's second report - S8707 on read_json, "
        "code-scanning alert 53 - which this library rejects: the guard is in the "
        "module already, so calling it on a read is one line, and it then refuses "
        "every relative path an operator types from a subdirectory. Confining reads "
        "instead of suppressing was measured twice on the write side, at 48 and 4 "
        "failing tests, and nothing on the read path observed either until "
        "test_read_path_policy; with this applied the whole suite failed only there",
        ("test_read_path_policy",),
    ),
    Mutation(
        "deployments overwrites the committed graph from a stale one",
        "build_deployments.py",
        "    refusal = graph_files.stale_refusal(config.GRAPH_PATH)",
        '    refusal = ""',
        "#198: this stage reads the uncompressed graph and writes both files "
        "back, so a leftover graph.json overwrites the committed archive and "
        "loses its clustering - the only failure in this class that destroys an "
        "artefact rather than describing one wrongly",
        ("test_stale_graph_refusal",),
    ),
    Mutation(
        "one repository's several components lose their deployment joins",
        "build_deployments.py",
        "        candidates = sorted(exact or loose, key=lambda r: (len(r), r)) or _prefix_claims(\n            bare, repos\n        )",
        "        candidates = sorted(exact or loose, key=lambda r: (len(r), r))",
        "#88: the two older name rules cannot see a repository named `alpha` "
        "shipping `alpha-agent`, `alpha-backend` and `alpha-frontend`, so it "
        "joined the service named exactly `alpha` and none of the components it "
        "also ships. Their deployment configuration then reached no code in the "
        "graph - a missing edge, which nothing in the report distinguishes from a "
        "service nobody deploys",
        ("test_deployment_prefix_match",),
    ),
    Mutation(
        "the deployment leading-run rule runs before the rules it must not move",
        "build_deployments.py",
        "        candidates = sorted(exact or loose, key=lambda r: (len(r), r)) or _prefix_claims(\n            bare, repos\n        )",
        "        candidates = _prefix_claims(bare, repos) or sorted(\n            exact or loose, key=lambda r: (len(r), r)\n        )",
        "#88: the rule is additive only because it runs last. Consulted first it "
        "re-points joins the segment and substring rules already made - "
        "`delta-agent` from `svc-deltaagent` to `delta` - which is not a new join "
        "but the same configuration silently attached to different code",
        ("test_deployment_prefix_match",),
    ),
    Mutation(
        "the deployment join crosses a word boundary again",
        "build_deployments.py",
        "if len(_norm(r)) >= MIN_PREFIX_REPO and _norm(r) in runs]",
        "if len(_norm(r)) >= MIN_PREFIX_REPO and _norm(name).startswith(_norm(r))]",
        "#88 as first written: a character prefix rather than whole segments, so "
        "`alpha` claimed `alphabet-agent` wherever the repository named `alphabet` "
        "was absent from the graph. That is a deployment relationship nothing "
        "declared - valid on the page, wrong, and counted as a join, so it raised "
        "the match rate rather than showing as the gap it is. The floor cannot "
        "catch it: `alpha` is above the floor",
        ("test_deployment_prefix_match",),
    ),
    Mutation(
        "the deployment leading-run rule matches in both directions",
        "build_deployments.py",
        "if len(_norm(r)) >= MIN_PREFIX_REPO and _norm(r) in runs]",
        "if len(_norm(r)) >= MIN_PREFIX_REPO and _norm(name) in _leading_runs(r)]",
        "#88: the runs come from the service, and splitting the repository instead "
        "inverts the join - a service named `alpha` would take a repository named "
        "`alpha-agent`, attaching one component's configuration to another's code, "
        "and the components the rule exists for lose their joins in the same move",
        ("test_deployment_prefix_match",),
    ),
    Mutation(
        "a short repository name collects a whole naming convention",
        "build_deployments.py",
        "if len(_norm(r)) >= MIN_PREFIX_REPO and _norm(r) in runs]",
        "if _norm(r) in runs]",
        "#88: whole segments removed the coincidence the floor was first measured "
        "against, and this is what it protects now that the segment rule does not "
        "need it. There the entire stem matched; here a leading run did, so at one "
        "to three characters the claimant is convention vocabulary (`ops-`, `ui-`) "
        "and a repository with that name takes every service behind the prefix",
        ("test_deployment_prefix_match",),
    ),
    Mutation(
        "the deployment join normalises before it segments",
        "build_deployments.py",
        '    segments = [_norm(part) for part in name.split("-")]',
        '    segments = _norm(name).split("-")',
        "#88, and the defect this change turns on: `_norm` strips every separator, "
        'so normalising first leaves nothing to segment on and "whole segments" '
        "silently means nothing. The runs vanish, and the repair anyone reaches for "
        "is a character prefix - which is how `alpha` came to claim "
        "`alphabet-agent`, a deployment relationship nothing declared. Reported by "
        "a store operator reading the implementation rather than its tests",
        ("test_deployment_prefix_match",),
    ),
    Mutation(
        "the more specific repository loses the deployment claim",
        "build_deployments.py",
        "    return sorted(claiming, key=lambda r: (-len(_norm(r)), r))",
        "    return sorted(claiming, key=lambda r: (len(_norm(r)), r))",
        "#88: shortest-first is the order the two rules above use, so it is the "
        "one line to copy by mistake. Both `alpha` and `alpha-agent` are leading "
        "runs of `alpha-agent-worker`, and the shorter hands one real "
        "repository's production configuration to another",
        ("test_deployment_prefix_match",),
    ),
    Mutation(
        "two deployment runs disagree about which repository was claimed",
        "build_deployments.py",
        "    return sorted(claiming, key=lambda r: (-len(_norm(r)), r))",
        "    return sorted(claiming, key=lambda r: -len(_norm(r)))",
        "#88: claimants tie on length only by normalising to the same string, so "
        "`alpha-core` and `alphacore` tie and the winner without the name key is "
        "whatever the hash seed put first in a set. Two runs of one build then "
        "write the deployment edge to different nodes, and the committed graph "
        "diff is the only place it is visible",
        ("test_deployment_prefix_match",),
    ),
    Mutation(
        "a service no repository could match reads as a matcher gap",
        "build_deployments.py",
        "    if tally.out_of_reach:",
        "    if False:",
        "#88: a deployment repository configures third-party components and report "
        'jobs whose code is in no repository here, and reported as "matched no '
        'repository" they send an operator to tune a matcher that cannot reach '
        "them. Measured on one estate, that was every unmatched service, so the "
        "instruction the report gave was wrong for all of them and the match rate "
        "read as a matcher missing half its joins",
        ("test_deployment_prefix_match",),
    ),
    Mutation(
        "a shared abbreviation counts as a repository worth looking at",
        "build_deployments.py",
        "if len(word) >= _MIN_WORD}",
        "if word}",
        "#88: `api` and `ops` are shared vocabulary rather than names, so counting "
        "them as name material hides the scope fact behind a coincidence - the "
        "expensive direction, because a suppressed scope fact reads as matcher work "
        "that can be done",
        ("test_deployment_prefix_match",),
    ),
    Mutation(
        "packages overwrites the committed graph from a stale one",
        "build_package_edges.py",
        "    refusal = graph_files.stale_refusal(config.GRAPH_PATH)",
        '    refusal = ""',
        "#198: this stage reads the uncompressed graph and writes both files "
        "back, so a leftover graph.json overwrites the committed archive and "
        "loses its clustering - the only failure in this class that destroys an "
        "artefact rather than describing one wrongly",
        ("test_stale_graph_refusal",),
    ),
    Mutation(
        "gherkin overwrites the committed graph from a stale one",
        "extract_gherkin.py",
        "    refusal = graph_files.stale_refusal(config.GRAPH_PATH)",
        '    refusal = ""',
        "#198: this stage reads the uncompressed graph and writes both files "
        "back, so a leftover graph.json overwrites the committed archive and "
        "loses its clustering - the only failure in this class that destroys an "
        "artefact rather than describing one wrongly",
        ("test_stale_graph_refusal",),
    ),
    Mutation(
        "the stale refusal stops saying which of the two files is stale",
        "graph_files.py",
        '        f"{stale_direction(described, other)}\\n"',
        '        ""',
        "#243: the refusal named one way out - decompress the archive over "
        "graph.json, or remove graph.json - and both destroy graph.json, so both "
        "are right only if the archive is the good copy. Mid-rebuild it is the "
        "other way round and the reader is told to discard the fresh merge, which "
        "cost a full extraction pass; the command exits 0, so nothing reports the "
        "loss. Reported firing on three stages in one documented sequence. Without "
        "this sentence the message names two directions and no way to tell them "
        "apart, which is a guess dressed as a choice",
        ("test_stale_remedy_directions",),
    ),
    Mutation(
        "layer merge re-points edges at unrelated nodes",
        "merge_layers.py",
        "        renamed[node_id] = new_id",
        "        renamed[node_id] = node_id",
        "#129: the documented route discards the semantic node on an id collision "
        "and concatenates its edges anyway, so every relationship it asserted about "
        "one entity becomes an assertion about another - 98 collisions carrying 311 "
        "edges on one estate, and the graph builds cleanly",
        ("test_merge_layers",),
    ),
    Mutation(
        "layer merge keeps an edge whose endpoint is in neither layer",
        "merge_layers.py",
        '        if str(moved["source"]) not in by_id or str(moved["target"]) not in by_id:',
        "        if False:",
        "guessing at a dangling endpoint is how a concatenation invents "
        "relationships; dropping it silently would be the same defect one step on, "
        "which is why the count is asserted too",
        ("test_merge_layers",),
    ),
    Mutation(
        "layer merge accepts an empty layer",
        "merge_layers.py",
        "    if not ast_nodes or not sem_nodes:",
        "    if False:",
        "every stage in this library that shipped doing nothing did so with a "
        "passing suite, and an empty input layer merges to a smaller graph that "
        "looks like a successful run",
        ("test_merge_layers",),
    ),
    Mutation(
        "most-connected report unwired",
        "status.py",
        "    _report_central(arguments.central)",
        "    pass",
        "#112: reporting through the function while nothing drives the CLI is the "
        "most repeated escape in this repository, and three existing entries in this "
        "module are the same shape",
        ("test_most_connected",),
    ),
    Mutation(
        "near-duplicate report unwired",
        "status.py",
        "    _report_duplicates(arguments.duplicates)",
        "    pass",
        "#246: two repositories on one estate were near-copies of two others and no "
        "count the pipeline reported made it visible; a report that never reaches the "
        "CLI is the same silence with more code, and this is the most repeated escape "
        "in this repository",
        ("test_duplicate_repositories",),
    ),
    Mutation(
        "near-duplicate ranking loses its name tiebreak",
        "duplicate_repositories.py",
        "        best.sort(key=lambda o: (-o.fraction, o.left, o.right))",
        "        best.sort(key=lambda o: -o.fraction)",
        "the sort is stable, so without the tiebreak two equally overlapping pairs "
        "print in whatever order the set sizes happened to evaluate them - two runs "
        "on one store then differ, which hash-ordered iteration has already done here "
        "once and which is invisible until somebody diffs two builds",
        ("test_duplicate_repositories",),
    ),
    Mutation(
        "near-duplicate pruning stops being an exact bound",
        "duplicate_repositories.py",
        "        (-(min(sizes[a], sizes[b]) / max(sizes[a], sizes[b])), a, b)",
        "        (-((min(sizes[a], sizes[b]) / max(sizes[a], sizes[b])) ** 2), a, b)",
        "the whole report rests on `|A n B| <= min(|A|, |B|)` being an upper bound: "
        "anything that shrinks it stops the walk before a pair that would have "
        "ranked, and the skipped near-copy reads as a clean estate - the exact "
        "failure #246 was raised about",
        ("test_duplicate_repositories",),
    ),
    Mutation(
        "near-duplicate report finds something on an estate with nothing",
        "duplicate_repositories.py",
        "    if not report.overlaps:\n        return []",
        "    if False:\n        return []",
        "the sensitivity control: a report that always prints a top ten is a report "
        "nobody reads, and this library has already shipped a check whose clean "
        "verdict was printed over nothing it had read",
        ("test_duplicate_repositories",),
    ),
    Mutation(
        "most-connected loses its edge-key fallback",
        "graph_files.py",
        "    if not found:",
        "    if False:",
        "graphify writes `links` in node-link JSON and `edges` in its extract files, "
        "so reading one key silently ranks nothing on half the artefacts this is "
        "pointed at - indistinguishable from a graph with no edges",
        ("test_most_connected",),
    ),
    Mutation(
        "the committed archive is no longer read when the plain graph is absent",
        "graph_files.py",
        "    return other if other is not None and other.is_file() else None",
        "    return None",
        "#262: reported from a store running 0.15.2 - `status --duplicates` and "
        "`--central` refused with `no graph at .../graph.json` on a store holding "
        "only the file it commits, which is the state a store spends most of its "
        "life in and the state an operator reaches for `--duplicates` from; both "
        "readers already accepted the `.gz` and only the guard refused, and the "
        "way out was a multi-gigabyte gunzip for a report documented as cheap",
        ("test_graph_to_read", "test_summaries_membership_drift"),
    ),
    Mutation(
        "the archive is preferred over the graph a run has just written",
        "graph_files.py",
        "    if preferred.is_file():\n        return preferred",
        "    if False:\n        return preferred",
        "the fallback's other direction: mid-pipeline the plain file is the fresh "
        "merge and the archive beside it is the previous build, so reading the "
        "archive there reports on the graph the run has already superseded - the "
        "rebuild case an operator reported when three stages refused in sequence",
        (
            "test_duplicate_repositories",
            "test_graph_to_read",
            "test_most_connected",
            "test_summaries_membership_drift",
        ),
    ),
    Mutation(
        "the most-connected ranking stops naming the graph it read",
        "status.py",
        '        f"Most connected nodes in {graph.name} (would you name these if asked what the "',
        '        "Most connected nodes (would you name these if asked what the "',
        "a store holds two graph files that can disagree, and centrality is "
        "degree-driven, so the two rank differently; the same silence about which "
        "file was read is what made an operator diff two graphs by hand after a "
        "stage reported confidently on a leftover",
        ("test_graph_to_read",),
    ),
    Mutation(
        "the near-duplicate report stops naming the graph it read",
        "status.py",
        "        duplicate_repositories.near_duplicates(graph), described=graph.name",
        "        duplicate_repositories.near_duplicates(graph)",
        "the same escape on the other report: an overlap percentage from the "
        "committed archive and the same percentage from a stale leftover are "
        "different claims, and a header that does not name its file leaves the "
        "reader to guess which of a store's two graphs the estate is described from",
        ("test_graph_to_read",),
    ),
    Mutation(
        "estate check stops matching name segments",
        "build_community_summaries.py",
        "            if len(normalised) >= MIN_SEGMENT_MATCH and normalised in segments:",
        "            if False:",
        "#179: a whole-label match reported `NgRx` absent while the estate held it "
        "under eight scoped package names across 228 labels - a check that fires on "
        "an entire ecosystem's naming convention gets switched off",
        ("test_estate_segment_match",),
    ),
    Mutation(
        "the segment floor is removed",
        "build_community_summaries.py",
        "MIN_SEGMENT_MATCH = 3",
        "MIN_SEGMENT_MATCH = 1",
        "without a floor the looser match becomes a substring match in effect, and "
        "a two-character segment would corroborate a two-character claim - the "
        "false-negative direction, which fails reassuringly",
        ("test_estate_segment_match",),
    ),
    Mutation(
        "the stem basis option stops affecting ids",
        "merge_chunks.py",
        "    stem = spec_stem(basis, keep_extension=keep_extension)",
        "    stem = spec_stem(basis)",
        "#115: threading an option through and having it change nothing is the "
        "wiring escape this gate already records four times, and here it would "
        "leave a store believing it had adopted the extension basis",
        ("test_stem_basis",),
    ),
    Mutation(
        "the migration cost stops being counted",
        "merge_chunks.py",
        "    if spec_stem(basis, keep_extension=not keep_extension) != stem:",
        "    if False:",
        "#115 can only be decided on each estate's own number; the issue's figures "
        "are one estate's, and a cost nobody can measure locally is one nobody acts "
        "on",
        ("test_stem_basis",),
    ),
    Mutation(
        "AST ids stop being namespaced by repository",
        "merge_layers.py",
        '        new_id = f"{repository}::{node_id}"',
        "        new_id = node_id",
        "#115: graphify drops the repositories/<repo>/ segment for declarations "
        "inside a file, so one estate had one shared id across most of its "
        "repositories - a dedupe then makes it a single node adjacent to many "
        "unrelated services and the highest-degree node in the graph",
        ("test_merge_layers",),
    ),
    Mutation(
        "a cross-repository AST edge is attributed to one side",
        "merge_layers.py",
        "        if source_repo and target_repo and source_repo != target_repo:",
        "        if False:",
        "the fix is exact only while both endpoints belong to one repository; "
        "attributing an edge that spans two is the guess the rewrite exists to "
        "avoid",
        ("test_merge_layers",),
    ),
    Mutation(
        "iter_array matches a nested key again",
        "graph_stream.py",
        "        if self.depth == 1 and self.token_start >= 0:",
        "        if self.token_start >= 0:",
        "the depth test was repeated in three places and every one-line mutation of "
        "each survived, because the other two still blocked - so it is now one "
        "guard, which is what makes it testable. #210: a merged graph carries `graph.hyperedges[].nodes`, a list of id "
        "strings, before its top-level node array - so the iterator yielded strings, "
        "type-checking consumers saw nothing, and graph_counts returned (0, 0) on a "
        "fully clustered graph. Two guards built on those counts then read (0, 0) "
        "against (0, 0) as agreement, and one of them was a refusal protecting an "
        "irreversible overwrite. Shipped in v0.14.0",
        ("test_top_level_array",),
    ),
    Mutation(
        "a stale graph is ranked rather than refused",
        "build_community_summaries.py",
        "    if stale:",
        "    if False:",
        'reported from a real store: `summaries adrift` printed "One of them is '
        'stale" and then returned a verdict of 1, whose documented response is to '
        "re-take the snapshot and re-author what the report names - which on that "
        "store would have destroyed five thousand correct summaries. A read failure "
        "answered by rewriting prose is the worst outcome this check has",
        ("test_summaries_membership_drift",),
    ),
    Mutation(
        "nothing compares the snapshot to the graph",
        "build_community_summaries.py",
        '        if entry["share"] < bar:',
        "        if False:",
        "the gap #193 reports: the library writes the membership snapshot, requires it, and "
        "reports counts derived from it, and nothing checked that it still described the "
        "graph - so every summary could sit on a community it no longer describes with "
        "`status` reporting the same coverage either way",
        ("test_summaries_membership_drift",),
    ),
    Mutation(
        "summaries with no snapshot entry silently excluded",
        "build_community_summaries.py",
        '        "unsnapshotted": sorted(wanted - set(snap_sets), key=_by_id),',
        '        "unsnapshotted": [],',
        "the `if cid in snapshot` shape: prose that can be neither checked nor re-keyed - a "
        "remap cannot even withdraw it - dropped from the population, after which the count "
        "reads as though it had covered everything",
        ("test_summaries_membership_drift",),
    ),
    Mutation(
        "a graph carrying no membership reported as total drift",
        "build_community_summaries.py",
        "    if clustered / total < coverage:",
        "    if False:",
        "graphify holds the assignment in `community`, so a renamed key or a clustering step "
        "that printed success without writing its result makes every comparison fail; read as "
        "drift that would send someone re-authoring an entire store over a one-line read "
        "failure",
        ("test_summaries_membership_drift",),
    ),
    Mutation(
        "an id-space mismatch reported as moved membership",
        "build_community_summaries.py",
        "    if (graph_share >= NAMESPACED_SHARE) == (snapshot_share >= NAMESPACED_SHARE):",
        "    if True:",
        "a first implementation of this check elsewhere reported 58 communities adrift of "
        "which 57 were not, because the snapshot's ids were bare and the graph's carried a "
        "`<repo>::` prefix; naming it is what keeps the fix from being a looser comparison",
        ("test_summaries_membership_drift",),
    ),
    Mutation(
        "status leaves its summary count to be read as a verdict",
        "status.py",
        "    pointer = snapshot_pointer()",
        '    pointer = ""',
        "the count is identical whether the prose still describes its community or not, and an "
        "operator read exactly that line as healthy; `status` cannot read the graph, so naming "
        "the blind spot is the only honest thing it can do there",
        ("test_summaries_membership_drift",),
    ),
    Mutation(
        "declared boundary never reaches the manifest",
        "build_knowledge_context.py",
        "        *boundary.manifest_section(boundary.read()),",
        "        *[],",
        "the unwired-check class this library has shipped twice - the parse works, "
        "the rendering works, and the committed artefact a reader opens says none of "
        "it, which is indistinguishable from an estate that declared nothing",
        ("test_estate_boundary",),
    ),
    Mutation(
        "store stops saying it does not claim completeness",
        "boundary.py",
        '    return lines + [NO_COMPLETENESS, ""]',
        "    return lines",
        "a declaration that reads as `this is all of it` is a new false claim "
        "replacing the old silent one; the estate that prompted this had enumerated "
        "its hosts and was still hunting services with no locatable repository",
        ("test_estate_boundary",),
    ),
    Mutation(
        "declared repository with no ruling vanishes from the manifest",
        "boundary.py",
        "    subjects = sorted({*declared.rulings, *declared.snapshots, *declared.aliases.values()})",
        "    subjects = sorted(declared.rulings)",
        "written this way first, and found by re-reading the artefact rather than by a "
        "test: a repository declared only by a snapshot date or only by an alias parsed "
        "cleanly, was counted in the status summary, and reached no reader at all",
        ("test_estate_boundary",),
    ),
    Mutation(
        "status no longer says the boundary is undeclared",
        "status.py",
        "    _report_boundary(recorded)",
        "    pass",
        "silence is the state every store starts in, so a report that speaks only "
        "for the configured case never reaches the stores that most need telling "
        "what their own absences mean",
        ("test_estate_boundary",),
    ),
    Mutation(
        "off-host name stops resolving to the repository held",
        "boundary.py",
        "        target = aliases.get(name, name)",
        "        target = name",
        "the false absence the declaration exists to remove, reintroduced inside it: "
        "a ruling written under the off-host name keys itself under a name no store "
        "holds, so `status` reports a held repository as missing",
        ("test_estate_boundary",),
    ),
    Mutation(
        "declaration stops being reconciled against disk",
        "status.py",
        "    disagreements = boundary.reconciliation(declared, set(recorded))",
        "    disagreements = {k: [] for k in ('active_absent', 'ruled_out_held', 'alias_absent')}",
        "a declaration nothing checks is a second artefact that can be quietly "
        "wrong, and a repository ruled live and not held is the exact shape of the "
        "published finding that was drawn honestly and was false",
        ("test_estate_boundary",),
    ),
    Mutation(
        "fan-out progress derived from the dispatch log again",
        "chunk_status.py",
        "    done = sorted(plan_ids & on_disk)",
        "    done = sorted(plan_ids & dispatched)",
        "#131: the defect this stage exists to remove, and it happened in an "
        "operator's own tally rather than here - a coverage gap of ninety-odd chunks "
        "announced by diffing the plan against a log that did not cover the early "
        "rounds, and a redundant round of a dozen agents launched for it. Every "
        "extraction was on disk the whole time",
        ("test_chunk_status", "test_fanout_dispatch_guidance"),
    ),
    Mutation(
        "never-sent folded back into in-flight",
        "chunk_status.py",
        "    never_sent = sorted(outstanding - dispatched)",
        "    never_sent = []",
        "#131: the concurrency ceiling rejects rather than queues, so the two causes "
        "of 'no output' need opposite responses - and merging them is what left a run "
        "of rejected low-numbered chunks sitting behind ninety higher-numbered ids "
        "under plan-ordered dispatch",
        ("test_chunk_status", "test_fanout_dispatch_guidance"),
    ),
    Mutation(
        "corrupt log tokens counted rather than reported",
        "chunk_status.py",
        "        if candidate in plan_ids:",
        "        if True:",
        "#131: appending batch files that carried no trailing newline fused the last "
        "id of one onto the first of the next; counted, those tokens inflated `in "
        "flight` and deflated `NEVER SENT` for several rounds while every total "
        "stayed plausible. A status tool that launders a corrupt log into a confident "
        "number is worse than no tool, because it is trusted",
        ("test_chunk_status",),
    ),
    Mutation(
        "never-sent asserted where it cannot be known",
        "chunk_status.py",
        "    if not had_log:",
        "    if False:",
        "written in this change and caught by its own test before review: with no "
        "log every outstanding chunk falls out of `classify` as never-sent, and "
        "printing that as a finding tells an operator to redispatch work in progress "
        "- the opposite error, and equally expensive",
        ("test_chunk_status",),
    ),
    Mutation(
        "an unusable chunk file counted as progress",
        "chunk_status.py",
        '        if "nodes" not in payload:',
        "        if False:",
        "#131: an agent killed mid-write and an agent that hit the output limit both "
        "leave a file, so a reader that counts files reports the chunk extracted and "
        "it is never redone. `merge-chunks` refuses the same file, so the gap would "
        "surface only once the archive had been assembled",
        ("test_chunk_status",),
    ),
    Mutation(
        "a truncated chunk file aborts the report",
        "chunk_status.py",
        "        except (json.JSONDecodeError, UnicodeDecodeError, OSError):",
        "        except (KeyError,):",
        "`io.read_json_dict` raises on malformed JSON - correct for a stage that "
        "cannot proceed, fatal for the one stage whose job is to describe the mess. "
        "One truncated file would take the whole progress report with it, at the "
        "moment it is most needed",
        ("test_chunk_status",),
    ),
    Mutation(
        "the chunk plan counted as an extraction",
        "chunk_status.py",
        '        if path.name.endswith("_plan.json"):',
        "        if False:",
        "`.graphify_chunk_plan.json` matches `.graphify_chunk_*.json`, so the stage's "
        "own denominator would arrive as a completed chunk - a wrong numerator and a "
        "wrong denominator at once. `merge-chunks` carries the same guard, which is "
        "why it is worth having twice",
        ("test_chunk_status",),
    ),
    Mutation(
        "progress estimated with no plan to measure against",
        "chunk_status.py",
        "    if not plan:",
        "    if False:",
        "the plan is the only map from chunk number to file list, so without it "
        "there is no denominator and nothing to name as missing. Reporting `0 of 0` "
        "reads as a finished fan-out",
        ("test_chunk_status",),
    ),
    Mutation(
        "content set written empty",
        "build_content_set.py",
        "    if not content:",
        "    if False:",
        "an empty path list makes every search over it return no matches, and no "
        "matches reads as a confident answer about the estate rather than as a "
        "missing detect result - the exact failure mode the artefact exists to end",
        ("test_content_set", "test_refusal_remedies_are_runnable"),
    ),
    Mutation(
        "refusal sends an operator to a step nothing provides",
        "build_chunk_plan.py",
        'f"No detection results at {config.DETECT_PATH}. {content_set.DETECT_PRODUCER} "',
        'f"No detection results at {config.DETECT_PATH}. graphify writes this when it '
        'scans the corpus, so run its detect step first. "',
        "the wording that shipped, and the reason #236 was raised: there is no "
        "`graphify detect` subcommand and `graphify update` at the store root writes "
        "no detect result, so the remedy named a step an operator could not perform "
        "and the only route to the file was undocumented",
        ("test_refusal_remedies_are_runnable",),
    ),
    Mutation(
        "noise figure claimed from a tree nobody measured",
        "build_content_set.py",
        '    if not tree:\n        return {"measured": False}',
        '    if False:\n        return {"measured": False}',
        "a sparse clone has no corpus, so the tree count is zero and zero renders "
        "as 'no noise' - the most flattering reading of the least measured case, on "
        "the one artefact whose whole purpose is to say the tree is mostly noise",
        ("test_content_set",),
    ),
    Mutation(
        "content files counted as noise",
        "content_set.py",
        "        if path in held:",
        "        if path in bearing:",
        "written and caught during #213: `bearing` holds directories, so no file "
        "path is ever in it and every content file was tallied as noise. The "
        "percentage came out higher, which is the direction that gets believed",
        ("test_content_set",),
    ),
    Mutation(
        "noise attributed to the file's own directory",
        "content_set.py",
        "    for depth in range(2, len(parts)):",
        "    for depth in range(len(parts) - 1, len(parts)):",
        "the shallowest contentless directory is the finding; the deepest one is "
        "thousands of content-hash directories holding one file each, which turns a "
        "single dominant noise source into an unreadable list",
        ("test_content_set",),
    ),
    Mutation(
        "noise roots lose their tiebreak",
        "content_set.py",
        "        key=lambda root: (-root.files, root.path),",
        "        key=lambda root: -root.files,",
        "two directories of equal size then come out in whatever order the caller "
        "supplied the tree in, so a committed manifest differs between two builds "
        "that changed nothing - the class of non-determinism that is invisible "
        "until somebody diffs two stores",
        ("test_content_set",),
    ),
    Mutation(
        "directory symlinks walked again",
        "content_set.py",
        "    for directory, _, names in os.walk(corpus, followlinks=False):",
        "    for directory, _, names in os.walk(corpus, followlinks=True):",
        "a followed link reports the same files twice under two paths, inflating "
        "the tree count that is the denominator of every percentage this stage "
        "prints; symlink duplication has already produced three wrong figures here",
        ("test_content_set",),
    ),
    Mutation(
        "content set report unwired",
        "status.py",
        "    _report_content_set()",
        "    pass",
        "the fourth instance of this repository's most repeated escape, and #213 "
        "itself is the consequence: the pipeline knew what it considered content "
        "and nothing said so, so every consumer re-derived it badly",
        ("test_content_set",),
    ),
    Mutation(
        "a set that cannot be judged reported as stale",
        "status.py",
        '"current": None if not config.DETECT_PATH.is_file() else recorded == here,',
        '"current": recorded == here,',
        "detect is a graphify working file a store need not keep, so a store "
        "without one would be told its content set was stale on every single run - "
        "which is how a real warning stops being read",
        ("test_content_set",),
    ),
    Mutation(
        "only the store root's output is refused",
        "extract_ast.py",
        "        in_a_clone = bool(name) and name in candidate.parts[:-1]",
        "        in_a_clone = False",
        "the first version of the refusal above, written and caught the same hour by "
        "reading this library's own build notes: `sync` ends with "
        "`git clean -fd -e graphify-out`, so that directory is the one thing in a "
        "clone sync deliberately preserves and every repository in the corpus can "
        "hold one. A refusal anchored at the store root alone passes the corpus copies "
        "through, which is where most of them are",
        ("test_extract_ast",),
    ),
    Mutation(
        "the pipeline parses its own output again",
        "extract_ast.py",
        "    if artefacts:",
        "    if False:",
        "a store driving extraction itself excluded dependency bundles, build output "
        "and state files from its hand-written list, and not the directory this "
        "pipeline writes - so several hundred of the pipeline's own artefacts were "
        "handed to the parser as source. Found by watching a run parse a graph file, "
        "because an exclusion list has no failing case: correct the day it is written "
        "and silently wrong at the next new artefact",
        ("test_extract_ast",),
    ),
    Mutation(
        "one repository's failure loses the estate again",
        "extract_ast.py",
        "except Exception as error:  # a parser raises whatever the grammar raises",
        "except KeyboardInterrupt as error:",
        "the reported failure this stage exists for: a whole-corpus call on a "
        "several-hundred-repository estate ran over eight minutes at full CPU and "
        "produced no output at all, with nothing to attribute it to. Reverting to an "
        "unisolated loop restores exactly that, and the run reads as a parser problem "
        "rather than as one repository's",
        ("test_extract_ast",),
    ),
    Mutation(
        "a partial layer reports success",
        "extract_ast.py",
        '            print(f"  {label}: {error}", file=sys.stderr)\n        return 1',
        '            print(f"  {label}: {error}", file=sys.stderr)\n        return 0',
        "the layer is written even when repositories failed, on purpose, so the exit "
        "code is the only thing carrying the failure. Returning 0 takes a CI build "
        "green over a layer missing whole repositories - the shape of every escape "
        "found in this library, a check that reads one artefact and is silent about "
        "the one that matters",
        ("test_extract_ast",),
    ),
    Mutation(
        "a repository that shrank is never named",
        "extract_ast.py",
        "    if before:",
        "    if False:",
        "the gap a reviewing operator found in the first version of this stage, and the "
        "only one of their four controls that had caught anything real - two genuine "
        "decreases on one refresh. Reconciling a run against its own input catches input "
        "dropped within a run; it cannot catch a repository extracting less than last "
        "time, where every count reconciles and the store loses structure quietly",
        ("test_extract_ast",),
    ),
    Mutation(
        "a decrease is reported as no movement",
        "extract_ast.py",
        "            if after[name] < before[name]",
        "            if False",
        "the narrower half of the same gap: with the report still printing for gone and "
        "new repositories, an operator sees a movement section and reads its silence on "
        "decreases as an all-clear. A report that runs is the strongest possible "
        "disguise for a check that does not",
        ("test_extract_ast",),
    ),
    Mutation(
        "a failed repository is dropped from the baseline",
        "extract_ast.py",
        "    return {**carried, **counts}",
        "    return dict(counts)",
        "found by re-reading this stage after its own review passed: a repository that "
        "failed vanished from the sidecar, so it read as absent from the content set on "
        "the next run and new on the one after, and the count its recovery would be "
        "compared against was gone. With every repository failing, the whole baseline was "
        "replaced with nothing - a failed run erasing the record of what it cost",
        ("test_extract_ast",),
    ),
    Mutation(
        "the per-repository bound never fires",
        "extract_ast.py",
        "signal.alarm(seconds)",
        "signal.alarm(0)",
        "per-repository extraction names the repository that is hanging, which is no use "
        "to an operator who is asleep - the run has to end. The first version of this "
        "stage had the attribution and no bound, so a pathological parse was identifiable "
        "and still unbounded",
        ("test_extract_ast",),
    ),
    Mutation(
        "files the extractor could not read are discarded again",
        "extract_ast.py",
        'unread = [str(f) for f in (result.get("failed_sources") or [])]',
        "unread = []",
        "found by asking the real extractor what it returns rather than trusting the "
        "stub: it names every file it could not read and returns *successfully* while "
        "doing so, and this stage dropped that field on the floor. A repository that "
        "parsed none of its files reported as extracted with a node count of zero and "
        "no explanation - the information existed, was handed over, and was not read",
        ("test_extract_ast",),
    ),
    Mutation(
        "the bound is swallowed by the extractor's own error handling",
        "extract_ast.py",
        "class RepositoryTimeout(BaseException):",
        "class RepositoryTimeout(Exception):",
        "found by running the bound against the real extractor rather than reasoning "
        "about it: it wraps each file in its own `except Exception`, so a TimeoutError "
        "was caught there, the file skipped, and the call returned successfully with "
        "fewer nodes. The bound silently converted a hang into a smaller layer - the "
        "exact failure the movement check exists to catch a whole run later - while "
        "reporting the repository as extracted. Deriving from Exception reads like a "
        "tidy-up",
        ("test_extract_ast",),
    ),
    Mutation(
        "the alarm is left armed for a later stage",
        "extract_ast.py",
        "        signal.alarm(0)\n        signal.signal(signal.SIGALRM, previous)",
        "        signal.signal(signal.SIGALRM, previous)",
        "`signal.alarm` is process-wide, so an alarm left set fires inside whatever stage "
        "runs next in the same process and names a repository that stage never touched. "
        "The hardest class of failure to attribute, introduced by the fix for the one "
        "above",
        ("test_extract_ast",),
    ),
    Mutation(
        "an empty layer looks like a successful extraction",
        "extract_ast.py",
        "    if not files:",
        "    if False:",
        "mirrors `merge-layers`, which carries the same refusal for an observed "
        "reason: an empty layer merged silently produces a smaller graph that reads "
        "as a successful run. Without it here the symptom surfaces two stages later "
        "as a merge failure rather than at the stage that produced it",
        ("test_extract_ast",),
    ),
    Mutation(
        "a cut's edges counted per endpoint rather than per edge",
        "size_cuts.py",
        'kept.get(str(edge.get("source")), 0) & kept.get(str(edge.get("target")), 0)',
        'kept.get(str(edge.get("source")), 0) | kept.get(str(edge.get("target")), 0)',
        "#116: an edge survives only when both its endpoints do, and this one operator "
        "is the difference between the measurement the issue asks for and the one that "
        "misled it. On the reporting estate a file-level cut kept tens of thousands of "
        "nodes joined by low hundreds of edges - with `|` that cut reports as keeping a "
        "graph, and nothing downstream disagrees until clustering",
        ("test_size_cuts",),
    ),
    Mutation(
        "an unmatched cut rule reported as a rule with nothing to do",
        "size_cuts.py",
        "            if not sizing.hits.get(rule.line):",
        "            if False:",
        "#116: a glob written for a path form the graph does not use selects nothing and "
        "reads exactly like a clean run - the same silence a `match` rule that selected "
        "no repository already has a reporter for, one artefact along",
        ("test_size_cuts",),
    ),
    Mutation(
        "a cut that strands every node it keeps reported as a smaller graph",
        "size_cuts.py",
        "        elif not edges:",
        "        elif False:",
        "#116: mass without structure is the failure the issue found counter-intuitive, "
        "and clustering, centrality and summaries are all degree-driven, so the estate "
        "prose is generated from a layer nothing joins",
        ("test_size_cuts",),
    ),
    Mutation(
        "sizing zeros written over the last refresh's counts",
        "size_cuts.py",
        "    if not sizing.nodes:",
        "    if False:",
        "#116 over #154: comparison against the store's own previous refresh is the only "
        "comparison available, because two estates measured layer ratios a hundredfold "
        "apart. A run pointed at a path holding no `nodes` array would record zeros over "
        "the only baseline there is, and report a successful run doing it",
        ("test_size_cuts",),
    ),
    Mutation(
        "the cut sizing measured and discarded",
        "size_cuts.py",
        "        telemetry.record(measurements(sizing, cuts))",
        "        measurements(sizing, cuts)",
        "#116 over #154: without the record a candidate's counts live in one afternoon's "
        "scrollback, so the next refresh cannot say whether the cut it applied still "
        "keeps what it kept - the wiring escape this gate already records six times",
        ("test_size_cuts",),
    ),
    Mutation(
        "a `**` glob accepted and read as a single wildcard",
        "size_cuts.py",
        '    if "**" in value:',
        "    if False:",
        "#116: `*` already crosses directories here, so `**/*.tf` requires a directory "
        "separator and silently drops a file at a repository's root. A rule that matches "
        "less than it says is the class that put a whole extension's nodes outside a cut "
        "nobody could see was narrow",
        ("test_size_cuts",),
    ),
    Mutation(
        "merge inputs read from the declaration again",
        "merge_inputs.py",
        "    for repository in sorted(config.REPOSITORIES_DIR.iterdir()):",
        "    for repository in sorted(\n"
        "        config.REPOSITORIES_DIR / name for name in (declared_repositories() or [])\n"
        "    ):",
        "the defect an operator measured: a repository discovered, cloned and extracted "
        "before a refresh aborted stayed on disk while the configuration naming it was "
        "discarded, and the glob-driven merge read it. Any reconciliation walking "
        "config/repositories.txt skips exactly that input and reports clean, which is "
        "worse than no check at all",
        ("test_merge_inputs",),
    ),
    Mutation(
        "provenance closure no longer checked",
        "merge_inputs.py",
        "        ungrounded=tuple(sorted(on_disk - set(recorded))),",
        "        ungrounded=(),",
        "the sharp half of the same report: provenance records what was read, and a "
        "glob-driven merge can read something it has no entry for, so an answer citing "
        "those nodes cannot name the commit they were read at",
        ("test_merge_inputs",),
    ),
    Mutation(
        "an undeclared input is counted but not named",
        "merge_inputs.py",
        "    if report.undeclared:",
        "    if False:",
        "the divergence the issue asked to have named rather than counted - an operator "
        "given a number knows something is wrong and not which repository to look at",
        ("test_merge_inputs",),
    ),
    Mutation(
        "an empty merge glob reads as a clean run",
        "merge_inputs.py",
        "    if not report.inputs or report.declared is None:\n        return 1",
        "    if False:\n        return 1",
        "the vacuity this library keeps meeting: a check over an empty set has nothing "
        "to report and exits 0, so a build that produced no per-repository graphs at "
        "all passes the gate meant to notice it",
        ("test_merge_inputs",),
    ),
    Mutation(
        "status stops reporting the merge inputs",
        "status.py",
        "    for line in merge_inputs.lines(merge_inputs.reconcile(), limit=5):\n        print(line)",
        "    return",
        "the operator who reported this was reading `status`, which described an "
        "entirely healthy store over a graph it could not account for; a stage nobody "
        "runs is how a reconciliation ships and changes nothing",
        ("test_merge_inputs",),
    ),
    Mutation(
        "secret-bearing content reported instead of refused",
        "build_content_set.py",
        "        _refuse_secret_bearing(refused, len(content))\n        return 2",
        "        _refuse_secret_bearing(refused, len(content))",
        "the reporting-instead-of-refusing shape, on the one input where reporting "
        "cannot work: a state file holds resolved secret values, and by the time a "
        "report is read the content is extracted and persists in the extraction cache "
        "and in each clone's own graphify-out/, which sync deliberately preserves. "
        "Reachable today only through a constructed content set - detect classifies no "
        ".tfstate, so the refusal is dormant defence-in-depth and this entry keeps its "
        "shape alive until the peer version changes",
        ("test_content_set",),
    ),
    Mutation(
        "the estate's own ruling ignored",
        "build_content_set.py",
        "    refused = content_set.secret_bearing(content, allowed)",
        "    refused = content_set.secret_bearing(content)",
        "the declaration is the only thing that makes this refusal safe to ship to "
        "stores that already build: whether a named file is safe is a ruling nothing "
        "in the pipeline can derive. An opt-out that is read and not applied reads "
        "exactly like one that was honoured, because the refusal simply stands. Same "
        "dormancy as the entry above - constructed input only, until detect classifies "
        "the format",
        ("test_content_set",),
    ),
    Mutation(
        "emulator dumps dropped in silence",
        "build_content_set.py",
        "    _report_emulator_dumps(dumps, len(content), len(exposed), arguments.top)",
        "    pass",
        "an exclusion nobody sees is indistinguishable from content that was never "
        "there, and this one changes a committed path list. The same escape as every "
        "other unwired report in this file, with the count that reconciles the set "
        "against what detect classified as its only observer",
        ("test_content_set",),
    ),
    Mutation(
        "the state-file rule anchored to the repository root",
        "content_set.py",
        "if path not in allowed and PurePosixPath(path).name.endswith(SECRET_BEARING_SUFFIXES)",
        "if path not in allowed and len(PurePosixPath(path).parts) == 3",
        "a rule that only fires on repositories/<repo>/<file> passes cleanly on every "
        "estate that keeps its infrastructure in a subdirectory, which is most of "
        "them - and passing cleanly is how it reads as compliance for files it never "
        "looked at. Constructed input only while the refusal is dormant; the emulator "
        "half of the same matcher is exercised on input the real detect pass produces",
        ("test_content_set",),
    ),
    Mutation(
        "the emulator-dump rule widened to a substring",
        "content_set.py",
        'EMULATOR_DUMP_NAMES = ("__azurite_db_*.json",)',
        'EMULATOR_DUMP_NAMES = ("*azurite*",)',
        "the opposite direction and the more tempting one: matching the emulator's "
        "name anywhere takes out an estate's own emulator wiring - a client module, a "
        "compose file, a fixture directory - all authored content, removed from a "
        "committed path list by a rule that was only ever meant to drop one generated "
        "filename",
        ("test_content_set",),
    ),
    Mutation(
        "a pin the lock does not deliver is reported as resolved",
        "check_install_docs.py",
        "        if locked.get(name) != version",
        "        if False",
        "the defect this was written from: a pin moved to a version an index had not "
        "published, so the lock could not be recompiled and kept the previous one. "
        "`pip install --require-hashes` reads the LOCK, so the build used a version the "
        "store's requirements input did not name - and three checks passed, none of them "
        "having opened that input for correctness",
        ("test_check_install_docs",),
    ),
    Mutation(
        "a pin the lock omits entirely reads as resolved",
        "check_install_docs.py",
        "        (name, version, locked.get(name))",
        "        (name, version, version)",
        "resolving a requirement not at all installs no such package, which is a "
        "different failure from resolving it at another version and needs a different "
        "fix; folding the two together sends an operator to recompile a lock that is "
        "missing the entry rather than holding a stale one",
        ("test_check_install_docs",),
    ),
    Mutation(
        "the resolution half stops reaching the exit code",
        "check_install_docs.py",
        "    resolution = _report_unresolved(config.REQUIREMENTS_PATH, lock)",
        "    resolution = 0",
        "a check that prints a disagreement and exits 0 is worse than one that does not "
        "look: the text scrolls past in a green run and nothing chains on it",
        ("test_check_install_docs",),
    ),
    Mutation(
        "graph-report check unwired",
        "status.py",
        "    _report_graph_report(arguments.verify_graph)",
        "    pass",
        "behaviour tested through the function; nothing drove `main()`",
        ("test_graph_report_staleness",),
    ),
    Mutation(
        "contentless check unwired",
        "status.py",
        "    _report_contentless(nodes)",
        "    pass",
        "same escape as above, three days later, in a different check",
        ("test_graph_report_staleness",),
    ),
    Mutation(
        "manifest scope statement unwired",
        "build_knowledge_context.py",
        "            *scope_statement(len(repository_dirs)),",
        "",
        "the statement was tested; that it reached the written file was not",
        ("test_estate_boundary", "test_manifest_scope"),
    ),
    Mutation(
        "precision floor defaults to off",
        "build_community_summaries.py",
        "DEFAULT_PRECISION = 0.2",
        "DEFAULT_PRECISION = 0.0",
        "every test passed the floor explicitly, so the shipped default was unheld",
        ("test_summaries_remap",),
    ),
    Mutation(
        "symlink exclusions ignored",
        "status.py",
        "        elif ignored(path):",
        "        elif False:",
        "a check that could not see its own mitigation, reported by an operator",
        ("test_missing_extractors",),
    ),
    Mutation(
        "distinct-target count dropped",
        "status.py",
        'found["targets"] = len(targets)',
        'found["targets"] = 0',
        "the test drove a stub, so it pinned the wording and not the computation",
        ("test_missing_extractors",),
    ),
    Mutation(
        "verify finding renamed back to 'unsupported'",
        "build_community_summaries.py",
        '"  [not in digest] community',
        '"  [unsupported] community',
        "a label that claimed 98% more than it measured, so the gate got ignored",
        ("test_summaries_verify",),
    ),
    Mutation(
        "the dashed rule no longer printed with its findings",
        "build_community_summaries.py",
        "    _report_dashed_rule({term for _, terms in unsupported for term in terms})",
        "    pass",
        "#248: a dashed token is checked only at three or more segments and a lowercase "
        "initial, and nothing an author could read said so - so authors on a real store "
        "rewrote correct English defensively, most of it compounds the rule never looks "
        "at. Every structural and lexical narrowing was measured and rejected, which "
        "leaves the printed statement as the whole fix: unreached, the report is back to "
        "naming terms without saying what was checked",
        ("test_dashed_token_guidance",),
    ),
    Mutation(
        "estate pass never narrows anything",
        "build_community_summaries.py",
        "            if normalised in estate:\n                continue",
        "            if True:\n                continue",
        "the wider check exists to narrow; a pass-through would look identical. "
        "Retargeted when #179 replaced the comprehension with an explicit loop - "
        "the gate refused to run rather than quietly stop testing this",
        (
            "test_estate_segment_match",
            "test_summaries_flagged_term_classes",
            "test_summaries_verify",
        ),
    ),
    Mutation(
        "partitioner check unwired",
        "status.py",
        "    _report_clustering()",
        "    pass",
        "the third instance of this escape: reported through the function, never through main()",
        ("test_clustering_partitioner",),
    ),
    Mutation(
        "unrecorded partitioner defaults to a partitioner",
        "record_clustering.py",
        "    return named if named in PARTITIONER_NAMES else None",
        "    return named or LOUVAIN",
        "an absent measurement reading as a clean result - shipped twice in one week",
        ("test_clustering_partitioner",),
    ),
    Mutation(
        "a broken graspologic reported as the fallback",
        "record_clustering.py",
        '        return None, f"importing graspologic raised',
        '        return LOUVAIN, f"importing graspologic raised',
        "graphify does not catch that import error, so Louvain there is a guess, not a probe",
        ("test_clustering_partitioner",),
    ),
    Mutation(
        "seed state not recorded",
        "record_clustering.py",
        '"hash_randomised": hash_randomisation(),',
        "",
        "a store that clustered unseeded then looks identical to one that did not",
        ("test_clustering_partitioner",),
    ),
    Mutation(
        "unpinned hashes not reported at record time",
        "record_clustering.py",
        "    if hash_randomisation():",
        "    if False:",
        "the record would say unseeded while the run said nothing",
        ("test_clustering_partitioner",),
    ),
    Mutation(
        "record does not say which file it described",
        "record_clustering.py",
        '"described": config.GRAPH_PATH.name,',
        "",
        "a reader then guesses which of a store's two graph files it refers to",
        ("test_clustering_partitioner",),
    ),
    Mutation(
        "absolute-path check unwired",
        "status.py",
        "    _report_absolute_paths(arguments.paths)",
        "    pass",
        "#176: the fifth instance of this repository's most repeated escape - four "
        "entries above are the same shape, in the same module, and each was written "
        "after the previous one was fixed",
        ("test_status_absolute_paths",),
    ),
    Mutation(
        "absolute-path check reports every absolute path",
        "status.py",
        "    return store_paths.relative(candidate) != candidate",
        "    return True",
        "the neighbouring-quantity failure this codebase has shipped repeatedly: "
        "'every absolute path' rather than 'every path this store wrote absolute' "
        "makes /etc/hosts and an API route findings, and a check whose first run is "
        "mostly false positives is switched off before it reports a real one",
        ("test_status_absolute_paths",),
    ),
    Mutation(
        "unreadable tracked files reported as a clean store",
        "status.py",
        '    if not scan["files"]:',
        "    if False:",
        "the '0 checked, none dangling' defect the corpus-citation check in this same "
        "module already shipped once - a measurement of nothing paired with a clean "
        "verdict, which reads as a pass",
        ("test_status_absolute_paths",),
    ),
    Mutation(
        "absolute paths lost at a read-block boundary",
        "status.py",
        "        if match.end() > end:",
        "        if False:",
        "the scan streams because a store's tracked artefacts run to gigabytes "
        "decompressed, and a path cut in half by a block boundary is the silent half "
        "of that trade: no count can show what it failed to see",
        ("test_status_absolute_paths",),
    ),
    Mutation(
        "the deferred path's own start is not resumed from",
        "status.py",
        "            resume = min(resume, match.start())",
        "            resume = min(resume, match.end())",
        "written and shipped wrong inside the change that added this check, and found "
        "only by reconciling a 2.4 MB fixture's 12,000 written paths against the 11,997 "
        "the scan reported - the ones spanning the hold-back point lost their head, the "
        "lookbehind refused the remainder, and neither pass counted them. 0.03% wrong, "
        "in the direction that reads as clean",
        ("test_status_absolute_paths",),
    ),
    Mutation(
        "ambiguous endpoints folded into the recovered total",
        "measure_dangling_endpoints.py",
        "    return AMBIGUOUS if found > 1 else ABSENT",
        "    return RECOVERABLE if found > 1 else ABSENT",
        "#162: the one constraint the measurement rests on. A repair built on "
        "this rate resolves only where the name is unambiguous, so a rate that "
        "counted the ambiguous ones promises recoveries the repair must refuse - "
        "and the totals still add up, so nothing else notices",
        ("test_dangling_endpoints",),
    ),
    Mutation(
        "entity names matched against the whole path-qualified id",
        "measure_dangling_endpoints.py",
        '    return str(value).strip().rsplit("::", 1)[-1].casefold()',
        "    return str(value).strip().casefold()",
        "#162, and the shape this codebase has shipped most often: a name matched "
        "against a path-qualified id cannot match, so the rate comes back a clean "
        "0.0% and reads as an estate with nothing to fix",
        ("test_dangling_endpoints",),
    ),
    Mutation(
        "recovery stops looking at local_id",
        "measure_dangling_endpoints.py",
        '    keys = {entity_name(node.get("id")), entity_name(node.get("local_id"))}',
        '    keys = {entity_name(node.get("id"))}',
        "#162's whole claim is that local_id carries the entity name; without it "
        "the estate whose endpoints dangle because a chunk named an id defined in "
        "another chunk measures zero, which is the answer that closes the issue",
        ("test_dangling_endpoints",),
    ),
    Mutation(
        "an empty walk reports a clean rate",
        "measure_dangling_endpoints.py",
        '    return (graphs, "") if graphs else ([], _no_graphs_message(repositories))',
        '    return graphs, ""',
        "#162: every stage in this library that shipped doing nothing did so with "
        "a passing suite, and '0 dangling endpoints' over a walk that read no "
        "files is indistinguishable from an estate that has none",
        ("test_dangling_endpoints",),
    ),
    Mutation(
        "graphs that could not be measured reported as a measured zero",
        "measure_dangling_endpoints.py",
        "    if not any(m.measurable for m in measurements):",
        "    if False:",
        "#162: a graph with no edges has no endpoints to dangle, so every count "
        "is zero and the rate is undefined - printed as a result it says the "
        "store is clean",
        ("test_dangling_endpoints",),
    ),
    Mutation(
        "the merged estate graph offered as a substitute",
        "measure_dangling_endpoints.py",
        '        candidate = config.ROOT / "graphify-out" / name\n        if candidate.is_file():',
        '        candidate = config.ROOT / "graphify-out" / name\n        if False:',
        "#162: one estate's first measurement came back at zero and was a "
        "tautology, because it read the file its own merge had already cleaned. "
        "That file is at the store root and is the one an operator reaches for "
        "next, so it has to be refused by name rather than merely not walked",
        ("test_dangling_endpoints",),
    ),
    Mutation(
        "the same graph measured twice from one clone",
        "measure_dangling_endpoints.py",
        "                found.append(candidate)\n                break",
        "                found.append(candidate)",
        "#162: a clone holding both graph forms would have every count doubled "
        "while the rate stayed put - an error that reconciles internally, which "
        "is the kind nobody finds",
        ("test_dangling_endpoints",),
    ),
    Mutation(
        "the report stops naming the artefacts it read",
        "measure_dangling_endpoints.py",
        "        *_read_lines(measurements),",
        "",
        "#162: a rate measured downstream of a store's own fix is not a rate, and "
        "the only way a reader can tell which it got is the list of files. A "
        "check's silence licenses a claim only about the artefact it read",
        ("test_dangling_endpoints",),
    ),
    Mutation(
        "named endpoints printed in set order",
        "measure_dangling_endpoints.py",
        "    result.classified = {kind: sorted(found) for kind, found in buckets.items()}",
        "    result.classified = {kind: list(found) for kind, found in buckets.items()}",
        "#162: two runs on the same graph must be byte-identical, and hash "
        "randomisation makes an unsorted list invisible until someone diffs two "
        "builds from different processes",
        ("test_dangling_endpoints",),
    ),
    Mutation(
        "retained failure double-counted",
        "sync_repositories.py",
        "total = len({*entries, *(name for name, _ in failures)})",
        "total = len(entries) + len(failures)",
        "shipped in v0.11.5; found by an estate, not by this suite",
        ("test_repo_list_and_sync",),
    ),
    Mutation(
        "telemetry overwrites the record without reading it",
        "telemetry.py",
        "    previous = read()",
        "    previous = {}",
        "#154: the defect the whole mechanism exists to remove - every number lived in "
        "one build's scrollback, so nothing could notice it moving. A record written and "
        "never read looks identical to one that was compared, because the file afterwards "
        "is the same either way",
        ("test_telemetry",),
    ),
    Mutation(
        "a collapsed measurement reported as an ordinary statistic",
        "telemetry.py",
        "        if movement.collapsed:",
        "        if False:",
        "#154: zero is the only condition assertable without knowing the estate, and a "
        "join that matched nothing was green on one store across its whole graph. Losing "
        "the routing prints the collapse among the healthy numbers, which is the shape of "
        "output people are already caught skimming",
        ("test_telemetry",),
    ),
    Mutation(
        "each stage's record erases the last stage's",
        "telemetry.py",
        "{**previous, **measurements}",
        "{**measurements}",
        "#154: three stages write this artefact in one refresh, so a replace leaves each "
        "record surviving only until the next stage runs and every comparison is against "
        "nothing - a mechanism that reads green and measures nothing, which is the class "
        "this gate exists for",
        ("test_telemetry",),
    ),
    Mutation(
        "layer sizes measured and discarded",
        "merge_layers.py",
        "    telemetry.record(layer_measurements(counters))",
        "    layer_measurements(counters)",
        "#154, and #116 rests on it: the AST-to-semantic ratio can only be judged against "
        "a store's own last build, because two estates measured it a hundredfold apart. "
        "Computing the counts and not recording them is the wiring escape this gate "
        "already records five times",
        ("test_telemetry",),
    ),
    Mutation(
        "the join cardinality measured and discarded",
        "build_explorer.py",
        "    telemetry.record(page_measurements(graph, entries, edges, size_bytes, breakdown))",
        "    page_measurements(graph, entries, edges, size_bytes, breakdown)",
        "#154 over #149: the join report refuses zero and prints the rate, and the "
        "half-dead case is neither of those. Without a record the rate reaches a terminal "
        "and is gone, which is how a join that should have been three times larger read "
        "as a working join on a sparse estate",
        ("test_build_explorer", "test_telemetry"),
    ),
    # The page's byte attribution (#245). A store watched its explorer page grow by a
    # large fraction while the graph's node count barely moved, with one number for
    # the whole file and no way to name the layer that paid for it. Every entry below
    # removes one line and leaves the breakdown still printing, which is the shape
    # that makes a wrong attribution worse than none: it is read and acted on.
    Mutation(
        "page bytes counted as characters",
        "build_explorer.py",
        '        block_name(placeholder): occurrences * len(blocks[placeholder].encode("utf-8"))',
        "        block_name(placeholder): occurrences * len(blocks[placeholder])",
        "#245: the page is UTF-8 and its prose blocks carry non-ASCII, so a character "
        "count is a plausible number that is not the file's size - the exact class of "
        "wrong measurement this pipeline has shipped, correct code answering a "
        "neighbouring question",
        ("test_build_explorer",),
    ),
    Mutation(
        "a slot filled twice charged once",
        "build_explorer.py",
        '        block_name(placeholder): occurrences * len(blocks[placeholder].encode("utf-8"))',
        '        block_name(placeholder): len(blocks[placeholder].encode("utf-8"))',
        "#245: `str.replace` fills every slot and `__TITLE__` has two, so a breakdown "
        "counting each block once is short by the title on every build - and it still "
        "adds up, because the residual line absorbs exactly what was missed",
        ("test_build_explorer",),
    ),
    Mutation(
        "the residual assumed rather than derived",
        "build_explorer.py",
        '    return {**breakdown, "unattributed": size_bytes - sum(breakdown.values())}',
        '    return {**breakdown, "unattributed": 0}',
        "#245 states it as the requirement: the breakdown must reconcile against the "
        "file's size on disk and not against the sum of what was measured. Zeroing the "
        "residual makes the total add up by construction, which is a breakdown that "
        "cannot report its own failure",
        ("test_build_explorer",),
    ),
    Mutation(
        "a new page block left unattributed",
        "build_explorer.py",
        "    if unfilled or unplaced:",
        "    if False:",
        "#245: the attribution is written once and read years later, so the way it goes "
        "wrong is a block added to the template that nobody counts. Refusing is the only "
        "form that survives that - a residual line would carry the loss, and the reader "
        "would take the named blocks for the whole page",
        ("test_build_explorer",),
    ),
    Mutation(
        "the page-size warning computed and never shown",
        "build_explorer.py",
        '    if warning:\n        print(warning, end="", file=sys.stderr)',
        "    pass",
        "#245, and the wiring escape this gate records repeatedly: the size only has a "
        "use before the write. GitHub refuses a push carrying a file above 100 MB, and "
        "the store that reported this was already past GitHub's own warning with the "
        "page committed",
        ("test_build_explorer",),
    ),
    Mutation(
        "the breakdown computed and never printed",
        "build_explorer.py",
        '    print(breakdown_report(breakdown, size_bytes, config.EXPLORER_PATH), end="")',
        "    pass",
        "#245: telemetry is a committed JSON file, and the operator meeting a page too "
        "large to push is reading a terminal. A number recorded where nobody looks is "
        "the reporting half of the same gap the issue was opened about",
        ("test_build_explorer",),
    ),
    Mutation(
        "the breakdown never reaches the record",
        "build_explorer.py",
        '        **{f"explorer.bytes_{name}": size for name, size in sorted(breakdown.items())},',
        "        **{},",
        "#245 over #154: one build's breakdown says where the bytes are, and only the "
        "predecessor says which block grew. The jump this was reported for happened "
        "between two builds, which no single build can attribute at all",
        ("test_build_explorer",),
    ),
    Mutation(
        "the indexed inventory measured and discarded",
        "build_intent_index.py",
        "    telemetry.record(summarise(index, commits_seen, report))",
        "    summarise(index, commits_seen, report)",
        "#154: a corpus inventory that collapsed reported a plausible smaller number and "
        "read as a smaller estate; nothing but its predecessor contradicts it",
        ("test_telemetry",),
    ),
    Mutation(
        "the record never reaches an operator",
        "status.py",
        "    _report_telemetry()\n\n    _report_graph_report",
        "    _report_graph_report",
        "#154: reporting through a function while nothing drives the CLI is the most "
        "repeated escape in this repository, and `status` alone accounts for three "
        "existing entries here",
        ("test_telemetry",),
    ),
    # Ingestion candidates (#101). The stage's whole value is that its numbers
    # answer the question its columns claim to, so the entries below are the
    # ways it could keep printing a plausible ranking that means something else -
    # the class this repository has shipped more than any other.
    Mutation(
        "the ranking stage is unreachable from the CLI",
        "cli.py",
        '    "gaps": (\n        "report_ingestion_gaps",',
        '    "gaps-unreachable": (\n        "report_ingestion_gaps",',
        "the unwired-stage class, twice shipped here: the reader is tested, the "
        "report is right, and nothing a user or a skill can type reaches it - while "
        "the documentation that tells them to type it still passes review",
        (
            "test_cli_help",
            "test_documented_stages",
            "test_ingestion_gaps",
            "test_repo_list_and_sync",
        ),
    ),
    Mutation(
        "the built side stops being subtracted",
        "report_ingestion_gaps.py",
        "        if coordinate in consumed and coordinate not in evidence.built:",
        "        if coordinate in consumed:",
        "the whole stage reduced to `list your internal dependencies`, which is a "
        "list nobody can act on; it still ranks, still classifies and still prints a "
        "confident table, with the estate's own artefacts at the top of it",
        ("test_ingestion_gaps",),
    ),
    Mutation(
        "framework plumbing ranks above domain again",
        "report_ingestion_gaps.py",
        "    rows.sort(key=lambda row: (KIND_ORDER[row.kind], -row.main, -row.test, -row.repos, row.group))",
        "    rows.sort(key=lambda row: (-row.main, -row.test, -row.repos, row.group))",
        "measured on one estate, two thirds of all reference weight was framework "
        "plumbing, so a weight-ordered ranking puts test utilities at the top and the "
        "repository actually worth adding below the fold - a correct number answering "
        "the wrong question, and the reason classification is ordered before weight",
        ("test_ingestion_gaps",),
    ),
    Mutation(
        "equal-weight namespaces fall back to hash order",
        "report_ingestion_gaps.py",
        "    rows.sort(key=lambda row: (KIND_ORDER[row.kind], -row.main, -row.test, -row.repos, row.group))",
        "    rows.sort(key=lambda row: (KIND_ORDER[row.kind], -row.main, -row.test, -row.repos))",
        "the tiebreak that makes two runs of one store byte-identical; the rows are "
        "grouped out of a set, so without it the order is the process's hash seed, "
        "and hash randomisation has broken determinism here before and been invisible "
        "until somebody diffed two builds",
        ("test_ingestion_gaps",),
    ),
    Mutation(
        "test scope is blended into the main column",
        "report_ingestion_gaps.py",
        '        if declaration.scope == "test":',
        "        if False:",
        "the strongest argument for the report-not-action framing, removed: a "
        "test-scope dependency counted as main says the estate's product needs "
        "something when what it says is that the estate writes tests against it. A "
        "single blended figure is worse than no figure, because its scope is invisible",
        ("test_ingestion_gaps",),
    ),
    Mutation(
        "directories of copies are read as this estate's dependencies",
        "report_ingestion_gaps.py",
        "            if entry.name not in SKIP_DIRS:",
        "            if True:",
        "`node_modules` holds every dependency's own manifest, `target` holds "
        "generated poms and `.terraform` holds the upstream modules themselves - so "
        "the report ranks other projects' dependencies as this estate's gaps. The "
        "same shape as the merge that picked up a previous run's outputs and looked "
        "healthy",
        ("test_ingestion_gaps",),
    ),
    Mutation(
        "an off-host alias becomes a repository to ingest",
        "report_ingestion_gaps.py",
        "        name = aliases.get(provider, provider)",
        "        name = provider",
        "a false absence invented inside the report whose subject is false absence: "
        "a module consumed under the off-host name of a repository the store already "
        "holds is reported as something to go and find",
        ("test_ingestion_gaps",),
    ),
    Mutation(
        "an unscoped package makes every public dependency internal",
        "report_ingestion_gaps.py",
        "    counts = Counter(namespace_of(coordinate.group) for coordinate in built if coordinate.group)",
        "    counts = Counter(namespace_of(coordinate.group) for coordinate in built)",
        "one unscoped npm package published by the estate turns the empty namespace "
        "into an internal one, after which the whole of npm is a candidate to ingest - "
        "a check that cannot fire on Maven and fires on everything under npm",
        ("test_ingestion_gaps",),
    ),
    Mutation(
        "an unreadable declaration reads as an estate that declared nothing",
        "report_ingestion_gaps.py",
        "        return None, str(error)",
        '        return None, ""',
        "the false absence the declaration exists to remove, one level in: a "
        "declaration that fails to parse is indistinguishable from an estate that "
        "wrote none, so a repository already ruled out is ranked as a candidate and "
        "one held under another name is reported absent - and the run still exits 0",
        ("test_ingestion_gaps",),
    ),
    Mutation(
        "the refusal to resolve a coordinate stops reaching the reader",
        "report_ingestion_gaps.py",
        '    lines += ["", FOOTER]',
        "    lines += []",
        "an operator who is not told a coordinate is unresolved reads the namespace as "
        "a repository name and searches the forge for it, which returns nothing for an "
        "artefact published to a binary repository and rate-limits while doing so",
        ("test_ingestion_gaps",),
    ),
    Mutation(
        "a corpus no clone contributed to is reported, not refused",
        "build_content_set.py",
        "    if contribution.clones and len(contribution.silent) == len(contribution.clones):",
        "    if False:",
        "hazard 2 of #112: detect honours .gitignore and repositories/ is gitignored, "
        "so a pass run at the store root classifies the store's own files and stops. "
        "The set is small rather than empty, so the empty-set refusal is stepped around, "
        "and every other guard in the library is comparative - which leaves it "
        "undetectable on a first build, the one build with no baseline to compare",
        ("test_content_set",),
    ),
    Mutation(
        "clones that contributed nothing are counted but not named",
        "build_content_set.py",
        "    for name in contribution.silent:",
        "    for name in ():",
        "the count is expected to be non-zero on a healthy estate - a repository "
        "created and never populated contributes nothing and appears in none of "
        "detect's exclusion buckets - so a bare number sends an operator hunting a "
        "defect that is not there. Reading one name settles it in seconds",
        ("test_content_set",),
    ),
    Mutation(
        "the clone count taken from the declaration instead of the directory",
        "content_set.py",
        "    clones = sorted(entry.name for entry in corpus.iterdir() if entry.is_dir())",
        "    from . import merge_inputs\n\n"
        "    clones = sorted(merge_inputs.declared_repositories() or ())",
        "#222 arriving in a second place: extraction is declaration-driven while the "
        "merge walks the corpus, and a guard against a first-build failure that reads "
        "the declaration inherits its blind spot precisely on a first build, where the "
        "declaration is most likely absent or behind the disk",
        ("test_content_set",),
    ),
    Mutation(
        "the zero-contributor report unwired",
        "build_content_set.py",
        "    _report_contribution(contribution)",
        "    pass",
        "this repository's most repeated escape, on a reconciliation nothing else "
        "performs: noise_roots aggregates non-content files by directory across "
        "repositories, so a clone that contributed nothing is visible in no other "
        "output the stage prints",
        ("test_content_set",),
    ),
    # This gate's own file, mutated through the same machinery: a run that is
    # killed used to leave a deliberately introduced defect in the working tree,
    # and every check below reads that tree. Each `find` is spelt as two literals
    # joined with `+` on purpose: the table quotes the code it mutates, so a single
    # literal would match itself - it sits earlier in the file - and `apply` would
    # rewrite this table rather than the code. `+` rather than adjacency because
    # `ruff format` joins adjacent literals that fit on one line, which would put
    # the whole string back. `test_a_mutation_of_this_gate_cannot_rewrite_its_own_table`
    # fails if either happens.
    Mutation(
        "a termination signal stops unwinding the restore",
        "tests/mutation_gate.py",
        "    previous = [(number, signal.signal(number, handle)) "
        + "for number in TERMINATION_SIGNALS]",
        "    previous = []",
        "#227: SIGTERM ends a process with no unwinding, so the restore in the loop's "
        "`finally` never ran - and SIGTERM is what a timeout, a job kill and a cancelled "
        "CI step send, which makes it the interrupt that actually happens. The comment on "
        "that `finally` claimed to cover it, which is why nobody looked",
        ("test_mutation_gate_recovery",),
    ),
    Mutation(
        "the record a SIGKILL cannot destroy is never written",
        "tests/mutation_gate.py",
        '    RECOVERY_PATH.write_bytes(mutation.module.encode("utf-8") ' + '+ b"\\n" + original)',
        "    pass",
        "#227: no signal handler covers SIGKILL, so the only thing that can put a file "
        "back is bytes already on disk when the process died. Without them the tree keeps "
        "the mutation and the next suite run in it reports real-looking failures in real code",
        ("test_mutation_gate_recovery",),
    ),
    Mutation(
        "a recovery record is left unread at startup",
        "tests/mutation_gate.py",
        "    if recover" + "():",
        "    if False:",
        "#227: the record is worth nothing unread, and it has to be read before the "
        "pre-check - a mutation still applied makes the suite fail for a reason that has "
        "nothing to do with the tree, which the pre-check then reports as the suite being "
        "broken. Written and never consulted is the half-wired shape this whole gate is for",
        ("test_mutation_gate_recovery",),
    ),
    Mutation(
        "a used recovery record is left on disk",
        "tests/mutation_gate.py",
        "    RECOVERY_PATH.unlink(" + "missing_ok=True)",
        "    pass",
        "#227: a record that outlives its restore describes a file that is no longer "
        "mutated, so the next run overwrites a healthy file with older bytes - a stale "
        "artefact read as current, the same class as the leftover graph refusals above",
        ("test_mutation_gate_recovery",),
    ),
    Mutation(
        "the file-list route grows a directory walk",
        "extract_ast.py",
        "        files = content_files(io.read_json_dict(source))",
        '        files = list(config.REPOSITORIES_DIR.rglob("*.py"))',
        "the builder guide tells an operator that exclusions applied at detect time carry "
        "through this stage because it never walks a directory. That paragraph's "
        "predecessor went stale when code changed and NOTHING failed - it was caught by "
        "reading. A walk added here as a convenience keeps every behavioural test green "
        "while reintroducing the second exclusion model the stage exists to remove",
        ("test_extract_ast", "test_file_list_route_guidance"),
    ),
    Mutation(
        "the guide keeps the conclusion and loses the mechanism",
        "docs/building-a-knowledge-store.md",
        "never walks a directory",
        "reads its input",
        '"both .graphifyignore placements work on this route" is only true because the '
        "stage has no scan root. An instruction whose reason has been removed is the one "
        "someone talks themselves out of, and this one competes with graphify's own "
        "printed instructions",
        ("test_file_list_route_guidance",),
    ),
    Mutation(
        "the placement table stops naming the version it was measured against",
        "docs/building-a-knowledge-store.md",
        "graphify 0.9.40",
        "graphify",
        "one cell of that table was disproved on a later graphify than the one it was "
        "written against, and there was no way to tell which - so the correction could "
        "not be distinguished from a contradiction. A version beside a measurement is "
        "what makes the next disagreement diagnosable",
        ("test_file_list_route_guidance",),
    ),
    Mutation(
        "the build skill lists the stages without telling anyone to stop",
        "skills/knowledge-store-build/SKILL.md",
        "and stop if",
        "and note",
        "the setup step this replaces stated a hand-typed library floor, which was wrong "
        "for four releases and blocked two publishes; the stage list replaces it because "
        "the stages are what `unknown stage` is about. A listing with no instruction on "
        "its failure is the vacuous form of the same check - it reads as informative, and "
        "a reader who continues reaches the missing stage only after earlier stages have "
        "written committed artefacts into a store repository",
        ("test_documented_stages",),
    ),
    Mutation(
        "the suite subprocess caches bytecode again",
        "tests/mutation_gate.py",
        '        env={**os.environ, "PYTHONDONT' + 'WRITEBYTECODE": "1"},',
        "        env=None,",
        "#228: CPython invalidates a .pyc on the source's (mtime seconds, size) pair, so "
        "two mutations of one module that produce a file of identical size inside one "
        "second are indistinguishable to it. Two entries added for #227 did exactly that, "
        "and the second run reported a wrong-but-plausible failure set - a spurious "
        "`caught` or `SURVIVED` about code that was never in the tree",
        ("test_mutation_gate_bytecode", "test_mutation_gate_recovery"),
    ),
    Mutation(
        "bytecode writing is disabled with a value CPython ignores",
        "tests/mutation_gate.py",
        '        env={**os.environ, "PYTHONDONT' + 'WRITEBYTECODE": "1"},',
        '        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "0"},',
        '#228: the variable is read as a flag, so "0" and "" both leave bytecode '
        "writing on while the name sits in the environment looking correct - the defect "
        "class this gate exists for, a check present and doing nothing",
        ("test_mutation_gate_bytecode", "test_mutation_gate_recovery"),
    ),
    Mutation(
        "the suite subprocess environment is replaced rather than extended",
        "tests/mutation_gate.py",
        '        env={**os.environ, "PYTHONDONT' + 'WRITEBYTECODE": "1"},',
        '        env={"PYTHONDONTWRITEBYTECODE": "1"},',
        "#228: passing an env at all replaces the inherited one, and PYTHONPATH is how "
        "the suite reaches src/ on a machine holding a non-editable install - so the fix "
        "for a stale .pyc would silently move every mutation run onto the installed "
        "package, which is a worse version of the same defect",
        ("test_mutation_gate_bytecode", "test_mutation_gate_recovery"),
    ),
    Mutation(
        "the second interpolation dialect dropped",
        "deploy_values.py",
        "    return _DOLLAR_INTERPOLATION.sub(PLACEHOLDER, stripped)",
        "    return stripped",
        "#88: two deployment layouts interpolate differently, `{{ }}` and `${ }`, and the "
        "second going unstripped publishes the variable names the first withholds. Nothing "
        "counts a name that was not withheld, so both layouts' output reads as healthy",
        ("test_deploy_values", "test_flux_kustomize_deployments"),
    ),
    Mutation(
        "the secret-location policy unwired",
        "deploy_values.py",
        '    _walk(withhold_secret_locations(value), "", out, max_chars)',
        '    _walk(value, "", out, max_chars)',
        "#88: a values file maps an environment variable to a store and an entry inside it, "
        "which is a map of where each credential lives, and the flattened output is "
        "committed. Every test of the primitive itself still passes with the call site "
        "gone - the wiring-never-asserted class this gate exists for",
        ("test_deploy_values", "test_flux_kustomize_deployments"),
    ),
    Mutation(
        "a nested secret store walked into",
        "deploy_values.py",
        "                PLACEHOLDER if reference and (store or entry) else "
        "withhold_secret_locations(item)",
        "                PLACEHOLDER\n"
        "                if reference and (store or entry) and not isinstance(item, dict)\n"
        "                else withhold_secret_locations(item)",
        "#88: the store half is frequently a mapping rather than a scalar, so withholding "
        "only scalars leaves the store name one level down, published under a key that "
        "reads as structure - a redaction that looks applied and is not",
        ("test_deploy_values",),
    ),
    Mutation(
        "the second deployment layout unwired",
        "build_deployments.py",
        "    facts = _merged_facts(found, reading, tally.flux)",
        "    facts = _values_facts(found)",
        "#88: the stage read one layout, so a Kustomize/Flux repository returned exactly the "
        "pair count the run returned without it. Every test of the reader itself still passes "
        "with the call site gone, which is the class of escape this gate exists for",
        ("test_flux_kustomize_deployments",),
    ),
    Mutation(
        "both layouts' facts appended rather than reconciled",
        "build_deployments.py",
        "        if key in facts:",
        "        if False:",
        "#88: one clone part-way through a migration declares a service in both layouts, and "
        "appending both puts two nodes with one id into the graph. A duplicate id raises "
        "nothing downstream - it is two answers to one question, and the run reports a higher "
        "pair count for it",
        ("test_flux_kustomize_deployments",),
    ),
    Mutation(
        "the base layer dropped from an environment's composition",
        "deploy_flux.py",
        "    ordered = [base] if base else []",
        "    ordered = []",
        "#88: an overlay declares only what it overrides, so a fact built from the patches "
        "alone reports a partial configuration as a whole one - the keys it does carry are "
        "right, which is why nothing looks wrong",
        ("test_flux_kustomize_deployments",),
    ),
    Mutation(
        "only the first directory a cluster reconciles is read",
        "deploy_flux.py",
        "        if any(_under(rel, directory) for directory in directories):",
        "        if any(_under(rel, directory) for directory in directories[:1]):",
        "#88: a cluster reconciles an environment overlay and its stack overlays together, so "
        "reading one of them loses limits that are really set and reports nothing missing",
        ("test_flux_kustomize_deployments",),
    ),
    Mutation(
        "composed values share the parsed document",
        "deploy_flux.py",
        "            base[key] = copy.deepcopy(value)",
        "            base[key] = value",
        "#88: one base declaration is composed into every environment, so overlaying onto the "
        "parsed document instead of a copy gives the environments composed later the earlier "
        "ones' overrides - both facts stay well-formed and one of them is fiction",
        ("test_flux_kustomize_deployments",),
    ),
    Mutation(
        "an unparsable Kustomize/Flux file skipped in silence",
        "build_deployments.py",
        "        if documents is None:\n            unparsed.append(rel)\n            continue",
        "        if documents is None:\n            continue",
        "#88's premise is a silent omission: a file this reader cannot parse is a service "
        "absent from every answer, and a walk that drops it reports the same clean run",
        ("test_flux_kustomize_deployments",),
    ),
    Mutation(
        "the layout that contributed nothing says nothing",
        "build_deployments.py",
        "    if flux.releases or flux.kustomizations:",
        "    if True:",
        "#88: the finding is a repository adding no nodes and no message. Counting documents "
        "for a tree that declared none reads as a healthy run, and the sensitivity control "
        "that proves this reader can tell the two apart is the report line itself",
        ("test_flux_kustomize_deployments",),
    ),
    Mutation(
        "the chart source dropped from a chart reference",
        "deploy_flux.py",
        '    source = _text(reference.get("name")) if reference.get("kind") in CHART_SOURCE_KINDS else ""',
        '    source = ""',
        "#88 and #122: `sourceRef` is a repository-to-repository dependency nothing else in a "
        "store holds. Losing it leaves the chart name and version on the node, so the fact "
        "reads as fully parsed",
        ("test_flux_kustomize_deployments",),
    ),
    Mutation(
        "every fact claims one route",
        "build_deployments.py",
        '            "route": fact.route,',
        '            "route": ROUTE_VALUES,',
        "#88: two layouts emit onto one set of environments, so the route is what makes "
        '"which services reach this environment by which route" answerable. The key is still '
        "present and every node still carries a value, which is how a wrong one survives",
        ("test_flux_kustomize_deployments",),
    ),
    Mutation(
        "every environment named after the root of the cluster tree",
        "deploy_flux.py",
        '    return parts[_ENVIRONMENT_SEGMENT] if len(parts) > _ENVIRONMENT_SEGMENT + 1 else ""',
        '    return parts[0] if len(parts) > 1 else ""',
        "#88: the environment name is the one thing these trees state by path, so taking the "
        "wrong segment collapses every environment into one - and a single environment holding "
        "every service is a plausible-looking answer, which is the failure this stage was "
        "built to avoid",
        ("test_flux_kustomize_deployments",),
    ),
    Mutation(
        "one role word is enough to redact",
        "deploy_values.py",
        "    return bool(stores) and bool(entries) and len(stores | entries) > 1",
        "    return bool(stores) or bool(entries)",
        "#88: `path`, `key` and `store` are ordinary configuration words on their own, so a "
        "rule satisfied by one of them withholds real configuration facts. This is the "
        "failure that reads as extra safety: a policy redacting everything answers no "
        "deployment question, and the stage reports the same key count either way",
        ("test_deploy_values",),
    ),
    Mutation(
        "flagged terms reported as one class again",
        "build_community_summaries.py",
        "    classified = classify_absent(absent) if absent else None",
        "    classified = None",
        "#249: the state that shipped. A real ticket the history datasets record and an "
        "invented class name were one figure, and measured on a large store the tickets were "
        "nearly half of it - so the count an operator was told to act on was inflated by that "
        "much and could not be split without chasing single terms by hand. The total was "
        "correct throughout, which is why it read as a clean finding",
        ("test_summaries_flagged_term_classes",),
    ),
    Mutation(
        "the history lookup no longer reads the datasets",
        "build_community_summaries.py",
        "        found |= _cited_in_dataset(dataset, outstanding)",
        "        pass",
        "#249: with the datasets unread every flagged term is absent from history, so the "
        "class that can contain invention swallows the whole flagged total. The report still "
        "reconciles and the actionable figure goes up, which reads as a stricter check rather "
        "than a check that has stopped looking",
        ("test_summaries_flagged_term_classes",),
    ),
    Mutation(
        "a subTest failure loses the module that saw it",
        "tests/mutation_gate.py",
        '    case = getattr(test, "test_' + 'case", None)',
        "    case = None",
        "#269: unittest reports a `subTest` failure through a `_SubTest`, whose own module "
        "is unittest's rather than the test's, so a reporter that reads the module off the "
        "failing object attributes every one of them to `runTest`. That is a single bucket: "
        "distinct modules collapse into one name no tests directory holds, so an entry "
        "under-names what protects it and `check_mapping` refuses a file that cannot exist. "
        "Deriving this table with the branch removed produced `runTest` 14 times and hid 6 "
        "real observers behind it, while every affected entry still looked plausible",
        ("test_mutation_gate_mapping", "test_mutation_gate_recovery"),
    ),
    Mutation(
        "the tree the child reads goes unchecked",
        "tests/mutation_gate.py",
        "    if check_import" + "_path():",
        "    if False:",
        "#269: the suite runs in `tests/`, so a relative `PYTHONPATH=src` resolves to "
        "`tests/src` in the child and it imports whatever copy is installed instead - after "
        "which no mutation of this tree is visible to any run. Both symptoms point away "
        "from the cause: the gate blames tests that pass when run directly, and "
        "`--derive-mapping` reports entries as observed by nothing. The heuristic watching "
        "for the second one does not fire on the case that happens, because an entry "
        "targeting a file outside the installed package is still seen - one run derived 7 "
        "observed and 72 unobserved, printed no warning, and cost half an hour of deriving "
        "an artefact that had to be thrown away",
        ("test_mutation_gate_recovery", "test_mutation_gate_refusals"),
    ),
)


class Terminated(Exception):
    """A termination signal, raised so that `finally` clauses run."""

    def __init__(self, number: int) -> None:
        self.number = number
        super().__init__(signal.Signals(number).name)


@contextmanager
def restoring_on_termination() -> Iterator[None]:
    """Turn SIGTERM and SIGHUP into `Terminated` for the duration of a block.

    Installed here rather than at import time, and the previous handlers are put
    back on the way out: this module is imported by the suite, and changing a
    test runner's signal disposition as a side effect of an import would be its
    own defect.
    """

    def handle(number: int, frame: FrameType | None) -> None:
        raise Terminated(number)

    previous = [(number, signal.signal(number, handle)) for number in TERMINATION_SIGNALS]
    try:
        yield
    finally:
        for number, handler in previous:
            signal.signal(number, handler)


def target_of(module: str) -> Path:
    """The file a mutation names: a module of the package, or a path relative to
    the repository root when it carries a directory - this gate's own file does."""
    return ROOT / module if "/" in module else SRC / module


def recover() -> int:
    """Restore a file a killed run left mutated. Non-zero when a run must not start.

    Called before the pre-check below, because a mutation still applied makes
    the suite fail for a reason that has nothing to do with the working tree,
    and the pre-check would report that as a real failure.
    """
    if not RECOVERY_PATH.is_file():
        return 0
    name, separator, original = RECOVERY_PATH.read_bytes().partition(b"\n")
    module = name.decode("utf-8", "replace")
    target = target_of(module) if separator and not _traversal(module) else None
    if target is None or not target.is_file():
        print(
            f"{RECOVERY_PATH} records a mutation from an earlier run but does not name a "
            f"file this gate can restore, so a source file may still hold a deliberately "
            f"introduced defect. Restore it from git, then delete {RECOVERY_PATH}.",
            file=sys.stderr,
        )
        return 1
    target.write_bytes(original)
    RECOVERY_PATH.unlink()
    print(
        f"Recovered {module}: an earlier run was killed with a mutation applied, and "
        f"{RECOVERY_PATH} held the original bytes."
    )
    return 0


def _traversal(module: str) -> bool:
    """True when a recovery record names something outside the tree it should."""
    return not module or Path(module).is_absolute() or ".." in Path(module).parts


def restore(module: str, original: bytes) -> None:
    """Put a mutated file back, byte for byte, and drop the recovery record."""
    target_of(module).write_bytes(original)
    RECOVERY_PATH.unlink(missing_ok=True)


# Prefixes the report line so the parent can find it in a stream every test in
# the suite also prints to. A test whose last print carried no newline would
# otherwise leave the report appended to the end of it, and `splitlines()[-1]`
# would hand back a line that is not JSON.
REPORT_MARKER = "mutation-gate-report "

# Run by `_run` in the suite's own directory. It reports which test modules
# failed rather than only whether the run passed, because the observers in the
# table above are derived from that set and checked against it. The module comes
# off the test object rather than out of unittest's printed failure header: a
# header is a display format, and reading a mapping this gate depends on out of
# one would be a parser that breaks silently.
REPORTER = """
import json
import sys
import unittest


def module_of(test):
    # A failure inside a test class carries its module on the class. A module
    # that failed to import, or whose setUpModule raised, is reported through
    # unittest's own stand-ins instead: _FailedTest keeps the module name where
    # a method name would go, and _ErrorHolder puts it in brackets after the
    # name of the hook that raised, and a subTest failure is reported through a
    # _SubTest whose own module is unittest's, with the real case hanging off it.
    module = type(test).__module__
    if not module.startswith("unittest"):
        return module
    case = getattr(test, "test_case", None)
    if case is not None:
        return type(case).__module__
    described = getattr(test, "description", "")
    if described:
        return described.partition("(")[2].rstrip(")").split(".")[0]
    return str(getattr(test, "_testMethodName", "")).split(".")[0]


names = json.loads(sys.argv[1])
marker = sys.argv[2]
loader = unittest.TestLoader()
suite = loader.loadTestsFromNames(names) if names else loader.discover(".")
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
failed = {module_of(test) for test, _ in result.failures + result.errors} - {""}
print(marker + json.dumps({"passed": result.wasSuccessful(), "modules": sorted(failed)}))
"""


@dataclass(frozen=True)
class SuiteRun:
    """What one suite run reported: whether it passed, and which modules failed."""

    passed: bool
    modules: tuple[str, ...]


def _run(names: tuple[str, ...] | None) -> SuiteRun:
    """Run the whole suite (`None`) or the named modules, in a fresh subprocess.

    Bytecode writing is off, and the environment is this process's plus that one
    variable rather than a replacement, so PYTHONPATH still points the child at
    `src/`. CPython invalidates a cached `.pyc` on the source's `(mtime seconds,
    size)` pair, and this gate rewrites one source file per mutation: two
    mutations of a module that produce a file of identical size inside the same
    second are indistinguishable to that check, so the second run would import
    the first mutation's bytecode and report on code that is not in the tree.
    Caching buys the gate nothing, because every run has a different source, so
    disabling it removes the invalidation question rather than narrowing the
    window (#228).

    A child that produced no report is refused rather than read as a pass or a
    fail: both are conclusions about a suite that did not report.

    The child runs in `tests/`, which is what makes a relative `PYTHONPATH=src`
    the trap it is on a machine holding a non-editable install of this library:
    it resolves to `tests/src`, the suite imports the installed package, and no
    mutation of this tree is visible to any run. Every entry then survives, or
    `--derive-mapping` reports every one as observed by nothing. Use an absolute
    path or `pip install -e .`.
    """
    completed = subprocess.run(
        [sys.executable, "-c", REPORTER, json.dumps(list(names or ())), REPORT_MARKER],
        cwd=ROOT / "tests",
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    reported = [line for line in completed.stdout.splitlines() if REPORT_MARKER in line]
    if not reported:
        raise SystemExit(
            f"the suite run reported nothing (exit {completed.returncode}), so neither a "
            f"pass nor a failure can be concluded from it. Its last output was:\n"
            f"{os.linesep.join(completed.stderr.splitlines()[-20:])}"
        )
    parsed = json.loads(reported[-1].partition(REPORT_MARKER)[2])
    return SuiteRun(bool(parsed["passed"]), tuple(parsed["modules"]))


# Asked of a child spawned where the suite is spawned, because the question is
# about that child and not about this process: the gate is started from the
# repository root and its children run in `tests/`, so a relative path on
# PYTHONPATH means two different things in the two places.
IMPORT_PROBE = "import knowledgestore, sys; sys.stdout.write(knowledgestore.__file__ or '')"


def check_import_path(run=subprocess.run) -> int:
    """Refuse a run whose suite subprocess reads a different tree. Non-zero to stop.

    The precondition every conclusion here rests on, asserted rather than
    inferred from its symptoms. With the child importing an installed copy,
    nothing this gate does to the tree is visible to any run it starts: every
    entry survives, or `--derive-mapping` reports every one as observed by
    nothing, and both readings are about a library nobody is changing (#269).

    Watching for the symptom was tried and is not enough. The warning for "every
    entry unobserved" does not fire on the case that happens, because an entry
    targeting a file outside the installed package - `tests/`, `docs/` - is still
    seen: one run derived 7 entries observed and 72 observed by nothing, printed
    no warning, and produced a mapping that looked like a mapping.

    `-B` rather than the variable `_run` sets, for two reasons: the child's
    environment has to arrive exactly as an operator left it, since that is the
    thing being measured, and a probe that wrote a `.pyc` of an unmutated module
    would leave one for a mutated run to read back (#228).
    """
    completed = run(
        [sys.executable, "-B", "-c", IMPORT_PROBE],
        cwd=ROOT / "tests",
        capture_output=True,
        text=True,
    )
    printed = (completed.stdout or "").strip()
    expected = SRC.resolve()
    if printed and Path(printed).resolve().parent == expected:
        return 0
    if printed:
        where = f"from {Path(printed).resolve()}"
    else:
        said = (completed.stderr or "").strip().splitlines()
        where = "from nowhere - it imported no such package at all: " + (
            said[-1] if said else f"it printed nothing and exited {completed.returncode}"
        )
    print(
        f"A test process started in {ROOT / 'tests'}, which is where this gate runs the "
        f"suite, imports knowledgestore {where} rather than from {expected}. Every "
        "mutation of this tree would be invisible to it, so nothing could be concluded "
        "about any of them - a surviving entry would mean the suite never saw the file "
        f"change. Give the child an absolute path (PYTHONPATH={ROOT / 'src'}), or install "
        f"this tree in place with `pip install -e .` from {ROOT}: a relative "
        f"`PYTHONPATH=src` resolves to {ROOT / 'tests' / 'src'} in that child and finds "
        "nothing.",
        file=sys.stderr,
    )
    return 1


def run_suite() -> SuiteRun:
    """What a whole-suite run of the tree as it stands reported.

    The run rather than a verdict, because the refusal that reads it names the
    modules that failed: a red suite the reader cannot see from where they are
    standing is the case that costs, and the names are already in hand.
    """
    return _run(None)


def run_modules(modules: Sequence[str]) -> bool:
    """True when every named test module passes, run on its own.

    An empty selection is refused rather than run. `unittest` reports a suite of
    no tests as a success, so an entry that named nothing would run no test at
    all and be reported as `caught` - a gate saying it had observed a defect it
    never looked for, which is worse than the whole-suite run this replaces.
    """
    if not modules:
        raise SystemExit(
            "a mutation reached the runner naming no test module. Running nothing "
            "reports success, which this gate would print as `caught`."
        )
    return _run(tuple(modules)).passed


def failing_modules() -> tuple[str, ...]:
    """The test modules that fail with the current tree, from a whole-suite run."""
    return _run(None).modules


def observer_path(module: str) -> Path:
    """The file an entry's observer names."""
    return ROOT / "tests" / f"{module}.py"


def check_mapping(mutations: Sequence[Mutation]) -> None:
    """Refuse a table whose entries do not name test modules this tree holds.

    Both refusals close the same hole, which is the one failure mode this change
    introduced. An entry naming nothing runs nothing; an entry naming a module
    that was renamed or misspelt loads a `_FailedTest` and fails for that reason
    rather than for the mutation. Either would print `caught` from a run that
    proved nothing, and neither is visible in a pass.
    """
    for mutation in mutations:
        if not mutation.observers:
            raise SystemExit(
                f"mutation '{mutation.name}' names no observing test module. Run "
                f"`--derive-mapping --only '{mutation.name}'` and paste what it prints: "
                "an unmapped entry cannot be run, because running nothing passes."
            )
        for module in mutation.observers:
            if not observer_path(module).is_file():
                raise SystemExit(
                    f"mutation '{mutation.name}' names '{module}' as an observer and "
                    f"{observer_path(module)} does not exist. A module unittest cannot "
                    "load fails the run, which this gate would report as `caught`."
                )


def apply(mutation: Mutation) -> bytes:
    """Apply one mutation, returning the original bytes for restoration.

    Bytes rather than text, and recorded on disk before the file is touched: a
    re-decoded restore is a re-derivation of the original rather than the
    original, and a process that is killed cannot restore anything at all.

    One occurrence exactly. Absent is the obvious failure and was always
    refused; two is the quiet one, because the replacement below takes the
    first and the run then reports on a line the entry does not describe - a
    `caught` that proves something about code nobody named. This gate cannot
    report that about itself from a pass or a fail, so it is checked here.
    """
    path = target_of(mutation.module)
    original = path.read_bytes()
    source = original.decode("utf-8")
    occurrences = source.count(mutation.find)
    if occurrences == 0:
        raise SystemExit(
            f"mutation '{mutation.name}' no longer applies: its target is absent from "
            f"{mutation.module}. Either the code moved - update the mutation - or the "
            "behaviour was removed, in which case decide deliberately rather than "
            "letting this gate quietly stop testing it."
        )
    if occurrences > 1:
        raise SystemExit(
            f"mutation '{mutation.name}' is ambiguous: its target appears {occurrences} "
            f"times in {mutation.module}, and only the first would be replaced. Widen the "
            "`find` until it names one site, or split the entry into one per site - a "
            "mutation reported as caught while a different line carried the defect says "
            "nothing about the behaviour the entry names."
        )
    RECOVERY_PATH.write_bytes(mutation.module.encode("utf-8") + b"\n" + original)
    path.write_bytes(source.replace(mutation.find, mutation.replace, 1).encode("utf-8"))
    return original


def each_mutation(mutations: Sequence[Mutation], observe: Callable[[Mutation], None]) -> int | None:
    """Apply each entry, hand it to `observe`, restore. An exit code, or None.

    The one place a mutation is ever applied for a sweep, so the restore and the
    signal handling are written once however the observation is made: a second
    copy of this loop for `--verify-mapping` would be a second chance to leave a
    deliberately introduced defect in the tree (#227).
    """
    with restoring_on_termination():
        for mutation in mutations:
            original = apply(mutation)
            try:
                observe(mutation)
            except Terminated as termination:
                print(
                    f"\n{termination} while '{mutation.name}' was applied. "
                    f"{mutation.module} is restored, so nothing is left mutated.",
                    file=sys.stderr,
                )
                return 128 + termination.number
            finally:
                # On a pass, on an exception, on SIGINT (CPython raises
                # KeyboardInterrupt) and on the signals `restoring_on_termination`
                # handles. Not on SIGKILL, which no process can handle - that is
                # what the record `apply` writes to RECOVERY_PATH is for.
                restore(mutation.module, original)
    return None


def sweep(mutations: Sequence[Mutation]) -> int:
    """Apply each entry and run the test modules it names. The gate proper."""
    check_mapping(mutations)
    survived: list[Mutation] = []

    def observe(mutation: Mutation) -> None:
        caught = not run_modules(mutation.observers)
        print(f"  {'caught ' if caught else 'SURVIVED'}  {mutation.name}")
        if not caught:
            survived.append(mutation)

    interrupted = each_mutation(mutations, observe)
    if interrupted is not None:
        return interrupted

    print(f"\n{len(mutations) - len(survived)} of {len(mutations)} mutations caught.")
    for mutation in survived:
        print(
            f"  SURVIVED: {mutation.name} - not caught by "
            f"{', '.join(mutation.observers)} - {mutation.escaped_as}",
            file=sys.stderr,
        )
    if survived:
        print(
            "\nA surviving mutation means that behaviour could be removed today with the "
            "suite still green. It is not a curiosity. Check `--verify-mapping --only` on "
            "the entry before changing the code: the modules it names may no longer be "
            "the ones that observe it. If they nearly all survived, the suite is not "
            "reading this tree at all - see `_run` on a relative PYTHONPATH.",
            file=sys.stderr,
        )
    return 1 if survived else 0


def derive_mapping(mutations: Sequence[Mutation]) -> int:
    """Print the observers of each entry, from a whole-suite run per entry.

    What the table's `observers` are filled in from. Slow by construction - one
    full suite per entry - and that is the trade the field exists to take: paid
    once when an entry is written rather than on every run.

    An entry nothing observes is a survivor of the whole suite, so this refuses
    rather than printing an empty tuple to be pasted in.
    """
    unobserved: list[Mutation] = []

    def observe(mutation: Mutation) -> None:
        modules = failing_modules()
        if not modules:
            unobserved.append(mutation)
        print(json.dumps({"name": mutation.name, "observers": list(modules)}), flush=True)
        print(f"  {len(modules) or 'NOTHING'} <- {mutation.name}", file=sys.stderr)

    interrupted = each_mutation(mutations, observe)
    if interrupted is not None:
        return interrupted

    for mutation in unobserved:
        print(
            f"  UNOBSERVED: {mutation.name} - no test module failed with it applied, so "
            f"the behaviour it describes is unprotected: {mutation.escaped_as}",
            file=sys.stderr,
        )
    if len(unobserved) == len(mutations) > 1:
        print(
            "\nNothing observed anything, which is what a suite that is not reading this "
            "tree looks like - see `_run` on a relative PYTHONPATH.",
            file=sys.stderr,
        )
    return 1 if unobserved else 0


def verify_mapping(mutations: Sequence[Mutation]) -> int:
    """Check every entry's observers against a whole-suite run. The slow, complete mode.

    A wrong name is invisible to a normal run, which is the cost of running only
    what an entry names: the gate would print `caught` either way. So the claim
    each entry makes - these modules and no others observe this defect - is
    checked here in both directions. A module that failed and is not named means
    the entry under-describes what protects it; a named module that did not fail
    means the entry describes protection that is not there.
    """
    check_mapping(mutations)
    wrong: list[str] = []
    disagreed: set[str] = set()

    def observe(mutation: Mutation) -> None:
        complaints = len(wrong)
        failed = set(failing_modules())
        named = set(mutation.observers)
        if not failed:
            wrong.append(f"{mutation.name}: nothing observes it, so it is a survivor")
        if failed - named:
            wrong.append(
                f"{mutation.name}: {', '.join(sorted(failed - named))} failed and is not named"
            )
        if named - failed:
            wrong.append(
                f"{mutation.name}: {', '.join(sorted(named - failed))} is named and did not fail"
            )
        if len(wrong) > complaints:
            disagreed.add(mutation.name)
        print(f"  {'agrees  ' if failed == named else 'DIFFERS '} {mutation.name}", flush=True)

    interrupted = each_mutation(mutations, observe)
    if interrupted is not None:
        return interrupted

    # `wrong` holds messages and one entry can raise three of them, so counting it
    # here would answer a different question than the sentence asks - it printed
    # `-1 of 1` for a single entry that disagreed in both directions at once.
    agreeing = len(mutations) - len(disagreed)
    print(f"\n{agreeing} of {len(mutations)} entries name what observes them.")
    for entry in wrong:
        print(f"  WRONG MAPPING: {entry}", file=sys.stderr)
    if wrong:
        print(
            "\nEvery run of this gate ran only the modules named above, so a mapping that "
            "disagrees with the suite means those runs proved less than they reported. Take "
            "the set from `--derive-mapping` rather than editing by hand.",
            file=sys.stderr,
        )
    return 1 if wrong else 0


def selected(patterns: Sequence[str]) -> tuple[Mutation, ...]:
    """The entries whose name contains one of `patterns`, or all of them.

    A pattern matching nothing is refused: a typo would otherwise run an empty
    table, and every mode here reports an empty table as a pass.
    """
    if not patterns:
        return MUTATIONS
    chosen = tuple(
        mutation for mutation in MUTATIONS if any(pattern in mutation.name for pattern in patterns)
    )
    if not chosen:
        raise SystemExit(f"no mutation's name contains any of {list(patterns)}")
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the mutations and exit")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="TEXT",
        help="restrict to entries whose name contains TEXT (repeatable)",
    )
    parser.add_argument(
        "--derive-mapping",
        action="store_true",
        help="print each entry's observers, from a whole-suite run per entry (slow)",
    )
    parser.add_argument(
        "--verify-mapping",
        action="store_true",
        help="check each entry's observers against a whole-suite run per entry (slow)",
    )
    arguments = parser.parse_args(argv)
    mutations = selected(arguments.only)

    if arguments.list:
        for mutation in mutations:
            print(
                f"{mutation.name:<38} {mutation.module:<32} "
                f"{','.join(mutation.observers):<40} {mutation.escaped_as}"
            )
        return 0

    if recover():
        return 1

    # Before the pre-check below, so that refusal is unreachable for this cause.
    # A child reading an installed release of a branch that adds functions fails
    # the suite, and the message would then send the reader to tests that pass
    # when run directly - correct about the suite, wrong about the suspect.
    if check_import_path():
        return 1

    baseline = run_suite()
    if not baseline.passed:
        # Named, because the reader is not always looking at the failure: a guard
        # over the table below fails this run while every test in the module they
        # are editing passes, and an anonymous refusal sends them there.
        blamed = (
            f"these failed: {', '.join(baseline.modules)}"
            if baseline.modules
            else "the run named no failing module, so it did not fail inside a test - run "
            "the suite directly and read what it printed"
        )
        print(
            "The suite is already failing, so nothing can be concluded about any "
            f"mutation. Fix that first - {blamed}.",
            file=sys.stderr,
        )
        return 1

    if arguments.derive_mapping:
        return derive_mapping(mutations)
    if arguments.verify_mapping:
        return verify_mapping(mutations)
    return sweep(mutations)


if __name__ == "__main__":
    raise SystemExit(main())
