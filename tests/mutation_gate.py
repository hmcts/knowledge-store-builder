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
    python3 tests/mutation_gate.py --status          # is one applied right now?
    python3 tests/mutation_gate.py --only recovery   # entries whose name matches
    python3 tests/mutation_gate.py --derive-mapping  # the observers, from a full run
    python3 tests/mutation_gate.py --verify-mapping  # check every entry's observers
    python3 tests/mutation_gate.py --verify-mapping --shard 2/4  # one runner's slice
    python3 tests/mutation_gate.py --isolate         # run each observer by itself

Each entry names the tests that observe it. A run applies the mutation, runs the
modules holding those tests, and asks whether the *named* ones failed. Whole
modules because that costs no more than one test and is what the mapping was
derived from; the named tests because a module fails for reasons an entry does
not describe. The whole suite per entry cost 8.7 seconds against 0.6 for a
module, which is the difference between 26 minutes and 2 - but the mapping is
worth more than the time. An entry that names its observer can be falsified by a
later reader; a gate that merely claims to be sensitive cannot.

Tests rather than modules because module granularity cannot tell an observation
from a coincidence, and the coincidence was inside this gate: the guards over the
table below fail for every entry that targets this file, whatever the mutation
did, so `caught` meant "a guard noticed the file changed" for four entries whose
only named module was the one holding them (#274). Those guards are named in
`TABLE_GUARDS` and subtracted from every set this gate derives or verifies.

The three slow modes are what keep the mapping honest, and none runs in the
`tests` job: `--derive-mapping` fills in a new entry from a full suite run,
`--verify-mapping` applies every entry against the whole suite and refuses a
named set that does not match the tests that failed, and `--isolate` re-runs
each named observer by itself. The `tests` workflow runs the second one on its
schedule and on `workflow_dispatch`, sharded over four runners with
`--shard N/M` and summarised by one job that fails if any of them did: it costs
a whole suite run per entry, which was 28 minutes in one job (#285).
The slices are reconciled against the table before anything is applied, because a
shard that drops an entry prints `N of N` over a smaller N and reads as a pass.

The last of them exists because the other two, and every ordinary run, select at
module granularity - so none of them ever runs a test alone, and a third way an
entry's claim can be untrue is invisible to all of them. A test that fails in
module scope because a sibling corrupted shared state is recorded as an
observer; one that would fail alone but passes in module scope is recorded as not
one. `--verify-mapping` cannot see either, because it compares a module-scope
answer against a module-scope answer: the artefact is stable and agrees with
itself. `--isolate` re-runs each named observer by itself under its entry's
mutation and refuses one that passes, which is the only shape that separates an
observation from a sibling's leftovers (#334). One test per subprocess against a
suite per entry makes it the cheapest of the three by orders of magnitude, and it
is still off the `tests` job's path, because what it can find is a claim the
table makes rather than a defect in the product.

`--derive-mapping` is also the one mode that does not require a valid mapping. It
reads no `observers` at all, and the check that refuses an entry without them runs
inside the suite - so an unmapped entry made the suite red and the red suite
blocked the command the refusal named. Deriving therefore excuses exactly the
entries that name nothing, for exactly as long as it runs; every other reason a
suite is red still stops it, because a mapping derived from a red suite describes
whatever was already failing (#287).

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

While a run is going, that mutation is genuinely on disk and nothing in the tree
says so - `git status` reports an ordinary edit. The record therefore names the
run as well as the file, and answers for it:

    python3 tests/mutation_gate.py --status         # is a mutation applied here?
    python3 tests/mutation_gate.py --status --json  # the same, for a caller

Both exit non-zero while one is applied, and the pre-commit hook is that command,
so a mutated file cannot be committed (#268).

`live` means four facts agree: the checkout, the host, the pid, and the start time
`ps` reports for that pid. A pid alone is not evidence - pids are reused, and a
record written on another machine names a process that was never here - and
anything the four cannot settle is reported as undecidable rather than guessed. It
can still be wrong in three ways, which a reader acting on `live` should know: a
pid reused within the same second reads live, a process that has exited and not
been reaped reads live because it can still be signalled, and a container sharing
this host's name and paths is indistinguishable from this host.

Stop a live run by signalling it - `kill -TERM <pid>` restores the tree on the way
out. Not `kill -9`, which cannot be handled and leaves the mutation applied, and
not a `pgrep` on this script's name: that matches every checkout on the machine,
and killed the wrong worktree's run once.

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
import socket
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
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

# The four answers `liveness` can give about the run a record names, and the whole
# reason the record carries a root and a pid: "a mutation is applied" does not say
# which checkout holds it, so it cannot stop a session stopping the wrong one - and
# a stale record and a live run read identically without them (#268).
LIVE = "live"
ABANDONED = "abandoned"
ELSEWHERE = "elsewhere"
UNDECIDABLE = "undecidable"

# The signals that end the process without unwinding, so a `finally` never
# runs: a timeout, a job kill and a cancelled CI step all send SIGTERM. SIGINT
# is absent because CPython already raises KeyboardInterrupt for it.
TERMINATION_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
)

# The tests that read the table below rather than any behaviour of the product,
# so nothing they say is an observation of a defect: each asserts that an entry's
# `find` names exactly one site in its target. A mutation whose target is this
# file replaces that site, so the count drops to zero and the check fails - for
# every such entry, every time, whatever the mutation did.
#
# Counting that as an observation made `caught` mean "a guard noticed the file
# changed" for four entries that named no other module, so these names are
# subtracted from every set this gate derives or verifies, and refused if an
# entry names one (#274).
#
# Subtracted rather than skipped while a mutation is applied, which was the other
# way out. Skipping removes the false signal without adding a true one: those
# four entries would have become survivors rather than observed behaviours, and
# it takes a live check out of the ordinary suite run to correct a claim this
# table makes. The exclusion is also readable from here, which a skip inside
# another module is not.
TABLE_GUARDS = (
    "test_mutation_gate_recovery.MutationGateRecoveryTest."
    + "test_a_mutation_of_this_gate_cannot_rewrite_its_own_table",
    "test_mutation_gate_recovery.MutationGateRecoveryTest."
    + "test_every_table_entry_names_one_site_in_its_target",
)


# How a run tells the suite it spawns which entries it is in the middle of
# mapping, as a JSON list of names. `check_mapping` runs inside the suite, so an
# entry naming no observer makes every child run red - the baseline that decides
# whether anything can be concluded, and the per-entry runs the observers are read
# out of. `--derive-mapping` exists to fill that field in, so it is the one mode
# that cannot require it to be filled in already (#287).
#
# Set by `excusing_unmapped` for the duration of a run and removed afterwards, and
# set to an empty list by every other mode: the variable is a run's channel to its
# own children rather than an operator's switch, so a value left in a shell cannot
# widen the excusal to a sweep.
DERIVING = "MUTATION_GATE_DERIVING"


@dataclass(frozen=True)
class Mutation:
    """One real defect, the escape it represents, and what observes it.

    `observers` names the tests that fail when this entry is applied, as
    `module.Class.test`, and the modules holding them are the only ones the gate
    runs for it. Three things follow, and the second is the reason the field
    exists rather than a consequence of it:

    - a run costs one module rather than the whole suite, which took 8.7 seconds
      per entry and 26 minutes over the table;
    - the entry becomes falsifiable by a later reader. "This gate is
      sensitivity-checked" cannot be checked; "this behaviour is observed by
      `test_x`" can, by `--verify-mapping`, which runs the whole suite and
      refuses a set that does not match;
    - a module that fails for a reason the entry does not describe no longer
      reads as an observation, which is the hole a module name left open (#274).

    A name carrying no test - the module alone - is what unittest reports when a
    failure is not inside a test at all: a module that could not be imported, or
    a `setUpModule` that raised. It is a legitimate observation and no shipped
    entry currently has one.

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
    # strictness, delivered where it can be acted on - and `--derive-mapping`, the
    # command it names, is excused from it so that the remedy can be run (#287).
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
        (
            "test_check_yield.BaselineFormatTests.test_a_gzipped_baseline_gives_the_same_report",
            "test_config_and_io.TheGraphLoaderHandlesGzip.test_a_gzipped_graph_is_read",
            "test_config_and_io.TheGraphLoaderHandlesGzip.test_an_unreadable_graph_raises_rather_than_returning_part_of_one",
            "test_config_and_io.TheJsonReaderHandlesGzip.test_a_gzipped_json_object_is_read",
            "test_read_path_policy.ReadsAreNotConfinedTest.test_a_read_outside_the_configured_store_root_succeeds",
            "test_read_path_policy.ReadsAreNotConfinedTest.test_a_read_path_that_climbs_upward_is_accepted",
            "test_summaries_graph_ambiguity.EstateCheckAbsentOrEmptyPlainGraphTest.test_an_absent_plain_graph_is_not_called_stale_and_the_archive_is_read",
        ),
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
        (
            "test_config_and_io.TheGraphLoaderHandlesGzip.test_a_gzipped_graph_is_read",
            "test_config_and_io.TheGraphLoaderHandlesGzip.test_an_unreadable_graph_raises_rather_than_returning_part_of_one",
        ),
    ),
    Mutation(
        "stale counterpart no longer named",
        "record_clustering.py",
        "        disagreement = counterpart_disagreement(config.GRAPH_PATH, (communities, clustered))",
        '        disagreement = ""',
        "the same operator had to diff two graph files by hand to find that the "
        "record described a stale leftover; every count in it reconciled",
        (
            "test_clustering_partitioner.TheCommittedGraphCanBeDescribed.test_a_stale_counterpart_is_named_with_both_counts",
        ),
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
        (
            "test_stale_remedy_directions.TheAdvisoryNotesCarryItToo.test_the_snapshot_note_gives_no_unconditional_destroying_instruction",
            "test_summaries_graph_ambiguity.GraphAmbiguityTest.test_snapshot_names_the_other_graph_file_when_both_exist",
            "test_summaries_graph_ambiguity.GraphAmbiguityTest.test_snapshot_reports_the_disagreeing_counts",
        ),
    ),
    Mutation(
        "summaries remap no longer names a stale graph",
        "build_community_summaries.py",
        '    print(_graph_disagreement(_membership({"nodes": nodes})), end="", file=sys.stderr)',
        "    pass",
        "the second call site of the same warning, and the one that rewrites the "
        "committed summaries file - the snapshot half being covered is exactly how "
        "a half-wired check reads as done",
        (
            "test_summaries_graph_ambiguity.GraphAmbiguityTest.test_remap_names_the_other_graph_file_too",
        ),
    ),
    Mutation(
        "explorer built from a stale graph in silence",
        "build_explorer.py",
        '    print(\n        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "explorer.html"),\n        end="",\n        file=sys.stderr,\n    )',
        "    pass",
        "the page is tracked and ships to readers who have no graph and no CLI to "
        "check it against; an operator hit the same shape when a refresh embedded a "
        "stale semantic layer beside a newly built graph and every gate passed",
        (
            "test_summaries_graph_ambiguity.ArtefactWritersNameTheGraphTest.test_explorer_names_the_other_graph",
        ),
    ),
    Mutation(
        "deep dives built from a stale graph in silence",
        "build_deep_dives.py",
        '    print(\n        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "dives.json"),\n        end="",\n        file=sys.stderr,\n    )',
        "    pass",
        "dives.json is a committed artefact, so a stale read is published rather than merely reported",
        (
            "test_summaries_graph_ambiguity.ArtefactWritersNameTheGraphTest.test_deep_dives_names_the_other_graph",
        ),
    ),
    Mutation(
        "semantic layer built from a stale graph in silence",
        "build_semantic_index.py",
        '    print(\n        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "the semantic layer"),\n        end="",\n        file=sys.stderr,\n    )',
        "    pass",
        "the layer is committed and the explorer page embeds it, so the stale read reaches the page twice over",
        (
            "test_summaries_graph_ambiguity.ArtefactWritersNameTheGraphTest.test_semantic_index_names_the_other_graph",
        ),
    ),
    Mutation(
        "the estate check runs against a stale graph in silence",
        "build_community_summaries.py",
        '    return graph_files.stale_note(plain, nodes, "the estate check")',
        '    return ""',
        "the strongest form of the class: a truthfulness gate reading the wrong artefact passes on the wrong data, and its silence then licenses a claim about something it never looked at",
        (
            "test_summaries_graph_ambiguity.ArtefactWritersNameTheGraphTest.test_the_estate_check_names_the_other_graph",
            "test_summaries_graph_ambiguity.EstateCheckAbsentOrEmptyPlainGraphTest.test_a_genuine_disagreement_still_reports_the_mismatch",
        ),
    ),
    Mutation(
        "the estate check reads only the plain graph, so an absent one yields an empty vocabulary",
        "build_community_summaries.py",
        "    read = graph_files.graph_to_read(config.GRAPH_PATH)\n",
        "    read = config.GRAPH_PATH if config.GRAPH_PATH.is_file() else None\n",
        "on a fresh clone the plain graph is gitignored, so reading only it leaves the estate check with nothing to compare against and no sign that it did nothing",
        (
            "test_summaries_graph_ambiguity.EstateCheckAbsentOrEmptyPlainGraphTest.test_an_absent_plain_graph_is_not_called_stale_and_the_archive_is_read",
        ),
    ),
    Mutation(
        "an empty plain graph is reported as a stale one",
        "build_community_summaries.py",
        "    if not nodes and archive.is_file():",
        "    if False:",
        "a zero-node graph.json is not out of date, it is empty; calling it stale sends an operator to compare two files when one holds nothing",
        (
            "test_summaries_graph_ambiguity.EstateCheckAbsentOrEmptyPlainGraphTest.test_an_empty_plain_graph_says_it_is_empty_and_names_what_was_read",
        ),
    ),
    Mutation(
        "verify stops running the rotation self-check",
        "build_community_summaries.py",
        "    _report_rotation(rotation_check(checked, prose, digests, {cid for cid, _ in unsupported}))\n",
        "",
        "a flagged rate shown without the rotation beside it cannot tell a working check from a vacuous one, and a falling rate reads as an improvement",
        (
            "test_summaries_rotation_selfcheck.RotationSelfCheckTest.test_a_check_that_stopped_discriminating_says_so_loudly_and_keeps_the_exit_code",
            "test_summaries_rotation_selfcheck.RotationSelfCheckTest.test_a_working_check_reports_what_the_rotation_produced_beside_the_real_figure",
            "test_summaries_rotation_selfcheck.RotationSelfCheckTest.test_fewer_than_two_communities_is_inconclusive_not_a_collapse",
            "test_summaries_rotation_selfcheck.RotationSelfCheckTest.test_nothing_grounded_to_begin_with_is_inconclusive",
        ),
    ),
    Mutation(
        "the rotation does not wrap the last summary onto the first",
        "build_community_summaries.py",
        "ids[(index + 1) % len(ids)]",
        "ids[min(index + 1, len(ids) - 1)]",
        "the last community would be compared with its own digest, so one summary always escapes the rotation and the collapse is understated",
        (
            "test_summaries_rotation_selfcheck.RotationSelfCheckTest.test_a_working_check_reports_what_the_rotation_produced_beside_the_real_figure",
        ),
    ),
    Mutation(
        "the rotation self-check passes whatever the rotated figure is",
        "build_community_summaries.py",
        "    elif rotated_grounded <= grounded * ROTATION_COLLAPSE:",
        "    elif True:",
        "a self-check that cannot fail reports a vacuous comparison as discriminating, which is the failure it exists to name",
        (
            "test_summaries_rotation_selfcheck.RotationSelfCheckTest.test_a_check_that_stopped_discriminating_says_so_loudly_and_keeps_the_exit_code",
        ),
    ),
    Mutation(
        "upward write paths accepted again",
        "io.py",
        '    if any(part == ".." for part in Path(path).parts):',
        "    if False:",
        "reported by SonarCloud as pythonsecurity:S8707 on write_json: a path "
        "assembled from CLI arguments reached write_text unvalidated, so whatever "
        "built those arguments chose where the process wrote",
        (
            "test_answers.CommandLine.test_an_out_path_that_climbs_upward_is_refused",
            "test_io_write_targets.WriteTargetTest.test_gzip_text_refuses_an_upward_path",
            "test_io_write_targets.WriteTargetTest.test_the_check_is_lexical_not_resolved",
            "test_io_write_targets.WriteTargetTest.test_the_guard_reports_which_path_it_refused",
            "test_io_write_targets.WriteTargetTest.test_write_json_refuses_an_upward_path",
            "test_read_path_policy.ReadsAreNotConfinedTest.test_the_same_upward_path_is_refused_for_writing",
        ),
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
        (
            "test_read_path_policy.ReadsAreNotConfinedTest.test_a_read_path_that_climbs_upward_is_accepted",
            "test_read_path_policy.ReadsAreNotConfinedTest.test_the_same_upward_path_is_refused_for_writing",
        ),
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
        ("test_stale_graph_refusal.StaleRefusalTest.test_deployments_refuses_and_writes_nothing",),
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
        (
            "test_deployment_prefix_match.DeploymentEdges.test_each_component_reaches_its_repository_and_the_stranger_is_reported",
            "test_deployment_prefix_match.PrefixClaims.test_a_separator_inside_the_repository_name_is_not_a_difference",
            "test_deployment_prefix_match.PrefixClaims.test_one_repository_claims_the_components_it_ships",
            "test_deployment_prefix_match.PrefixClaims.test_the_longest_repository_name_claims_the_service",
            "test_deployment_prefix_match.PrefixClaims.test_the_service_is_segmented_before_it_is_normalised",
            "test_deployment_prefix_match.PrefixClaims.test_two_processes_with_different_hash_seeds_name_the_same_claimant",
            "test_deployment_prefix_match.ReachabilityReport.test_a_fully_joined_run_says_nothing_about_reach",
            "test_deployment_prefix_match.ReachabilityReport.test_the_run_names_the_services_no_repository_could_match",
        ),
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
        (
            "test_deployment_prefix_match.PrefixClaims.test_the_segment_and_substring_rules_keep_precedence",
        ),
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
        (
            "test_deployment_prefix_match.PrefixClaims.test_a_namesake_does_not_claim_a_service_it_merely_starts",
            "test_deployment_prefix_match.PrefixClaims.test_the_service_is_segmented_before_it_is_normalised",
        ),
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
        (
            "test_deployment_prefix_match.DeploymentEdges.test_each_component_reaches_its_repository_and_the_stranger_is_reported",
            "test_deployment_prefix_match.PrefixClaims.test_a_separator_inside_the_repository_name_is_not_a_difference",
            "test_deployment_prefix_match.PrefixClaims.test_one_repository_claims_the_components_it_ships",
            "test_deployment_prefix_match.PrefixClaims.test_the_longest_repository_name_claims_the_service",
            "test_deployment_prefix_match.PrefixClaims.test_the_service_is_segmented_before_it_is_normalised",
            "test_deployment_prefix_match.PrefixClaims.test_two_processes_with_different_hash_seeds_name_the_same_claimant",
            "test_deployment_prefix_match.ReachabilityReport.test_a_fully_joined_run_says_nothing_about_reach",
            "test_deployment_prefix_match.ReachabilityReport.test_the_run_names_the_services_no_repository_could_match",
        ),
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
        (
            "test_deployment_prefix_match.PrefixClaims.test_a_repository_name_below_the_floor_claims_nothing",
        ),
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
        (
            "test_deployment_prefix_match.DeploymentEdges.test_each_component_reaches_its_repository_and_the_stranger_is_reported",
            "test_deployment_prefix_match.PrefixClaims.test_a_separator_inside_the_repository_name_is_not_a_difference",
            "test_deployment_prefix_match.PrefixClaims.test_one_repository_claims_the_components_it_ships",
            "test_deployment_prefix_match.PrefixClaims.test_the_longest_repository_name_claims_the_service",
            "test_deployment_prefix_match.PrefixClaims.test_the_service_is_segmented_before_it_is_normalised",
            "test_deployment_prefix_match.PrefixClaims.test_two_processes_with_different_hash_seeds_name_the_same_claimant",
            "test_deployment_prefix_match.ReachabilityReport.test_a_fully_joined_run_says_nothing_about_reach",
            "test_deployment_prefix_match.ReachabilityReport.test_the_run_names_the_services_no_repository_could_match",
        ),
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
        (
            "test_deployment_prefix_match.PrefixClaims.test_the_longest_repository_name_claims_the_service",
        ),
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
        (
            "test_deployment_prefix_match.PrefixClaims.test_two_processes_with_different_hash_seeds_name_the_same_claimant",
        ),
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
        (
            "test_deployment_prefix_match.ReachabilityReport.test_the_run_names_the_services_no_repository_could_match",
        ),
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
        (
            "test_deployment_prefix_match.Reachability.test_a_word_too_short_to_identify_is_not_a_candidate",
        ),
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
        (
            "test_stale_graph_refusal.StaleRefusalTest.test_package_edges_refuses_and_writes_nothing",
        ),
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
        ("test_stale_graph_refusal.StaleRefusalTest.test_gherkin_refuses_and_writes_nothing",),
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
        (
            "test_stale_remedy_directions.TheRefusalCarriesItsUncertainty.test_the_refusal_states_the_signal_that_separates_them",
        ),
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
        (
            "test_merge_layers.MergeLayersTest.test_a_colliding_edge_never_points_at_the_ast_node",
            "test_merge_layers.MergeLayersTest.test_main_writes_the_merged_extract",
        ),
    ),
    Mutation(
        "layer merge keeps an edge whose endpoint is in neither layer",
        "merge_layers.py",
        '        if str(moved["source"]) not in by_id or str(moved["target"]) not in by_id:',
        "        if False:",
        "guessing at a dangling endpoint is how a concatenation invents "
        "relationships; dropping it silently would be the same defect one step on, "
        "which is why the count is asserted too",
        (
            "test_merge_layers.MergeLayersTest.test_an_edge_with_no_endpoint_in_either_layer_is_dropped_and_counted",
        ),
    ),
    Mutation(
        "layer merge accepts an empty layer",
        "merge_layers.py",
        "    if not ast_nodes or not sem_nodes:",
        "    if False:",
        "every stage in this library that shipped doing nothing did so with a "
        "passing suite, and an empty input layer merges to a smaller graph that "
        "looks like a successful run",
        ("test_merge_layers.MergeLayersTest.test_it_refuses_an_empty_layer",),
    ),
    Mutation(
        "most-connected report unwired",
        "status.py",
        "    _report_central(arguments.central)",
        "    pass",
        "#112: reporting through the function while nothing drives the CLI is the "
        "most repeated escape in this repository, and three existing entries in this "
        "module are the same shape",
        (
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_central_names_the_file_it_read",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_central_ranks_from_the_archive_alone",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_central_reads_the_plain_graph_when_both_exist",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_central_refuses_when_neither_file_exists",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_neither_refusal_points_at_only_one_of_the_two_files",
            "test_most_connected.StatusCentralTest.test_the_flag_reports_the_ranking",
        ),
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
        (
            "test_duplicate_repositories.StatusDuplicatesTest.test_an_absent_graph_is_named_rather_than_reported_as_clean",
            "test_duplicate_repositories.StatusDuplicatesTest.test_the_flag_reports_the_ranking",
            "test_duplicate_repositories.StatusDuplicatesTest.test_the_graph_is_only_read_when_the_flag_is_given",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_duplicates_names_the_file_it_read",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_duplicates_ranks_from_the_archive_alone",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_duplicates_reads_the_plain_graph_when_both_exist",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_duplicates_refuses_when_neither_file_exists",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_neither_refusal_points_at_only_one_of_the_two_files",
        ),
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
        (
            "test_duplicate_repositories.DeterminismTest.test_equal_overlaps_are_ordered_by_name",
            "test_duplicate_repositories.PruningTest.test_a_bound_equal_to_the_worst_kept_overlap_is_still_evaluated",
        ),
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
        (
            "test_duplicate_repositories.PruningTest.test_a_bound_equal_to_the_worst_kept_overlap_is_still_evaluated",
            "test_duplicate_repositories.PruningTest.test_an_underestimating_bound_would_stop_before_a_pair_that_ranks",
        ),
    ),
    Mutation(
        "near-duplicate report finds something on an estate with nothing",
        "duplicate_repositories.py",
        "    if not report.overlaps:\n        return []",
        "    if False:\n        return []",
        "the sensitivity control: a report that always prints a top ten is a report "
        "nobody reads, and this library has already shipped a check whose clean "
        "verdict was printed over nothing it had read",
        (
            "test_duplicate_repositories.OverlapMeasurementTest.test_an_estate_sharing_no_pair_prints_nothing",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_an_estate_sharing_no_pair_still_prints_nothing",
        ),
    ),
    Mutation(
        "most-connected loses its edge-key fallback",
        "graph_files.py",
        "    if not found:",
        "    if False:",
        "graphify writes `links` in node-link JSON and `edges` in its extract files, "
        "so reading one key silently ranks nothing on half the artefacts this is "
        "pointed at - indistinguishable from a graph with no edges",
        ("test_most_connected.MostConnectedTest.test_it_reads_edges_as_well_as_links",),
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
        (
            "test_check_citations.TheStage.test_the_committed_archive_is_read_when_the_plain_graph_is_absent",
            "test_graph_to_read.GraphToReadTest.test_the_committed_archive_is_read_when_the_plain_graph_is_absent",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_an_estate_sharing_no_pair_still_prints_nothing",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_central_names_the_file_it_read",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_central_ranks_from_the_archive_alone",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_duplicates_names_the_file_it_read",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_duplicates_ranks_from_the_archive_alone",
            "test_summaries_graph_ambiguity.EstateCheckAbsentOrEmptyPlainGraphTest.test_an_absent_plain_graph_is_not_called_stale_and_the_archive_is_read",
            "test_summaries_membership_drift.AdriftStage.test_a_refusal_message_names_the_file_that_was_actually_read",
            "test_summaries_membership_drift.AdriftStage.test_the_committed_archive_is_read_when_the_plain_graph_is_absent",
        ),
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
            "test_answers.CommandLine.test_each_summary_note_sits_on_its_own_quantity",
            "test_answers.CommandLine.test_out_writes_one_record_per_answer",
            "test_answers.CommandLine.test_without_out_it_prints_a_summary_and_writes_nothing",
            "test_answers.Comparing.test_a_malformed_record_file_is_refused_cleanly",
            "test_answers.Comparing.test_a_repeated_question_is_compared_as_its_median",
            "test_answers.Comparing.test_an_unpaired_question_is_listed_on_its_own_side",
            "test_duplicate_repositories.StatusDuplicatesTest.test_the_flag_reports_the_ranking",
            "test_duplicate_repositories.StatusDuplicatesTest.test_the_graph_is_only_read_when_the_flag_is_given",
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_a_phrase_between_name_separators_still_corroborates",
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_a_truncated_name_is_still_reported_absent",
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_a_word_out_of_a_camel_case_name_is_still_reported_absent",
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_an_identifier_in_a_phrase_label_no_longer_reads_as_absent",
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_an_invented_term_is_still_reported_absent",
            "test_estate_segment_match.AbsentFromEstateTest.test_a_genuinely_absent_term_is_still_reported",
            "test_estate_segment_match.AbsentFromEstateTest.test_a_scoped_package_no_longer_reads_as_absent",
            "test_estate_segment_match.AbsentFromEstateTest.test_a_short_term_is_not_matched_against_a_segment",
            "test_graph_to_read.GraphToReadTest.test_the_archive_is_offered_from_a_path_that_already_names_it",
            "test_graph_to_read.GraphToReadTest.test_the_plain_graph_is_preferred_when_both_exist",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_central_reads_the_plain_graph_when_both_exist",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_duplicates_reads_the_plain_graph_when_both_exist",
            "test_most_connected.StatusCentralTest.test_the_flag_reports_the_ranking",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_store_with_no_history_datasets_does_not_claim_the_split",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_term_a_recorded_file_path_names_is_credited_to_history",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_term_in_neither_the_graph_nor_history_is_the_class_that_can_contain_invention",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_ticket_the_history_datasets_cite_is_not_counted_as_possible_invention",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_history_credits_a_whole_token_rather_than_a_longer_name_containing_it",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_the_two_classes_sum_to_the_flagged_total",
            "test_summaries_graph_ambiguity.ArtefactWritersNameTheGraphTest.test_the_estate_check_names_the_other_graph",
            "test_summaries_graph_ambiguity.EstateCheckAbsentOrEmptyPlainGraphTest.test_a_genuine_disagreement_still_reports_the_mismatch",
            "test_summaries_graph_ambiguity.EstateCheckAbsentOrEmptyPlainGraphTest.test_an_empty_plain_graph_says_it_is_empty_and_names_what_was_read",
            "test_summaries_membership_drift.AdriftStage.test_a_healthy_store_exits_zero",
            "test_summaries_membership_drift.AdriftStage.test_drift_exits_non_zero_so_a_refresh_can_gate_on_it",
            "test_summaries_membership_drift.AdriftStage.test_no_membership_in_the_graph_exits_two_and_names_the_cause",
            "test_summaries_membership_drift.AdriftStage.test_the_check_names_the_graph_file_it_read",
            "test_summaries_membership_drift.AdriftStage.test_the_report_reconciles_its_populations_against_the_committed_summaries",
            "test_summaries_membership_drift.AdriftStage.test_the_subcommand_is_reachable_and_takes_its_flags",
            "test_summaries_verify.VerifyNamesWhatItMeasuresTest.test_a_term_in_neither_is_reported_as_a_candidate_not_a_fabrication",
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
        (
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_central_names_the_file_it_read",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_central_reads_the_plain_graph_when_both_exist",
        ),
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
        (
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_duplicates_names_the_file_it_read",
            "test_graph_to_read.StatusReportsFromTheCommittedArchive.test_duplicates_reads_the_plain_graph_when_both_exist",
        ),
    ),
    Mutation(
        "estate check stops matching name segments",
        "build_community_summaries.py",
        "            if len(normalised) >= MIN_SEGMENT_MATCH and normalised in segments:",
        "            if False:",
        "#179: a whole-label match reported `NgRx` absent while the estate held it "
        "under eight scoped package names across 228 labels - a check that fires on "
        "an entire ecosystem's naming convention gets switched off",
        (
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_a_phrase_between_name_separators_still_corroborates",
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_an_identifier_in_a_phrase_label_no_longer_reads_as_absent",
            "test_estate_segment_match.AbsentFromEstateTest.test_a_scoped_package_no_longer_reads_as_absent",
        ),
    ),
    Mutation(
        "the segment floor is removed",
        "build_community_summaries.py",
        "MIN_SEGMENT_MATCH = 3",
        "MIN_SEGMENT_MATCH = 1",
        "without a floor the looser match becomes a substring match in effect, and "
        "a two-character segment would corroborate a two-character claim - the "
        "false-negative direction, which fails reassuringly",
        (
            "test_estate_phrase_label_segments.PhraseLabelSegmentTest.test_the_ecosystem_cases_the_rule_was_built_for_are_unchanged",
            "test_estate_segment_match.AbsentFromEstateTest.test_a_short_term_is_not_matched_against_a_segment",
            "test_estate_segment_match.NameSegmentTest.test_segments_below_the_floor_are_not_offered",
        ),
    ),
    Mutation(
        "an identifier inside a phrase label is unreachable again",
        "build_community_summaries.py",
        r'_PHRASE_SEPARATORS = re.compile(r"[^A-Za-z0-9/@.\-_:]+")',
        r'_PHRASE_SEPARATORS = re.compile(r"[/@.\-_:]+")',
        "#303, and the replacement is the class that shipped, so this is the defect "
        "rather than an invented one: semantic and document nodes are labelled with a "
        "phrase, and with no whitespace in the class the identifier in the middle of "
        "one arrived welded to the prose either side of it. `--estate` then reported "
        "it absent from the graph while it sat in the citing community's own node, on "
        "the one class of finding the report tells an operator to act on",
        (
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_an_identifier_in_a_phrase_label_no_longer_reads_as_absent",
            "test_estate_phrase_label_segments.PhraseLabelSegmentTest.test_a_constant_inside_a_phrase_is_offered_whole_and_in_parts",
            "test_estate_phrase_label_segments.PhraseLabelSegmentTest.test_an_identifier_between_words_of_a_phrase_is_a_segment",
            "test_estate_phrase_label_segments.PhraseLabelSegmentTest.test_brackets_and_quotes_separate_as_whitespace_does",
        ),
    ),
    Mutation(
        "the stem basis option stops affecting ids",
        "merge_chunks.py",
        "    stem = spec_stem(basis, keep_extension=keep_extension)",
        "    stem = spec_stem(basis)",
        "#115: threading an option through and having it change nothing is the "
        "wiring escape this gate already records four times, and here it would "
        "leave a store believing it had adopted the extension basis",
        (
            "test_stem_basis.MigrationCostTest.test_the_opt_in_basis_keeps_two_extension_siblings_apart",
        ),
    ),
    Mutation(
        "the migration cost stops being counted",
        "merge_chunks.py",
        "    if spec_stem(basis, keep_extension=not keep_extension) != stem:",
        "    if False:",
        "#115 can only be decided on each estate's own number; the issue's figures "
        "are one estate's, and a cost nobody can measure locally is one nobody acts "
        "on",
        (
            "test_stem_basis.CliTest.test_main_reports_the_cost",
            "test_stem_basis.MigrationCostTest.test_the_cost_is_counted_on_a_default_run",
        ),
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
        (
            "test_merge_layers.NamespaceByRepositoryTest.test_an_edge_inside_one_repository_is_rewritten",
            "test_merge_layers.NamespaceByRepositoryTest.test_an_edge_spanning_two_repositories_is_left_alone_and_counted",
            "test_merge_layers.NamespaceByRepositoryTest.test_the_same_declaration_in_two_repositories_stays_two_nodes",
        ),
    ),
    Mutation(
        "a cross-repository AST edge is attributed to one side",
        "merge_layers.py",
        "        if source_repo and target_repo and source_repo != target_repo:",
        "        if False:",
        "the fix is exact only while both endpoints belong to one repository; "
        "attributing an edge that spans two is the guess the rewrite exists to "
        "avoid",
        (
            "test_merge_layers.NamespaceByRepositoryTest.test_an_edge_spanning_two_repositories_is_left_alone_and_counted",
        ),
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
        (
            "test_top_level_array.TopLevelArrayTest.test_a_nested_array_of_the_same_name_is_ignored",
            "test_top_level_array.TopLevelArrayTest.test_graph_counts_is_not_zero_on_a_clustered_graph",
            "test_top_level_array.TopLevelArrayTest.test_it_reads_a_gzipped_graph",
            "test_top_level_array.TopLevelArrayTest.test_it_yields_nodes_not_hyperedge_id_strings",
            "test_top_level_array.TopLevelArrayTest.test_the_data_loss_refusal_fires_on_a_graph_with_hyperedges",
            "test_top_level_array.TopLevelArrayTest.test_the_mismatch_line_fires_on_a_graph_with_hyperedges",
        ),
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
        (
            "test_summaries_membership_drift.AdriftStage.test_a_stale_counterpart_graph_refuses_rather_than_ranking",
            "test_summaries_membership_drift.AdriftStage.test_no_count_is_stated_before_the_staleness_check",
            "test_summaries_membership_drift.AdriftStage.test_the_disagreement_line_ends_with_a_newline",
        ),
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
        (
            "test_summaries_membership_drift.AdriftStage.test_drift_exits_non_zero_so_a_refresh_can_gate_on_it",
            "test_summaries_membership_drift.AdriftStage.test_the_subcommand_is_reachable_and_takes_its_flags",
            "test_summaries_membership_drift.MembershipDrift.test_a_community_whose_members_scattered_is_adrift",
            "test_summaries_membership_drift.MembershipDrift.test_communities_are_reported_in_a_stable_order",
            "test_summaries_membership_drift.MembershipDrift.test_ids_are_compared_exactly_as_the_remap_compares_them",
            "test_summaries_membership_drift.MembershipDrift.test_the_snapshot_taken_from_a_stale_graph_is_caught",
        ),
    ),
    Mutation(
        "summaries with no snapshot entry silently excluded",
        "build_community_summaries.py",
        '        "unsnapshotted": sorted(wanted - set(snap_sets), key=_by_id),',
        '        "unsnapshotted": [],',
        "the `if cid in snapshot` shape: prose that can be neither checked nor re-keyed - a "
        "remap cannot even withdraw it - dropped from the population, after which the count "
        "reads as though it had covered everything",
        (
            "test_summaries_membership_drift.AdriftStage.test_the_report_reconciles_its_populations_against_the_committed_summaries",
            "test_summaries_membership_drift.MembershipDrift.test_a_summary_with_no_snapshot_entry_is_reported_not_skipped",
            "test_summaries_membership_drift.MembershipDrift.test_an_empty_snapshot_entry_counts_as_unsnapshotted_not_as_total_loss",
        ),
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
        (
            "test_summaries_membership_drift.MembershipDrift.test_no_membership_read_is_named_as_its_own_cause",
        ),
    ),
    Mutation(
        "an id-space mismatch reported as moved membership",
        "build_community_summaries.py",
        "    if (graph_share >= NAMESPACED_SHARE) == (snapshot_share >= NAMESPACED_SHARE):",
        "    if True:",
        "a first implementation of this check elsewhere reported 58 communities adrift of "
        "which 57 were not, because the snapshot's ids were bare and the graph's carried a "
        "`<repo>::` prefix; naming it is what keeps the fix from being a looser comparison",
        (
            "test_summaries_membership_drift.MembershipDrift.test_an_id_space_mismatch_is_named_rather_than_silently_corrected",
        ),
    ),
    Mutation(
        "status leaves its summary count to be read as a verdict",
        "status.py",
        "    pointer = snapshot_pointer()",
        '    pointer = ""',
        "the count is identical whether the prose still describes its community or not, and an "
        "operator read exactly that line as healthy; `status` cannot read the graph, so naming "
        "the blind spot is the only honest thing it can do there",
        (
            "test_summaries_membership_drift.StatusNamesItsBlindSpot.test_a_missing_snapshot_is_named_where_the_count_is_reported",
            "test_summaries_membership_drift.StatusNamesItsBlindSpot.test_status_says_the_summary_count_does_not_mean_the_prose_still_fits",
        ),
    ),
    Mutation(
        "declared boundary never reaches the manifest",
        "build_knowledge_context.py",
        "        *boundary.manifest_section(boundary.read()),",
        "        *[],",
        "the unwired-check class this library has shipped twice - the parse works, "
        "the rendering works, and the committed artefact a reader opens says none of "
        "it, which is indistinguishable from an estate that declared nothing",
        (
            "test_estate_boundary.ManifestTest.test_a_malformed_declaration_stops_the_manifest_build",
            "test_estate_boundary.ManifestTest.test_a_snapshot_or_alias_with_no_ruling_still_reaches_the_manifest",
            "test_estate_boundary.ManifestTest.test_completeness_is_never_claimed_either_way",
            "test_estate_boundary.ManifestTest.test_the_declaration_reaches_the_written_manifest",
            "test_estate_boundary.ManifestTest.test_the_manifest_says_when_no_boundary_is_declared",
        ),
    ),
    Mutation(
        "store stops saying it does not claim completeness",
        "boundary.py",
        '    return lines + [NO_COMPLETENESS, ""]',
        "    return lines",
        "a declaration that reads as `this is all of it` is a new false claim "
        "replacing the old silent one; the estate that prompted this had enumerated "
        "its hosts and was still hunting services with no locatable repository",
        ("test_estate_boundary.ManifestTest.test_completeness_is_never_claimed_either_way",),
    ),
    Mutation(
        "declared repository with no ruling vanishes from the manifest",
        "boundary.py",
        "    subjects = sorted({*declared.rulings, *declared.snapshots, *declared.aliases.values()})",
        "    subjects = sorted(declared.rulings)",
        "written this way first, and found by re-reading the artefact rather than by a "
        "test: a repository declared only by a snapshot date or only by an alias parsed "
        "cleanly, was counted in the status summary, and reached no reader at all",
        (
            "test_estate_boundary.ManifestTest.test_a_snapshot_or_alias_with_no_ruling_still_reaches_the_manifest",
        ),
    ),
    Mutation(
        "status no longer says the boundary is undeclared",
        "status.py",
        "    _report_boundary(recorded)",
        "    pass",
        "silence is the state every store starts in, so a report that speaks only "
        "for the configured case never reaches the stores that most need telling "
        "what their own absences mean",
        ("test_estate_boundary.StatusTest.test_the_report_is_wired_into_the_status_run",),
    ),
    Mutation(
        "off-host name stops resolving to the repository held",
        "boundary.py",
        "        target = aliases.get(name, name)",
        "        target = name",
        "the false absence the declaration exists to remove, reintroduced inside it: "
        "a ruling written under the off-host name keys itself under a name no store "
        "holds, so `status` reports a held repository as missing",
        (
            "test_estate_boundary.ParsingTest.test_a_ruling_under_an_off_host_name_lands_on_the_repository_held",
            "test_estate_boundary.ParsingTest.test_two_rulings_for_one_repository_are_refused",
        ),
    ),
    Mutation(
        "declaration stops being reconciled against disk",
        "status.py",
        "    disagreements = boundary.reconciliation(declared, set(recorded))",
        "    disagreements = {k: [] for k in ('active_absent', 'ruled_out_held', 'alias_absent')}",
        "a declaration nothing checks is a second artefact that can be quietly "
        "wrong, and a repository ruled live and not held is the exact shape of the "
        "published finding that was drawn honestly and was false",
        (
            "test_estate_boundary.StatusTest.test_it_names_a_repository_declared_active_and_not_held",
            "test_estate_boundary.StatusTest.test_it_names_a_repository_held_and_ruled_out",
        ),
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
        (
            "test_chunk_status.ChunkStatusTest.test_a_chunk_in_the_log_with_no_output_is_not_done",
            "test_chunk_status.ChunkStatusTest.test_a_chunk_on_disk_but_absent_from_the_log_counts_as_done",
            "test_chunk_status.ChunkStatusTest.test_a_well_formed_file_with_no_nodes_key_is_not_done",
            "test_chunk_status.ChunkStatusTest.test_an_unusable_chunk_file_is_not_counted_as_done",
            "test_chunk_status.ChunkStatusTest.test_output_for_an_unplanned_chunk_is_reported",
            "test_fanout_dispatch_guidance.FanoutDispatchGuidanceTest.test_the_command_the_skill_documents_runs_and_reports",
        ),
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
        (
            "test_chunk_status.ChunkStatusTest.test_a_fused_token_is_diagnosed_but_never_repaired",
            "test_chunk_status.ChunkStatusTest.test_a_log_token_matching_no_chunk_is_reported_not_counted",
            "test_chunk_status.ChunkStatusTest.test_never_sent_is_separated_from_in_flight",
            "test_chunk_status.ChunkStatusTest.test_several_logs_are_read_together",
            "test_chunk_status.ChunkStatusTest.test_without_a_log_the_split_is_reported_as_unknown",
            "test_fanout_dispatch_guidance.FanoutDispatchGuidanceTest.test_the_command_the_skill_documents_runs_and_reports",
        ),
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
        (
            "test_chunk_status.ChunkStatusTest.test_a_log_token_matching_no_chunk_is_reported_not_counted",
            "test_chunk_status.ChunkStatusTest.test_fused_ids_are_diagnosed_as_a_concatenation",
        ),
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
        ("test_chunk_status.ChunkStatusTest.test_without_a_log_the_split_is_reported_as_unknown",),
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
        (
            "test_chunk_status.ChunkStatusTest.test_a_well_formed_file_with_no_nodes_key_is_not_done",
        ),
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
        ("test_chunk_status.ChunkStatusTest.test_an_unusable_chunk_file_is_not_counted_as_done",),
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
        (
            "test_chunk_status.ChunkStatusTest.test_a_well_formed_file_with_no_nodes_key_is_not_done",
            "test_chunk_status.ChunkStatusTest.test_an_unusable_chunk_file_is_not_counted_as_done",
            "test_chunk_status.ChunkStatusTest.test_the_plan_is_not_mistaken_for_an_extraction",
            "test_headless_workers.ExtractTests.test_a_missing_output_is_not_done_and_a_written_one_lands_where_chunk_status_counts_it",
        ),
    ),
    Mutation(
        "progress estimated with no plan to measure against",
        "chunk_status.py",
        "    if not plan:",
        "    if False:",
        "the plan is the only map from chunk number to file list, so without it "
        "there is no denominator and nothing to name as missing. Reporting `0 of 0` "
        "reads as a finished fan-out",
        ("test_chunk_status.ChunkStatusTest.test_no_plan_is_refused_rather_than_estimated",),
    ),
    Mutation(
        "content set written empty",
        "build_content_set.py",
        "    if not content:",
        "    if False:",
        "an empty path list makes every search over it return no matches, and no "
        "matches reads as a confident answer about the estate rather than as a "
        "missing detect result - the exact failure mode the artefact exists to end",
        (
            "test_content_set.StageTest.test_it_refuses_to_write_an_empty_set",
            "test_refusal_remedies_are_runnable.TheRefusalsAnOperatorActuallySees.test_every_refusal_names_the_call_that_produces_the_file",
            "test_refusal_remedies_are_runnable.TheRefusalsAnOperatorActuallySees.test_every_stage_refuses_rather_than_writing_something",
        ),
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
        (
            "test_refusal_remedies_are_runnable.TheRefusalsAnOperatorActuallySees.test_every_refusal_names_the_call_that_produces_the_file",
            "test_refusal_remedies_are_runnable.TheRefusalsAnOperatorActuallySees.test_every_refusal_resolves_under_the_sweep",
            "test_refusal_remedies_are_runnable.TheSweepOverEveryOperatorMessage.test_no_message_names_a_step_nothing_provides",
        ),
    ),
    Mutation(
        "image gallery needs no minimum count",
        "build_chunk_plan.py",
        "        if images[directory] >= GALLERY_MIN_IMAGES\n",
        "        if True\n",
        "two images in an all-image directory are a pair of diagrams as often as a "
        "gallery, and without the count the share alone packs them in with ordinary "
        "files, so each loses the vision pass it is extracted for",
        (
            "test_chunk_plan.ImageGalleryTest.test_a_lone_diagram_and_a_pair_in_a_docs_folder_keep_their_own_chunks",
            "test_chunk_plan.ImageGalleryTest.test_two_images_in_an_all_image_directory_keep_their_own_chunks",
            "test_chunk_plan.PlanningTest.test_each_image_gets_its_own_chunk",
            "test_chunk_plan_delta.DeltaTest.test_each_image_in_a_delta_gets_its_own_chunk",
            "test_chunk_plan_delta.TheImageRuleHasOneHome.test_a_full_plan_and_a_delta_give_the_same_files_a_chunk_of_their_own",
        ),
    ),
    Mutation(
        "image gallery needs no majority of images",
        "build_chunk_plan.py",
        "        and images[directory] >= GALLERY_MIN_SHARE * detected[directory]\n",
        "        and True\n",
        "three figures in a folder of prose are that prose's diagrams, and without "
        "the share they are packed as a gallery and extracted without their own "
        "vision context",
        (
            "test_chunk_plan.ImageGalleryTest.test_every_file_lands_in_exactly_one_chunk",
            "test_chunk_plan.ImageGalleryTest.test_images_at_exactly_half_the_directory_are_a_gallery",
            "test_chunk_plan.ImageGalleryTest.test_three_images_outnumbered_by_files_keep_their_own_chunks",
        ),
    ),
    Mutation(
        "document export figure folder packed as a gallery",
        "build_chunk_plan.py",
        "        and not is_export_companion(directory)\n",
        "        and True\n",
        "a word processor's web-page export keeps a document's real diagrams in "
        "`<stem>_files` or `<stem>.fld`, which is all images; a count and share rule "
        "alone packs them, the one population the rule exists to keep apart",
        (
            "test_chunk_plan.ImageGalleryTest.test_a_document_exports_figure_folder_keeps_one_chunk_per_image",
            "test_chunk_plan.ImageGalleryTest.test_an_export_stem_with_spaces_a_dot_and_brackets_is_matched_literally",
            "test_chunk_plan_delta.DeltaTest.test_a_delta_keeps_an_exports_figures_in_their_own_chunks",
        ),
    ),
    Mutation(
        "document export stem matched as a pattern",
        "build_chunk_plan.py",
        "            if any((folder.parent / (stem + page)).is_file() for page in EXPORT_PAGE_SUFFIXES):",
        "            if folder.parent.is_dir() and any(__import__('re').fullmatch(stem + "
        "r'\\.html?', p.name) for p in folder.parent.iterdir()):",
        "real export stems carry dots and bracketed copy numbers, which are pattern "
        "syntax; an unescaped match never finds the page, so the export's diagrams "
        "silently read as a gallery and are packed",
        (
            "test_chunk_plan.ImageGalleryTest.test_an_export_stem_with_spaces_a_dot_and_brackets_is_matched_literally",
        ),
    ),
    Mutation(
        "full plan counts galleries from the planned files",
        "build_chunk_plan.py",
        "    galleries = image_galleries(detect)\n",
        '    galleries = image_galleries({"files": files})\n',
        "an uncached plan holds a file or two per directory, so counting only those "
        "calls a gallery's screenshot a lone diagram and plans it differently from "
        "the full build of the same directory",
        (
            "test_chunk_plan.ImageGalleryTest.test_an_uncached_plan_classifies_galleries_from_the_whole_detect_result",
            "test_chunk_plan.ImageGalleryTest.test_images_at_exactly_half_the_directory_are_a_gallery",
        ),
    ),
    Mutation(
        "delta counts galleries from its own files",
        "build_chunk_plan.py",
        "    galleries = image_galleries(detect, store_paths.relative)\n",
        "    galleries = image_galleries(\n"
        '        {"files": {"image": [f for f in files if f in images], '
        '"other": [f for f in files if f not in images]}}\n'
        "    )\n",
        "a delta touches one screenshot of a gallery, sees one image, and pays a "
        "worker for it that a full build would not, so it stops extracting what a "
        "full build would",
        (
            "test_chunk_plan_delta.DeltaTest.test_a_delta_keeps_an_own_chunk_for_an_image_outside_a_gallery",
            "test_chunk_plan_delta.DeltaTest.test_a_delta_packs_an_image_whose_directory_is_a_gallery",
        ),
    ),
    Mutation(
        "noise figure claimed from a tree nobody measured",
        "build_content_set.py",
        '    if not tree:\n        return {"measured": False}',
        '    if False:\n        return {"measured": False}',
        "a sparse clone has no corpus, so the tree count is zero and zero renders "
        "as 'no noise' - the most flattering reading of the least measured case, on "
        "the one artefact whose whole purpose is to say the tree is mostly noise",
        (
            "test_content_set.StageTest.test_it_claims_no_noise_figure_when_the_corpus_is_not_on_disk",
            "test_content_set.StageTest.test_it_warns_when_a_path_could_not_be_made_relative",
            "test_content_set.StatusReportsTheContentSetTest.test_it_claims_no_tree_percentage_when_the_manifest_did_not_measure_one",
        ),
    ),
    Mutation(
        "content files counted as noise",
        "content_set.py",
        "        if path in held:",
        "        if path in bearing:",
        "written and caught during #213: `bearing` holds directories, so no file "
        "path is ever in it and every content file was tallied as noise. The "
        "percentage came out higher, which is the direction that gets believed",
        (
            "test_content_set.NoiseAttributionTest.test_a_content_file_is_never_counted_as_noise",
            "test_content_set.NoiseAttributionTest.test_a_non_content_file_beside_content_is_named_rather_than_dropped",
            "test_content_set.NoiseAttributionTest.test_every_non_content_file_is_attributed_exactly_once",
            "test_content_set.NoiseAttributionTest.test_the_order_is_stable_when_two_directories_tie",
            "test_content_set.StageTest.test_it_names_the_dominant_noise_directory_and_reconciles_the_tally",
        ),
    ),
    Mutation(
        "noise attributed to the file's own directory",
        "content_set.py",
        "    for depth in range(2, len(parts)):",
        "    for depth in range(len(parts) - 1, len(parts)):",
        "the shallowest contentless directory is the finding; the deepest one is "
        "thousands of content-hash directories holding one file each, which turns a "
        "single dominant noise source into an unreadable list",
        (
            "test_content_set.NoiseAttributionTest.test_a_repository_holding_no_content_is_named_as_a_whole",
            "test_content_set.NoiseAttributionTest.test_noise_is_attributed_to_the_shallowest_directory_holding_no_content",
        ),
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
        (
            "test_content_set.NoiseAttributionTest.test_the_order_is_stable_when_two_directories_tie",
        ),
    ),
    Mutation(
        "directory symlinks walked again",
        "content_set.py",
        "    for directory, _, names in os.walk(corpus, followlinks=False):",
        "    for directory, _, names in os.walk(corpus, followlinks=True):",
        "a followed link reports the same files twice under two paths, inflating "
        "the tree count that is the denominator of every percentage this stage "
        "prints; symlink duplication has already produced three wrong figures here",
        ("test_content_set.TreeWalkTest.test_a_directory_symlink_is_not_followed",),
    ),
    Mutation(
        "content set report unwired",
        "status.py",
        "    _report_content_set()",
        "    pass",
        "the fourth instance of this repository's most repeated escape, and #213 "
        "itself is the consequence: the pipeline knew what it considered content "
        "and nothing said so, so every consumer re-derived it badly",
        (
            "test_content_set.StatusReportsTheContentSetTest.test_it_claims_no_tree_percentage_when_the_manifest_did_not_measure_one",
            "test_content_set.StatusReportsTheContentSetTest.test_it_does_not_call_a_set_stale_when_there_is_no_detect_result_to_compare",
            "test_content_set.StatusReportsTheContentSetTest.test_it_says_a_content_set_is_stale_when_detect_has_moved_on",
            "test_content_set.StatusReportsTheContentSetTest.test_it_says_the_content_set_is_not_exposed_when_nothing_wrote_one",
        ),
    ),
    Mutation(
        "a set that cannot be judged reported as stale",
        "status.py",
        '"current": None if not config.DETECT_PATH.is_file() else recorded == here,',
        '"current": recorded == here,',
        "detect is a graphify working file a store need not keep, so a store "
        "without one would be told its content set was stale on every single run - "
        "which is how a real warning stops being read",
        (
            "test_content_set.StatusReportsTheContentSetTest.test_it_does_not_call_a_set_stale_when_there_is_no_detect_result_to_compare",
        ),
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
        ("test_extract_ast.ExtractAstTest.test_a_per_clone_output_directory_is_refused_too",),
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
        (
            "test_extract_ast.ExtractAstTest.test_a_per_clone_output_directory_is_refused_too",
            "test_extract_ast.ExtractAstTest.test_the_pipelines_own_output_is_refused",
            "test_extract_ast.ExtractAstTest.test_the_refusal_covers_an_explicit_file_list_too",
        ),
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
        (
            "test_extract_ast.ExtractAstTest.test_a_partial_layer_still_exits_non_zero",
            "test_extract_ast.ExtractAstTest.test_one_repository_failing_does_not_lose_the_others",
            "test_extract_ast.MovementTest.test_a_failed_repository_is_not_recorded_as_zero",
            "test_extract_ast.MovementTest.test_a_failure_does_not_erase_the_baseline_it_would_be_measured_against",
            "test_extract_ast.MovementTest.test_a_run_where_everything_fails_leaves_the_baseline_standing",
        ),
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
        (
            "test_extract_ast.ExtractAstTest.test_a_partial_layer_still_exits_non_zero",
            "test_extract_ast.ExtractAstTest.test_one_repository_failing_does_not_lose_the_others",
            "test_extract_ast.MovementTest.test_a_failed_repository_is_not_recorded_as_zero",
            "test_extract_ast.MovementTest.test_a_failure_does_not_erase_the_baseline_it_would_be_measured_against",
            "test_extract_ast.MovementTest.test_a_run_where_everything_fails_leaves_the_baseline_standing",
            "test_extract_ast.TimeLimitTest.test_a_repository_that_hangs_is_timed_out_named_and_counted",
            "test_extract_ast.TimeLimitTest.test_an_extractor_that_swallows_exceptions_cannot_swallow_the_bound",
        ),
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
        (
            "test_extract_ast.MovementTest.test_a_failure_does_not_erase_the_baseline_it_would_be_measured_against",
            "test_extract_ast.MovementTest.test_a_repository_that_shrank_is_named_and_does_not_fail_the_run",
            "test_extract_ast.MovementTest.test_gone_and_new_are_reported_as_different_things",
            "test_extract_ast.MovementTest.test_nothing_moving_says_so_rather_than_printing_a_movement_report",
        ),
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
        (
            "test_extract_ast.MovementTest.test_a_failure_does_not_erase_the_baseline_it_would_be_measured_against",
            "test_extract_ast.MovementTest.test_a_repository_that_shrank_is_named_and_does_not_fail_the_run",
        ),
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
        (
            "test_extract_ast.MovementTest.test_a_failure_does_not_erase_the_baseline_it_would_be_measured_against",
            "test_extract_ast.MovementTest.test_a_run_where_everything_fails_leaves_the_baseline_standing",
        ),
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
        (
            "test_extract_ast.TimeLimitTest.test_a_repository_that_hangs_is_timed_out_named_and_counted",
            "test_extract_ast.TimeLimitTest.test_an_extractor_that_swallows_exceptions_cannot_swallow_the_bound",
        ),
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
        ("test_extract_ast.UnreadFilesTest.test_files_the_extractor_could_not_read_are_named",),
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
        (
            "test_extract_ast.TimeLimitTest.test_an_extractor_that_swallows_exceptions_cannot_swallow_the_bound",
        ),
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
        ("test_extract_ast.TimeLimitTest.test_the_alarm_is_cleared_afterwards",),
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
        ("test_extract_ast.ExtractAstTest.test_an_empty_content_set_is_refused",),
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
        (
            "test_size_cuts.SurvivingEdges.test_a_file_level_cut_keeps_mass_and_no_structure",
            "test_size_cuts.SurvivingEdges.test_an_edge_between_two_survivors_is_kept",
            "test_size_cuts.SurvivingEdges.test_an_edge_with_one_surviving_endpoint_is_not_kept",
            "test_size_cuts.SurvivingEdges.test_ids_are_not_shared_between_graph_files",
            "test_size_cuts.TheStageAsRun.test_the_counts_reach_the_telemetry_record_as_integers",
            "test_size_cuts.WhatTheReportSays.test_a_cut_that_keeps_nodes_and_no_edges_is_named",
        ),
    ),
    Mutation(
        "an unmatched cut rule reported as a rule with nothing to do",
        "size_cuts.py",
        "            if not sizing.hits.get(rule.line):",
        "            if False:",
        "#116: a glob written for a path form the graph does not use selects nothing and "
        "reads exactly like a clean run - the same silence a `match` rule that selected "
        "no repository already has a reporter for, one artefact along",
        (
            "test_size_cuts.WhatTheReportSays.test_a_rule_that_matched_nothing_is_named_with_its_line",
        ),
    ),
    Mutation(
        "a cut that strands every node it keeps reported as a smaller graph",
        "size_cuts.py",
        "        elif not edges:",
        "        elif False:",
        "#116: mass without structure is the failure the issue found counter-intuitive, "
        "and clustering, centrality and summaries are all degree-driven, so the estate "
        "prose is generated from a layer nothing joins",
        ("test_size_cuts.WhatTheReportSays.test_a_cut_that_keeps_nodes_and_no_edges_is_named",),
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
        ("test_size_cuts.TheStageAsRun.test_reading_no_node_refuses_and_records_nothing",),
    ),
    Mutation(
        "the cut sizing measured and discarded",
        "size_cuts.py",
        "        telemetry.record(measurements(sizing, cuts))",
        "        measurements(sizing, cuts)",
        "#116 over #154: without the record a candidate's counts live in one afternoon's "
        "scrollback, so the next refresh cannot say whether the cut it applied still "
        "keeps what it kept - the wiring escape this gate already records six times",
        (
            "test_size_cuts.TheStageAsRun.test_a_compressed_per_repository_graph_is_read",
            "test_size_cuts.TheStageAsRun.test_it_reads_the_layer_as_extracted_by_default",
            "test_size_cuts.TheStageAsRun.test_the_counts_reach_the_telemetry_record_as_integers",
            "test_size_cuts.TheStageAsRun.test_the_uncompressed_graph_wins_and_the_pair_is_reported",
        ),
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
        (
            "test_size_cuts.CutsFileRefusals.test_a_double_star_glob_is_refused",
            "test_size_cuts.TheStageAsRun.test_an_unreadable_cuts_file_is_named_before_any_graph_is_read",
        ),
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
        (
            "test_merge_inputs.DiscoveryWalksTheGlob.test_a_graph_no_declaration_names_is_still_found",
            "test_merge_inputs.ReconciliationNamesEachDivergence.test_an_undeclared_input_is_named",
            "test_merge_inputs.StatusReportsItAndStillSucceeds.test_status_caps_the_names_it_prints",
            "test_merge_inputs.StatusReportsItAndStillSucceeds.test_status_names_the_undeclared_input",
            "test_merge_inputs.TheReportNamesRatherThanCounts.test_all_four_divergences_are_reported_together",
            "test_merge_inputs.TheReportNamesRatherThanCounts.test_the_stage_names_every_divergence_and_the_dashboard_caps_them",
            "test_merge_inputs.TheReportNamesRatherThanCounts.test_the_undeclared_repository_appears_in_the_text",
            "test_merge_inputs.TheStageIsWired.test_it_reports_an_undeclared_input_and_still_exits_zero",
            "test_merge_inputs.TheStageIsWired.test_paths_writes_the_inputs_to_stdout_and_the_report_to_stderr",
            "test_merge_inputs.TheStageIsWired.test_strict_fails_on_an_undeclared_input",
        ),
    ),
    Mutation(
        "provenance closure no longer checked",
        "merge_inputs.py",
        "        ungrounded=tuple(sorted(on_disk - set(recorded))),",
        "        ungrounded=(),",
        "the sharp half of the same report: provenance records what was read, and a "
        "glob-driven merge can read something it has no entry for, so an answer citing "
        "those nodes cannot name the commit they were read at",
        (
            "test_merge_inputs.ReconciliationNamesEachDivergence.test_an_input_provenance_cannot_date_is_named",
            "test_merge_inputs.TheReportNamesRatherThanCounts.test_all_four_divergences_are_reported_together",
            "test_merge_inputs.TheStageIsWired.test_strict_fails_on_an_input_provenance_cannot_date",
        ),
    ),
    Mutation(
        "an undeclared input is counted but not named",
        "merge_inputs.py",
        "    if report.undeclared:",
        "    if False:",
        "the divergence the issue asked to have named rather than counted - an operator "
        "given a number knows something is wrong and not which repository to look at",
        (
            "test_merge_inputs.StatusReportsItAndStillSucceeds.test_status_caps_the_names_it_prints",
            "test_merge_inputs.StatusReportsItAndStillSucceeds.test_status_names_the_undeclared_input",
            "test_merge_inputs.TheReportNamesRatherThanCounts.test_all_four_divergences_are_reported_together",
            "test_merge_inputs.TheReportNamesRatherThanCounts.test_the_stage_names_every_divergence_and_the_dashboard_caps_them",
            "test_merge_inputs.TheReportNamesRatherThanCounts.test_the_undeclared_repository_appears_in_the_text",
            "test_merge_inputs.TheStageIsWired.test_it_reports_an_undeclared_input_and_still_exits_zero",
            "test_merge_inputs.TheStageIsWired.test_paths_writes_the_inputs_to_stdout_and_the_report_to_stderr",
        ),
    ),
    Mutation(
        "an empty merge glob reads as a clean run",
        "merge_inputs.py",
        "    if not report.inputs or report.declared is None:\n        return 1",
        "    if False:\n        return 1",
        "the vacuity this library keeps meeting: a check over an empty set has nothing "
        "to report and exits 0, so a build that produced no per-repository graphs at "
        "all passes the gate meant to notice it",
        (
            "test_merge_inputs.TheStageIsWired.test_an_empty_glob_fails_without_strict",
            "test_merge_inputs.TheStageIsWired.test_an_unreadable_declaration_fails_without_strict",
        ),
    ),
    Mutation(
        "status stops reporting the merge inputs",
        "status.py",
        "    for line in merge_inputs.lines(merge_inputs.reconcile(), limit=5):\n        print(line)",
        "    return",
        "the operator who reported this was reading `status`, which described an "
        "entirely healthy store over a graph it could not account for; a stage nobody "
        "runs is how a reconciliation ships and changes nothing",
        (
            "test_merge_inputs.StatusReportsItAndStillSucceeds.test_status_caps_the_names_it_prints",
            "test_merge_inputs.StatusReportsItAndStillSucceeds.test_status_names_the_undeclared_input",
        ),
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
        (
            "test_content_set.DocumentedRefusalCountTest.test_each_document_states_the_refusal_count",
            "test_content_set.DocumentedRefusalCountTest.test_the_module_refuses_at_the_number_of_sites_named_here",
            "test_content_set.NamedFormatStageTest.test_a_state_file_in_the_content_set_refuses_the_run",
            "test_content_set.NamedFormatStageTest.test_the_refusal_names_every_offending_path_and_the_way_past_it",
        ),
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
        (
            "test_content_set.NamedFormatStageTest.test_a_declared_state_file_is_exposed_rather_than_refused",
            "test_content_set.NamedFormatStageTest.test_the_flag_lets_one_run_through_without_committing_a_declaration",
        ),
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
        (
            "test_content_set.NamedFormatStageTest.test_a_truncated_list_of_dumps_says_how_many_it_did_not_name",
            "test_content_set.NamedFormatStageTest.test_an_emulator_dump_is_excluded_and_counted_but_does_not_refuse",
        ),
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
        (
            "test_content_set.NamedFormatStageTest.test_a_state_file_in_the_content_set_refuses_the_run",
            "test_content_set.NamedFormatStageTest.test_the_refusal_names_every_offending_path_and_the_way_past_it",
            "test_content_set.NamedFormatsTest.test_a_state_file_is_caught_at_any_depth",
            "test_content_set.StageTest.test_it_does_not_list_its_own_outputs_as_content",
            "test_content_set.StageTest.test_it_reports_content_files_that_are_no_longer_on_disk",
            "test_content_set.StageTest.test_it_still_lists_other_files_under_knowledge",
            "test_content_set.StatusReportsTheContentSetTest.test_it_claims_no_tree_percentage_when_the_manifest_did_not_measure_one",
            "test_content_set.StatusReportsTheContentSetTest.test_it_does_not_call_a_set_stale_when_there_is_no_detect_result_to_compare",
            "test_content_set.StatusReportsTheContentSetTest.test_it_says_a_content_set_is_stale_when_detect_has_moved_on",
        ),
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
        (
            "test_content_set.NamedFormatStageTest.test_an_ordinary_infrastructure_store_is_untouched",
            "test_content_set.NamedFormatsTest.test_an_emulator_dump_is_matched_by_filename_and_a_neighbour_is_not",
        ),
    ),
    Mutation(
        "an extras-bearing pin silently stops being compared",
        "check_install_docs.py",
        "(?:\\[[^\\]]*\\])?",
        "",
        "found by review: with the extras group deleted, `alpha[deploy]==1.2.3` matches "
        "nothing and the pin is not compared at all - and the test asserted an empty "
        "result, which is equally what agreement produces. An emptiness assertion "
        "cannot tell `compared and agreed` from `never parsed`, which is the shape this "
        "whole check exists to refuse one level out",
        (
            "test_check_install_docs.TheLockMustResolveThePinTest.test_extras_and_markers_do_not_read_as_a_disagreement",
        ),
    ),
    Mutation(
        "the stage's single exit stops carrying the resolution result",
        "check_install_docs.py",
        "    return resolution",
        "    return 0",
        "found by review: there were two `return resolution` sites, so either could be "
        "severed alone, and every stage test reached only the first because all of them "
        "use a lock naming its index. The uncovered branch was the no-index one, which "
        "is the shape the older half of this check was written for. Folded to one site: "
        "the only place it can be carried, and the only place it can be broken",
        (
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_a_declared_lock_is_still_compared_when_the_input_states_a_pin",
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_a_lock_that_only_quotes_the_marker_does_not_declare_it",
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_an_undeclared_lock_with_no_input_is_still_refused",
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_the_refusal_offers_the_marker_alongside_the_two_it_already_offered",
            "test_check_install_docs.InstallDocsGateTest.test_a_lock_that_does_not_deliver_the_pin_fails_the_stage",
            "test_check_install_docs.InstallDocsGateTest.test_the_no_index_branch_still_carries_the_resolution",
            "test_check_install_docs.NothingToCompareTest.test_an_absent_input_is_refused_rather_than_read_as_agreement",
            "test_check_install_docs.NothingToCompareTest.test_an_input_stating_no_pin_is_refused_rather_than_reported_as_resolved",
        ),
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
        (
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_a_declared_lock_is_still_compared_when_the_input_states_a_pin",
            "test_check_install_docs.InstallDocsGateTest.test_a_lock_that_does_not_deliver_the_pin_fails_the_stage",
            "test_check_install_docs.InstallDocsGateTest.test_the_no_index_branch_still_carries_the_resolution",
            "test_check_install_docs.TheLockMustResolveThePinTest.test_a_lock_delivering_another_version_is_reported",
            "test_check_install_docs.TheLockMustResolveThePinTest.test_a_pin_the_lock_omits_entirely_is_distinguished",
            "test_check_install_docs.TheLockMustResolveThePinTest.test_every_pin_is_checked_rather_than_one_named_package",
            "test_check_install_docs.TheLockMustResolveThePinTest.test_extras_and_markers_do_not_read_as_a_disagreement",
        ),
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
        (
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_a_declared_lock_is_still_compared_when_the_input_states_a_pin",
            "test_check_install_docs.TheLockMustResolveThePinTest.test_a_lock_delivering_another_version_is_reported",
            "test_check_install_docs.TheLockMustResolveThePinTest.test_a_pin_the_lock_omits_entirely_is_distinguished",
            "test_check_install_docs.TheLockMustResolveThePinTest.test_every_pin_is_checked_rather_than_one_named_package",
            "test_check_install_docs.TheLockMustResolveThePinTest.test_extras_and_markers_do_not_read_as_a_disagreement",
        ),
    ),
    Mutation(
        "the resolution half stops reaching the exit code",
        "check_install_docs.py",
        "    resolution = _report_unresolved(config.REQUIREMENTS_PATH, lock)",
        "    resolution = 0",
        "a check that prints a disagreement and exits 0 is worse than one that does not "
        "look: the text scrolls past in a green run and nothing chains on it",
        (
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_a_declared_lock_is_still_compared_when_the_input_states_a_pin",
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_a_lock_that_only_quotes_the_marker_does_not_declare_it",
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_an_undeclared_lock_with_no_input_is_still_refused",
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_the_documented_command_half_still_runs_behind_the_marker",
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_the_refusal_offers_the_marker_alongside_the_two_it_already_offered",
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_the_skip_says_it_compared_nothing_rather_than_passing_quietly",
            "test_check_install_docs.InstallDocsGateTest.test_a_lock_that_does_not_deliver_the_pin_fails_the_stage",
            "test_check_install_docs.InstallDocsGateTest.test_the_no_index_branch_still_carries_the_resolution",
            "test_check_install_docs.NothingToCompareTest.test_an_absent_input_is_refused_rather_than_read_as_agreement",
            "test_check_install_docs.NothingToCompareTest.test_an_input_stating_no_pin_is_refused_rather_than_reported_as_resolved",
            "test_check_install_docs.NothingToCompareTest.test_an_input_whose_pins_all_resolve_still_passes_and_still_says_so",
        ),
    ),
    Mutation(
        "an empty pin parse reads as a clean comparison",
        "check_install_docs.py",
        "    if not pinned:",
        "    if False:",
        "#277, and the state that shipped: an empty result has two causes the code could "
        "not tell apart - every pin resolved, or no pin ever parsed - and the second "
        'printed "resolves every one of the 0 pin(s)" and exited 0, on the same path, in '
        "the same words and with the same exit code as a real pass. The route in is not a "
        "store that pins nothing on purpose but the parse quietly ceasing to match, which "
        "this library has shipped before in a repository-list reader that was green "
        "because every fixture used bare names",
        (
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_a_lock_that_only_quotes_the_marker_does_not_declare_it",
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_an_undeclared_lock_with_no_input_is_still_refused",
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_the_documented_command_half_still_runs_behind_the_marker",
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_the_refusal_offers_the_marker_alongside_the_two_it_already_offered",
            "test_check_install_docs.AHandAuthoredLockDeclaresItselfTest.test_the_skip_says_it_compared_nothing_rather_than_passing_quietly",
            "test_check_install_docs.NothingToCompareTest.test_an_absent_input_is_refused_rather_than_read_as_agreement",
            "test_check_install_docs.NothingToCompareTest.test_an_input_stating_no_pin_is_refused_rather_than_reported_as_resolved",
        ),
    ),
    Mutation(
        "an empty set of documented installs claims every one of them passes",
        "check_install_docs.py",
        "    if not installs:",
        "    if False:",
        "#277 one function along, and also the state that shipped: the affirmative line "
        "was printed over an empty list. The ways that list empties are invisible from the "
        "message - a documentation suffix the walk does not read, a SKIP_DIRS entry that "
        "grew to cover where the docs live, the lock renamed, a requirement-flag spelling "
        "the pattern does not match - and every one of them reads as a store whose "
        "commands are all correct. This half cannot refuse, because a store need not "
        "document installing from its lock at all, so naming what it read is the fix",
        (
            "test_check_install_docs.NothingToCompareTest.test_no_documented_install_is_not_reported_as_every_install_passing",
        ),
    ),
    Mutation(
        "graph-report check unwired",
        "status.py",
        "    _report_graph_report(arguments.verify_graph)",
        "    pass",
        "behaviour tested through the function; nothing drove `main()`",
        (
            "test_graph_report_staleness.WiredIntoStatusTest.test_status_reports_a_stale_report",
            "test_graph_report_staleness.WiredIntoStatusTest.test_the_verify_graph_flag_is_accepted_and_reaches_the_check",
            "test_graph_report_staleness.WiredIntoStatusTest.test_verify_graph_also_reports_contentless_nodes",
        ),
    ),
    Mutation(
        "contentless check unwired",
        "status.py",
        "    _report_contentless(nodes)",
        "    pass",
        "same escape as above, three days later, in a different check",
        (
            "test_graph_report_staleness.WiredIntoStatusTest.test_verify_graph_also_reports_contentless_nodes",
        ),
    ),
    Mutation(
        "manifest scope statement unwired",
        "build_knowledge_context.py",
        "            *scope_statement(len(repository_dirs)),",
        "",
        "the statement was tested; that it reached the written file was not",
        (
            "test_estate_boundary.ManifestTest.test_a_malformed_declaration_stops_the_manifest_build",
            "test_estate_boundary.ManifestTest.test_a_snapshot_or_alias_with_no_ruling_still_reaches_the_manifest",
            "test_estate_boundary.ManifestTest.test_completeness_is_never_claimed_either_way",
            "test_estate_boundary.ManifestTest.test_the_declaration_reaches_the_written_manifest",
            "test_estate_boundary.ManifestTest.test_the_manifest_says_when_no_boundary_is_declared",
            "test_manifest_scope.ScopeStatementTest.test_the_statement_reaches_the_written_manifest",
        ),
    ),
    Mutation(
        "precision floor defaults to off",
        "build_community_summaries.py",
        "DEFAULT_PRECISION = 0.2",
        "DEFAULT_PRECISION = 0.0",
        "every test passed the floor explicitly, so the shipped default was unheld",
        (
            "test_documented_flags.TheRealDocumentsMatchTheCode.test_every_mention_is_accounted_for_in_exactly_one_class",
            "test_documented_flags.TheRealDocumentsMatchTheCode.test_no_document_claims_a_flag_or_default_the_code_lacks",
            "test_documented_flags.TheRunnerReportsWhatItRead.test_the_real_tree_reports_its_counts_and_exits_zero",
            "test_summaries_remap.RemapCliTest.test_the_default_precision_floor_drops_a_ballooned_cluster",
            "test_summaries_remap.RemapTest.test_the_default_precision_floor_drops_a_ballooned_cluster",
            "test_summaries_remap_fallback.RemapFallbackTest.test_a_fallback_refusal_leaves_the_node_routes_verdict_standing",
        ),
    ),
    Mutation(
        "symlink exclusions ignored",
        "status.py",
        "        elif ignored(path):",
        "        elif False:",
        "a check that could not see its own mitigation, reported by an operator",
        (
            "test_missing_extractors.DuplicatingSymlinkTest.test_an_excluded_symlink_is_not_reported_as_exposed",
        ),
    ),
    Mutation(
        "distinct-target count dropped",
        "status.py",
        'found["targets"] = len(targets)',
        'found["targets"] = 0',
        "the test drove a stub, so it pinned the wording and not the computation",
        (
            "test_missing_extractors.DuplicatingSymlinkTest.test_links_sharing_a_target_are_counted_once_as_a_target",
        ),
    ),
    Mutation(
        "verify finding renamed back to 'unsupported'",
        "build_community_summaries.py",
        '"  [not in digest] community',
        '"  [unsupported] community',
        "a label that claimed 98% more than it measured, so the gate got ignored",
        (
            "test_summaries_verify.VerifyNamesWhatItMeasuresTest.test_the_digest_finding_is_not_called_unsupported",
        ),
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
        (
            "test_dashed_token_guidance.DashedRuleInTheReportTest.test_the_digest_pass_prints_the_rule_and_the_instruction",
            "test_dashed_token_guidance.DashedRuleInTheReportTest.test_the_estate_pass_prints_it_too",
            "test_dashed_token_guidance.DashedRuleInTheReportTest.test_this_gate_notices_a_dropped_element",
        ),
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
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_a_phrase_between_name_separators_still_corroborates",
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_a_truncated_name_is_still_reported_absent",
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_a_word_out_of_a_camel_case_name_is_still_reported_absent",
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_an_identifier_in_a_phrase_label_no_longer_reads_as_absent",
            "test_estate_phrase_label_segments.PhraseLabelEstateTest.test_an_invented_term_is_still_reported_absent",
            "test_estate_segment_match.AbsentFromEstateTest.test_a_genuinely_absent_term_is_still_reported",
            "test_estate_segment_match.AbsentFromEstateTest.test_a_scoped_package_no_longer_reads_as_absent",
            "test_estate_segment_match.AbsentFromEstateTest.test_a_short_term_is_not_matched_against_a_segment",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_store_with_no_history_datasets_does_not_claim_the_split",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_term_a_recorded_file_path_names_is_credited_to_history",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_term_in_neither_the_graph_nor_history_is_the_class_that_can_contain_invention",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_ticket_the_history_datasets_cite_is_not_counted_as_possible_invention",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_history_credits_a_whole_token_rather_than_a_longer_name_containing_it",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_the_two_classes_sum_to_the_flagged_total",
            "test_summaries_verify.VerifyNamesWhatItMeasuresTest.test_a_term_in_neither_is_reported_as_a_candidate_not_a_fabrication",
        ),
    ),
    Mutation(
        "partitioner check unwired",
        "status.py",
        "    _report_clustering()",
        "    pass",
        "the third instance of this escape: reported through the function, never through main()",
        ("test_clustering_partitioner.Reported.test_status_main_drives_the_check",),
    ),
    Mutation(
        "unrecorded partitioner defaults to a partitioner",
        "record_clustering.py",
        "    return named if named in PARTITIONER_NAMES else None",
        "    return named or LOUVAIN",
        "an absent measurement reading as a clean result - shipped twice in one week",
        (
            "test_clustering_partitioner.Recording.test_an_unrecognised_recorded_value_reads_as_unknown",
            "test_clustering_partitioner.Recording.test_no_record_at_all_reads_as_unknown",
        ),
    ),
    Mutation(
        "a broken graspologic reported as the fallback",
        "record_clustering.py",
        '        return None, f"importing graspologic raised',
        '        return LOUVAIN, f"importing graspologic raised',
        "graphify does not catch that import error, so Louvain there is a guess, not a probe",
        (
            "test_clustering_partitioner.Detection.test_a_broken_graspologic_is_undeterminable_not_louvain",
        ),
    ),
    Mutation(
        "seed state not recorded",
        "record_clustering.py",
        '"hash_randomised": hash_randomisation(),',
        "",
        "a store that clustered unseeded then looks identical to one that did not",
        (
            "test_clustering_partitioner.DescribesTheGraphThatShips.test_the_record_captures_whether_hashes_were_randomised",
        ),
    ),
    Mutation(
        "unpinned hashes not reported at record time",
        "record_clustering.py",
        "    if hash_randomisation():",
        "    if False:",
        "the record would say unseeded while the run said nothing",
        (
            "test_clustering_partitioner.DescribesTheGraphThatShips.test_an_unpinned_run_says_so_at_record_time",
        ),
    ),
    Mutation(
        "record does not say which file it described",
        "record_clustering.py",
        '"described": config.GRAPH_PATH.name,',
        "",
        "a reader then guesses which of a store's two graph files it refers to",
        (
            "test_clustering_partitioner.DescribesTheGraphThatShips.test_an_explicit_graph_overrides_the_default",
            "test_clustering_partitioner.DescribesTheGraphThatShips.test_the_record_says_which_file_it_described",
            "test_clustering_partitioner.TheCommittedGraphCanBeDescribed.test_a_gzipped_graph_can_be_read",
        ),
    ),
    Mutation(
        "absolute-path check unwired",
        "status.py",
        "    _report_absolute_paths(arguments.paths)",
        "    pass",
        "#176: the fifth instance of this repository's most repeated escape - four "
        "entries above are the same shape, in the same module, and each was written "
        "after the previous one was fixed",
        ("test_status_absolute_paths.ReachableFromTheCommandLine.test_the_flag_reaches_the_check",),
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
        (
            "test_status_absolute_paths.WhatCounts.test_an_absolute_path_outside_the_store_is_not_a_finding",
        ),
    ),
    Mutation(
        "unreadable tracked files reported as a clean store",
        "status.py",
        '    if not scan["files"]:',
        "    if False:",
        "the '0 checked, none dangling' defect the corpus-citation check in this same "
        "module already shipped once - a measurement of nothing paired with a clean "
        "verdict, which reads as a pass",
        (
            "test_status_absolute_paths.TheReport.test_a_store_whose_files_could_not_be_read_is_not_reported_as_clean",
        ),
    ),
    Mutation(
        "absolute paths lost at a read-block boundary",
        "status.py",
        "        if match.end() > end:",
        "        if False:",
        "the scan streams because a store's tracked artefacts run to gigabytes "
        "decompressed, and a path cut in half by a block boundary is the silent half "
        "of that trade: no count can show what it failed to see",
        (
            "test_status_absolute_paths.ReachableFromTheCommandLine.test_the_flag_reaches_the_check",
            "test_status_absolute_paths.TheIntendedException.test_an_in_flight_artefact_is_separated_from_the_findings",
            "test_status_absolute_paths.TheReport.test_more_files_than_the_report_names_are_counted_but_summarised",
            "test_status_absolute_paths.TheReport.test_the_finding_names_the_count_the_file_and_the_rule",
            "test_status_absolute_paths.WhatCounts.test_a_path_this_store_wrote_absolute_is_counted_and_its_file_named",
            "test_status_absolute_paths.WhatCounts.test_an_absolute_path_from_another_machine_is_still_a_finding",
            "test_status_absolute_paths.WhatIsRead.test_a_compressed_tracked_file_is_read",
            "test_status_absolute_paths.WhatIsRead.test_a_corrupt_archive_does_not_take_the_stage_down",
            "test_status_absolute_paths.WhatIsRead.test_a_path_straddling_a_read_block_boundary_is_counted_once",
            "test_status_absolute_paths.WhatIsRead.test_an_unreadable_tracked_file_is_named_rather_than_passed_over",
        ),
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
        (
            "test_status_absolute_paths.WhatIsRead.test_a_path_spanning_the_hold_back_point_is_not_lost",
        ),
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
        (
            "test_dangling_endpoints.Classification.test_an_endpoint_naming_exactly_one_node_is_recoverable",
            "test_dangling_endpoints.Classification.test_an_endpoint_naming_two_nodes_is_ambiguous_and_not_recovered",
            "test_dangling_endpoints.Classification.test_the_rate_is_recovered_over_dangling_and_excludes_ambiguity",
            "test_dangling_endpoints.TheStageIsWiredAndWritesNothing.test_the_json_output_reconciles_with_the_printed_report",
        ),
    ),
    Mutation(
        "entity names matched against the whole path-qualified id",
        "measure_dangling_endpoints.py",
        '    return str(value).strip().rsplit("::", 1)[-1].casefold()',
        "    return str(value).strip().casefold()",
        "#162, and the shape this codebase has shipped most often: a name matched "
        "against a path-qualified id cannot match, so the rate comes back a clean "
        "0.0% and reads as an estate with nothing to fix",
        (
            "test_dangling_endpoints.EntityNaming.test_a_node_answering_to_one_name_twice_counts_once",
            "test_dangling_endpoints.EntityNaming.test_the_final_scope_segment_is_the_entity_name",
        ),
    ),
    Mutation(
        "recovery stops looking at local_id",
        "measure_dangling_endpoints.py",
        '    keys = {entity_name(node.get("id")), entity_name(node.get("local_id"))}',
        '    keys = {entity_name(node.get("id"))}',
        "#162's whole claim is that local_id carries the entity name; without it "
        "the estate whose endpoints dangle because a chunk named an id defined in "
        "another chunk measures zero, which is the answer that closes the issue",
        (
            "test_dangling_endpoints.LocalIdIsWhatMakesRecoveryPossible.test_a_node_is_found_by_local_id_when_its_id_shares_nothing",
        ),
    ),
    Mutation(
        "an empty walk reports a clean rate",
        "measure_dangling_endpoints.py",
        '    return (graphs, "") if graphs else ([], _no_graphs_message(repositories))',
        '    return graphs, ""',
        "#162: every stage in this library that shipped doing nothing did so with "
        "a passing suite, and '0 dangling endpoints' over a walk that read no "
        "files is indistinguishable from an estate that has none",
        (
            "test_dangling_endpoints.AntiVacuity.test_a_walk_that_finds_no_graphs_fails_rather_than_reporting_a_rate",
            "test_dangling_endpoints.AntiVacuity.test_the_merged_estate_graph_is_named_as_not_a_substitute",
        ),
    ),
    Mutation(
        "graphs that could not be measured reported as a measured zero",
        "measure_dangling_endpoints.py",
        "    if not any(m.measurable for m in measurements):",
        "    if False:",
        "#162: a graph with no edges has no endpoints to dangle, so every count "
        "is zero and the rate is undefined - printed as a result it says the "
        "store is clean",
        ("test_dangling_endpoints.AntiVacuity.test_a_graph_with_no_edges_is_not_measured",),
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
        (
            "test_dangling_endpoints.AntiVacuity.test_the_merged_estate_graph_is_named_as_not_a_substitute",
        ),
    ),
    Mutation(
        "the same graph measured twice from one clone",
        "measure_dangling_endpoints.py",
        "                found.append(candidate)\n                break",
        "                found.append(candidate)",
        "#162: a clone holding both graph forms would have every count doubled "
        "while the rate stayed put - an error that reconciles internally, which "
        "is the kind nobody finds",
        (
            "test_dangling_endpoints.SaysWhatItRead.test_one_clone_holding_both_graph_forms_is_measured_once_and_says_so",
        ),
    ),
    Mutation(
        "the report stops naming the artefacts it read",
        "measure_dangling_endpoints.py",
        "        *_read_lines(measurements),",
        "",
        "#162: a rate measured downstream of a store's own fix is not a rate, and "
        "the only way a reader can tell which it got is the list of files. A "
        "check's silence licenses a claim only about the artefact it read",
        (
            "test_dangling_endpoints.SaysWhatItRead.test_every_graph_read_is_named_in_the_output",
            "test_dangling_endpoints.SaysWhatItRead.test_one_clone_holding_both_graph_forms_is_measured_once_and_says_so",
            "test_dangling_endpoints.TheStageIsWiredAndWritesNothing.test_an_unreadable_graph_is_named_rather_than_taking_the_stage_down",
        ),
    ),
    Mutation(
        "named endpoints printed in set order",
        "measure_dangling_endpoints.py",
        "    result.classified = {kind: sorted(found) for kind, found in buckets.items()}",
        "    result.classified = {kind: list(found) for kind, found in buckets.items()}",
        "#162: two runs on the same graph must be byte-identical, and hash "
        "randomisation makes an unsorted list invisible until someone diffs two "
        "builds from different processes",
        (
            "test_dangling_endpoints.Determinism.test_named_endpoints_come_out_sorted_whatever_order_they_were_read_in",
        ),
    ),
    Mutation(
        "the page's edge list falls back to set order",
        "build_explorer.py",
        "        for neighbour in sorted(adjacency.get(node_id, ())):",
        "        for neighbour in adjacency.get(node_id, ()):",
        "#276: the observer for the two-build diff, and the reason that check had "
        "to exist. `adjacency` holds sets, so without the sort the edge block is "
        "emitted in hash order and the page differs between two builds of an "
        "unchanged store - measured: with this applied, one test of 1,451 fails, "
        "and before `test_two_builds_are_identical` none did. The page is "
        "committed in consumer repositories, so the cost is every rebuild landing "
        "as a spurious diff with any real change buried in it",
        (
            "test_build_explorer.KeptEdgesTest.test_the_pair_order_does_not_follow_the_arrival_order_of_the_neighbours",
            "test_two_builds_are_identical.TwoBuildsAreByteIdentical.test_every_artefact_is_byte_identical",
        ),
    ),
    Mutation(
        "retained failure double-counted",
        "sync_repositories.py",
        "total = len({*entries, *(name for name, _ in failures)})",
        "total = len(entries) + len(failures)",
        "shipped in v0.11.5; found by an estate, not by this suite",
        (
            "test_repo_list_and_sync.FailedSyncKeepsProvenanceTest.test_the_failure_count_does_not_double_count_a_retained_record",
        ),
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
        (
            "test_build_intent_index.BranchTextTest.test_the_run_counts_tickets_carrying_branch_text",
            "test_telemetry.ExplorerWiringTest.test_a_join_that_died_between_builds_is_warned_about",
            "test_telemetry.IntentWiringTest.test_a_collapsed_inventory_is_warned_about_on_the_next_build",
            "test_telemetry.IntentWiringTest.test_the_stage_records_how_much_work_is_only_proposed",
            "test_telemetry.IntentWiringTest.test_the_stage_records_repositories_whose_history_predates_reach",
            "test_telemetry.TelemetryRecordTest.test_a_measurement_that_fell_to_zero_is_a_warning_not_a_statistic",
            "test_telemetry.TelemetryRecordTest.test_a_measurement_that_merely_fell_stays_a_statistic",
            "test_telemetry.TelemetryRecordTest.test_a_metric_that_was_zero_and_is_not_reads_as_recovery",
            "test_telemetry.TelemetryRecordTest.test_another_stage_s_measurements_survive_this_stage_s_record",
            "test_telemetry.TelemetryRecordTest.test_the_second_record_reports_what_moved_since_the_first",
        ),
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
        (
            "test_telemetry.ExplorerWiringTest.test_a_join_that_died_between_builds_is_warned_about",
            "test_telemetry.IntentWiringTest.test_a_collapsed_inventory_is_warned_about_on_the_next_build",
            "test_telemetry.TelemetryRecordTest.test_a_measurement_that_fell_to_zero_is_a_warning_not_a_statistic",
        ),
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
        (
            "test_build_intent_index.BranchTextTest.test_the_run_counts_tickets_carrying_branch_text",
            "test_telemetry.IntentWiringTest.test_the_stage_records_how_much_work_is_only_proposed",
            "test_telemetry.IntentWiringTest.test_the_stage_records_repositories_whose_history_predates_reach",
            "test_telemetry.TelemetryRecordTest.test_another_stage_s_measurements_survive_this_stage_s_record",
        ),
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
        (
            "test_telemetry.LayerMergeWiringTest.test_the_recorded_counts_recover_the_previous_ratio",
            "test_telemetry.LayerMergeWiringTest.test_the_stage_records_both_layer_sizes",
        ),
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
        (
            "test_build_explorer.PageByteAttributionTest.test_telemetry_records_the_breakdown_beside_the_page_size",
            "test_telemetry.ExplorerWiringTest.test_a_join_that_died_between_builds_is_warned_about",
            "test_telemetry.ExplorerWiringTest.test_the_page_records_the_join_it_shipped",
            "test_telemetry.ExplorerWiringTest.test_the_recorded_join_counts_the_column_the_page_ships",
        ),
    ),
    Mutation(
        "equal-degree connections fall back to hash order",
        "build_explorer.py",
        "    for neighbour in sorted(adjacency.get(node_id, ()), key=lambda n: (-degree[n], n)):",
        "    for neighbour in sorted(adjacency.get(node_id, ()), key=lambda n: -degree[n]):",
        "#280 measured it before this entry had an observer: dropping the `n` passed the "
        "entire suite, and the page regression with it. `adjacency` holds sets and the "
        "sort is stable, so without the name half two equally connected neighbours print "
        "in whatever order the process's hash seed iterated them, and the connection line "
        "on a committed page differs between two builds of one graph - hash randomisation "
        "has broken determinism here before and is invisible until somebody diffs two "
        "pages. The observer has to supply the arrival order itself: the fixture does hold "
        "a tied pair that reaches the page, but comparing two builds only sees the swap "
        "when the two seeds happen to disagree, which is a coin toss rather than a gate",
        (
            "test_build_explorer.EntryConnectionsTest.test_equal_degree_neighbours_are_ordered_by_id_not_by_arrival",
        ),
    ),
    Mutation(
        "page bytes counted as characters",
        "build_explorer.py",
        '        block_name(placeholder): occurrences * len(blocks[placeholder].encode("utf-8"))',
        "        block_name(placeholder): occurrences * len(blocks[placeholder])",
        "#245: the page is UTF-8 and its prose blocks carry non-ASCII, so a character "
        "count is a plausible number that is not the file's size - the exact class of "
        "wrong measurement this pipeline has shipped, correct code answering a "
        "neighbouring question",
        (
            "test_build_explorer.PageByteAttributionTest.test_a_page_over_the_hard_limit_is_refused_instead_of_written",
            "test_build_explorer.PageByteAttributionTest.test_every_reported_block_is_the_size_of_that_block_in_the_page",
            "test_build_explorer.PageByteAttributionTest.test_the_breakdown_reconciles_with_the_page_on_disk",
            "test_build_explorer.PageByteAttributionTest.test_the_warning_names_the_largest_blocks_above_the_threshold",
        ),
    ),
    Mutation(
        "a slot filled twice charged once",
        "build_explorer.py",
        '        block_name(placeholder): occurrences * len(blocks[placeholder].encode("utf-8"))',
        '        block_name(placeholder): len(blocks[placeholder].encode("utf-8"))',
        "#245: `str.replace` fills every slot and `__TITLE__` has two, so a breakdown "
        "counting each block once is short by the title on every build - and it still "
        "adds up, because the residual line absorbs exactly what was missed",
        (
            "test_build_explorer.PageByteAttributionTest.test_a_page_over_the_hard_limit_is_refused_instead_of_written",
            "test_build_explorer.PageByteAttributionTest.test_the_breakdown_reconciles_with_the_page_on_disk",
            "test_build_explorer.PageByteAttributionTest.test_the_warning_names_the_largest_blocks_above_the_threshold",
        ),
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
        (
            "test_build_explorer.PageByteAttributionTest.test_a_residual_between_the_attribution_and_the_file_is_named",
        ),
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
        (
            "test_build_explorer.PageByteAttributionTest.test_a_block_whose_slot_left_the_template_is_refused",
            "test_build_explorer.PageByteAttributionTest.test_a_new_template_block_is_refused_rather_than_left_unattributed",
        ),
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
        (
            "test_build_explorer.PageByteAttributionTest.test_a_page_over_the_warning_and_under_the_limit_still_only_warns",
            "test_build_explorer.PageByteAttributionTest.test_the_threshold_comes_from_the_environment",
            "test_build_explorer.PageByteAttributionTest.test_the_warning_arrives_before_the_page_is_written",
            "test_build_explorer.PageByteAttributionTest.test_the_warning_names_the_largest_blocks_above_the_threshold",
        ),
    ),
    Mutation(
        "the oversized page refused in the message and written anyway",
        "build_explorer.py",
        '                "explorer.refused_over_bytes": config.EXPLORER_MAX_BYTES,\n            }\n        )\n        return 1',
        '                "explorer.refused_over_bytes": config.EXPLORER_MAX_BYTES,\n            }\n        )',
        "#245's last part, and the shape the stage shipped in: the size was printed, the "
        "page was written, and the stage exited 0. A message an exit code contradicts is "
        "read as advice - the build was committed and the store met the real refusal at "
        "`git push`, which names neither the stage nor the layer that grew. The mutation "
        "leaves the text intact and removes only the refusal, because the text was never "
        "the missing half",
        (
            "test_build_explorer.PageByteAttributionTest.test_a_page_over_the_hard_limit_is_refused_instead_of_written",
            "test_build_explorer.PageByteAttributionTest.test_a_refused_build_leaves_the_page_the_store_already_had",
            "test_build_explorer.PageByteAttributionTest.test_a_refused_build_records_why_it_stopped",
            "test_build_explorer.PageByteAttributionTest.test_the_hard_limit_comes_from_the_environment",
        ),
    ),
    Mutation(
        "the breakdown computed and never printed",
        "build_explorer.py",
        '    print(breakdown_report(breakdown, size_bytes, config.EXPLORER_PATH), end="")',
        "    pass",
        "#245: telemetry is a committed JSON file, and the operator meeting a page too "
        "large to push is reading a terminal. A number recorded where nobody looks is "
        "the reporting half of the same gap the issue was opened about",
        (
            "test_build_explorer.PageByteAttributionTest.test_every_reported_block_is_the_size_of_that_block_in_the_page",
            "test_build_explorer.PageByteAttributionTest.test_every_substituted_block_appears_in_the_breakdown",
            "test_build_explorer.PageByteAttributionTest.test_telemetry_records_the_breakdown_beside_the_page_size",
            "test_build_explorer.PageByteAttributionTest.test_the_breakdown_reconciles_with_the_page_on_disk",
            "test_build_explorer.PageByteAttributionTest.test_the_warning_names_the_largest_blocks_above_the_threshold",
        ),
    ),
    Mutation(
        "the breakdown never reaches the record",
        "build_explorer.py",
        '        **{f"explorer.bytes_{name}": size for name, size in sorted(breakdown.items())},',
        "        **{},",
        "#245 over #154: one build's breakdown says where the bytes are, and only the "
        "predecessor says which block grew. The jump this was reported for happened "
        "between two builds, which no single build can attribute at all",
        (
            "test_build_explorer.PageByteAttributionTest.test_telemetry_records_the_breakdown_beside_the_page_size",
        ),
    ),
    Mutation(
        "the page's edge pairs follow hash order again",
        "build_explorer.py",
        "        for neighbour in sorted(adjacency.get(node_id, ())):",
        "        for neighbour in adjacency.get(node_id, ()):",
        "#284: `adjacency` holds sets, so dropping the sort emits the edge pairs in "
        "per-process hash order - three distinct pages across four hash seeds, with the "
        "suite and the page regression both green. The tiebreak was present, correct and "
        "unobserved, which is indistinguishable from absent",
        (
            "test_build_explorer.KeptEdgesTest.test_the_pair_order_does_not_follow_the_arrival_order_of_the_neighbours",
            "test_two_builds_are_identical.TwoBuildsAreByteIdentical.test_every_artefact_is_byte_identical",
        ),
    ),
    Mutation(
        "equal-degree entries fall back to the graph file's order",
        "build_explorer.py",
        "    kept.sort(key=lambda item: (-degree[item[0]], item[0]))",
        "    kept.sort(key=lambda item: -degree[item[0]])",
        "#284, and the more serious half: this sort shipped with no name tiebreak at all, "
        "deterministic only because `nodes` preserves the order the graph file listed its "
        "nodes in - an assumption about a peer tool's serialisation that nothing stated "
        "and nothing checked. The entry order is the page's own tiebreak, since every "
        "client-side ranking of equally-weighted entries falls back to it, so anything "
        "rebuilding `nodes` from a set or a dict would reorder rankings throughout the "
        "page with no check here able to notice",
        (
            "test_build_explorer.BuildIndexTest.test_equal_degree_entries_are_ordered_by_id_whatever_order_the_graph_lists_them",
        ),
    ),
    Mutation(
        "the indexed inventory measured and discarded",
        "build_intent_index.py",
        "    telemetry.record(summarise(index, commits_seen, report))",
        "    summarise(index, commits_seen, report)",
        "#154: a corpus inventory that collapsed reported a plausible smaller number and "
        "read as a smaller estate; nothing but its predecessor contradicts it",
        (
            "test_telemetry.IntentWiringTest.test_a_collapsed_inventory_is_warned_about_on_the_next_build",
            "test_telemetry.IntentWiringTest.test_the_stage_records_how_much_work_is_only_proposed",
            "test_telemetry.IntentWiringTest.test_the_stage_records_the_inventory_it_indexed",
        ),
    ),
    Mutation(
        "the record never reaches an operator",
        "status.py",
        "    _report_telemetry()\n\n    _report_graph_report",
        "    _report_graph_report",
        "#154: reporting through a function while nothing drives the CLI is the most "
        "repeated escape in this repository, and `status` alone accounts for three "
        "existing entries here",
        (
            "test_telemetry.StatusReportsTheRecordTest.test_status_prints_every_recorded_measurement",
            "test_telemetry.StatusReportsTheRecordTest.test_status_says_when_nothing_has_been_recorded",
        ),
    ),
    Mutation(
        "the ranking stage is unreachable from the CLI",
        "cli.py",
        '    "gaps": (\n        "report_ingestion_gaps",',
        '    "gaps-unreachable": (\n        "report_ingestion_gaps",',
        "the unwired-stage class, twice shipped here: the reader is tested, the "
        "report is right, and nothing a user or a skill can type reaches it - while "
        "the documentation that tells them to type it still passes review",
        (
            "test_cli_help.SelfParsingStaysHonest.test_every_self_parsing_stage_really_builds_a_parser",
            "test_cli_help.SelfParsingStaysHonest.test_no_stage_outside_the_list_builds_a_parser",
            "test_documented_flags.TheRealDocumentsMatchTheCode.test_every_stage_module_is_read",
            "test_documented_stages.DocumentedStagesExist.test_every_documented_stage_is_a_real_stage",
            "test_documented_stages.EveryStageIsDocumented.test_every_stage_is_named_by_a_shipped_document",
            "test_ingestion_gaps.TheStageIsWired.test_the_cli_runs_the_stage_and_reports_a_finding_without_failing",
            "test_repo_list_and_sync.CliTest.test_stages_are_listed_in_pipeline_order",
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
        (
            "test_ingestion_gaps.RankingAndSubtraction.test_a_coordinate_the_estate_builds_is_not_a_candidate",
            "test_ingestion_gaps.TheReport.test_the_limit_says_how_much_it_hid",
            "test_ingestion_gaps.TheReport.test_the_scope_of_what_was_read_is_stated",
        ),
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
        (
            "test_ingestion_gaps.RankingAndSubtraction.test_a_domain_namespace_ranks_above_a_heavier_framework_one",
        ),
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
        (
            "test_ingestion_gaps.RankingAndSubtraction.test_namespaces_of_equal_weight_are_ordered_by_name",
        ),
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
        (
            "test_ingestion_gaps.RankingAndSubtraction.test_the_weights_are_declaring_files_split_by_scope",
        ),
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
        (
            "test_ingestion_gaps.RankingAndSubtraction.test_a_coordinate_the_estate_builds_is_not_a_candidate",
            "test_ingestion_gaps.RankingAndSubtraction.test_generated_and_vendored_directories_are_not_read_as_this_estate",
            "test_ingestion_gaps.RankingAndSubtraction.test_the_estate_reads_every_build_format_it_holds",
            "test_ingestion_gaps.RankingAndSubtraction.test_the_weights_are_declaring_files_split_by_scope",
            "test_ingestion_gaps.TheReport.test_the_built_side_says_how_much_of_it_is_convention",
            "test_ingestion_gaps.TheReport.test_the_scope_of_what_was_read_is_stated",
        ),
    ),
    Mutation(
        "an off-host alias becomes a repository to ingest",
        "report_ingestion_gaps.py",
        "        name = aliases.get(provider, provider)",
        "        name = provider",
        "a false absence invented inside the report whose subject is false absence: "
        "a module consumed under the off-host name of a repository the store already "
        "holds is reported as something to go and find",
        (
            "test_ingestion_gaps.BoundaryIntegration.test_a_module_consumed_under_an_off_host_alias_of_a_held_repository_is_no_gap",
        ),
    ),
    Mutation(
        "an unscoped package makes every public dependency internal",
        "report_ingestion_gaps.py",
        "    counts = Counter(namespace_of(coordinate.group) for coordinate in built if coordinate.group)",
        "    counts = Counter(namespace_of(coordinate.group) for coordinate in built)",
        "one unscoped npm package published by the estate turns the empty namespace "
        "into an internal one, after which the whole of npm is a candidate to ingest - "
        "a check that cannot fire on Maven and fires on everything under npm",
        (
            "test_ingestion_gaps.NamespaceDerivation.test_an_unscoped_built_package_does_not_make_every_dependency_internal",
        ),
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
        (
            "test_ingestion_gaps.BoundaryIntegration.test_an_unreadable_declaration_is_reported_not_read_as_no_boundary",
        ),
    ),
    Mutation(
        "the refusal to resolve a coordinate stops reaching the reader",
        "report_ingestion_gaps.py",
        '    lines += ["", FOOTER]',
        "    lines += []",
        "an operator who is not told a coordinate is unresolved reads the namespace as "
        "a repository name and searches the forge for it, which returns nothing for an "
        "artefact published to a binary repository and rate-limits while doing so",
        (
            "test_ingestion_gaps.TheReport.test_the_report_refuses_to_resolve_a_coordinate_to_a_repository",
        ),
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
        (
            "test_content_set.ClonesThatContributedNothingTest.test_a_repository_only_the_detect_result_names_is_neither_clone_nor_contributor",
            "test_content_set.ClonesThatContributedNothingTest.test_a_store_whose_single_clone_is_all_but_empty_is_refused_and_told_why",
            "test_content_set.ClonesThatContributedNothingTest.test_it_refuses_when_no_clone_on_disk_contributed_anything",
        ),
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
        (
            "test_content_set.ClonesThatContributedNothingTest.test_a_clone_that_contributed_nothing_is_named_and_the_run_still_passes",
            "test_content_set.ClonesThatContributedNothingTest.test_a_clone_the_declaration_never_named_still_counts_as_a_clone",
        ),
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
        (
            "test_content_set.ClonesThatContributedNothingTest.test_a_clone_that_contributed_nothing_is_named_and_the_run_still_passes",
            "test_content_set.ClonesThatContributedNothingTest.test_a_clone_the_declaration_never_named_still_counts_as_a_clone",
            "test_content_set.ClonesThatContributedNothingTest.test_a_repository_only_the_detect_result_names_is_neither_clone_nor_contributor",
            "test_content_set.ClonesThatContributedNothingTest.test_a_store_whose_single_clone_is_all_but_empty_is_refused_and_told_why",
            "test_content_set.ClonesThatContributedNothingTest.test_it_refuses_when_no_clone_on_disk_contributed_anything",
        ),
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
        (
            "test_content_set.ClonesThatContributedNothingTest.test_a_clone_that_contributed_nothing_is_named_and_the_run_still_passes",
            "test_content_set.ClonesThatContributedNothingTest.test_a_clone_the_declaration_never_named_still_counts_as_a_clone",
        ),
    ),
    Mutation(
        "a named test that stopped observing reads as caught",
        "tests/mutation_gate.py",
        "        elif len(observed) " + "< len(mutation.observers):",
        "        elif False:",
        "the sweep already knows which named tests failed; reading any one of them as "
        "the entry holding let a test #406 stopped failing pass every pull request and "
        "turn the nightly `--verify-mapping` red for days after the merge",
        (
            "test_mutation_gate_mapping.MutationGateMappingTest.test_a_named_test_that_stopped_observing_fails_the_sweep_beside_one_that_did",
        ),
    ),
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
        (
            "test_mutation_gate_recovery.MutationGateRecoveryTest.test_a_termination_signal_leaves_the_source_file_byte_identical",
            "test_mutation_gate_recovery.MutationGateRecoveryTest.test_the_signal_handlers_are_removed_once_the_loop_is_over",
        ),
    ),
    Mutation(
        "the record a SIGKILL cannot destroy is never written",
        "tests/mutation_gate.py",
        "    RECOVERY_PATH.write_bytes(_header(mutation.module) " + '+ b"\\n" + original)',
        "    pass",
        "#227: no signal handler covers SIGKILL, so the only thing that can put a file "
        "back is bytes already on disk when the process died. Without them the tree keeps "
        "the mutation and the next suite run in it reports real-looking failures in real code",
        (
            "test_mutation_gate_recovery.MutationGateRecoveryTest.test_a_clean_run_leaves_no_record_behind",
            "test_mutation_gate_recovery.MutationGateRecoveryTest.test_a_record_left_by_a_killed_run_is_used_before_anything_else_runs",
            "test_mutation_gate_visibility.MutationGateVisibilityTest.test_the_record_apply_writes_names_the_tree_and_the_process",
        ),
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
        (
            "test_mutation_gate_recovery.MutationGateRecoveryTest.test_a_record_left_by_a_killed_run_is_used_before_anything_else_runs",
        ),
    ),
    Mutation(
        "a used recovery record is left on disk",
        "tests/mutation_gate.py",
        "    RECOVERY_PATH.unlink(" + "missing_ok=True)",
        "    pass",
        "#227: a record that outlives its restore describes a file that is no longer "
        "mutated, so the next run overwrites a healthy file with older bytes - a stale "
        "artefact read as current, the same class as the leftover graph refusals above",
        (
            "test_mutation_gate_recovery.MutationGateRecoveryTest.test_a_clean_run_leaves_no_record_behind",
            "test_mutation_gate_recovery.MutationGateRecoveryTest.test_a_termination_signal_leaves_the_source_file_byte_identical",
            "test_mutation_gate_visibility.MutationGateVisibilityTest.test_the_record_apply_writes_names_the_tree_and_the_process",
            "test_observer_staleness.SettlingReadsWhatTheArrivalsObserveTest.test_an_arrived_observer_the_entry_already_names_is_clean",
            "test_observer_staleness.SettlingReadsWhatTheArrivalsObserveTest.test_an_arrived_observer_the_entry_does_not_name_is_stale",
            "test_observer_staleness.SettlingReadsWhatTheArrivalsObserveTest.test_an_arrived_test_failing_before_any_mutation_cannot_be_judged",
            "test_observer_staleness.SettlingReadsWhatTheArrivalsObserveTest.test_an_arrived_test_that_observes_nothing_is_clean",
        ),
    ),
    Mutation(
        "the query cannot tell a mutated tree from a clean one",
        "tests/mutation_gate.py",
        "    present = RECOVERY_PATH.is_file" + "()",
        "    present = False",
        "#268: a mutation sits in a real file while the gate runs, and from "
        "outside the process it looks like an ordinary edit - one reader staged it and "
        "nearly committed it, another read the tree mid-mutation and refused a job for a "
        "reason that was untrue a minute later. The pre-commit hook is this query, so a "
        "query that always reports clean is a hook that reads as protection and is none",
        (
            "test_mutation_gate_visibility.MutationGateVisibilityTest.test_a_record_the_query_cannot_read_is_refused_rather_than_ignored",
            "test_mutation_gate_visibility.MutationGateVisibilityTest.test_the_live_remedy_says_how_to_find_which_checkout_a_run_holds",
            "test_mutation_gate_visibility.MutationGateVisibilityTest.test_the_pre_commit_hook_runs_the_query_and_refuses_a_live_mutation",
            "test_mutation_gate_visibility.MutationGateVisibilityTest.test_the_query_carries_the_pid_for_a_caller_that_wants_to_signal_it",
            "test_mutation_gate_visibility.MutationGateVisibilityTest.test_the_query_refuses_while_a_mutation_is_applied_and_names_the_path",
            "test_mutation_gate_visibility.MutationGateVisibilityTest.test_the_query_tells_an_abandoned_record_from_a_live_run",
        ),
    ),
    Mutation(
        "the record stops naming the run that holds the tree",
        "tests/mutation_gate.py",
        '        "root": str(ROOT),\n        "pid": os.getpid' + "(),",
        '        "root": "",\n        "pid": 0,',
        "#268: existence is not identity. A record saying a mutation is applied somewhere "
        "cannot stop a session stopping the wrong checkout's run, which is what happened - "
        "a match on this script's name found every clone on the machine and killed another "
        "worktree's wrapper. The root and the pid are what let a caller ask about the tree "
        "it cares about and signal the one process that holds it",
        (
            "test_mutation_gate_visibility.MutationGateVisibilityTest.test_the_record_apply_writes_names_the_tree_and_the_process",
        ),
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
        (
            "test_extract_ast.ExtractAstTest.test_a_per_clone_output_directory_is_refused_too",
            "test_extract_ast.ExtractAstTest.test_the_pipelines_own_output_is_refused",
            "test_extract_ast.MovementTest.test_gone_and_new_are_reported_as_different_things",
            "test_extract_ast.TimeLimitTest.test_a_repository_that_hangs_is_timed_out_named_and_counted",
            "test_extract_ast.TimeLimitTest.test_an_extractor_that_swallows_exceptions_cannot_swallow_the_bound",
            "test_file_list_route_guidance.TheStageTakesAListRatherThanWalking.test_both_input_routes_are_still_the_only_two",
            "test_file_list_route_guidance.TheStageTakesAListRatherThanWalking.test_the_module_never_walks_a_directory",
        ),
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
        (
            "test_file_list_route_guidance.TheGuideStillExplainsWhy.test_the_guide_states_the_mechanism",
        ),
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
        (
            "test_file_list_route_guidance.TheGuideStillExplainsWhy.test_the_version_the_table_was_measured_against_is_named",
        ),
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
        (
            "test_documented_stages.TheBuildSkillChecksTheStagesItUses.test_the_build_skill_tells_a_reader_to_check_for_them",
        ),
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
        (
            "test_mutation_gate_bytecode.SuiteSubprocessBytecodeTest.test_the_suite_subprocess_is_told_not_to_write_bytecode",
        ),
    ),
    Mutation(
        "bytecode writing is disabled with a value CPython ignores",
        "tests/mutation_gate.py",
        '        env={**os.environ, "PYTHONDONT' + 'WRITEBYTECODE": "1"},',
        '        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "0"},',
        '#228: the variable is read as a flag, so "0" and "" both leave bytecode '
        "writing on while the name sits in the environment looking correct - the defect "
        "class this gate exists for, a check present and doing nothing",
        (
            "test_mutation_gate_bytecode.SuiteSubprocessBytecodeTest.test_the_suite_subprocess_is_told_not_to_write_bytecode",
        ),
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
        (
            "test_mutation_gate_bytecode.SuiteSubprocessBytecodeTest.test_the_suite_subprocess_keeps_the_environment_it_inherits",
            "test_mutation_gate_refusals.MutationGateRefusalsTest.test_a_suite_red_for_its_own_reasons_still_refuses_to_derive",
            "test_mutation_gate_refusals.MutationGateRefusalsTest.test_an_entry_with_no_observers_can_still_be_derived",
        ),
    ),
    Mutation(
        "the second interpolation dialect dropped",
        "deploy_values.py",
        "    return _DOLLAR_INTERPOLATION.sub(PLACEHOLDER, stripped)",
        "    return stripped",
        "#88: two deployment layouts interpolate differently, `{{ }}` and `${ }`, and the "
        "second going unstripped publishes the variable names the first withholds. Nothing "
        "counts a name that was not withheld, so both layouts' output reads as healthy",
        (
            "test_deploy_values.StripTemplateDollarDialect.test_a_dollar_brace_interpolation_loses_the_variable_name",
            "test_deploy_values.StripTemplateDollarDialect.test_an_escaped_interpolation_is_withheld_rather_than_kept_literal",
            "test_deploy_values.StripTemplateDollarDialect.test_an_interpolation_spanning_lines_is_stripped_whole",
            "test_deploy_values.StripTemplateDollarDialect.test_both_dialects_withhold_the_same_name_identically",
            "test_flux_kustomize_deployments.WithholdingWhatTheseFilesCarry.test_a_dollar_brace_reference_loses_the_variable_name",
            "test_secret_mask.InstrumentationKeys.test_an_account_key_placeholder_is_unchanged",
            "test_secret_mask.KubernetesEnvLists.test_an_item_named_for_no_secret_or_holding_a_reference_is_unchanged",
            "test_secret_mask.LineEndingsPreserved.test_a_crlf_file_holding_no_secret_is_unchanged",
            "test_secret_mask.LiteralsInsideCode.test_a_lookup_name_or_interpolation_in_a_call_is_unchanged",
            "test_secret_mask.ReferencesAreNotSecrets.test_references_and_non_secrets_are_unchanged",
            "test_secret_mask.StructuredBlockScalars.test_embedded_configuration_holding_no_secret_is_unchanged",
            "test_secret_mask.UrlQuerySecrets.test_an_ordinary_parameter_or_a_name_like_a_secret_is_unchanged",
            "test_secret_mask.XmlConfig.test_xml_holding_no_secret_is_unchanged",
            "test_secret_mask.YamlBlockScalars.test_a_block_under_a_key_naming_no_secret_or_holding_a_reference_is_unchanged",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
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
        (
            "test_deploy_values.SecretReferences.test_a_mapping_of_named_references_keeps_its_names",
            "test_deploy_values.SecretReferences.test_a_nested_store_reference_is_withheld_whole",
            "test_deploy_values.SecretReferences.test_a_reference_keeps_the_variable_and_withholds_store_and_entry",
            "test_deploy_values.SecretReferences.test_the_entry_may_be_a_level_down_through_a_list",
            "test_deploy_values.SecretReferences.test_the_entry_may_be_named_a_level_down_as_the_store_already_could",
            "test_deploy_values.SecretReferences.test_the_policy_keeps_the_sorted_cap_and_ignores_insertion_order",
            "test_flux_kustomize_deployments.WithholdingWhatTheseFilesCarry.test_a_secret_reference_keeps_the_variable_and_loses_the_store_and_the_path",
            "test_flux_kustomize_deployments.WithholdingWhatTheseFilesCarry.test_neither_the_vault_nor_the_entry_reaches_the_written_graph",
        ),
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
        (
            "test_deploy_values.SecretReferences.test_a_nested_store_reference_is_withheld_whole",
            "test_deploy_values.SecretReferences.test_the_entry_may_be_a_level_down_through_a_list",
            "test_deploy_values.SecretReferences.test_the_entry_may_be_named_a_level_down_as_the_store_already_could",
        ),
    ),
    Mutation(
        "the second deployment layout unwired",
        "build_deployments.py",
        "    facts = _merged_facts(found, reading, tally.flux)",
        "    facts = _values_facts(found)",
        "#88: the stage read one layout, so a Kustomize/Flux repository returned exactly the "
        "pair count the run returned without it. Every test of the reader itself still passes "
        "with the call site gone, which is the class of escape this gate exists for",
        (
            "test_flux_kustomize_deployments.BothRoutesReachOneEnvironment.test_both_facts_hang_off_the_one_environment_by_an_edge",
            "test_flux_kustomize_deployments.BothRoutesReachOneEnvironment.test_both_routes_reached_prod",
            "test_flux_kustomize_deployments.BothRoutesReachOneEnvironment.test_each_fact_records_the_route_that_produced_it",
            "test_flux_kustomize_deployments.BothRoutesReachOneEnvironment.test_prod_is_one_environment_node_not_one_per_route",
            "test_flux_kustomize_deployments.ChartReferencesAreDependencies.test_a_source_the_estate_does_hold_keeps_its_repository",
            "test_flux_kustomize_deployments.ChartReferencesAreDependencies.test_a_source_the_estate_does_not_hold_claims_no_repository",
            "test_flux_kustomize_deployments.ChartReferencesAreDependencies.test_the_chart_and_its_pinned_version_travel_on_the_fact",
            "test_flux_kustomize_deployments.ChartReferencesAreDependencies.test_the_chart_source_becomes_an_edge_citing_the_file_that_declares_it",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_every_fact_cites_a_file_that_exists_and_names_the_layers_it_composed",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_one_environments_patch_does_not_bleed_into_another",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_base_layer_is_a_fact_of_its_own_named_as_the_base",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_environment_patch_wins_and_the_base_still_supplies_the_rest",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_fact_takes_the_node_shape_the_stage_already_emits",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_service_joins_the_repository_that_holds_it",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_stack_overlay_the_cluster_reconciles_is_applied",
            "test_flux_kustomize_deployments.OneCloneDeclaringAServiceTwice.test_the_clash_is_named_rather_than_resolved_quietly",
            "test_flux_kustomize_deployments.RunningTwice.test_the_graph_is_the_same_size_after_a_second_run",
            "test_flux_kustomize_deployments.SayingWhatItCouldNotRead.test_a_malformed_document_is_counted_and_named_and_the_run_continues",
            "test_flux_kustomize_deployments.TheTerraformModuleLayerIsLeftAlone.test_the_module_node_and_its_edge_survive_two_deployment_runs",
            "test_flux_kustomize_deployments.WithholdingWhatTheseFilesCarry.test_a_dollar_brace_reference_loses_the_variable_name",
            "test_flux_kustomize_deployments.WithholdingWhatTheseFilesCarry.test_a_secret_reference_keeps_the_variable_and_loses_the_store_and_the_path",
            "test_flux_kustomize_deployments.WithholdingWhatTheseFilesCarry.test_neither_the_vault_nor_the_entry_reaches_the_written_graph",
        ),
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
        (
            "test_flux_kustomize_deployments.OneCloneDeclaringAServiceTwice.test_the_clash_is_named_rather_than_resolved_quietly",
            "test_flux_kustomize_deployments.OneCloneDeclaringAServiceTwice.test_the_clash_is_one_node_and_the_existing_route_keeps_it",
        ),
    ),
    Mutation(
        "the base layer dropped from an environment's composition",
        "deploy_flux.py",
        "    ordered = [base] if base else []",
        "    ordered = []",
        "#88: an overlay declares only what it overrides, so a fact built from the patches "
        "alone reports a partial configuration as a whole one - the keys it does carry are "
        "right, which is why nothing looks wrong",
        (
            "test_flux_kustomize_deployments.ChartReferencesAreDependencies.test_the_chart_and_its_pinned_version_travel_on_the_fact",
            "test_flux_kustomize_deployments.ChartReferencesAreDependencies.test_the_chart_source_becomes_an_edge_citing_the_file_that_declares_it",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_one_environments_patch_does_not_bleed_into_another",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_environment_patch_wins_and_the_base_still_supplies_the_rest",
        ),
    ),
    Mutation(
        "only the first directory a cluster reconciles is read",
        "deploy_flux.py",
        "        if any(_under(rel, directory) for directory in directories):",
        "        if any(_under(rel, directory) for directory in directories[:1]):",
        "#88: a cluster reconciles an environment overlay and its stack overlays together, so "
        "reading one of them loses limits that are really set and reports nothing missing",
        (
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_stack_overlay_the_cluster_reconciles_is_applied",
        ),
    ),
    Mutation(
        "composed values share the parsed document",
        "deploy_flux.py",
        "            base[key] = copy.deepcopy(value)",
        "            base[key] = value",
        "#88: one base declaration is composed into every environment, so overlaying onto the "
        "parsed document instead of a copy gives the environments composed later the earlier "
        "ones' overrides - both facts stay well-formed and one of them is fiction",
        (
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_one_environments_patch_does_not_bleed_into_another",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_environment_patch_wins_and_the_base_still_supplies_the_rest",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_stack_overlay_the_cluster_reconciles_is_applied",
        ),
    ),
    Mutation(
        "an unparsable Kustomize/Flux file skipped in silence",
        "build_deployments.py",
        "        if documents is None:\n            unparsed.append(rel)\n            continue",
        "        if documents is None:\n            continue",
        "#88's premise is a silent omission: a file this reader cannot parse is a service "
        "absent from every answer, and a walk that drops it reports the same clean run",
        (
            "test_flux_kustomize_deployments.SayingWhatItCouldNotRead.test_a_malformed_document_is_counted_and_named_and_the_run_continues",
        ),
    ),
    Mutation(
        "the layout that contributed nothing says nothing",
        "build_deployments.py",
        "    if flux.releases or flux.kustomizations:",
        "    if True:",
        "#88: the finding is a repository adding no nodes and no message. Counting documents "
        "for a tree that declared none reads as a healthy run, and the sensitivity control "
        "that proves this reader can tell the two apart is the report line itself",
        (
            "test_flux_kustomize_deployments.SayingWhatItCouldNotRead.test_a_tree_without_this_layout_yields_nothing_and_says_so",
        ),
    ),
    Mutation(
        "the chart source dropped from a chart reference",
        "deploy_flux.py",
        '    source = _text(reference.get("name")) if reference.get("kind") in CHART_SOURCE_KINDS else ""',
        '    source = ""',
        "#88 and #122: `sourceRef` is a repository-to-repository dependency nothing else in a "
        "store holds. Losing it leaves the chart name and version on the node, so the fact "
        "reads as fully parsed",
        (
            "test_flux_kustomize_deployments.ChartReferencesAreDependencies.test_a_source_the_estate_does_hold_keeps_its_repository",
            "test_flux_kustomize_deployments.ChartReferencesAreDependencies.test_a_source_the_estate_does_not_hold_claims_no_repository",
            "test_flux_kustomize_deployments.ChartReferencesAreDependencies.test_the_chart_source_becomes_an_edge_citing_the_file_that_declares_it",
            "test_flux_kustomize_deployments.RunningTwice.test_the_graph_is_the_same_size_after_a_second_run",
            "test_flux_kustomize_deployments.TheTerraformModuleLayerIsLeftAlone.test_the_module_node_and_its_edge_survive_two_deployment_runs",
        ),
    ),
    Mutation(
        "every fact claims one route",
        "build_deployments.py",
        '            "route": fact.route,',
        '            "route": ROUTE_VALUES,',
        "#88: two layouts emit onto one set of environments, so the route is what makes "
        '"which services reach this environment by which route" answerable. The key is still '
        "present and every node still carries a value, which is how a wrong one survives",
        (
            "test_flux_kustomize_deployments.BothRoutesReachOneEnvironment.test_each_fact_records_the_route_that_produced_it",
            "test_flux_kustomize_deployments.BothRoutesReachOneEnvironment.test_prod_is_one_environment_node_not_one_per_route",
        ),
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
        (
            "test_flux_kustomize_deployments.BothRoutesReachOneEnvironment.test_both_facts_hang_off_the_one_environment_by_an_edge",
            "test_flux_kustomize_deployments.BothRoutesReachOneEnvironment.test_both_routes_reached_prod",
            "test_flux_kustomize_deployments.BothRoutesReachOneEnvironment.test_each_fact_records_the_route_that_produced_it",
            "test_flux_kustomize_deployments.BothRoutesReachOneEnvironment.test_prod_is_one_environment_node_not_one_per_route",
            "test_flux_kustomize_deployments.ChartReferencesAreDependencies.test_the_chart_and_its_pinned_version_travel_on_the_fact",
            "test_flux_kustomize_deployments.ChartReferencesAreDependencies.test_the_chart_source_becomes_an_edge_citing_the_file_that_declares_it",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_every_fact_cites_a_file_that_exists_and_names_the_layers_it_composed",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_one_environments_patch_does_not_bleed_into_another",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_environment_patch_wins_and_the_base_still_supplies_the_rest",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_fact_takes_the_node_shape_the_stage_already_emits",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_run_reports_what_it_read",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_service_joins_the_repository_that_holds_it",
            "test_flux_kustomize_deployments.ComposingABaseWithItsPatches.test_the_stack_overlay_the_cluster_reconciles_is_applied",
            "test_flux_kustomize_deployments.OneCloneDeclaringAServiceTwice.test_the_clash_is_named_rather_than_resolved_quietly",
            "test_flux_kustomize_deployments.RunningTwice.test_the_graph_is_the_same_size_after_a_second_run",
            "test_flux_kustomize_deployments.SayingWhatItCouldNotRead.test_a_malformed_document_is_counted_and_named_and_the_run_continues",
        ),
    ),
    Mutation(
        "one role word is enough to redact",
        "deploy_values.py",
        "    return bool(stores) and bool(entries) and len(stores | entries) > 1 "
        "and named_outright",
        "    return (bool(stores) or bool(entries)) and named_outright",
        "#88: `path`, `key` and `store` are ordinary configuration words on their own, so a "
        "rule satisfied by one of them withholds real configuration facts. This is the "
        "failure that reads as extra safety: a policy redacting everything answers no "
        "deployment question, and the stage reports the same key count either way",
        (
            "test_deploy_values.FourMoreShapes.test_a_bare_store_url_and_name_outside_a_collection_is_left_alone",
            "test_deploy_values.FourMoreShapes.test_a_collection_whose_members_do_not_all_pair_is_left_alone",
            "test_deploy_values.FourMoreShapes.test_a_reference_to_something_that_is_not_a_secret_is_left_alone",
            "test_deploy_values.FourMoreShapes.test_a_registry_of_vaults_naming_no_secrets_is_left_alone",
            "test_deploy_values.FourMoreShapes.test_a_service_pointing_at_a_vault_keeps_its_own_name",
            "test_deploy_values.FourMoreShapes.test_a_store_url_alone_or_a_flag_beside_a_name_is_left_alone",
            "test_deploy_values.SecretReferences.test_an_ordinary_mapping_is_untouched",
        ),
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
        (
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_term_a_recorded_file_path_names_is_credited_to_history",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_term_in_neither_the_graph_nor_history_is_the_class_that_can_contain_invention",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_ticket_the_history_datasets_cite_is_not_counted_as_possible_invention",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_history_credits_a_whole_token_rather_than_a_longer_name_containing_it",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_the_two_classes_sum_to_the_flagged_total",
            "test_summaries_flagged_term_classes.TheMutationEntriesForThisChangeTest.test_each_entry_matches_exactly_one_line_of_the_stage",
        ),
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
        (
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_term_a_recorded_file_path_names_is_credited_to_history",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_term_in_neither_the_graph_nor_history_is_the_class_that_can_contain_invention",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_a_ticket_the_history_datasets_cite_is_not_counted_as_possible_invention",
            "test_summaries_flagged_term_classes.FlaggedTermClassesTest.test_the_two_classes_sum_to_the_flagged_total",
            "test_summaries_flagged_term_classes.TheHistoryLookupIsBoundedTest.test_no_further_dataset_is_opened_once_every_term_is_located",
            "test_summaries_flagged_term_classes.TheHistoryLookupIsBoundedTest.test_the_pass_stops_reading_a_dataset_once_every_term_is_located",
            "test_summaries_flagged_term_classes.TheMutationEntriesForThisChangeTest.test_each_entry_matches_exactly_one_line_of_the_stage",
        ),
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
        (
            "test_mutation_gate_mapping.MutationGateMappingTest.test_a_failure_inside_a_subtest_is_attributed_to_its_own_module",
            "test_mutation_gate_mapping.MutationGateMappingTest.test_a_module_failing_only_in_a_subtest_still_reads_as_failing",
        ),
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
        (
            "test_mutation_gate_refusals.MutationGateRefusalsTest.test_the_wrong_tree_is_refused_before_the_tests_are_blamed",
        ),
    ),
    Mutation(
        "a shard of the table drops an entry",
        "tests/mutation_gate.py",
        "    return tuple(mutations[index - 1" + " :: count])",
        "    return tuple(mutations[index :: count])",
        "#285 named this as the defect sharding the mapping check invites, and it is "
        "invisible from the run that took it: the first entry of the table lands in no "
        "shard, every leg prints `N of N entries name what observes them` over its own "
        "smaller N, and the entry nobody applied is one whose mapping nobody checked. "
        "Only the reconciliation against the whole table disagrees",
        (
            "test_mutation_gate_shards.OneShardRunsItsOwnSliceTest.test_a_shard_says_how_its_entries_reconcile_with_the_whole_table",
            "test_mutation_gate_shards.OneShardRunsItsOwnSliceTest.test_each_shard_runs_its_own_entries_and_between_them_they_run_the_table",
            "test_mutation_gate_shards.ShardsPartitionTheTableTest.test_every_entry_lands_in_exactly_one_shard_of_every_count",
            "test_mutation_gate_shards.ShardsPartitionTheTableTest.test_the_shipped_table_reconciles",
        ),
    ),
    Mutation(
        "the mapping check fails toward skipping when it cannot tell",
        "tests/mapping_trigger.py",
        '    return Decision(True, f"{why}, so nothing could be compared and it runs rather than guess.")',
        '    return Decision(False, f"{why}, so nothing could be compared and it is skipped.")',
        "#285: the sharded check now runs only when something has landed that could have "
        "invalidated a mapping, and that decision is asymmetric - a run that should have "
        "skipped costs 28 runner-minutes and says so, while a skip that should have run is "
        "indistinguishable from a pass: no leg runs, the summary is green, and a mapping "
        "nobody checked keeps being reported as checked. Every uncertainty ends at this "
        "line, and none of them happens on a good day: no previous verification, a failed "
        "API call, a base a shallow clone does not hold",
        (
            "test_mapping_trigger.FailTowardRunningTest.test_an_event_nothing_knows_how_to_compare_runs_the_check",
            "test_mapping_trigger.FailTowardRunningTest.test_nothing_to_compare_against_runs_the_check",
            "test_mapping_trigger.TheDecisionIsAnnouncedTest.test_a_nightly_with_no_previous_verification_runs_anyway",
            "test_mapping_trigger.TheDecisionIsAnnouncedTest.test_a_push_that_names_no_previous_commit_runs_anyway",
        ),
    ),
    Mutation(
        "the nightly predicate becomes an allow-list",
        "tests/mapping_trigger.py",
        "    return path not in CANNOT_BE_READ",
        "    return False",
        "#285: the nightly decides from a deny-list - everything can invalidate a mapping "
        "unless it provably cannot - because an allow-list fails toward skipping: a kind of "
        "file that did not exist when the list was written reads as harmless, silently and "
        "forever. Inverted, this reports every night as nothing having landed, which is the "
        "one output of this policy that cannot be told from working",
        (
            "test_mapping_trigger.WhatWarrantsARunTest.test_a_path_nobody_thought_about_is_read_as_one_that_matters",
            "test_mapping_trigger.WhatWarrantsARunTest.test_the_nightly_runs_for_a_document_the_table_mutates",
            "test_mapping_trigger.WhatWarrantsARunTest.test_the_nightly_runs_when_only_source_changed",
        ),
    ),
    Mutation(
        "the entries being mapped are refused by the guard over the mapping",
        "tests/mutation_gate.py",
        "            if mutation.name in " + "excused:",
        "            if False:",
        "#287: this check runs inside the suite, so an entry naming no observer makes the "
        "suite red - and a red suite is what the baseline refusal stops a run on, including "
        "`--derive-mapping`, the command the refusal itself prints. The remedy for an "
        "unmapped entry could not be run, and the way round it was to paste a plausible "
        "observer set in, derive, and take it back out: the gate reports `caught` with that "
        "placeholder in place because the named module fails for its own reasons, and a "
        "find-and-replace that matches nothing looks exactly like success. Hit repeatedly in "
        "one day while giving observer sets to entries arriving from other branches",
        # Derived twice on purpose. The first run had the entry itself unmapped - the
        # state this change exists to make workable - so the guard failed for that
        # absence as well as for the mutation, and named a third module that stops
        # observing the moment the entry is mapped. The set below is the fixed point:
        # derived again with the field filled in, and `--verify-mapping` agrees.
        (
            "test_mutation_gate_refusals.MutationGateRefusalsTest.test_a_suite_red_for_its_own_reasons_still_refuses_to_derive",
            "test_mutation_gate_refusals.MutationGateRefusalsTest.test_an_entry_with_no_observers_can_still_be_derived",
        ),
    ),
    Mutation(
        "the coverage block stops describing the sample it records",
        "build_community_summaries.py",
        '        "business_features": features[:TOP_FEATURES],',
        '        "business_features": features[: TOP_FEATURES - 1],',
        "#299: a digest caps three fields and the coverage block says how much of each "
        "one it is showing, which is only worth recording if the two cannot drift. The "
        "counts are computed from the caps and cross-checked against the fields, so a cap "
        "lowered in one place and not the other stops the write. Take the check away and "
        "the block keeps claiming five where four are shown - a number that reads as "
        "measured, is written into a committed artefact, and is what the verifier "
        "subtracts from",
        (
            "test_summaries_coverage.DigestCoverageTest.test_a_capped_field_records_the_total_behind_the_cap",
            "test_summaries_coverage.DigestCoverageTest.test_every_capped_field_is_covered",
            "test_summaries_coverage.DigestCoverageTest.test_the_block_reconciles_on_a_truncated_community",
            "test_summaries_coverage.DigestCoverageTest.test_the_shown_count_is_checked_against_what_the_digest_holds",
            "test_summaries_coverage.MergedArtefactTest.test_a_coverage_block_that_does_not_add_up_stops_the_write",
            "test_summaries_coverage.MergedArtefactTest.test_a_summary_with_no_digest_gets_no_invented_coverage",
            "test_summaries_coverage.MergedArtefactTest.test_the_artefact_carries_a_coverage_block_per_community",
            "test_summaries_coverage.MergedArtefactTest.test_the_metadata_block_keys_are_written_in_a_fixed_order",
            "test_summaries_coverage.WriteGateTest.test_a_new_community_rewrites_the_file",
            "test_summaries_coverage.WriteGateTest.test_a_refresh_that_moved_only_a_count_does_not_rewrite_the_file",
            "test_summaries_coverage.WriteGateTest.test_changed_prose_rewrites_the_file",
            "test_summaries_coverage.WriteGateTest.test_the_recorded_digest_covers_the_prose_and_not_the_metadata",
        ),
    ),
    Mutation(
        "the merged summaries write is no longer gated on content",
        "build_community_summaries.py",
        '    unchanged = recorded == metadata["content_digest"]',
        "    unchanged = False",
        "#299: coverage counts move whenever the graph is re-extracted, so recording them "
        "in the committed artefact without this gate rewrites every summary on every "
        "refresh - permanently, in every consuming store's diff, for a change no reader "
        "can act on. Ungated it always writes, which is exactly what a run looks like "
        "when it is working",
        (
            "test_prose_integrity.SummariesDriftTest.test_a_second_run_after_that_writes_nothing",
            "test_summaries_coverage.WriteGateTest.test_a_refresh_that_moved_only_a_count_does_not_rewrite_the_file",
            "test_summaries_remap.RemapWriteGateTest.test_a_merge_after_a_remap_recognises_the_digest_the_remap_recorded",
            "test_summaries_remap.RemapWriteGateTest.test_a_second_remap_of_the_same_clustering_writes_nothing",
            "test_summaries_remap.RemapWriteGateTest.test_an_identity_remap_does_not_rewrite_the_committed_file",
            "test_summaries_remap.RemapWriteGateTest.test_the_skipped_write_leaves_the_coverage_merge_recorded",
        ),
    ),
    Mutation(
        "the merged summaries write never happens",
        "build_community_summaries.py",
        '    unchanged = recorded == metadata["content_digest"]',
        "    unchanged = True",
        "#299: the other direction of the same gate, and the reason one test could not "
        "stand for it. A gate that decides never to write passes the check that a "
        "count-only refresh leaves the file alone - the artefact is untouched, the run "
        "prints its usual counts, and authored prose simply never reaches disk",
        (
            "test_prose_integrity.SummariesDriftTest.test_a_remap_reports_a_summary_edited_before_it_ran",
            "test_prose_integrity.SummariesDriftTest.test_a_remap_that_moves_an_id_leaves_the_record_identical",
            "test_prose_integrity.SummariesDriftTest.test_a_run_that_changed_no_prose_still_records_the_digests",
            "test_prose_integrity.SummariesDriftTest.test_a_second_run_after_that_writes_nothing",
            "test_prose_integrity.SummariesDriftTest.test_an_edit_does_not_change_the_exit_code",
            "test_prose_integrity.SummariesDriftTest.test_an_edited_summary_is_reported",
            "test_prose_integrity.SummariesDriftTest.test_the_artefact_records_one_digest_per_summary",
            "test_prose_integrity.SummariesDriftTest.test_the_report_names_only_the_edited_summary",
            "test_summaries_coverage.MergedArtefactTest.test_a_summary_with_no_digest_gets_no_invented_coverage",
            "test_summaries_coverage.MergedArtefactTest.test_the_artefact_carries_a_coverage_block_per_community",
            "test_summaries_coverage.MergedArtefactTest.test_the_metadata_block_keys_are_written_in_a_fixed_order",
            "test_summaries_coverage.WriteGateTest.test_a_new_community_rewrites_the_file",
            "test_summaries_coverage.WriteGateTest.test_a_refresh_that_moved_only_a_count_does_not_rewrite_the_file",
            "test_summaries_coverage.WriteGateTest.test_changed_prose_rewrites_the_file",
            "test_summaries_coverage.WriteGateTest.test_the_recorded_digest_covers_the_prose_and_not_the_metadata",
            "test_summaries_remap.RemapCliTest.test_a_community_that_swallowed_an_old_one_does_not_carry_its_prose",
            "test_summaries_remap.RemapCliTest.test_a_summary_whose_members_have_all_gone_is_dropped",
            "test_summaries_remap.RemapCliTest.test_a_summary_with_no_snapshot_entry_is_dropped_not_carried_forward",
            "test_summaries_remap.RemapCliTest.test_an_identical_member_set_still_carries",
            "test_summaries_remap.RemapCliTest.test_collision_winner_is_the_largest_share_not_the_lowest_id",
            "test_summaries_remap.RemapCliTest.test_remap_sub_command_carries_summaries_across",
            "test_summaries_remap.RemapCliTest.test_summary_is_carried_when_the_dominant_cluster_holds_enough_members",
            "test_summaries_remap.RemapCliTest.test_the_carry_criterion_is_settable_from_the_command_line",
            "test_summaries_remap.RemapCliTest.test_the_overlap_criterion_carries_the_swallowing_case_when_asked_for",
            "test_summaries_remap.RemapCliTest.test_two_old_clusters_merging_into_one_keeps_a_single_summary",
            "test_summaries_remap.RemapTest.test_a_community_that_swallowed_an_old_one_does_not_carry_its_prose",
            "test_summaries_remap.RemapTest.test_a_summary_whose_members_have_all_gone_is_dropped",
            "test_summaries_remap.RemapTest.test_a_summary_with_no_snapshot_entry_is_dropped_not_carried_forward",
            "test_summaries_remap.RemapTest.test_an_identical_member_set_still_carries",
            "test_summaries_remap.RemapTest.test_collision_winner_is_the_largest_share_not_the_lowest_id",
            "test_summaries_remap.RemapTest.test_summary_is_carried_when_the_dominant_cluster_holds_enough_members",
            "test_summaries_remap.RemapTest.test_the_overlap_criterion_carries_the_swallowing_case_when_asked_for",
            "test_summaries_remap.RemapTest.test_two_old_clusters_merging_into_one_keeps_a_single_summary",
            "test_summaries_remap.RemapWriteGateTest.test_a_remap_that_moves_an_id_still_rewrites_the_file",
            "test_summaries_remap.RemapWriteGateTest.test_an_identity_remap_does_not_rewrite_the_committed_file",
            "test_summaries_remap.RemapWriteGateTest.test_non_ascii_prose_survives_a_remap_as_merge_wrote_it",
            "test_summaries_remap.RemapWriteGateTest.test_the_skipped_write_leaves_the_coverage_merge_recorded",
            "test_summaries_remap_fallback.RemapFallbackTest.test_the_renamed_community_is_carried_onto_its_new_id",
            "test_summary_batches.MergeRefusesNothingTest.test_a_batch_cut_by_batches_and_authored_merges_through_its_out_file",
            "test_ticket_titles_and_summaries.SummariesMergeTest.test_valid_summary_merges",
        ),
    ),
    Mutation(
        "every unsampled term downgraded to informational",
        "build_community_summaries.py",
        "    return [field for field in COVERAGE_FIELDS if _unshown(coverage.get(field)) > 0]",
        "    return list(COVERAGE_FIELDS)",
        "#299: the coverage block lets `summaries verify` separate a term the digest never "
        "showed from one the community does not hold. Read as though every field were "
        "truncated, every finding becomes informational and `--strict` stops failing on "
        "anything - and the coverage tests all still pass, because the blocks are correct "
        "and only the conclusion drawn from them is gone",
        (
            "test_summaries_coverage.VerifyReadsCoverageTest.test_a_term_absent_from_a_digest_that_withheld_nothing_still_fails",
            "test_summaries_coverage.VerifyReadsCoverageTest.test_the_report_reconciles_the_two_classes_against_the_total",
        ),
    ),
    Mutation(
        "the summaries metadata block read as a community",
        "io.py",
        "    return {key: value for key, value in document.items() if key != SUMMARIES_METADATA_KEY}",
        "    return dict(document)",
        "#299: the merged artefact gained one reserved key, and six stages iterate that "
        "file. Unstripped, the block becomes a community: prose in the explorer page, "
        "tokens in the semantic vocabulary, one more in the count `status` prints, and a "
        "displaced entry in a remap report. Each of those is ordinary-looking output, "
        "which is why the strip lives in one reader rather than in six",
        (
            "test_build_deep_dives.MainDispatchTest.test_merge_dispatch_produces_the_same_artefact_as_a_direct_call",
            "test_build_deep_dives.MergeTest.test_merge_rejects_a_dossier_that_does_not_state_its_build",
            "test_build_deep_dives.MergeTest.test_one_bad_dossier_does_not_block_a_good_one",
            "test_build_topic_briefs.MergeTest.test_merge_validates_and_renders",
            "test_prose_integrity.BriefsDriftTest.test_an_untouched_artefact_is_not_reported",
            "test_prose_integrity.BriefsDriftTest.test_editing_the_markdown_source_is_not_reported_as_an_edit",
            "test_prose_integrity.DivesDriftTest.test_an_untouched_artefact_is_not_reported",
            "test_prose_integrity.ExplorerDoesNotEmbedTheBlockTest.test_the_page_embeds_the_entries_without_the_metadata_block",
            "test_prose_integrity.MetadataIsNotAnEntryTest.test_status_does_not_count_the_block_as_a_brief",
            "test_prose_integrity.MetadataIsNotAnEntryTest.test_the_block_carries_no_prose_for_the_drift_check_to_read",
            "test_prose_integrity.MetadataIsNotAnEntryTest.test_the_shared_reader_returns_the_entries_alone",
            "test_prose_integrity.SummariesDriftTest.test_a_remap_reports_a_summary_edited_before_it_ran",
            "test_prose_integrity.SummariesDriftTest.test_a_remap_that_moves_an_id_leaves_the_record_identical",
            "test_prose_integrity.SummariesDriftTest.test_a_remap_that_moves_an_id_reports_no_mismatch",
            "test_prose_integrity.SummariesDriftTest.test_a_run_that_changed_no_prose_still_records_the_digests",
            "test_prose_integrity.SummariesDriftTest.test_a_second_run_after_that_writes_nothing",
            "test_prose_integrity.SummariesDriftTest.test_an_edit_does_not_change_the_exit_code",
            "test_prose_integrity.SummariesDriftTest.test_an_edited_summary_is_reported",
            "test_prose_integrity.SummariesDriftTest.test_an_untouched_artefact_is_not_reported",
            "test_prose_integrity.SummariesDriftTest.test_the_report_names_only_the_edited_summary",
            "test_summaries_coverage.MetadataIsNotACommunityTest.test_status_does_not_count_it_as_a_summary",
            "test_summaries_coverage.MetadataIsNotACommunityTest.test_the_shared_reader_returns_the_prose_alone",
            "test_summaries_coverage.MetadataIsNotACommunityTest.test_verify_does_not_read_it_as_prose",
            "test_summaries_coverage.WriteGateTest.test_a_new_community_rewrites_the_file",
            "test_summaries_coverage.WriteGateTest.test_a_refresh_that_moved_only_a_count_does_not_rewrite_the_file",
            "test_summaries_coverage.WriteGateTest.test_changed_prose_rewrites_the_file",
            "test_summaries_provenance_freshness.ProvenanceFreshnessTest.test_a_report_written_for_this_clustering_still_reports_the_split",
            "test_summaries_provenance_freshness.ProvenanceFreshnessTest.test_the_report_records_the_clustering_it_was_written_for",
            "test_summaries_provenance_states.ProvenanceStatesTest.test_an_identity_remap_reports_nothing_carried_across_a_move",
            "test_summaries_provenance_states.ProvenanceStatesTest.test_each_group_carries_its_own_flag_rate",
            "test_summaries_provenance_states.ProvenanceStatesTest.test_every_checked_summary_lands_in_exactly_one_group",
            "test_summaries_provenance_states.ProvenanceStatesTest.test_prose_re_keyed_onto_a_changed_set_is_reported_as_carried_across_a_move",
            "test_summaries_provenance_states.ProvenanceStatesTest.test_the_remap_marked_exactly_the_two_communities_whose_members_moved",
            "test_summaries_remap.RemapCliTest.test_the_floor_guard_can_be_lowered_for_a_genuinely_small_store",
            "test_summaries_remap.RemapTest.test_the_floor_guard_can_be_lowered_for_a_genuinely_small_store",
            "test_summaries_remap.RemapWriteGateTest.test_a_merge_after_a_remap_recognises_the_digest_the_remap_recorded",
            "test_summaries_remap.RemapWriteGateTest.test_a_remap_that_moves_an_id_still_rewrites_the_file",
            "test_summaries_remap.RemapWriteGateTest.test_a_second_remap_of_the_same_clustering_writes_nothing",
            "test_summaries_remap.RemapWriteGateTest.test_an_identity_remap_does_not_rewrite_the_committed_file",
            "test_summaries_remap.RemapWriteGateTest.test_non_ascii_prose_survives_a_remap_as_merge_wrote_it",
            "test_summaries_remap.RemapWriteGateTest.test_the_skipped_write_leaves_the_coverage_merge_recorded",
        ),
    ),
    Mutation(
        "a swallowing community reads as the same set",
        "build_community_summaries.py",
        "        identical = set(members) == members_of[target]",
        "        identical = True",
        "#296, the state that shipped, arrived as a recall-only carry: a new community "
        "that swallowed an old one whole scored 1.00 however much unrelated material came "
        "with it, so prose about a small coherent community was re-attached to a large "
        "incoherent one. Reported on a real rebuild as the large majority of everything "
        "carried. It is the shape rather than the size that hid it - every summary still "
        "had a community and every community still had prose, so the store looked healthy "
        "and the retention figure read as reassurance while being the opposite",
        (
            "test_documented_reference_tables.TheDocumentedEnumerationsAgreeWithTheCode.test_below_the_bar_is_unreachable_under_the_shipped_criterion",
            "test_summaries_provenance_states.ProvenanceStatesTest.test_each_group_carries_its_own_flag_rate",
            "test_summaries_provenance_states.ProvenanceStatesTest.test_prose_re_keyed_onto_a_changed_set_is_reported_as_carried_across_a_move",
            "test_summaries_provenance_states.ProvenanceStatesTest.test_the_remap_marked_exactly_the_two_communities_whose_members_moved",
            "test_summaries_remap.ClaimTargetsTest.test_a_dominant_target_is_claimed_with_its_share",
            "test_summaries_remap.ExactClaimTargetsTest.test_a_community_holding_the_old_set_and_one_more_node_is_withdrawn",
            "test_summaries_remap.ExactClaimTargetsTest.test_a_member_that_left_the_graph_is_named_as_a_changed_set_not_a_near_miss",
            "test_summaries_remap.ExactClaimTargetsTest.test_repeated_node_ids_do_not_read_as_an_identical_set",
            "test_summaries_remap.RemapCliTest.test_a_community_that_swallowed_an_old_one_does_not_carry_its_prose",
            "test_summaries_remap.RemapCliTest.test_carried_and_withdrawn_reconcile_against_the_summaries_read",
            "test_summaries_remap.RemapCliTest.test_prose_carried_below_set_equality_is_marked_in_the_report",
            "test_summaries_remap.RemapCliTest.test_the_swallowed_communitys_prose_is_withdrawn_rather_than_dropped",
            "test_summaries_remap.RemapCliTest.test_the_withdrawal_count_is_reported_beside_the_retention_figure",
            "test_summaries_remap.RemapTest.test_a_community_that_swallowed_an_old_one_does_not_carry_its_prose",
            "test_summaries_remap.RemapTest.test_carried_and_withdrawn_reconcile_against_the_summaries_read",
            "test_summaries_remap.RemapTest.test_prose_carried_below_set_equality_is_marked_in_the_report",
            "test_summaries_remap.RemapTest.test_the_swallowed_communitys_prose_is_withdrawn_rather_than_dropped",
            "test_summaries_remap.RemapTest.test_the_withdrawal_count_is_reported_beside_the_retention_figure",
        ),
    ),
    Mutation(
        "the carry criterion defaults back to a tolerance",
        "build_community_summaries.py",
        "DEFAULT_CARRY = CARRY_EXACT",
        "DEFAULT_CARRY = CARRY_OVERLAP",
        "the shipped-default class again: the tolerance is still reachable and still "
        "tested, so every assertion about it passes with the default pointing at it. What "
        "ships is the criterion nobody passes an argument for",
        (
            "test_documented_flags.TheRealDocumentsMatchTheCode.test_every_mention_is_accounted_for_in_exactly_one_class",
            "test_documented_flags.TheRealDocumentsMatchTheCode.test_no_document_claims_a_flag_or_default_the_code_lacks",
            "test_documented_flags.TheRunnerReportsWhatItRead.test_the_real_tree_reports_its_counts_and_exits_zero",
            "test_documented_reference_tables.TheDocumentedEnumerationsAgreeWithTheCode.test_below_the_bar_is_unreachable_under_the_shipped_criterion",
            "test_summaries_remap.DocumentedCarryBarIsConditionalTest.test_the_shipped_criterion_is_exact",
            "test_summaries_remap.RemapCliTest.test_a_community_that_swallowed_an_old_one_does_not_carry_its_prose",
            "test_summaries_remap.RemapCliTest.test_carried_and_withdrawn_reconcile_against_the_summaries_read",
            "test_summaries_remap.RemapCliTest.test_the_swallowed_communitys_prose_is_withdrawn_rather_than_dropped",
            "test_summaries_remap.RemapCliTest.test_the_withdrawal_count_is_reported_beside_the_retention_figure",
            "test_summaries_remap.RemapTest.test_a_community_that_swallowed_an_old_one_does_not_carry_its_prose",
            "test_summaries_remap.RemapTest.test_carried_and_withdrawn_reconcile_against_the_summaries_read",
            "test_summaries_remap.RemapTest.test_the_swallowed_communitys_prose_is_withdrawn_rather_than_dropped",
            "test_summaries_remap.RemapTest.test_the_withdrawal_count_is_reported_beside_the_retention_figure",
        ),
    ),
    Mutation(
        "withdrawn prose no longer written beside the summaries",
        "build_community_summaries.py",
        "    io.write_json(\n        config.SUMMARIES_WITHDRAWN_PATH,\n"
        "        dict(sorted(withdrawn.items(), key=lambda kv: int(kv[0]))),\n"
        "        indent=1,\n    )",
        "    pass",
        "#296: set equality withdraws far more prose than the tolerance it replaced, so the "
        "change is only defensible while the writing survives it. Unwritten, every count "
        "still reconciles and the run still reports what it withdrew - the artefact naming "
        "the paragraphs somebody is meant to revise is the only thing missing",
        (
            "test_summaries_remap.RemapCliTest.test_a_carried_summary_is_not_also_withdrawn",
            "test_summaries_remap.RemapCliTest.test_a_run_that_withdraws_nothing_replaces_an_earlier_runs_file",
            "test_summaries_remap.RemapCliTest.test_carried_and_withdrawn_reconcile_against_the_summaries_read",
            "test_summaries_remap.RemapCliTest.test_the_swallowed_communitys_prose_is_withdrawn_rather_than_dropped",
            "test_summaries_remap.RemapTest.test_a_carried_summary_is_not_also_withdrawn",
            "test_summaries_remap.RemapTest.test_a_run_that_withdraws_nothing_replaces_an_earlier_runs_file",
            "test_summaries_remap.RemapTest.test_carried_and_withdrawn_reconcile_against_the_summaries_read",
            "test_summaries_remap.RemapTest.test_the_swallowed_communitys_prose_is_withdrawn_rather_than_dropped",
            "test_summaries_remap_fallback.RemapFallbackTest.test_a_fallback_refusal_leaves_the_node_routes_verdict_standing",
            "test_summaries_remap_fallback.RemapFallbackTest.test_a_store_with_no_file_snapshot_remaps_as_it_did_before",
            "test_summaries_remap_fallback.RemapFallbackTest.test_the_fallback_never_takes_a_target_from_the_node_id_route",
            "test_summaries_remap_fallback.RemapFallbackTest.test_the_renamed_community_is_carried_onto_its_new_id",
        ),
    ),
    Mutation(
        "the provenance split is printed without checking the report describes these summaries",
        "build_community_summaries.py",
        "    withheld = _stale_provenance(report, digests)",
        '    withheld = ""',
        "#305, the state that shipped: `verify` read `remap-report.json` with no freshness "
        "check at all, so a report a previous rebuild had left behind relabelled every current "
        "summary carried or authored. Community ids are small integers and are reused across "
        "re-clusters, so every id in the old report still resolved against the current "
        "summaries - no error, no empty group, just a different partition reported to two "
        "significant figures, with the two groups summing to a population the store did not "
        "hold. Reported from a real store, and the line's own docstring says retention must "
        "never be read without it",
        (
            "test_summaries_provenance_freshness.ProvenanceFreshnessTest.test_a_report_from_a_previous_clustering_does_not_produce_a_split",
            "test_summaries_provenance_freshness.ProvenanceFreshnessTest.test_ids_reused_by_the_new_clustering_are_not_read_as_a_match",
            "test_summaries_provenance_freshness.ProvenanceFreshnessTest.test_the_withheld_split_says_why_on_stderr",
            "test_summaries_provenance_freshness.ProvenanceFreshnessWithoutARecordTest.test_a_report_older_than_the_digests_does_not_produce_a_split",
        ),
    ),
    Mutation(
        "a configured root keeps the spelling it was given",
        "config.py",
        '        module["ROOT"] = Path(root).expanduser().resolve()',
        '        module["ROOT"] = Path(root).expanduser()',
        "every ROOT-relative path is re-derived from ROOT, so an unresolved ROOT lets one "
        "directory have two spellings - a path written under the caller's spelling and the "
        "same path read back through config compare unequal. Twice red on this. First as a "
        "phantom platform divergence in the file-list entry; then this entry, added to "
        "prevent that, went red the same way - five observers derived on macOS against one "
        "on Linux. Four tests about *repointing* handed configure an unresolved root and "
        "asserted against a resolved one, which on macOS made them accidental observers of "
        "a resolve they are not about. They now resolve once and compare against that, so "
        "the only observer is the test that builds its own symlink and holds everywhere",
        (
            "test_harness_root_spelling.ConfiguredRootHasOneSpelling.test_configure_resolves_a_root_reached_through_a_symlink",
        ),
    ),
    Mutation(
        "the shared harness keeps its own spelling of the root",
        "tests/settings_isolation.py",
        "        self.root = config.ROOT",
        "        self.root = pathlib.Path(self._tmp.name)",
        "the other half of the entry above, and the half that actually bit. A harness "
        "holding the unresolved temp path compares its own paths against stage output "
        "spelled the resolved way. On Linux the two are one string and nothing happens, so "
        "the whole suite stays green while a maintainer's machine disagrees with CI - the "
        "failure mode is a disagreeing suite rather than a red one, which is worse because "
        "the artefact it corrupts is the observer table this gate ships",
        (
            "test_harness_root_spelling.ConfiguredRootHasOneSpelling.test_the_shared_harness_keeps_the_spelling_config_uses",
        ),
    ),
    Mutation(
        "acronyms carrying a digit read as names again",
        "build_community_summaries.py",
        "if not _DIGIT_WORD_ACRONYM.fullmatch(token)\n",
        "\n",
        "the shipped extractor: `S2S` matched the case-change pattern on its "
        "digit-then-capital, so `summaries verify` and `check-batch` reported a "
        "service-to-service acronym as an invented class name; a store operator "
        "measured it as most of the false flags over the summaries that store ships",
        (
            "test_summaries_verify.IdentifierExtractionTest.test_acronyms_carrying_a_digit_are_not_identifiers",
        ),
    ),
    Mutation(
        "the acronym exemption widens to every all-caps token",
        "build_community_summaries.py",
        "if not _DIGIT_WORD_ACRONYM.fullmatch(token)\n",
        "if not token.isupper()\n",
        "the first version of the fix above, rejected before it shipped: exempting "
        "any token with no lowercase letter also stops checking form and offence "
        "codes like `D45A`, and a summary citing a code its digest does not show "
        "is exactly what the check is for",
        (
            "test_summaries_verify.IdentifierExtractionTest.test_codes_carrying_a_digit_are_still_identifiers",
        ),
    ),
    Mutation(
        "a clean exit with no output reads as done",
        "headless_workers.py",
        "if not _output_parses(job.out):",
        "if False:",
        "a worker denied a read exits 0 having written nothing, which is the failure "
        "recorded on #396; treating the exit as success would report an unextracted "
        "chunk as finished and let the merge proceed without it",
        (
            "test_headless_workers.ExtractTests.test_a_chunk_of_files_that_cannot_be_masked_still_runs_on_the_raw_files",
            "test_headless_workers.ExtractTests.test_a_missing_output_is_not_done_and_a_written_one_lands_where_chunk_status_counts_it",
            "test_headless_workers.OutcomeTests.test_clean_exit_without_output_is_no_output",
            "test_headless_workers.OutcomeTests.test_unparseable_output_is_no_output",
        ),
    ),
    Mutation(
        "the wave keeps launching after an API error",
        "headless_workers.py",
        "stopped = True",
        "stopped = False",
        "an API error (a rate limit, an expired login) fails every worker that follows "
        "it; without the stop a wave spends a run per remaining chunk learning that "
        "and bills for every one of them",
        ("test_headless_workers.WaveTests.test_api_error_stops_the_wave",),
    ),
    Mutation(
        "repair rounds are not resumed",
        "headless_workers.py",
        'argv += ["--resume", resume]',
        "pass",
        "a repair round that starts a fresh session sends the gate's findings to a "
        "worker that has not read the chunk, so it cannot fix them and the round costs "
        "a full extraction again",
        (
            "test_headless_workers.ExtractTests.test_a_repair_round_still_reads_the_masked_copy",
            "test_headless_workers.OutcomeTests.test_gate_fail_then_pass_resumes_the_session",
            "test_headless_workers.PermissionTests.test_command_argv_is_exact",
        ),
    ),
    Mutation(
        "a worker may run commands",
        "headless_workers.py",
        '"Read,Write",',
        '"Read,Write,Bash",',
        "the permission rules name only Read and Edit, so a Bash tool is denied "
        "anyway, but the narrow tool list is what keeps the per-turn context small and "
        "is the second layer behind the rules; widening it is invisible in a run",
        ("test_headless_workers.PermissionTests.test_command_argv_is_exact",),
    ),
    Mutation(
        "complete chunks are re-run",
        "headless_workers.py",
        "if job.complete():",
        "if False:",
        "re-running the stage after a partial wave spawns a worker for every chunk "
        "again, paying for work already on disk and overwriting extractions the "
        "gate had passed",
        (
            "test_headless_workers.ExtractTests.test_a_rerun_skips_the_complete_chunk_without_spawning",
            "test_headless_workers.ExtractTests.test_a_skipped_chunk_is_not_masked",
            "test_headless_workers.OutcomeTests.test_complete_job_is_skipped_without_spawning",
        ),
    ),
    Mutation(
        "an unsafe path runs under a widened rule",
        "headless_workers.py",
        'if any(RULE_UNSAFE.search(p) or not p.startswith("/") for p in paths):',
        'if any(not p.startswith("/") for p in paths):',
        "a path holding a glob character is a pattern, not a file, so the rule built "
        "from it grants every file it matches; the chunk must be refused rather than "
        "run under permission wider than the one the worker was meant to have",
        (
            "test_headless_workers.OutcomeTests.test_unsafe_path_is_refused_not_run",
            "test_headless_workers.PermissionTests.test_unsafe_characters_and_relative_paths_are_refused",
        ),
    ),
    Mutation(
        "workers read the raw file again",
        "headless_workers.py",
        "reads.append(str(copy))",
        "reads.append(path)",
        "the masked copy is still written, so the run looks as it should, but the "
        "worker is granted the original and reads every secret value committed in it "
        "into the model's context and the session transcript - the exposure masking "
        "exists to prevent, and nothing in the report would change",
        (
            "test_headless_workers.ExtractTests.test_a_file_that_is_not_text_is_read_raw_and_counted_unmasked",
            "test_headless_workers.ExtractTests.test_a_path_with_a_space_is_masked_granted_and_mapped",
            "test_headless_workers.ExtractTests.test_a_repair_round_still_reads_the_masked_copy",
            "test_headless_workers.ExtractTests.test_a_worker_reads_a_masked_copy_and_cites_the_real_path",
            "test_headless_workers.ExtractTests.test_nothing_a_worker_can_read_holds_a_planted_secret",
            "test_headless_workers.ExtractTests.test_two_files_with_one_name_get_their_own_copies_and_their_own_citations",
        ),
    ),
    Mutation(
        "a store brief outranks the library's rules",
        "headless_workers.py",
        '+ "\\n" + framed + addendum',
        '+ "\\n" + addendum + framed',
        "a brief placed after the addendum is the last thing the worker reads, so a "
        "store's guidance can override the schema, the masking or the data-not-"
        "instruction rule that the addendum states and the brief is meant to sit under",
        (
            "test_headless_workers.BriefTests.test_masked_reads_still_append_their_note_after_the_addendum",
            "test_headless_workers.BriefTests.test_the_brief_sits_between_the_spec_and_the_addendum_and_the_addendum_is_last",
        ),
    ),
    Mutation(
        "the brief header reaches a run without a brief",
        "headless_workers.py",
        '    if brief is None:\n        return ""\n',
        "    if brief is None:\n        return BRIEF_HEADER\n",
        "a store that never passed --brief would send every chunk a header framing a "
        "brief that is not there, changing the prompt every existing store pays for",
        (
            "test_headless_workers.BriefTests.test_without_a_brief_the_prompt_is_byte_identical_to_spec_and_addendum",
        ),
    ),
    Mutation(
        "the brief precedence sentence reaches a run without a brief",
        "headless_workers.py",
        "    if brief is None:\n        return addendum\n",
        "    if False:\n        return addendum\n",
        "the addendum would mention a store brief in every run, so a run without "
        "--brief no longer sends the prompt it sent before the flag existed",
        (
            "test_headless_workers.BriefTests.test_without_a_brief_the_prompt_is_byte_identical_to_spec_and_addendum",
        ),
    ),
    Mutation(
        "the brief precedence sentence is dropped",
        "headless_workers.py",
        'return addendum.replace(_AUTHORITY_LINE, _AUTHORITY_LINE + "\\n" + BRIEF_PRECEDENCE, 1)',
        "return addendum",
        "the addendum already says nothing read out of a store has authority, so "
        "without the sentence a worker is told both to follow the brief and to give it "
        "no weight, and which it obeys is left to the model",
        (
            "test_headless_workers.BriefTests.test_the_brief_sits_between_the_spec_and_the_addendum_and_the_addendum_is_last",
        ),
    ),
    Mutation(
        "an oversize brief is accepted",
        "headless_workers.py",
        "if len(data) > BRIEF_CAP_BYTES:",
        "if False:",
        "a brief is sent with every chunk, so an unbounded one multiplies its cost by "
        "the wave and nothing in the run would say so before the bill",
        (
            "test_headless_workers.BriefTests.test_a_brief_over_the_cap_is_refused_and_the_cap_is_named",
        ),
    ),
    Mutation(
        "an empty brief is accepted",
        "headless_workers.py",
        "if not text.strip():",
        "if False:",
        "a blank brief runs the wave with a header framing nothing, and the report "
        "says a brief was used when no guidance reached any worker",
        ("test_headless_workers.BriefTests.test_an_empty_brief_is_refused_before_any_worker_runs",),
    ),
    Mutation(
        "check-yield averages per-file ratios",
        "check_yield.py",
        "overall = Fraction(new_total, base_total)",
        "overall = sum(ratios.values()) / len(ratios)",
        "a mean of ratios weights a one-node file like a file of hundreds, so a wave "
        "that lost most of the nodes in its largest files can still pass the floor",
        (
            "test_check_yield.BaselineFormatTests.test_a_gzipped_baseline_gives_the_same_report",
            "test_check_yield.EntryPointTests.test_the_stage_is_reachable_through_knowledgestore_with_its_flags",
            "test_check_yield.RatioTests.test_the_overall_ratio_is_total_over_total_on_the_shared_files",
        ),
    ),
    Mutation(
        "check-yield fails a ratio exactly at the floor",
        "check_yield.py",
        "failed = overall < floor",
        "failed = overall <= floor",
        "the documented rule is below the floor fails; at it, a wave that meets the "
        "store's own bar would be refused",
        (
            "test_check_yield.FloorTests.test_a_ratio_exactly_at_the_floor_passes_and_one_node_fewer_fails",
            "test_check_yield.FloorTests.test_the_floor_flag_moves_the_verdict",
        ),
    ),
    Mutation(
        "check-yield's file floor counts files at the floor",
        "check_yield.py",
        "if ratios[f] < file_floor",
        "if ratios[f] <= file_floor",
        "a file that kept exactly the share the operator asked for would be reported "
        "as below it, inflating the count an operator reads to decide on a re-run",
        (
            "test_check_yield.RatioTests.test_files_below_the_file_floor_are_counted_and_the_worst_are_listed_worst_first",
        ),
    ),
    Mutation(
        "check-yield passes when nothing was compared",
        "check_yield.py",
        "        return UNUSABLE\n",
        "        return 0\n",
        "a comparison over no shared file, an unreadable chunk or a missing baseline "
        "would exit 0, and a wave would be merged on the strength of a check that "
        "read nothing (CLAUDE.md 2.10)",
        (
            "test_check_yield.NothingComparedTests.test_a_missing_or_shapeless_baseline_exits_2",
            "test_check_yield.NothingComparedTests.test_an_empty_chunk_directory_exits_2",
            "test_check_yield.NothingComparedTests.test_an_unreadable_chunk_exits_2_and_names_it",
            "test_check_yield.NothingComparedTests.test_no_shared_file_exits_2_and_shows_a_path_from_each_side",
        ),
    ),
    Mutation(
        "check-yield skips an unreadable chunk",
        "check_yield.py",
        "problems.append(str(violation))",
        "pass",
        "a truncated chunk dropped from the count makes the ratio describe part of the "
        "wave, and the files it held fall on the baseline side where they are not compared",
        ("test_check_yield.NothingComparedTests.test_an_unreadable_chunk_exits_2_and_names_it",),
    ),
    Mutation(
        "check-yield lists tied files in set order",
        "check_yield.py",
        "ranked = sorted(shared, key=lambda f: (ratios[f], f))",
        "ranked = sorted(shared, key=lambda f: ratios[f])",
        "tied files would list in hash order, so two runs over the same inputs print "
        "different reports (CLAUDE.md 1.3)",
        ("test_check_yield.DeterminismTests.test_tied_files_are_listed_by_path",),
    ),
    Mutation(
        "check-yield cannot read a gzipped baseline",
        "check_yield.py",
        "payload = pio.read_json(path)",
        'payload = __import__("json").loads(path.read_text(encoding="utf-8")) '
        "if path.exists() else None",
        "stores commit the compressed graph and often hold no other copy, so a plain "
        "JSON read would refuse the baseline an operator is most likely to have",
        ("test_check_yield.BaselineFormatTests.test_a_gzipped_baseline_gives_the_same_report",),
    ),
    Mutation(
        "a key-name secret is not masked",
        "secret_mask.py",
        "    text = _apply_key_names(text, tally)\n",
        "",
        "a literal `password: ...` in a config file is the commonest shape of a "
        "committed credential and matches none of the value shapes, so without the "
        "key-name rule it reaches the worker whole",
        (
            "test_headless_workers.ExtractTests.test_a_path_with_a_space_is_masked_granted_and_mapped",
            "test_headless_workers.ExtractTests.test_a_repair_round_still_reads_the_masked_copy",
            "test_headless_workers.ExtractTests.test_a_worker_reads_a_masked_copy_and_cites_the_real_path",
            "test_headless_workers.ExtractTests.test_nothing_a_worker_can_read_holds_a_planted_secret",
            "test_headless_workers.ExtractTests.test_two_files_with_one_name_get_their_own_copies_and_their_own_citations",
            "test_secret_mask.CredentialLookingKeys.test_a_credential_under_a_key_or_webhook_is_masked",
            "test_secret_mask.Determinism.test_masking_masked_text_finds_nothing_more",
            "test_secret_mask.Determinism.test_no_counts_argument_is_accepted",
            "test_secret_mask.FusedSecretWords.test_a_fused_secret_word_masks_the_value",
            "test_secret_mask.KeyNameRules.test_each_format_masks_the_value_and_keeps_the_key",
            "test_secret_mask.LineEndingsPreserved.test_crlf_survives_masking_byte_for_byte",
            "test_secret_mask.LiteralsInsideCode.test_a_literal_in_a_call_is_masked_and_the_call_kept",
            "test_secret_mask.OnlyTheValueChanges.test_the_closers_after_a_masked_literal_survive",
            "test_secret_mask.PointersAreNotSecrets.test_what_is_not_a_pointer_is_still_masked",
            "test_secret_mask.StructuredBlockScalars.test_only_the_secret_inside_embedded_configuration_is_masked",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a reference is masked as if it were a secret",
        "secret_mask.py",
        "if not value or MASK in value or _is_reference(value):",
        "if not value or MASK in value:",
        "`password: ${DB_PASSWORD}` names where a service reads its secret, which is "
        "architecture the extraction should record; masking it removes that fact, "
        "protects nothing, and inflates the masked count the operator reads",
        (
            "test_secret_mask.InstrumentationKeys.test_an_account_key_placeholder_is_unchanged",
            "test_secret_mask.KubernetesEnvLists.test_an_item_named_for_no_secret_or_holding_a_reference_is_unchanged",
            "test_secret_mask.KubernetesSecretManifests.test_a_config_map_and_a_templated_secret_are_unchanged",
            "test_secret_mask.LineEndingsPreserved.test_a_crlf_file_holding_no_secret_is_unchanged",
            "test_secret_mask.LiteralsInsideCode.test_a_lookup_name_or_interpolation_in_a_call_is_unchanged",
            "test_secret_mask.ReferencesAreNotSecrets.test_references_and_non_secrets_are_unchanged",
            "test_secret_mask.StructuredBlockScalars.test_embedded_configuration_holding_no_secret_is_unchanged",
            "test_secret_mask.UrlQuerySecrets.test_an_ordinary_parameter_or_a_name_like_a_secret_is_unchanged",
            "test_secret_mask.XmlConfig.test_xml_holding_no_secret_is_unchanged",
            "test_secret_mask.YamlBlockScalars.test_a_block_under_a_key_naming_no_secret_or_holding_a_reference_is_unchanged",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "the credential-looking guard dropped under a key word",
        "secret_mask.py",
        "        return _looks_like_credential(value)\n",
        "        return True\n",
        "`key` names a lookup as often as a secret - `key: password` in a secretKeyRef, "
        "`keyFile: /etc/x.pem`, `KEY_VAULT_NAME: my-vault` - so counting it as a secret "
        "word without asking whether the value looks like a credential masks the names "
        "and paths a worker needs, and inflates the masked count the operator reads",
        (
            "test_headless_workers.ExtractTests.test_nothing_a_worker_can_read_holds_a_planted_secret",
            "test_secret_mask.CredentialLookingKeys.test_a_name_path_or_short_value_under_a_key_is_unchanged",
            "test_secret_mask.DocumentedLimits.test_each_documented_limit_still_reaches_the_worker",
            "test_secret_mask.KubernetesEnvLists.test_an_item_named_for_no_secret_or_holding_a_reference_is_unchanged",
            "test_secret_mask.KubernetesEnvLists.test_the_value_of_an_item_named_for_a_secret_is_masked",
            "test_secret_mask.PointersAreNotSecrets.test_a_url_or_terraform_reference_is_unchanged",
            "test_secret_mask.ReferencesAreNotSecrets.test_references_and_non_secrets_are_unchanged",
            "test_secret_mask.XmlConfig.test_an_xml_secret_is_masked_and_its_markup_kept",
            "test_secret_mask.XmlConfig.test_xml_holding_no_secret_is_unchanged",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
            "test_secret_mask_corpus.CorpusProperties.test_source_code_is_not_masked_at_all",
        ),
    ),
    Mutation(
        "key is not a secret word",
        "secret_mask.py",
        '_CREDENTIAL_TERM = re.compile(r"key|webhook")',
        '_CREDENTIAL_TERM = re.compile(r"webhook")',
        "an estate's comparison against a calibrated masker found `JWT_KEY`, "
        "`APP_INSIGHTS_KEY` and `secret_key_base` values reaching the worker whole: no "
        "key ending in `key` alone was named for a secret, and none of those values "
        "matched a shape",
        (
            "test_headless_workers.ExtractTests.test_nothing_a_worker_can_read_holds_a_planted_secret",
            "test_secret_mask.CredentialLookingKeys.test_a_credential_under_a_key_or_webhook_is_masked",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a fused secret word is not read",
        "secret_mask.py",
        "    strong = _spelt_ends(words, _SECRET_TERM) + _fused_ends(words, _FUSED_SECRET)",
        "    strong = _spelt_ends(words, _SECRET_TERM)",
        "`PIN_DEFAULTPASSWORD=...` reached the worker while `DB_PASSWORD=...` did not: "
        "the key is split into words at case and separators, and a secret word fused "
        "onto another has neither",
        (
            "test_headless_workers.ExtractTests.test_nothing_a_worker_can_read_holds_a_planted_secret",
            "test_secret_mask.FusedSecretWords.test_a_fused_secret_word_masks_the_value",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a plain URL is masked as a secret",
        "secret_mask.py",
        '    return authority is not None and "@" not in authority.group(0)',
        "    return False",
        "an endpoint under an `auth` key - `S2S_AUTH: http://service:4502` - was masked "
        "whole, withholding which service is wired in while protecting nothing: a URL's "
        "password is the `url-credential` shape's and a query secret the key rule's",
        (
            "test_secret_mask.DocumentedLimits.test_each_documented_limit_still_reaches_the_worker",
            "test_secret_mask.PointersAreNotSecrets.test_a_url_or_terraform_reference_is_unchanged",
            "test_secret_mask.PointersAreNotSecrets.test_what_is_not_a_pointer_is_still_masked",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a templated URL host is masked as a secret",
        "secret_mask.py",
        "    value = deploy_values." + "strip_template(value)\n    scheme = _URL_SCHEME",
        "    scheme = _URL_SCHEME",
        "`S2S_AUTH: http://auth-{{ .Values.env }}.internal` in a Helm values template "
        "was masked whole, because the spaces inside the interpolation read as text "
        "rather than as the placeholder the template stands for",
        ("test_secret_mask.PointersAreNotSecrets.test_a_url_or_terraform_reference_is_unchanged",),
    ),
    Mutation(
        "a Terraform reference is masked as a secret",
        "secret_mask.py",
        "    return head is not None and _HCL_TAIL.fullmatch(value, head.end()) is not None",
        "    return False",
        "`vm_admin_password = var.admin_password` names the variable a module reads, "
        "which is architecture; it was masked as though it were the password",
        (
            "test_secret_mask.PointersAreNotSecrets.test_a_url_or_terraform_reference_is_unchanged",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a literal inside a call under a secret key is not masked",
        "secret_mask.py",
        "        return _masked_literals(kind, value, counts)",
        "        return None",
        '`DB_PASSWORD = base64encode("...")` reached the worker whole, because a value '
        "that is a call reads as code and code is left alone",
        (
            "test_headless_workers.ExtractTests.test_nothing_a_worker_can_read_holds_a_planted_secret",
            "test_secret_mask.LiteralsInsideCode.test_a_literal_in_a_call_is_masked_and_the_call_kept",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "the closers after a masked literal are eaten",
        "secret_mask.py",
        '    rest = value[literal.end() :] if literal else ""',
        '    rest = ""',
        "`rawPassword = '...');` became `rawPassword = [masked];`: the line rule reads a "
        "value to the end of its line, so the closing quote and bracket went with the "
        "secret and the worker read code that is not in the file",
        (
            "test_secret_mask.OnlyTheValueChanges.test_the_closers_after_a_masked_literal_survive",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a private-key block is collapsed onto one line",
        "secret_mask.py",
        "        return whole[:start] + _line_by_line(whole[start:end], masked) + whole[end:]",
        "        return whole[:start] + masked + whole[end:]",
        "a PEM block masked as one `[masked]` dropped its inner newlines, so every line a "
        "worker cited below it in the masked copy pointed at the wrong line of the file",
        (
            "test_secret_mask.PemBlocksKeepTheirLines.test_each_line_of_the_block_is_masked_and_every_line_ending_kept",
            "test_secret_mask.ValueShapeRules.test_each_shape_is_masked_under_its_own_rule_name",
            "test_secret_mask_corpus.CorpusProperties.test_masking_keeps_every_line_of_every_file",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a SAS signature after an escaped ampersand is not masked",
        "config.py",
        """    "sas-signature": r"[?&;]sig=([^&;\\s\\"'<>]+)",""",
        """    "sas-signature": r"[?&]sig=([^&;\\s\\"'<>]+)",""",
        "a signed URL pasted into markdown or HTML writes `&` as `&amp;`, so its `sig=` "
        "follows a `;` and the shape that required `?` or `&` let it through",
        (
            "test_headless_workers.ExtractTests.test_nothing_a_worker_can_read_holds_a_planted_secret",
            "test_secret_mask.EscapedSasSignatures.test_the_signature_after_an_html_entity_is_masked_and_the_rest_kept",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a secret in a URL query is not masked",
        "secret_mask.py",
        "    text = _apply_query_secrets(text, tally)\n",
        "",
        "`CALLBACK: https://.../cb?token=...` reached the worker whole: the key-name rule "
        "read the line as one assignment under a key naming no secret and never reached "
        "the parameter inside the URL",
        (
            "test_headless_workers.ExtractTests.test_nothing_a_worker_can_read_holds_a_planted_secret",
            "test_secret_mask.UrlQuerySecrets.test_a_secret_parameter_is_masked_and_the_rest_of_the_url_kept",
            "test_secret_mask.UrlQuerySecrets.test_a_signature_is_counted_once_by_its_own_rule",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "the credential guard dropped for code and key query parameters",
        "secret_mask.py",
        "    return _looks_like_credential(value) if guarded else named is not None",
        "    return guarded or named is not None",
        "`code` and `key` are ordinary query names as often as secret ones - `?code=200`, "
        "`?key=name` - so masking every value under them withholds status codes and "
        "lookups from the worker and inflates the masked count",
        (
            "test_secret_mask.UrlQuerySecrets.test_an_ordinary_parameter_or_a_name_like_a_secret_is_unchanged",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "an instrumentation key is not masked",
        "config.py",
        '        r"|InstrumentationKey=(?P<guarded>[^;\\s\\"\']+))"\n',
        '        r")"\n',
        "an Application Insights connection string under `APPINSIGHTS_CONNECTION_STRING` "
        "reached the worker with its key: the key names no secret word any more, and no "
        "connection-string shape knew the `InstrumentationKey=` field",
        (
            "test_headless_workers.ExtractTests.test_nothing_a_worker_can_read_holds_a_planted_secret",
            "test_secret_mask.InstrumentationKeys.test_the_instrumentation_key_is_masked_and_the_endpoint_kept",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a guarded shape group is masked whatever its value",
        "secret_mask.py",
        "        if group == guarded and not _looks_like_credential(whole[start:end]):",
        "        if False:",
        "`InstrumentationKey=your-instrumentation-key` in a template is a word, not a key; "
        "masking every value of the field withholds the template's shape and inflates the "
        "masked count",
        (
            "test_secret_mask.InstrumentationKeys.test_a_placeholder_or_a_word_in_the_key_field_is_unchanged",
        ),
    ),
    Mutation(
        "a template placeholder is masked as a secret",
        "secret_mask.py",
        "    if any(placeholder.fullmatch(value) for placeholder in _PLACEHOLDERS):",
        "    if False:",
        "`AccountKey=<storage-account-key>` and `__STORAGE_KEY__` stand for a value a "
        "pipeline substitutes; masking them protects nothing and hides which field the "
        "template fills",
        (
            "test_secret_mask.InstrumentationKeys.test_an_account_key_placeholder_is_unchanged",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a structured block scalar is masked wholesale",
        "secret_mask.py",
        "    if kind != _MANIFEST and _is_structured(content):",
        "    if False:",
        "Jenkins configuration-as-code embedded under `credentials: |` or `auth: |` became "
        "a column of `[masked]`: credential ids, scopes and role-matrix entries withheld "
        "when only the secrets inside the block are secret",
        (
            "test_secret_mask.DocumentedLimits.test_each_documented_limit_still_reaches_the_worker",
            "test_secret_mask.StructuredBlockScalars.test_embedded_configuration_holding_no_secret_is_unchanged",
            "test_secret_mask.StructuredBlockScalars.test_the_body_is_read_as_a_file_of_its_own",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a base64 blob is read as an assignment",
        "secret_mask.py",
        '    re.compile(r"[A-Za-z_][\\w.-]*[ \\t]*=(?!=)[ \\t]*\\S"),',
        '    re.compile(r"[A-Za-z_][\\w.-]*[ \\t]*=[ \\t]*\\S"),',
        "a base64 secret ending in `==` reads as `name=value` to a loose assignment test, "
        "so the block holding it would be treated as configuration, masked by rules that "
        "find nothing in it, and reach the worker whole",
        ("test_secret_mask.StructuredBlockScalars.test_an_opaque_body_is_still_masked_whole",),
    ),
    Mutation(
        "a Kubernetes env item's value is not masked",
        "secret_mask.py",
        "        if _VALUE_KEY in fields and kind:",
        "        if False:",
        "`- name: DB_PASSWORD` with `value:` on the next line is how most deployments "
        "hand a container its secrets, and the key on the value's own line names "
        "nothing, so without the pairing every literal in an `env:` list reaches the "
        "worker whole",
        (
            "test_headless_workers.ExtractTests.test_nothing_a_worker_can_read_holds_a_planted_secret",
            "test_secret_mask.KubernetesEnvLists.test_the_value_of_an_item_named_for_a_secret_is_masked",
            "test_secret_mask.LineEndingsPreserved.test_crlf_survives_masking_byte_for_byte",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "an XML appSetting's value is not masked",
        "secret_mask.py",
        "        kind = _secret_kind(key) or (paired if key.lower() == _VALUE_KEY else None)",
        "        kind = _secret_kind(key)",
        '`<add key="DbPassword" value="..."/>` is the .NET configuration shape, and '
        "the attribute holding the secret is called `value`; reading attributes one at "
        "a time masks only those named for a secret and passes this one through",
        (
            "test_headless_workers.ExtractTests.test_nothing_a_worker_can_read_holds_a_planted_secret",
            "test_secret_mask.XmlConfig.test_an_xml_secret_is_masked_and_its_markup_kept",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a CRLF file loses its carriage returns",
        "secret_mask.py",
        '    return "\\n".join(line + "\\r" * original.endswith("\\r") for line, original in zip(lines, raw))',
        '    return "\\n".join(lines)',
        "the by-line walks read each line without its `\\r`; rejoining without it turns "
        "every CRLF file a worker reads into an LF one, so the masked copy differs from "
        "the original on every line rather than only at its secrets",
        (
            "test_secret_mask.LineEndingsPreserved.test_a_crlf_file_holding_no_secret_is_unchanged",
            "test_secret_mask.LineEndingsPreserved.test_crlf_survives_masking_byte_for_byte",
            "test_secret_mask.PemBlocksKeepTheirLines.test_each_line_of_the_block_is_masked_and_every_line_ending_kept",
            "test_secret_mask.StructuredBlockScalars.test_the_body_is_read_as_a_file_of_its_own",
            "test_secret_mask.UrlQuerySecrets.test_a_secret_parameter_is_masked_and_the_rest_of_the_url_kept",
            "test_secret_mask_corpus.CorpusProperties.test_masking_keeps_every_line_of_every_file",
            "test_secret_mask_corpus.CorpusProperties.test_no_planted_value_survives_and_nothing_else_changes",
        ),
    ),
    Mutation(
        "a repair round re-grants the original files",
        "headless_workers.py",
        '        resume = parsed.get("session_id")\n',
        '        resume = parsed.get("session_id")\n'
        "        rules = permission_rules(job.reads, job.out) or rules\n",
        "the gate failing round 1 is ordinary, and a resumed session rebuilt from the "
        "job's own reads is granted the originals: the worker that was kept from every "
        "secret in round 1 is handed them in round 2, and the report still says masked",
        ("test_headless_workers.ExtractTests.test_a_repair_round_still_reads_the_masked_copy",),
    ),
    Mutation(
        "ticket refresh: a fetched record is final again",
        "fetch_tickets.py",
        "    return age is None or age >= config.TRACKER_REFRESH_DAYS\n",
        "    return False\n",
        "the shipped defect in #421: any fetched record counted as final, so a ticket "
        "fetched while open kept that status, description and comments for ever, and "
        "one store deleted its unresolved records before every run to work around it",
        (
            "test_fetch_tickets.RefreshBoundsTest.test_a_ticket_the_store_no_longer_cites_is_not_refreshed",
            "test_fetch_tickets.RefreshBoundsTest.test_refreshes_are_batched_into_pages_of_their_own",
            "test_fetch_tickets.RefreshBoundsTest.test_the_cap_bounds_refreshes_per_run_and_the_rest_carry_over",
            "test_fetch_tickets.RefreshBoundsTest.test_the_oldest_records_are_refreshed_first",
            "test_fetch_tickets.RefreshBoundsTest.test_the_selection_is_the_same_whatever_order_keys_arrive_in",
            "test_fetch_tickets.RefreshKeepsTest.test_a_kept_refresh_waits_its_interval_and_does_not_starve_the_queue",
            "test_fetch_tickets.RefreshKeepsTest.test_a_refresh_never_leaves_a_record_holding_less",
            "test_fetch_tickets.RefreshTest.test_a_refresh_the_tracker_does_not_answer_keeps_the_earlier_record",
            "test_fetch_tickets.RefreshTest.test_an_open_record_with_no_checked_date_is_due",
            "test_fetch_tickets.RefreshTest.test_an_open_ticket_is_fetched_again_once_its_record_is_a_week_old",
            "test_fetch_tickets.RefreshTest.test_the_interval_is_read_from_its_setting",
            "test_fetch_tickets.ThreadTruncationTest.test_a_kept_longer_thread_is_marked_when_the_tracker_reports_more",
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_refresh_counts_its_follow_ups_in_its_cost",
            "test_fetch_tickets.TwgRefreshTest.test_a_refresh_through_twg_is_capped_and_batched_like_any_other",
        ),
    ),
    Mutation(
        "ticket refresh: the minimum age is ignored",
        "fetch_tickets.py",
        "    return age is None or age >= config.TRACKER_REFRESH_DAYS\n",
        "    return True\n",
        "the easy over-correction of #421: every run re-requests every open ticket, "
        "which on a large estate is most of a first run's traffic again on each build",
        (
            "test_fetch_tickets.RefreshKeepsTest.test_a_kept_refresh_waits_its_interval_and_does_not_starve_the_queue",
            "test_fetch_tickets.RefreshTest.test_an_open_ticket_is_not_fetched_again_inside_the_interval",
        ),
    ),
    Mutation(
        "ticket refresh: an undated open record is presumed fresh",
        "fetch_tickets.py",
        "    return age is None or age >= config.TRACKER_REFRESH_DAYS\n",
        "    return age is not None and age >= config.TRACKER_REFRESH_DAYS\n",
        "every open record in a cache committed before #421 has no date, so reading "
        "no date as just-read leaves exactly the records the issue was about frozen",
        (
            "test_fetch_tickets.RefreshBoundsTest.test_the_oldest_records_are_refreshed_first",
            "test_fetch_tickets.RefreshTest.test_an_open_record_with_no_checked_date_is_due",
        ),
    ),
    Mutation(
        "ticket refresh: resolved tickets are re-requested",
        "fetch_tickets.py",
        '    return bool(record.get("resolution") or record.get("resolved"))\n',
        "    return False\n",
        "the refresh that cannot tell resolved from open re-requests the whole cache "
        "once it ages, which is a first run every interval for answers that cannot change",
        (
            "test_fetch_tickets.ProjectionTest.test_projected_fields_are_stored_in_reduced_form",
            "test_fetch_tickets.RefreshTest.test_a_resolved_ticket_is_never_fetched_again",
            "test_fetch_tickets.RefreshTest.test_a_ticket_resolved_without_a_resolution_name_is_still_resolved",
            "test_fetch_tickets.RefreshTest.test_an_open_ticket_is_fetched_again_once_its_record_is_a_week_old",
            "test_fetch_tickets.TwgRequestTest.test_a_page_is_one_bulk_get_answered_in_the_v2_shape",
        ),
    ),
    Mutation(
        "ticket refresh: a ticket resolved by date alone reads as open",
        "fetch_tickets.py",
        '    return bool(record.get("resolution") or record.get("resolved"))\n',
        '    return bool(record.get("resolution"))\n',
        "a workflow can stamp the resolution date without naming a resolution, and "
        "reading only the name re-requests those finished tickets every interval",
        (
            "test_fetch_tickets.RefreshTest.test_a_ticket_resolved_without_a_resolution_name_is_still_resolved",
        ),
    ),
    Mutation(
        "ticket refresh: an open record carries no date",
        "fetch_tickets.py",
        "    return dict(sorted({**record, CHECKED: today}.items()))\n",
        "    return record\n",
        "without the day it was read, an open record's age is unknowable, so it is due "
        "on every run and the interval does nothing",
        (
            "test_fetch_tickets.ProjectionTest.test_an_open_ticket_stores_no_empty_fields",
            "test_fetch_tickets.RefreshBoundsTest.test_the_cap_bounds_refreshes_per_run_and_the_rest_carry_over",
            "test_fetch_tickets.RefreshKeepsTest.test_a_kept_refresh_waits_its_interval_and_does_not_starve_the_queue",
            "test_fetch_tickets.RefreshKeepsTest.test_a_refresh_never_leaves_a_record_holding_less",
            "test_fetch_tickets.RefreshTest.test_an_open_ticket_is_fetched_again_once_its_record_is_a_week_old",
            "test_fetch_tickets.RefreshTest.test_an_open_ticket_is_not_fetched_again_inside_the_interval",
            "test_fetch_tickets.RefreshTest.test_the_interval_is_read_from_its_setting",
            "test_fetch_tickets.TwgRequestTest.test_a_page_is_one_bulk_get_answered_in_the_v2_shape",
        ),
    ),
    Mutation(
        "ticket refresh: a refresh downgrades a fetched record",
        "fetch_tickets.py",
        "        if not is_fetched(record):\n            if not is_resolved(previous):\n                cache[key] = dict(sorted({**previous, CHECKED: today}.items()))\n            report.kept += 1\n            return KEPT\n",
        "",
        "a search omits tickets the token cannot browse, so a refresh by a narrower operator caches a fetched ticket as absent - which is final - and its text is gone from the store for good",
        (
            "test_fetch_tickets.RefreshKeepsTest.test_a_kept_refresh_waits_its_interval_and_does_not_starve_the_queue",
            "test_fetch_tickets.RefreshTest.test_a_refresh_the_tracker_does_not_answer_keeps_the_earlier_record",
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_refresh_counts_its_follow_ups_in_its_cost",
        ),
    ),
    Mutation(
        "ticket refresh: refreshes are not counted",
        "fetch_tickets.py",
        "        report.refreshed += 1\n",
        "",
        "#421 asks the run to say how many records it refreshed; a refresh that stops "
        "happening is otherwise indistinguishable from nothing being open",
        (
            "test_fetch_tickets.RefreshBoundsTest.test_the_cap_bounds_refreshes_per_run_and_the_rest_carry_over",
            "test_fetch_tickets.RefreshTest.test_an_open_ticket_is_fetched_again_once_its_record_is_a_week_old",
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_refresh_counts_its_follow_ups_in_its_cost",
            "test_fetch_tickets.TwgRefreshTest.test_a_refresh_through_twg_is_capped_and_batched_like_any_other",
        ),
    ),
    Mutation(
        "ticket refresh: the per-run cap is ignored",
        "fetch_tickets.py",
        "    return due[: max(config.TRACKER_REFRESH_MAX, 0)], len(due)\n",
        "    return due, len(due)\n",
        "a store upgrading from a release without refresh has every open record due at once, and an uncapped refresh lands that whole backlog on one build",
        (
            "test_fetch_tickets.RefreshBoundsTest.test_the_cap_bounds_refreshes_per_run_and_the_rest_carry_over",
            "test_fetch_tickets.RefreshBoundsTest.test_the_oldest_records_are_refreshed_first",
            "test_fetch_tickets.RefreshBoundsTest.test_the_selection_is_the_same_whatever_order_keys_arrive_in",
            "test_fetch_tickets.RefreshKeepsTest.test_a_kept_refresh_waits_its_interval_and_does_not_starve_the_queue",
            "test_fetch_tickets.TwgRefreshTest.test_a_refresh_through_twg_is_capped_and_batched_like_any_other",
        ),
    ),
    Mutation(
        "ticket refresh: the cap takes the newest first",
        "fetch_tickets.py",
        "        key=lambda key: (_read_on(cache[key]), key),\n",
        "        key=lambda key: (_read_on(cache[key]), key),\n        reverse=True,\n",
        "the cap then keeps refreshing the records read most recently, and the oldest wait for ever",
        (
            "test_fetch_tickets.RefreshBoundsTest.test_refreshes_are_batched_into_pages_of_their_own",
            "test_fetch_tickets.RefreshBoundsTest.test_the_cap_bounds_refreshes_per_run_and_the_rest_carry_over",
            "test_fetch_tickets.RefreshBoundsTest.test_the_oldest_records_are_refreshed_first",
            "test_fetch_tickets.RefreshBoundsTest.test_the_selection_is_the_same_whatever_order_keys_arrive_in",
            "test_fetch_tickets.RefreshKeepsTest.test_a_kept_refresh_waits_its_interval_and_does_not_starve_the_queue",
            "test_fetch_tickets.TwgRefreshTest.test_a_refresh_through_twg_is_capped_and_batched_like_any_other",
        ),
    ),
    Mutation(
        "ticket refresh: the cap takes keys in key order",
        "fetch_tickets.py",
        "        key=lambda key: (_read_on(cache[key]), key),\n",
        "        key=lambda key: key,\n",
        "ordering by key alone ignores age, so the same low-numbered tickets win every run while older records wait",
        ("test_fetch_tickets.RefreshBoundsTest.test_the_oldest_records_are_refreshed_first",),
    ),
    Mutation(
        "ticket refresh: ties follow the order keys arrived in",
        "fetch_tickets.py",
        "        key=lambda key: (_read_on(cache[key]), key),\n",
        "        key=lambda key: (_read_on(cache[key]),),\n",
        "which records change in a committed artefact would then depend on iteration order rather than on the inputs",
        (
            "test_fetch_tickets.RefreshBoundsTest.test_the_selection_is_the_same_whatever_order_keys_arrive_in",
        ),
    ),
    Mutation(
        "ticket refresh: zero days refreshes every run",
        "fetch_tickets.py",
        "    if config.TRACKER_REFRESH_DAYS <= 0:\n        return False\n",
        "",
        "0 is the documented way back to the behaviour before refresh existed; read as an age it is the most expensive setting instead",
        ("test_fetch_tickets.RefreshBoundsTest.test_zero_days_turns_refresh_off",),
    ),
    Mutation(
        "ticket refresh: one request per refreshed ticket",
        "fetch_tickets.py",
        "    batches += [(page, True) for page in pages(refreshing, size)]\n",
        "    batches += [(page, True) for page in pages(refreshing, 1)]\n",
        "a refresh outside the paged search costs a request per ticket, which is the cost the stage's batching exists to avoid",
        (
            "test_fetch_tickets.RefreshBoundsTest.test_refreshes_are_batched_into_pages_of_their_own",
            "test_fetch_tickets.RefreshBoundsTest.test_the_cap_bounds_refreshes_per_run_and_the_rest_carry_over",
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_refresh_counts_its_follow_ups_in_its_cost",
            "test_fetch_tickets.TwgRefreshTest.test_a_refresh_through_twg_is_capped_and_batched_like_any_other",
        ),
    ),
    Mutation(
        "ticket refresh: refresh requests are not counted",
        "fetch_tickets.py",
        "        report.refresh_requests += calls if refresh else 0\n",
        "",
        "what a refresh cost is only measurable if its requests are counted apart from new fetches",
        (
            "test_fetch_tickets.RefreshBoundsTest.test_refreshes_are_batched_into_pages_of_their_own",
            "test_fetch_tickets.RefreshBoundsTest.test_the_cap_bounds_refreshes_per_run_and_the_rest_carry_over",
            "test_fetch_tickets.RefreshTest.test_an_open_ticket_is_fetched_again_once_its_record_is_a_week_old",
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_refresh_counts_its_follow_ups_in_its_cost",
            "test_fetch_tickets.TwgRefreshTest.test_a_refresh_through_twg_is_capped_and_batched_like_any_other",
        ),
    ),
    Mutation(
        "ticket refresh: tickets the store no longer cites are refreshed",
        "fetch_tickets.py",
        "        (key for key in keys if refresh_due(cache.get(key), today)),\n",
        "        (key for key in cache if refresh_due(cache.get(key), today)),\n",
        "candidates drawn from the cache pay for tickets no commit cites any more, and re-read prefixes withdrawn into the deny list",
        (
            "test_fetch_tickets.RefreshBoundsTest.test_a_ticket_the_store_no_longer_cites_is_not_refreshed",
        ),
    ),
    Mutation(
        "ticket refresh: refresh cost is not recorded in telemetry",
        "fetch_tickets.py",
        "    telemetry.record(refresh_measurements(report))\n",
        "",
        "without a recorded predecessor the next run cannot say whether refresh cost moved",
        (
            "test_fetch_tickets.RefreshBoundsTest.test_the_cap_bounds_refreshes_per_run_and_the_rest_carry_over",
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_refresh_counts_its_follow_ups_in_its_cost",
        ),
    ),
    Mutation(
        "twg transport: a mention renders the display name",
        "twg_transport.py",
        '    return f"[~accountid:{account}]" if isinstance(account, str) and account else ""\n',
        '    return str(attrs.get("text", ""))\n',
        "#420 point 3, a hard privacy requirement: an ADF mention carries the person's display name in attrs.text, and the store is republished",
        (
            "test_fetch_tickets.TwgBodyTest.test_a_mention_deep_inside_other_nodes_is_still_an_account_id",
            "test_fetch_tickets.TwgBodyTest.test_document_bodies_are_flattened_one_line_per_block",
            "test_fetch_tickets.TwgBodyTest.test_no_person_s_name_reaches_the_store",
            "test_fetch_tickets.TwgListTest.test_a_mention_in_a_list_item_is_still_an_account_id",
            "test_fetch_tickets.TwgThreadTest.test_a_partial_thread_is_completed_by_one_counted_follow_up",
            "test_fetch_tickets.TwgThreadTest.test_a_thread_left_partial_is_marked_truncated",
        ),
    ),
    Mutation(
        "twg transport: a mention with no id falls back to the name",
        "twg_transport.py",
        '    return f"[~accountid:{account}]" if isinstance(account, str) and account else ""\n',
        '    return f"[~accountid:{account}]" if isinstance(account, str) and account else str(attrs.get("text", ""))\n',
        "the friendly fallback for a mention with no id is the one that publishes a name",
        (
            "test_fetch_tickets.TwgBodyTest.test_a_mention_deep_inside_other_nodes_is_still_an_account_id",
        ),
    ),
    Mutation(
        "twg transport: document bodies are stored as nothing",
        "twg_transport.py",
        "    if not isinstance(body, dict):\n        return body\n",
        "    return body\n",
        "#420 point 2: Cloud bodies are ADF mappings and the library keeps only strings, so an unflattened body stores nothing and reports success",
        (
            "test_fetch_tickets.TwgBodyTest.test_a_mention_deep_inside_other_nodes_is_still_an_account_id",
            "test_fetch_tickets.TwgBodyTest.test_document_bodies_are_flattened_one_line_per_block",
            "test_fetch_tickets.TwgBodyTest.test_no_person_s_name_reaches_the_store",
            "test_fetch_tickets.TwgListTest.test_a_bullet_list_keeps_its_markers",
            "test_fetch_tickets.TwgListTest.test_a_hard_break_stays_a_line_break",
            "test_fetch_tickets.TwgListTest.test_a_mention_in_a_list_item_is_still_an_account_id",
            "test_fetch_tickets.TwgListTest.test_a_nested_list_is_indented_two_spaces_per_level",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_honours_its_start_number",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_is_numbered_from_one",
            "test_fetch_tickets.TwgPrivacyTest.test_an_emoji_and_a_link_card_render",
            "test_fetch_tickets.TwgPrivacyTest.test_no_node_s_attrs_text_reaches_the_flattened_text",
            "test_fetch_tickets.TwgThreadTest.test_a_partial_thread_is_completed_by_one_counted_follow_up",
            "test_fetch_tickets.TwgThreadTest.test_a_thread_left_partial_is_marked_truncated",
        ),
    ),
    Mutation(
        "twg transport: blocks run together",
        "twg_transport.py",
        '    return "\\n".join(out)\n',
        '    return " ".join(out)\n',
        "#420 point 2 asks for one line per block; joined, a heading, a list item and a table cell read as one sentence",
        (
            "test_fetch_tickets.TwgBodyTest.test_document_bodies_are_flattened_one_line_per_block",
            "test_fetch_tickets.TwgListTest.test_a_bullet_list_keeps_its_markers",
            "test_fetch_tickets.TwgListTest.test_a_hard_break_stays_a_line_break",
            "test_fetch_tickets.TwgListTest.test_a_nested_list_is_indented_two_spaces_per_level",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_honours_its_start_number",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_is_numbered_from_one",
        ),
    ),
    Mutation(
        "twg transport: nested blocks are not read",
        "twg_transport.py",
        "        _lines(child, out)\n",
        "        pass\n",
        "list items, table cells and panels hold most of a structured description; skipping containers drops them and every mention inside",
        (
            "test_fetch_tickets.TwgBodyTest.test_a_mention_deep_inside_other_nodes_is_still_an_account_id",
            "test_fetch_tickets.TwgBodyTest.test_document_bodies_are_flattened_one_line_per_block",
            "test_fetch_tickets.TwgBodyTest.test_no_person_s_name_reaches_the_store",
            "test_fetch_tickets.TwgListTest.test_a_bullet_list_keeps_its_markers",
            "test_fetch_tickets.TwgListTest.test_a_hard_break_stays_a_line_break",
            "test_fetch_tickets.TwgListTest.test_a_mention_in_a_list_item_is_still_an_account_id",
            "test_fetch_tickets.TwgListTest.test_a_nested_list_is_indented_two_spaces_per_level",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_honours_its_start_number",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_is_numbered_from_one",
            "test_fetch_tickets.TwgPrivacyTest.test_an_emoji_and_a_link_card_render",
            "test_fetch_tickets.TwgPrivacyTest.test_no_node_s_attrs_text_reaches_the_flattened_text",
            "test_fetch_tickets.TwgPrivacyTest.test_the_transport_hands_the_library_no_comment_author",
            "test_fetch_tickets.TwgThreadTest.test_a_partial_thread_is_completed_by_one_counted_follow_up",
            "test_fetch_tickets.TwgThreadTest.test_a_thread_left_partial_is_marked_truncated",
        ),
    ),
    Mutation(
        "twg transport: the exit code is read as no answer",
        "twg_transport.py",
        "    return completed.stdout\n",
        '    if completed.returncode:\n        raise OSError("twg failed")\n    return completed.stdout\n',
        "#420 point 4: twg exits 3 when any key in a page is unreturned, which is most pages, so reading the exit code leaves nearly every ticket uncached",
        (
            "test_fetch_tickets.TwgBoundaryTest.test_output_that_is_not_valid_utf_8_is_still_read",
            "test_fetch_tickets.TwgExitCodeTest.test_stdout_is_read_whatever_twg_exits_with",
        ),
    ),
    Mutation(
        "twg transport: output it cannot read is taken as absence",
        "twg_transport.py",
        "    if any(key not in items for key in keys):\n",
        "    if False:\n",
        "absent is final, so a signed-out run or a reshaped reply read as an empty answer loses every ticket in the page for good",
        (
            "test_fetch_tickets.TwgExitCodeTest.test_an_answer_that_omits_a_key_is_no_answer",
            "test_fetch_tickets.TwgExitCodeTest.test_output_that_does_not_parse_is_no_answer",
        ),
    ),
    Mutation(
        "twg transport: an unreadable follow-up is taken as absence",
        "twg_transport.py",
        "            return ABSENT\n        return UNANSWERED\n",
        "            return ABSENT\n        return ABSENT\n",
        "the same permanent loss through the second request: only the tracker's own not-found is an answer",
        ("test_fetch_tickets.TwgMovedTicketTest.test_only_a_not_found_from_get_is_absence",),
    ),
    Mutation(
        "twg transport: moved tickets are not followed",
        "twg_transport.py",
        "                issue = self.followed(key, fields)\n",
        "                issue = ABSENT\n",
        "#420's correction: bulk-get does not follow moves, so without the second request every moved ticket is cached as absent",
        (
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_follow_up_is_counted_and_paced_like_a_request",
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_refresh_counts_its_follow_ups_in_its_cost",
            "test_fetch_tickets.TwgFollowUpCostTest.test_follow_ups_stop_at_the_per_run_budget",
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_follow_up_that_fails_as_a_process_costs_only_its_key",
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_thread_completion_that_fails_as_a_process_leaves_the_thread_marked",
            "test_fetch_tickets.TwgMovedTicketTest.test_a_follow_up_reply_that_is_not_twg_s_envelope_is_no_answer",
            "test_fetch_tickets.TwgMovedTicketTest.test_a_ticket_moved_to_an_allowlisted_project_is_filed_under_the_cited_key",
            "test_fetch_tickets.TwgMovedTicketTest.test_an_unreadable_follow_up_is_no_answer_not_absence",
            "test_fetch_tickets.TwgMovedTicketTest.test_only_a_not_found_from_get_is_absence",
            "test_fetch_tickets.TwgThreadTest.test_a_partial_thread_is_completed_by_one_counted_follow_up",
        ),
    ),
    Mutation(
        "twg transport: a move crosses the allowlist",
        "twg_transport.py",
        "            if found.upper() != key.upper() and not self.may_read(found):\n",
        "            if False:\n",
        "a ticket moved to an undecided or denied project would be read although nobody authorised that project",
        (
            "test_fetch_tickets.TwgMovedTicketTest.test_a_ticket_moved_to_a_project_nobody_allowed_stays_absent",
        ),
    ),
    Mutation(
        "twg transport: a moved ticket is filed under its new key",
        "twg_transport.py",
        '        issues.append({"key": key, "fields": projected(issue, fields)})\n',
        '        issues.append({"key": found, "fields": projected(issue, fields)})\n',
        "the library files a page's answers by the keys the commits cite; under the new key the record is absent where it is looked up",
        (
            "test_fetch_tickets.TwgMovedTicketTest.test_a_ticket_moved_to_an_allowlisted_project_is_filed_under_the_cited_key",
        ),
    ),
    Mutation(
        "twg transport: the request is widened to every field",
        "twg_transport.py",
        '        "--fields",\n        ",".join(fields),\n',
        '        "--full",\n',
        "a widened request fetches narrative nobody enabled, which the HTTP route guarantees against by never asking",
        (
            "test_fetch_tickets.TwgBodyTest.test_document_bodies_are_flattened_one_line_per_block",
            "test_fetch_tickets.TwgMovedTicketTest.test_a_ticket_moved_to_an_allowlisted_project_is_filed_under_the_cited_key",
            "test_fetch_tickets.TwgRefreshTest.test_a_refresh_through_twg_is_capped_and_batched_like_any_other",
            "test_fetch_tickets.TwgRequestTest.test_a_page_is_one_bulk_get_answered_in_the_v2_shape",
            "test_fetch_tickets.TwgThreadTest.test_a_partial_thread_is_completed_by_one_counted_follow_up",
        ),
    ),
    Mutation(
        "twg flatten (#436): a hard break is a space",
        "twg_transport.py",
        '    return "\\n"\n',
        '    return " "\n',
        "a hard break joined with a space stores two lines a writer kept apart as one, unlike the same ticket over Data Center",
        (
            "test_fetch_tickets.TwgBodyTest.test_document_bodies_are_flattened_one_line_per_block",
            "test_fetch_tickets.TwgListTest.test_a_hard_break_stays_a_line_break",
        ),
    ),
    Mutation(
        "twg flatten (#436): list items lose their markers",
        "twg_transport.py",
        "            out.append(marker + lines[0])\n",
        "            out.append(lines[0])\n",
        "a bare item reads as an unrelated sentence, where the Data Center body of the same ticket keeps its marker",
        (
            "test_fetch_tickets.TwgBodyTest.test_a_mention_deep_inside_other_nodes_is_still_an_account_id",
            "test_fetch_tickets.TwgBodyTest.test_document_bodies_are_flattened_one_line_per_block",
            "test_fetch_tickets.TwgListTest.test_a_bullet_list_keeps_its_markers",
            "test_fetch_tickets.TwgListTest.test_a_mention_in_a_list_item_is_still_an_account_id",
            "test_fetch_tickets.TwgListTest.test_a_nested_list_is_indented_two_spaces_per_level",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_honours_its_start_number",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_is_numbered_from_one",
        ),
    ),
    Mutation(
        "twg flatten (#436): ordered items are bulleted",
        "twg_transport.py",
        '    return "- " if number is None else f"{number}. "\n',
        '    return "- "\n',
        "an ordered list loses the order its numbers carried",
        (
            "test_fetch_tickets.TwgListTest.test_a_mention_in_a_list_item_is_still_an_account_id",
            "test_fetch_tickets.TwgListTest.test_a_nested_list_is_indented_two_spaces_per_level",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_honours_its_start_number",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_is_numbered_from_one",
        ),
    ),
    Mutation(
        "twg flatten (#436): numbering does not advance",
        "twg_transport.py",
        "            number += 1\n",
        "            pass\n",
        "every item of an ordered list is numbered the same",
        (
            "test_fetch_tickets.TwgListTest.test_a_nested_list_is_indented_two_spaces_per_level",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_honours_its_start_number",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_is_numbered_from_one",
        ),
    ),
    Mutation(
        "twg flatten (#436): the start number is ignored",
        "twg_transport.py",
        '    number = _start(node) if node.get("type") == "orderedList" else None\n',
        '    number = 1 if node.get("type") == "orderedList" else None\n',
        "a list continued after a paragraph restarts at one, so its items are misnumbered",
        (
            "test_fetch_tickets.TwgListTest.test_a_mention_in_a_list_item_is_still_an_account_id",
            "test_fetch_tickets.TwgListTest.test_an_ordered_list_honours_its_start_number",
        ),
    ),
    Mutation(
        "twg flatten (#436): a non-numeric start is used as the number",
        "twg_transport.py",
        "    return order if isinstance(order, int) and not isinstance(order, bool) and order >= 0 else 1\n",
        "    return order if order is not None else 1\n",
        "attrs.order is a start number; read as anything else an attribute's text reaches the body",
        ("test_fetch_tickets.TwgListTest.test_an_ordered_list_honours_its_start_number",),
    ),
    Mutation(
        "twg flatten (#436): nested lists are not indented",
        "twg_transport.py",
        '            out.extend("  " + line for line in lines[1:])\n',
        "            out.extend(lines[1:])\n",
        "a sub-point reads as a sibling of its parent",
        ("test_fetch_tickets.TwgListTest.test_a_nested_list_is_indented_two_spaces_per_level",),
    ),
    Mutation(
        "twg transport: the route still demands a token",
        "fetch_tickets.py",
        "    if config.TRACKER_TRANSPORT == TWG:\n        return []\n",
        "",
        "twg signs in by itself, so demanding KSB_TRACKER_TOKEN makes the route unusable without a credential it never sends",
        (
            "test_fetch_tickets.TwgBodyTest.test_document_bodies_are_flattened_one_line_per_block",
            "test_fetch_tickets.TwgBodyTest.test_no_person_s_name_reaches_the_store",
            "test_fetch_tickets.TwgBoundaryTest.test_a_refused_key_costs_only_itself",
            "test_fetch_tickets.TwgBoundaryTest.test_output_that_is_not_valid_utf_8_is_still_read",
            "test_fetch_tickets.TwgExitCodeTest.test_output_that_does_not_parse_is_no_answer",
            "test_fetch_tickets.TwgExitCodeTest.test_stdout_is_read_whatever_twg_exits_with",
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_follow_up_is_counted_and_paced_like_a_request",
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_refresh_counts_its_follow_ups_in_its_cost",
            "test_fetch_tickets.TwgFollowUpCostTest.test_follow_ups_stop_at_the_per_run_budget",
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_follow_up_that_fails_as_a_process_costs_only_its_key",
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_thread_completion_that_fails_as_a_process_leaves_the_thread_marked",
            "test_fetch_tickets.TwgMovedTicketTest.test_a_follow_up_reply_that_is_not_twg_s_envelope_is_no_answer",
            "test_fetch_tickets.TwgMovedTicketTest.test_a_ticket_moved_to_a_project_nobody_allowed_stays_absent",
            "test_fetch_tickets.TwgMovedTicketTest.test_a_ticket_moved_to_an_allowlisted_project_is_filed_under_the_cited_key",
            "test_fetch_tickets.TwgMovedTicketTest.test_an_unreadable_follow_up_is_no_answer_not_absence",
            "test_fetch_tickets.TwgMovedTicketTest.test_only_a_not_found_from_get_is_absence",
            "test_fetch_tickets.TwgRefreshTest.test_a_refresh_through_twg_is_capped_and_batched_like_any_other",
            "test_fetch_tickets.TwgRequestTest.test_a_page_is_one_bulk_get_answered_in_the_v2_shape",
            "test_fetch_tickets.TwgThreadTest.test_a_partial_thread_is_completed_by_one_counted_follow_up",
            "test_fetch_tickets.TwgThreadTest.test_a_thread_left_partial_is_marked_truncated",
            "test_fetch_tickets.TwgThreadTest.test_no_thread_is_fetched_unless_comments_are_requested",
        ),
    ),
    Mutation(
        "twg transport: an unknown transport falls back",
        "fetch_tickets.py",
        "    if config.TRACKER_TRANSPORT not in TRANSPORTS:\n",
        "    if False:\n",
        "a misspelt transport silently becomes the HTTP route or a not-configured no-op, and its result reads as the tracker's",
        (
            "test_fetch_tickets.TwgRequestTest.test_an_unknown_transport_is_refused_before_anything_is_asked",
        ),
    ),
    Mutation(
        "answers: a tool result opens an answer",
        "answers.py",
        '        isinstance(block, dict) and block.get("type") == "tool_result" for block in content',
        "        False for block in content",
        "a tool's result comes back to the session as a user-role line; read as a prompt "
        "it splits one answer at every tool call, so the answer count is wrong and no "
        "question pairs with its earlier run",
        (
            "test_answers.CommandLine.test_out_writes_one_record_per_answer",
            "test_answers.CommandLine.test_without_out_it_prints_a_summary_and_writes_nothing",
            "test_answers.HeadlessCost.test_dollars_come_only_from_a_matching_headless_result",
            "test_answers.SplittingASession.test_a_resumed_session_counts_each_repeated_turn_once",
            "test_answers.SplittingASession.test_a_tool_result_does_not_open_an_answer",
            "test_answers.SplittingASession.test_a_turn_split_over_lines_is_counted_once",
            "test_answers.SplittingASession.test_an_interrupt_marker_is_not_a_prompt",
            "test_answers.SplittingASession.test_the_answer_text_is_the_last_text_before_the_next_prompt",
            "test_answers.SubagentsAreFolded.test_a_linked_subagent_that_is_missing_is_counted",
            "test_answers.SubagentsAreFolded.test_a_subagent_s_usage_is_part_of_the_answer_that_dispatched_it",
            "test_answers.SubagentsAreFolded.test_the_subagent_s_text_is_not_the_answer",
        ),
    ),
    Mutation(
        "answers: usage counted per line, not per message",
        "cost.py",
        "        if message_id in self.largest_output:",
        "        if False:",
        "a transcript writes one line per content block of a turn, each carrying the "
        "whole turn's usage, so a per-line sum reads a three-block turn three times - "
        "in `cost`'s run total and in every answer's input alike, since both share it",
        (
            "test_answers.CommandLine.test_without_out_it_prints_a_summary_and_writes_nothing",
            "test_answers.SplittingASession.test_a_resumed_session_counts_each_repeated_turn_once",
            "test_answers.SplittingASession.test_a_tool_result_does_not_open_an_answer",
            "test_answers.SplittingASession.test_a_turn_split_over_lines_is_counted_once",
            "test_cost.CostStage.test_a_turn_written_as_several_content_blocks_is_counted_once",
            "test_cost.CostStage.test_one_turn_seen_in_two_transcripts_is_counted_once",
            "test_cost.CostStage.test_the_report_gives_the_de_duplicated_figure_as_the_input",
        ),
    ),
    Mutation(
        "answers: subagent usage dropped",
        "answers.py",
        "            self._subagent(transcript, answer, session)",
        "            pass",
        "an answer that delegates its search to a subagent reads as the cheapest answer "
        "in the run, because the parent session holds only the dispatch",
        (
            "test_answers.SubagentsAreFolded.test_a_linked_subagent_that_is_missing_is_counted",
            "test_answers.SubagentsAreFolded.test_a_subagent_s_usage_is_part_of_the_answer_that_dispatched_it",
            "test_answers.SubagentsAreFolded.test_the_subagent_s_text_is_not_the_answer",
        ),
    ),
    Mutation(
        "answers: a file outside the store counted",
        "answers.py",
        "                if candidate.is_relative_to(self.store) and candidate.is_file():",
        "                if candidate.is_file():",
        "every file a session touched lands in `store_files`, so a store's footprint "
        "includes the operator's home directory and the corpus clones",
        ("test_answers.StoreFiles.test_only_files_under_the_store_are_counted",),
    ),
    Mutation(
        "answers: a Bash path read with its separator",
        "answers.py",
        "        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)",
        "        lexer = shlex.shlex(command, posix=True)",
        "`cat a.md; echo done` names `a.md;`, which is no file, so the commonest shell "
        "form drops out of `store_files`; found by the first run over a real session",
        ("test_answers.StoreFiles.test_only_files_under_the_store_are_counted",),
    ),
    Mutation(
        "answers: a corpus path classed by the graph",
        "answers.py",
        '    if text.removeprefix("./").startswith("repositories/"):\n'
        '        return "corpus_path"\n',
        "",
        "a `repositories/...` path names the sync tree, which no reader of a published "
        "answer can open; classed by the graph it reads as a resolving citation",
        ("test_answers.Citations.test_each_citation_is_classed_once",),
    ),
    Mutation(
        "answers: compare drops an unpaired question",
        "answers.py",
        '    comparison.only_before = [rows[0]["question"] for sha, rows in old.items() if sha not in new]',
        "    comparison.only_before = []",
        "a question the second run no longer answers vanishes from the comparison, and "
        "the paired medians read as the whole story",
        ("test_answers.Comparing.test_an_unpaired_question_is_listed_on_its_own_side",),
    ),
    Mutation(
        "answers: absence matched inside a code span",
        "answers.py",
        "        if _ABSENCE.search(masked[start:end]):",
        "        if _ABSENCE.search(prose[start:end]):",
        "an answer quoting a log message such as `order is not found` is flagged as "
        "claiming that something is absent - and a span holding a full stop was cut "
        "in two by the sentence split before it could be masked, which the first "
        "version of the masking missed",
        (
            "test_answers.AbsenceClaims.test_a_code_span_holding_a_sentence_break_is_not_a_claim",
            "test_answers.AbsenceClaims.test_absence_words_inside_a_code_span_are_not_a_claim",
        ),
    ),
    Mutation(
        "answers: an interrupt marker read as a prompt",
        "answers.py",
        '    "[Request interrupted",\n',
        "",
        "the harness writes the interrupt marker as a user line of text with no "
        "origin; read as a prompt, every interrupted answer becomes two and the empty "
        "one pairs with every other run's interrupt",
        ("test_answers.SplittingASession.test_an_interrupt_marker_is_not_a_prompt",),
    ),
    Mutation(
        "answers: a shell line read as a prompt",
        "answers.py",
        '    "<bash-input>",\n',
        "",
        "a `!` command typed at the prompt opens an answer of its own",
        ("test_answers.SplittingASession.test_a_shell_line_is_not_a_prompt",),
    ),
    Mutation(
        "answers: another user's home expanded",
        "answers.py",
        '                word = Path(named).expanduser() if named == "~" or named.startswith("~/") else named',
        "                word = Path(named).expanduser()",
        "`~nosuchuser/x` in any Bash command raises RuntimeError and loses every record in the run",
        ("test_answers.StoreFiles.test_a_word_naming_another_user_s_home_does_not_stop_the_run",),
    ),
    Mutation(
        "answers: a column citation classed unresolved",
        "answers.py",
        '    "column": "column",',
        '    "column": "unresolved",',
        "a `table.column` citation fails as code the graph lacks, though the graph "
        "holds code and never schemas",
        ("test_answers.Citations.test_each_citation_is_classed_once",),
    ),
    Mutation(
        "answers: a summary note on the wrong quantity",
        "answers.py",
        '    "output_floor": " - a floor: transcript lines under-report output",',
        '    "cache_write": " - a floor: transcript lines under-report output",',
        "the summary calls an exact figure a floor and leaves output unqualified, "
        "which is how notes placed by line position fail when the fields are reordered",
        ("test_answers.CommandLine.test_each_summary_note_sits_on_its_own_quantity",),
    ),
    Mutation(
        "answers: --out written through an upward component",
        "answers.py",
        "            io.checked_write_target(arguments.out)",
        "            pass",
        "--out is a path from the command line; without the write policy's check a "
        "`..` in it writes wherever whatever built the arguments chose",
        ("test_answers.CommandLine.test_an_out_path_that_climbs_upward_is_refused",),
    ),
    Mutation(
        "answers: a malformed record file raises",
        "answers.py",
        "    except (KeyError, TypeError, AttributeError) as error:",
        "    except () as error:",
        "--compare over a file another tool wrote prints a traceback rather than "
        "saying which file is not a record file",
        ("test_answers.Comparing.test_a_malformed_record_file_is_refused_cleanly",),
    ),
    Mutation(
        "answers: a carried-over prompt answered twice",
        "answers.py",
        "            if not self.first_reading(record):\n                if prompt:",
        "            if False:\n                if prompt:",
        "a resumed session repeats the lines it carried over; read again, each earlier "
        "question is answered a second time at no cost and drags the medians down",
        ("test_answers.SplittingASession.test_a_resumed_session_counts_each_repeated_turn_once",),
    ),
    Mutation(
        "answers: an injected user line read as a prompt",
        "answers.py",
        '    if isinstance(origin, dict) and origin.get("kind") != "human":',
        "    if False:",
        "a background agent's completion notice arrives as a user line; read as a "
        "prompt it splits the answer that dispatched the agent in two",
        (
            "test_answers.SplittingASession.test_injected_user_lines_are_not_prompts",
            "test_answers.SubagentsAreFolded.test_a_linked_subagent_that_is_missing_is_counted",
            "test_answers.SubagentsAreFolded.test_a_subagent_s_usage_is_part_of_the_answer_that_dispatched_it",
        ),
    ),
    Mutation(
        "answers: the first text taken as the answer",
        "answers.py",
        '                answer.text = str(block.get("text") or "")',
        '                answer.text = answer.text or str(block.get("text") or "")',
        "the preamble before the first tool call is read as the answer, so the answer's "
        "citations and absence claims are never read",
        (
            "test_answers.SplittingASession.test_the_answer_text_is_the_last_text_before_the_next_prompt",
        ),
    ),
    Mutation(
        "answers: a subagent's text taken as the answer",
        "answers.py",
        '            if block.get("type") == "text" and not record.get("isSidechain"):',
        '            if block.get("type") == "text":',
        "the subagent's report to its parent is classed as the answer the person read",
        ("test_answers.SubagentsAreFolded.test_the_subagent_s_text_is_not_the_answer",),
    ),
    Mutation(
        "answers: a run's cost put on one of several answers",
        "answers.py",
        "        if len(owned) == 1 and len(dollars) == 1:",
        "        if owned and dollars:",
        "a headless result prices the whole run; attached to a session's first answer "
        "it charges one question for all of them",
        ("test_answers.HeadlessCost.test_dollars_come_only_from_a_matching_headless_result",),
    ),
    Mutation(
        "answers: a repeated question compared by its first asking",
        "answers.py",
        "                deltas.append(statistics.median(now) - statistics.median(was))",
        "                deltas.append(now[0] - was[0])",
        "a question asked three times is compared on whichever repetition came first, "
        "so one slow or fast run decides the change",
        ("test_answers.Comparing.test_a_repeated_question_is_compared_as_its_median",),
    ),
    Mutation(
        "answers: the question hashed untrimmed",
        "answers.py",
        '    return hashlib.sha256(question.strip().encode("utf-8")).hexdigest()',
        '    return hashlib.sha256(question.encode("utf-8")).hexdigest()',
        "a question asked with a trailing newline in one run fails to pair with itself "
        "in the other, and is listed as unpaired on both sides",
        ("test_answers.SplittingASession.test_the_question_hash_ignores_surrounding_whitespace",),
    ),
    Mutation(
        "a squash-merged branch file reads as unlanded",
        "export_git_history.py",
        '            if file["path"] in landed_paths:',
        "            if False:",
        "a squash merge lands a branch's work without its commits, so the branch's files "
        "read as proposals though their content is on the default branch",
        ("test_export_git_history.ReachTest.test_a_squash_merged_branch_file_is_marked_landed",),
    ),
    Mutation(
        "the empty blob matches unrelated work as landed",
        "export_git_history.py",
        "            if blob not in (_NULL_BLOB, EMPTY_BLOB):",
        "            if blob != _NULL_BLOB:",
        "every empty file shares one blob, so an empty file on any branch read as landed",
        (
            "test_export_git_history.ReachTest.test_an_empty_file_never_counts_as_landed",
            "test_export_git_history.ReachTest.test_raw_writes_drops_deletions_and_the_empty_blob",
        ),
    ),
    Mutation(
        "reach is read from the remote default instead of the configured branch",
        "export_git_history.py",
        '        for ref in (f"refs/remotes/origin/{default_branch}", f"refs/heads/{default_branch}"):',
        "        for ref in ():",
        "a repository whose origin/HEAD names a ticket branch classed that branch's "
        "proposals as landed and the real default's work as proposed",
        (
            "test_export_git_history.ExportRepositoryEndToEndTest.test_export_repository_produces_dataset_files",
            "test_export_git_history.ReachTest.test_a_branch_that_exists_only_locally_still_resolves",
            "test_export_git_history.ReachTest.test_each_commit_is_classed_against_the_configured_branch",
        ),
    ),
    Mutation(
        "a release line reached only by a tag reads as a proposal",
        "export_git_history.py",
        "    elif sha in reach.tagged:",
        "    elif False:",
        "shipped work on a release line that never merged back was published as a proposed change",
        (
            "test_export_git_history.ReachTest.test_a_release_line_reached_only_by_a_tag_is_tag",
            "test_export_git_history.ReachTest.test_with_nothing_to_resolve_untagged_commits_are_branch_and_tagged_ones_stay_tag",
        ),
    ),
    Mutation(
        "a branch commit is recorded without the branch it was found on",
        "export_git_history.py",
        '        record["ref"] = branch_name(source.strip())',
        "        pass",
        "an agent could not say which branch held a proposal",
        (
            "test_export_git_history.ReachTest.test_each_commit_is_classed_against_the_configured_branch",
            "test_export_git_history.ReachTest.test_squashed_branch_commits_are_branch_and_name_their_branch",
        ),
    ),
    Mutation(
        "a branch-only commit is linked as landed work",
        "build_intent_index.py",
        '            entry["tickets" if file_landed else "proposed"][ticket] += 1',
        '            entry["tickets"][ticket] += 1',
        "an operator found an unmerged branch's ticket stated as what a file implements",
        (
            "test_build_intent_index.ProposedLinksTest.test_a_branch_only_link_is_proposed_not_a_ticket",
            "test_build_intent_index.ProposedLinksTest.test_landed_wins_on_one_file_and_proposed_stays_on_another",
            "test_build_intent_index.ProposedLinksTest.test_proposed_values_are_counts_never_branch_names",
            "test_telemetry.IntentWiringTest.test_the_stage_records_how_much_work_is_only_proposed",
            "test_telemetry.IntentWiringTest.test_the_stage_records_the_inventory_it_indexed",
        ),
    ),
    Mutation(
        "a branch file whose content landed is published as proposed",
        "build_intent_index.py",
        '        file_landed = is_landed or changed.get("landed")',
        "        file_landed = is_landed",
        "squash-merged work, whose commits are on no default-branch path, read as a proposal",
        (
            "test_build_intent_index.ProposedLinksTest.test_a_branch_file_whose_content_landed_is_a_ticket",
            "test_build_intent_index.ProposedLinksTest.test_omit_keeps_a_branch_file_whose_content_landed",
            "test_build_intent_index.ProposedLinksTest.test_omit_leaves_the_unlanded_files_of_a_partly_landed_commit_out",
        ),
    ),
    Mutation(
        "a ticket landed through a branch file is still flagged as only proposed",
        "build_intent_index.py",
        '            descriptions[ticket]["landed"] = True',
        "            pass",
        "a squash-merged ticket's record said it had never landed",
        (
            "test_build_intent_index.ProposedLinksTest.test_a_branch_file_whose_content_landed_is_a_ticket",
            "test_build_intent_index.ProposedLinksTest.test_omit_keeps_a_branch_file_whose_content_landed",
        ),
    ),
    Mutation(
        "a ticket landed on a file is also listed as proposed there",
        "build_intent_index.py",
        '        proposed = {t: n for t, n in data["proposed"].items() if t not in data["tickets"]}',
        '        proposed = dict(data["proposed"])',
        "one ticket on one file read as both implemented and only proposed",
        (
            "test_build_intent_index.ProposedLinksTest.test_landed_wins_on_one_file_and_proposed_stays_on_another",
        ),
    ),
    Mutation(
        "history without reach reads as branch-only",
        "build_intent_index.py",
        '    return commit.get("reach", "default") != "branch"',
        '    return commit.get("reach", "branch") != "branch"',
        "a store that had not re-exported would have seen every link move to proposed",
        (
            "test_build_intent_index.ApplyCommitTest.test_body_contributes_at_most_three_tickets",
            "test_build_intent_index.ApplyCommitTest.test_body_fallback_description_is_truncated_at_a_word_boundary",
            "test_build_intent_index.ApplyCommitTest.test_body_ticket_is_indexed_when_the_subject_names_none",
            "test_build_intent_index.ApplyCommitTest.test_good_subject_description_is_not_replaced_by_the_body",
            "test_build_intent_index.ApplyCommitTest.test_junk_subject_falls_back_to_the_body_first_paragraph",
            "test_build_intent_index.ApplyCommitTest.test_subject_ticket_is_not_diluted_by_body_tickets",
            "test_build_intent_index.ApplyCommitTest.test_ticketed_commit_updates_files_and_descriptions",
            "test_build_intent_index.AutomatedAuthorTest.test_human_noreply_github_address_is_kept",
            "test_build_intent_index.AutomatedAuthorTest.test_human_prose_naming_a_bot_is_kept",
            "test_build_intent_index.BoilerplateTest.test_boilerplate_is_learned_per_repository",
            "test_build_intent_index.BoilerplateTest.test_counting_ignores_case_and_spacing_but_survivors_keep_their_text",
            "test_build_intent_index.BoilerplateTest.test_line_recurring_in_five_bodies_is_dropped_from_every_one",
            "test_build_intent_index.BoilerplateTest.test_line_recurring_in_only_four_bodies_is_kept",
            "test_build_intent_index.BoilerplateTest.test_line_under_two_percent_of_a_large_repository_is_kept",
            "test_build_intent_index.BranchTextTest.test_a_ticket_with_no_branch_commits_is_unchanged",
            "test_build_intent_index.ConfigurableAutomationTest.test_emptying_the_list_leaves_only_the_bot_convention",
            "test_build_intent_index.ConfigurableAutomationTest.test_narrowing_the_list_keeps_that_person",
            "test_build_intent_index.ConfigurableAutomationTest.test_the_list_is_read_when_called_not_when_imported",
            "test_build_intent_index.DocumentedRecordShapeTest.test_a_cap_the_stage_does_not_keep_is_reported",
            "test_build_intent_index.DocumentedRecordShapeTest.test_a_documented_view_the_record_does_not_carry_is_reported",
            "test_build_intent_index.DocumentedRecordShapeTest.test_a_singular_description_is_reported",
            "test_build_intent_index.DocumentedRecordShapeTest.test_a_wrong_presence_claim_is_reported",
            "test_build_intent_index.DocumentedRecordShapeTest.test_an_always_present_field_not_declared_as_one_is_reported",
            "test_build_intent_index.DocumentedRecordShapeTest.test_only_d_is_written_on_a_ticket_with_nothing_to_store",
            "test_build_intent_index.DocumentedRecordShapeTest.test_the_caps_are_the_hand_derived_ones_on_both_sides",
            "test_build_intent_index.DocumentedRecordShapeTest.test_the_forged_block_this_gate_is_driven_with_is_itself_clean",
            "test_build_intent_index.DocumentedRecordShapeTest.test_the_skill_agrees_with_the_record_it_documents",
            "test_build_intent_index.IndexRepositoryTest.test_end_to_end_over_ndjson",
            "test_build_intent_index.ProposedLinksTest.test_history_exported_before_reach_existed_is_all_landed",
            "test_build_intent_index.RedactedValuesTest.test_branch_only_text_is_redacted_like_landed_text",
            "test_build_intent_index.RedactedValuesTest.test_redaction_leaves_the_ticket_and_its_links_alone",
            "test_telemetry.IntentWiringTest.test_the_stage_records_how_much_work_is_only_proposed",
            "test_telemetry.IntentWiringTest.test_the_stage_records_the_inventory_it_indexed",
        ),
    ),
    Mutation(
        "proposed links are published although the store opted out",
        "build_intent_index.py",
        '    omit_proposed = not is_landed and config.PROPOSED_LINKS == "omit"',
        "    omit_proposed = False",
        "KSB_PROPOSED_LINKS=omit had no effect",
        (
            "test_build_intent_index.ProposedLinksTest.test_omit_keeps_a_branch_file_whose_content_landed",
            "test_build_intent_index.ProposedLinksTest.test_omit_leaves_out_branch_only_material_entirely",
            "test_build_intent_index.ProposedLinksTest.test_omit_leaves_the_unlanded_files_of_a_partly_landed_commit_out",
        ),
    ),
    Mutation(
        "a ticket that landed somewhere is flagged as only proposed",
        "build_intent_index.py",
        '    if not info["landed"]:',
        "    if True:",
        "a shipped ticket would read as never landed",
        (
            "test_build_intent_index.BranchTextTest.test_a_landed_tickets_branch_text_is_kept_apart",
            "test_build_intent_index.BranchTextTest.test_a_ticket_with_no_branch_commits_is_unchanged",
            "test_build_intent_index.ProposedLinksTest.test_a_branch_file_whose_content_landed_is_a_ticket",
            "test_build_intent_index.ProposedLinksTest.test_a_branch_only_link_is_proposed_not_a_ticket",
            "test_build_intent_index.ProposedLinksTest.test_history_exported_before_reach_existed_is_all_landed",
            "test_build_intent_index.ProposedLinksTest.test_landed_wins_on_one_file_and_proposed_stays_on_another",
            "test_build_intent_index.ProposedLinksTest.test_omit_keeps_a_branch_file_whose_content_landed",
            "test_build_intent_index.ProposedLinksTest.test_tag_reach_is_landed",
            "test_telemetry.IntentWiringTest.test_the_stage_records_how_much_work_is_only_proposed",
        ),
    ),
    Mutation(
        "the context stops explaining what a proposed link means",
        "build_knowledge_context.py",
        "A ticket listed under `proposed` in the intent index",
        "A ticket in the intent index",
        "agents read a branch-only link as an ordinary ticket link",
        (
            "test_build_knowledge_context.BuildOutputsTest.test_context_says_a_proposed_link_is_not_the_code",
        ),
    ),
    Mutation(
        "omit drops a branch commit whose files landed",
        "build_intent_index.py",
        '    if omit_proposed and not any(f.get("landed") for f in commit.get("files", [])):',
        "    if omit_proposed:",
        "KSB_PROPOSED_LINKS=omit discarded squash-merged work along with the proposals",
        (
            "test_build_intent_index.ProposedLinksTest.test_omit_keeps_a_branch_file_whose_content_landed",
            "test_build_intent_index.ProposedLinksTest.test_omit_leaves_the_unlanded_files_of_a_partly_landed_commit_out",
        ),
    ),
    Mutation(
        "omit still publishes the unlanded files of a branch commit",
        "build_intent_index.py",
        "        if omit_proposed and not file_landed:",
        "        if False:",
        "a store that opted out of proposals still got them from a commit that was partly landed",
        (
            "test_build_intent_index.ProposedLinksTest.test_omit_leaves_the_unlanded_files_of_a_partly_landed_commit_out",
        ),
    ),
    Mutation(
        "omit still records the branch of a landed branch commit",
        "build_intent_index.py",
        '        elif commit.get("ref") and not omit_proposed:',
        '        elif commit.get("ref"):',
        "a store that opted out of proposals still published branch names",
        (
            "test_build_intent_index.ProposedLinksTest.test_omit_keeps_a_branch_file_whose_content_landed",
        ),
    ),
    Mutation(
        "a branch name is stored unredacted",
        "build_intent_index.py",
        '            info["branches"].add(sensitive.redact(commit["ref"], counts))',
        '            info["branches"].add(commit["ref"])',
        "a branch named after a person was embedded in the explorer page",
        (
            "test_build_intent_index.ProposedLinksTest.test_a_branch_name_is_redacted_before_it_is_stored",
        ),
    ),
    Mutation(
        "configure leaves PROPOSED_LINKS as given",
        "config.py",
        '    "PROPOSED_LINKS": lambda given: str(given).strip().lower() or "write",',
        '    "PROPOSED_LINKS": lambda given: given,',
        "configure(PROPOSED_LINKS='OMIT') behaved as write",
        (
            "test_build_intent_index.ProposedLinksTest.test_the_setting_is_normalised_however_it_arrives",
        ),
    ),
    Mutation(
        "source url: a clone URL on another host is linked as github.com",
        "source_urls.py",
        r"(?:git@|ssh://git@|https://|http://|git://)github\.com[:/]",
        r"(?:git@|ssh://git@|https://|http://|git://)[\w.-]+[:/]",
        "a repository hosted on GitLab or a self-hosted server would get a github.com link "
        "naming a repository that does not exist there: a wrong link where the rule is to "
        "write none",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_a_node_that_cannot_be_linked_correctly_has_no_url",
            "test_build_source_urls.SourceUrlsStageTest.test_an_estate_configured_on_another_host_still_passes",
            "test_build_source_urls.SourceUrlsStageTest.test_the_report_counts_what_was_linked_and_why_the_rest_was_not",
            "test_explorer_source_links.SourceLinksOnThePage.test_a_row_that_gets_no_link_carries_no_line",
            "test_explorer_source_links.SourceLinksOnThePage.test_a_sync_after_source_urls_is_warned_about",
            "test_explorer_source_links.SourceLinksOnThePage.test_each_row_joined_to_its_base_is_the_graph_url_for_its_node",
            "test_explorer_source_links.SourceLinksOnThePage.test_the_page_carries_a_base_for_each_repository_its_rows_can_link",
            "test_source_urls.BuildingTheUrl.test_no_url_is_written_where_one_cannot_be_right",
            "test_source_urls.BuildingTheUrl.test_the_base_is_the_prefix_every_link_for_the_repository_starts_with",
            "test_source_urls.ParsingALink.test_a_link_names_the_configured_repository_whatever_its_local_name",
            "test_source_urls.ParsingTheCloneUrl.test_a_clone_url_on_another_host_names_nothing",
        ),
    ),
    Mutation(
        "source url: the link loses its line anchor",
        "source_urls.py",
        'base + escaped + line_anchor(source_location), "")',
        'base + escaped, "")',
        "every node would link the top of its file rather than the line `source_location` "
        "names, so a citation of one method in a long class sends the reader to find it",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_a_file_name_that_cannot_be_encoded_is_left_unlinked_and_counted",
            "test_build_source_urls.SourceUrlsStageTest.test_each_estate_node_links_to_its_line_at_the_build_commit",
            "test_build_source_urls.SourceUrlsStageTest.test_the_report_counts_what_was_linked_and_why_the_rest_was_not",
            "test_build_source_urls.SourceUrlsStageTest.test_the_single_root_route_links_the_same_file",
            "test_explorer_source_links.SourceLinksOnThePage.test_each_row_joined_to_its_base_is_the_graph_url_for_its_node",
            "test_source_urls.BuildingTheUrl.test_a_node_links_to_its_line_at_the_build_commit",
            "test_source_urls.ParsingALink.test_a_link_the_builder_writes_parses_back_to_its_parts",
        ),
    ),
    Mutation(
        "source url: the single-root corpus prefix is kept",
        "source_urls.py",
        "        path = path[len(own) :]\n",
        "",
        "a store built by the single-root route writes `repositories/<repo>/<path>`, and "
        "keeping the prefix links a path inside the repository that does not exist",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_the_single_root_route_links_the_same_file",
            "test_check_citations.VerdictsOnSourceLinks.test_a_link_to_a_real_file_at_the_build_commit_resolves",
            "test_explorer_source_links.SourceLinksOnThePage.test_each_row_joined_to_its_base_is_the_graph_url_for_its_node",
            "test_source_urls.BuildingTheUrl.test_both_path_forms_give_one_url",
            "test_source_urls.TheRepositoryRelativePath.test_both_build_routes_name_the_same_file",
        ),
    ),
    Mutation(
        "source url: a single-root path naming another repository is accepted",
        "source_urls.py",
        "        if not path.startswith(own):\n",
        "        if False:\n",
        "a single-root node whose `source_file` names a different repository's clone would "
        "be linked under its own repository's name: a link to a file that is not there",
        (
            "test_source_urls.TheRepositoryRelativePath.test_a_path_that_is_not_inside_its_own_repository_names_nothing",
        ),
    ),
    Mutation(
        "source url: one prefixed path makes a repository single-root",
        "source_urls.py",
        "        prefixed[repo] = prefixed.get(repo, True) and own\n",
        "        prefixed[repo] = prefixed.get(repo, False) or own\n",
        "deciding the route per path misreads a per-repository repository that really holds "
        "a `repositories/` directory: its own files are stripped to a different path and "
        "its files under another name are refused",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_a_real_repositories_directory_is_not_read_as_the_corpus_prefix",
            "test_build_source_urls.SourceUrlsStageTest.test_each_estate_node_links_to_its_line_at_the_build_commit",
            "test_build_source_urls.SourceUrlsStageTest.test_the_report_counts_what_was_linked_and_why_the_rest_was_not",
            "test_source_urls.TheBuildRoute.test_a_repository_is_single_root_only_when_every_file_carries_its_prefix",
        ),
    ),
    Mutation(
        "source url: an empty sha is written into the link",
        "source_urls.py",
        "            elif not isinstance(sha, str) or not sha:\n",
        "            elif not isinstance(sha, str):\n",
        "a repository with no recorded commit would get `.../blob//<path>`, which GitHub "
        "does not resolve - a link that reads as a citation and opens nothing",
        ("test_source_urls.BuildingTheUrl.test_no_url_is_written_where_one_cannot_be_right",),
    ),
    Mutation(
        "source url: a malformed sha is reported as a missing one",
        "source_urls.py",
        "                self._missing[repo] = MALFORMED_SHA\n",
        "                self._missing[repo] = NO_SHA\n",
        "an operator told `no provenance sha` beside a provenance file that has one looks "
        "for a missing sync rather than an abbreviated or uppercase value",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_a_run_whose_shas_are_all_unusable_keeps_every_link",
            "test_source_urls.BuildingTheUrl.test_no_url_is_written_where_one_cannot_be_right",
        ),
    ),
    Mutation(
        "source url: a path character that changes the URL is written raw",
        "source_urls.py",
        "            escaped = quote(inside, safe=_PATH_SAFE)\n",
        "            escaped = inside\n",
        "a `#` in a file name turns the rest of the path into a fragment and a space ends "
        "the URL in most renderers, so the link opens a different file or none",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_a_file_name_that_cannot_be_encoded_is_left_unlinked_and_counted",
            "test_explorer_source_links.SourceLinksOnThePage.test_a_file_name_that_cannot_be_encoded_gets_no_line_and_stops_nothing",
            "test_explorer_source_links.SourceLinksOnThePage.test_each_row_joined_to_its_base_is_the_graph_url_for_its_node",
            "test_source_urls.BuildingTheUrl.test_a_character_that_would_change_the_url_is_escaped",
            "test_source_urls.BuildingTheUrl.test_no_url_is_written_where_one_cannot_be_right",
            "test_source_urls.BuildingTheUrl.test_the_escaping_matches_the_links_stores_already_publish",
            "test_source_urls.ParsingALink.test_a_link_the_builder_writes_parses_back_to_its_parts",
        ),
    ),
    Mutation(
        "source url: the escaping widens past the links stores already publish",
        "source_urls.py",
        '_PATH_SAFE = "/"\n',
        '_PATH_SAFE = "/!$&\'()*+,;=:@-._~"\n',
        "RFC 3986 allows `(` raw, and a consuming store's committed links encode it: a "
        "file written both ways is one citation that compares unequal across the two writers",
        (
            "test_explorer_source_links.SourceLinksOnThePage.test_each_row_joined_to_its_base_is_the_graph_url_for_its_node",
            "test_source_urls.BuildingTheUrl.test_the_escaping_matches_the_links_stores_already_publish",
        ),
    ),
    Mutation(
        "source url: the local repository name stands in for the name on the host",
        "source_urls.py",
        'f"https://github.com/{org}/{name}/blob/{sha}/"',
        'f"https://github.com/{org}/{repo}/blob/{sha}/"',
        "a repository listed under a local name that differs from its name on GitHub would "
        "link to a 404",
        (
            "test_explorer_source_links.SourceLinksOnThePage.test_a_repository_name_cannot_close_the_config_block",
            "test_source_urls.BuildingTheUrl.test_the_base_is_the_prefix_every_link_for_the_repository_starts_with",
            "test_source_urls.BuildingTheUrl.test_the_org_and_name_come_from_the_clone_url_not_the_local_name",
            "test_source_urls.ParsingALink.test_a_link_the_builder_writes_parses_back_to_its_parts",
            "test_source_urls.ReadingTheStore.test_the_store_files_are_read_in_the_formats_their_stages_write",
        ),
    ),
    Mutation(
        "source url stage: a link from an earlier build survives",
        "build_source_urls.py",
        '        had = node.pop("url", None) is not None\n',
        '        had = node.get("url") is not None\n',
        "a repository that left the configuration or moved host keeps the link an earlier "
        "build wrote, citing a commit this build did not read",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_a_node_that_cannot_be_linked_correctly_has_no_url",
        ),
    ),
    Mutation(
        "source url stage: removed links are not counted",
        "build_source_urls.py",
        "            outcome.removed += had\n",
        "",
        "links an earlier build published disappear from the graph with nothing on the "
        "console saying so - the skip counts name why a node is unlinked, not that it "
        "lost a link",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_the_report_counts_what_was_linked_and_why_the_rest_was_not",
        ),
    ),
    Mutation(
        "source url stage: missing provenance strips every link",
        "build_source_urls.py",
        "    if not provenance.read():\n",
        "    if False:\n",
        "a machine without `knowledge/provenance.json` would remove every link the store "
        "published, from both graph files, and exit 0",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_missing_provenance_is_refused_and_nothing_is_written",
        ),
    ),
    Mutation(
        "source url stage: a run that links nothing removes every link",
        "build_source_urls.py",
        "    if outcome.carried and not outcome.linked:\n",
        "    if False:\n",
        "a store whose provenance records no usable sha, or whose repository list is "
        "missing, would lose every link it published and exit 0",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_a_run_that_would_remove_every_link_is_refused",
            "test_build_source_urls.SourceUrlsStageTest.test_a_run_whose_shas_are_all_unusable_keeps_every_link",
        ),
    ),
    Mutation(
        "source url stage: a missing repository list links nothing and exits 0",
        "build_source_urls.py",
        "    if outcome.estate and outcome.skipped[NOT_CONFIGURED] == outcome.estate:\n",
        "    if False:\n",
        "a store run from the wrong root, or without `config/repositories.txt`, gets a "
        "graph with no links and a clean exit, because the graph never carried a link "
        "for the other refusal to protect",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_a_missing_repository_list_with_estate_nodes_is_refused",
            "test_build_source_urls.SourceUrlsStageTest.test_a_repository_list_naming_none_of_the_graphs_repositories_is_refused",
        ),
    ),
    Mutation(
        "source url stage: a graph with no estate node is refused for a missing list",
        "build_source_urls.py",
        "    if outcome.estate and outcome.skipped[NOT_CONFIGURED] == outcome.estate:\n",
        "    if outcome.skipped[NOT_CONFIGURED] == outcome.estate:\n",
        "a graph holding nothing to link - no node with a repository and a file - "
        "fails the stage when the repository list is absent, though it ran clean before",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_a_missing_repository_list_with_no_estate_nodes_passes",
        ),
    ),
    Mutation(
        "source url stage: an estate on another host is refused on every run",
        "build_source_urls.py",
        "    if outcome.estate and outcome.skipped[NOT_CONFIGURED] == outcome.estate:\n",
        "    if outcome.estate and not outcome.linked:\n",
        "a store whose repositories are configured on a host other than github.com links "
        "nothing by design, and would be refused on every build",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_an_estate_configured_on_another_host_still_passes",
        ),
    ),
    Mutation(
        "citation gate: --allow-none is ignored",
        "check_citations.py",
        "        return 0 if arguments.allow_none else 2\n",
        "        return 2\n",
        "a store whose gate chain runs `check-citations` before it has published any "
        "answers reads as a failed chain, with no way to say that is expected",
        (
            "test_check_citations.TheStage.test_allow_none_lets_a_store_with_no_answers_pass_on_purpose",
        ),
    ),
    Mutation(
        "citation gate: no answers passes by default",
        "check_citations.py",
        "        return 0 if arguments.allow_none else 2\n",
        "        return 0\n",
        "a run that read no answer reports a pass, so a gate pointed at the wrong "
        "directory certifies answers it never looked at",
        ("test_check_citations.TheStage.test_no_answers_asserts_nothing_and_does_not_pass",),
    ),
    Mutation(
        "citation link: the sha is not checked",
        "check_citations.py",
        "    built = {r for r in repos if urls.build_sha(r) == link.sha}\n",
        "    built = set(repos)\n",
        "a link pinned to a commit the store never read, or to a branch, passes the "
        "publish gate as a citation of this build and drifts off the line it cites",
        (
            "test_check_citations.AskingTheClone.test_git_is_never_handed_a_ref_the_link_supplied",
            "test_check_citations.AskingTheClone.test_without_a_clone_a_wrong_sha_still_fails",
            "test_check_citations.LinksToFilesNoNodeNames.test_without_a_clone_a_wrong_sha_still_fails",
            "test_check_citations.TheStageOnSourceLinks.test_it_fails_on_a_bad_link_and_says_which_of_the_two_it_is",
            "test_check_citations.VerdictsOnSourceLinks.test_a_link_at_another_commit_fails_and_says_it_is_the_sha",
            "test_check_citations.VerdictsOnSourceLinks.test_a_link_wrong_in_both_ways_names_both",
            "test_check_citations.VerdictsOnSourceLinks.test_an_unpinned_link_reports_the_sha_alone",
        ),
    ),
    Mutation(
        "citation link: a path is resolved across the estate",
        "check_citations.py",
        "    holding = {r for r in repos if link.path in vocabulary.repository_files(r)}\n",
        "    holding = {\n"
        "        r\n"
        "        for r in repos\n"
        "        if any(link.path in vocabulary.repository_files(o) for o in vocabulary.sources)\n"
        "    }\n",
        "a link naming a file that only another repository holds passes, though it opens "
        "a 404 in the repository it names",
        (
            "test_check_citations.VerdictsOnSourceLinks.test_a_link_to_a_file_its_repository_does_not_hold_fails_and_says_so",
        ),
    ),
    Mutation(
        "citation link: only backticked spans are read",
        "check_citations.py",
        "        if parse_blob_url(link) is not None:\n",
        "        if False:\n",
        "answers cite a link as a markdown link or bare, so a made-up one there is never "
        "looked at and the gate's silence is about text it did not read",
        (
            "test_check_citations.AskingTheClone.test_a_link_no_clone_can_decide_is_listed_as_undecided",
            "test_check_citations.AskingTheClone.test_the_stage_reads_the_clones_under_the_store",
            "test_check_citations.LinksToFilesNoNodeNames.test_a_link_no_clone_can_decide_is_listed_as_undecided",
            "test_check_citations.LinksToFilesNoNodeNames.test_the_stage_reads_the_clones_under_the_store",
            "test_check_citations.TheLinksOfAnAnswer.test_a_bare_link_ends_where_github_ends_it",
            "test_check_citations.TheLinksOfAnAnswer.test_a_link_is_a_citation_however_it_is_written",
            "test_check_citations.TheStageOnSourceLinks.test_a_clean_answer_citing_links_passes",
            "test_check_citations.TheStageOnSourceLinks.test_it_fails_on_a_bad_link_and_says_which_of_the_two_it_is",
            "test_check_citations.TheStageOnSourceLinks.test_links_with_no_repository_list_stop_the_gate",
        ),
    ),
    Mutation(
        "citation link: the cited path is compared still percent-encoded",
        "source_urls.py",
        '    path = unquote(match.group("path"))\n',
        '    path = match.group("path")\n',
        "a real link to a file whose name holds a space, `#` or bracket reads as a file the "
        "store does not hold, failing an honest answer",
        (
            "test_check_citations.AskingTheClone.test_a_nul_in_a_link_is_not_a_link",
            "test_source_urls.ParsingALink.test_a_link_the_builder_writes_parses_back_to_its_parts",
            "test_source_urls.ParsingALink.test_only_a_github_blob_link_is_parsed",
        ),
    ),
    Mutation(
        "citation link: an unpinned link is not parsed",
        "source_urls.py",
        r"/blob/(?P<sha>[^/?#\s]+)",
        r"/blob/(?P<sha>[0-9a-f]{40})",
        "a link to `blob/main/...` or an abbreviated sha falls back to being skipped, which "
        "is the hole the gate closes: it passes unchecked",
        (
            "test_check_citations.AskingTheClone.test_git_is_never_handed_a_ref_the_link_supplied",
            "test_check_citations.TheStageOnSourceLinks.test_it_fails_on_a_bad_link_and_says_which_of_the_two_it_is",
            "test_check_citations.VerdictsOnSourceLinks.test_a_link_at_another_commit_fails_and_says_it_is_the_sha",
            "test_check_citations.VerdictsOnSourceLinks.test_an_unpinned_link_reports_the_sha_alone",
            "test_source_urls.ParsingALink.test_a_link_pinned_to_a_branch_still_parses",
        ),
    ),
    Mutation(
        "citation link: the single-root prefix is kept on the graph's side",
        "check_citations.py",
        "                inside = (repo_relative(name, p, name in single_root) for p in paths)\n",
        "                inside = (repo_relative(name, p, False) for p in paths)\n",
        "on a store built by the single-root route every correct link reads as an unknown "
        "file, so the gate is red for answers that are right",
        (
            "test_check_citations.VerdictsOnSourceLinks.test_a_link_to_a_real_file_at_the_build_commit_resolves",
        ),
    ),
    Mutation(
        "citation link: a link outside the estate fails the gate",
        "check_citations.py",
        '    if urls is None or not repos:\n        return Verdict("unchecked", reason=OUTSIDE_STORE)\n',
        '    if urls is None or not repos:\n        return Verdict("unresolved", reason=OUTSIDE_STORE)\n',
        "a store whose answers cite a third-party library's source could never go green, "
        "so it would stop running the gate",
        (
            "test_check_citations.TheStageOnSourceLinks.test_it_fails_on_a_bad_link_and_says_which_of_the_two_it_is",
            "test_check_citations.VerdictsOnSourceLinks.test_a_link_to_a_repository_the_store_does_not_build_is_neither_passed_nor_failed",
            "test_check_citations.VerdictsOnSourceLinks.test_without_the_store_files_a_link_is_unchecked_not_passed",
        ),
    ),
    Mutation(
        "citation link: an unreadable repository list is read as an empty estate",
        "check_citations.py",
        '        print(f"Cannot read {config.REPOSITORIES_CONFIG}: {error}", file=sys.stderr)\n'
        "        return 2\n",
        '        print(f"Cannot read {config.REPOSITORIES_CONFIG}: {error}", file=sys.stderr)\n'
        "        urls = None\n",
        "every source link becomes unchecked and the gate passes over all of them, exit 0",
        (
            "test_check_citations.TheStageOnSourceLinks.test_an_unreadable_repository_list_stops_the_gate",
        ),
    ),
    Mutation(
        "citation link: the report does not say which claim is wrong",
        "check_citations.py",
        "        if reason:\n",
        "        if False:\n",
        "an operator told only that a link does not resolve cannot tell whether to fix the "
        "path or re-pin the commit",
        (
            "test_check_citations.AskingTheClone.test_the_stage_reads_the_clones_under_the_store",
            "test_check_citations.LinksToFilesNoNodeNames.test_the_stage_reads_the_clones_under_the_store",
            "test_check_citations.TheStageOnSourceLinks.test_it_fails_on_a_bad_link_and_says_which_of_the_two_it_is",
        ),
    ),
    Mutation(
        "citation link: a file no node names is not asked of the clone",
        "check_citations.py",
        "        if state == HELD:\n",
        "        if False:\n",
        "every link to a corpus-search match fails as an unknown file, though the "
        "repository holds it - the links the question skill tells an agent to build",
        (
            "test_check_citations.AskingTheClone.test_a_file_the_clone_holds_at_the_build_commit_resolves",
            "test_check_citations.AskingTheClone.test_the_stage_reads_the_clones_under_the_store",
            "test_check_citations.LinksToFilesNoNodeNames.test_a_file_the_clone_holds_at_the_build_commit_resolves",
            "test_check_citations.LinksToFilesNoNodeNames.test_the_stage_reads_the_clones_under_the_store",
        ),
    ),
    Mutation(
        "citation link: the clone is asked at its head, not the build commit",
        "check_citations.py",
        "vocabulary.clone_holds(r, urls.build_sha(r), link.path)",
        'vocabulary.clone_holds(r, "HEAD", link.path)',
        "a file added after the build passes as though the store had read it, and a "
        "file deleted since fails though the cited commit holds it",
        (
            "test_check_citations.AskingTheClone.test_a_file_the_clone_does_not_hold_at_the_build_commit_fails",
            "test_check_citations.AskingTheClone.test_a_file_the_clone_holds_at_the_build_commit_resolves",
            "test_check_citations.AskingTheClone.test_a_path_climbing_with_dot_dot_is_never_asked",
            "test_check_citations.AskingTheClone.test_the_question_carries_end_of_options_where_git_honours_it",
            "test_check_citations.AskingTheClone.test_the_stage_reads_the_clones_under_the_store",
            "test_check_citations.LinksToFilesNoNodeNames.test_a_file_the_clone_does_not_hold_at_the_build_commit_fails",
            "test_check_citations.LinksToFilesNoNodeNames.test_a_file_the_clone_holds_at_the_build_commit_resolves",
            "test_check_citations.LinksToFilesNoNodeNames.test_the_stage_reads_the_clones_under_the_store",
            "test_check_citations.TheStageOnSourceLinks.test_it_fails_on_a_bad_link_and_says_which_of_the_two_it_is",
            "test_check_citations.VerdictsOnSourceLinks.test_a_link_to_a_file_its_repository_does_not_hold_fails_and_says_so",
            "test_check_citations.VerdictsOnSourceLinks.test_a_link_wrong_in_both_ways_names_both",
        ),
    ),
    Mutation(
        "citation link: a missing path in the clone passes",
        "check_citations.py",
        "        except subprocess.CalledProcessError:\n            return False\n",
        "        except subprocess.CalledProcessError:\n            return True\n",
        "with the clone present, any invented path passes the gate: the clone's no is "
        "read as a yes",
        (
            "test_check_citations.AskingTheClone.test_a_file_the_clone_does_not_hold_at_the_build_commit_fails",
            "test_check_citations.AskingTheClone.test_the_stage_reads_the_clones_under_the_store",
            "test_check_citations.LinksToFilesNoNodeNames.test_a_file_the_clone_does_not_hold_at_the_build_commit_fails",
            "test_check_citations.LinksToFilesNoNodeNames.test_the_stage_reads_the_clones_under_the_store",
            "test_check_citations.TheStageOnSourceLinks.test_it_fails_on_a_bad_link_and_says_which_of_the_two_it_is",
            "test_check_citations.VerdictsOnSourceLinks.test_a_link_to_a_file_its_repository_does_not_hold_fails_and_says_so",
            "test_check_citations.VerdictsOnSourceLinks.test_a_link_wrong_in_both_ways_names_both",
        ),
    ),
    Mutation(
        "citation link: no clone fails the link",
        "check_citations.py",
        "    if not holding and undecided and built:\n"
        '        return Verdict("unchecked", reason=CLONE_UNASKED if UNASKED in undecided else NO_CLONE)\n',
        "",
        "a machine without the corpus - every one but the build machine - fails links "
        "nothing it holds can decide, so the gate is red for honest answers",
        (
            "test_check_citations.AskingTheClone.test_a_git_that_cannot_be_asked_leaves_the_link_unchecked",
            "test_check_citations.AskingTheClone.test_a_link_no_clone_can_decide_is_listed_as_undecided",
            "test_check_citations.AskingTheClone.test_without_a_clone_a_file_no_node_names_is_unchecked_not_failed",
            "test_check_citations.LinksToFilesNoNodeNames.test_a_link_no_clone_can_decide_is_listed_as_undecided",
            "test_check_citations.LinksToFilesNoNodeNames.test_without_a_clone_a_file_no_node_names_is_unchecked_not_failed",
        ),
    ),
    Mutation(
        "citation link: a missing clone hides a wrong sha",
        "check_citations.py",
        "    if not holding and undecided and built:\n",
        "    if not holding and undecided:\n",
        "a link pinned to the wrong commit passes as unchecked on any machine without a "
        "clone, though provenance alone shows it is wrong",
        (
            "test_check_citations.AskingTheClone.test_git_is_never_handed_a_ref_the_link_supplied",
            "test_check_citations.AskingTheClone.test_without_a_clone_a_wrong_sha_still_fails",
            "test_check_citations.LinksToFilesNoNodeNames.test_without_a_clone_a_wrong_sha_still_fails",
        ),
    ),
    Mutation(
        "citation link: the stage does not hand over the clones",
        "check_citations.py",
        "load_vocabulary(graph, urls, config.REPOSITORIES_DIR)",
        "load_vocabulary(graph, urls)",
        "the stage lists every corpus-search link as unchecked, so an invented one "
        "passes on the machine that holds the clone that could refute it",
        (
            "test_check_citations.AskingTheClone.test_the_stage_reads_the_clones_under_the_store",
            "test_check_citations.LinksToFilesNoNodeNames.test_the_stage_reads_the_clones_under_the_store",
            "test_check_citations.TheStageOnSourceLinks.test_it_fails_on_a_bad_link_and_says_which_of_the_two_it_is",
        ),
    ),
    Mutation(
        "citation link: answers hands a link it cannot see to resolve",
        "answers.py",
        "    if _URL.fullmatch(text) or parse_blob_url(text) is not None:\n",
        "    if _URL.fullmatch(text):\n",
        "a hand-written link with a raw `(` in its path slips past the URL pattern into "
        "`resolve`, which returns `unchecked`, and the class lookup raises: one link ends "
        "the whole measurement",
        (
            "test_answers.ClassingALinkCheckCitationsCanParse.test_a_link_with_a_raw_bracket_is_a_pinned_url_not_a_crash",
        ),
    ),
    Mutation(
        "citation link: an undecided link is called outside the estate",
        "check_citations.py",
        "if UNASKED in undecided else NO_CLONE)",
        "if UNASKED in undecided else OUTSIDE_STORE)",
        "a corpus-search link with no clone here is listed as a repository the store "
        "does not build, so a reader checks the repository list rather than the file",
        (
            "test_check_citations.AskingTheClone.test_a_link_no_clone_can_decide_is_listed_as_undecided",
            "test_check_citations.LinksToFilesNoNodeNames.test_a_link_no_clone_can_decide_is_listed_as_undecided",
        ),
    ),
    Mutation(
        "citation link: links with no repository list pass as outside the estate",
        "check_citations.py",
        "    if not config.REPOSITORIES_CONFIG.is_file() and cites_links(files):\n",
        "    if False:\n",
        "run from outside the store root, every cited link is listed as a repository the "
        "store does not build and the gate exits 0 - silence about a file it never read",
        (
            "test_check_citations.TheStageOnSourceLinks.test_links_with_no_repository_list_stop_the_gate",
        ),
    ),
    Mutation(
        "citation link: emphasis stays on a bare link",
        "check_citations.py",
        '_LINK_TRAILING = ".,;:!?*_~"',
        '_LINK_TRAILING = ".,;:!?"',
        "a bolded link `**<link>**` cites `a.py**`, which no repository holds, and the "
        "gate fails an honest answer",
        ("test_check_citations.TheLinksOfAnAnswer.test_a_bare_link_ends_where_github_ends_it",),
    ),
    Mutation(
        "citation link: a curly apostrophe stays on a bare link",
        "check_citations.py",
        "`'\\\"\\u2019]+)",
        "`'\\\"]+)",
        "`<link>’s` cites a file ending `’s`, which no repository holds, and the gate "
        "fails an honest answer",
        ("test_check_citations.TheLinksOfAnAnswer.test_a_bare_link_ends_where_github_ends_it",),
    ),
    Mutation(
        "citation link: a control character in a link reaches git",
        "source_urls.py",
        "    if _CONTROL.search(path):\n        return None\n",
        "",
        "`%00` decodes to a NUL that subprocess refuses, and the stage dies with a "
        "traceback and no report",
        ("test_source_urls.ParsingALink.test_only_a_github_blob_link_is_parsed",),
    ),
    Mutation(
        "citation link: a git that cannot be asked is a traceback",
        "check_citations.py",
        "        except (OSError, ValueError):\n            return None\n",
        "",
        "a machine with no git binary, or an argument git cannot be handed, ends the gate "
        "with a traceback instead of listing the link as undecided",
        (
            "test_check_citations.AskingTheClone.test_a_git_that_cannot_be_asked_leaves_the_link_unchecked",
        ),
    ),
    Mutation(
        "citation link: git is handed the link's own ref",
        "check_citations.py",
        "vocabulary.clone_holds(r, urls.build_sha(r), link.path)",
        "vocabulary.clone_holds(r, urls.build_sha(r) or link.sha, link.path)",
        "with no build sha recorded, text from a published answer reaches a subprocess's "
        "argv, for a verdict the sha already decides",
        ("test_check_citations.AskingTheClone.test_git_is_never_handed_a_ref_the_link_supplied",),
    ),
    Mutation(
        "citation link: the full-sha guard on the clone question is dropped",
        "check_citations.py",
        "        if not is_full_sha(sha):\n            return UNASKED\n",
        "",
        "a ref that is not a commit id - `--help`, a branch - is put in front of git as a "
        "revision, where only a hex sha guarantees it cannot read as an option",
        ("test_check_citations.AskingTheClone.test_git_is_never_handed_a_ref_the_link_supplied",),
    ),
    Mutation(
        "citation link: the clone question goes without end-of-options",
        "check_citations.py",
        '            self._flags[repo] = ["--end-of-options"] if probe else []\n',
        "            self._flags[repo] = []\n",
        "the revision is handed to git with nothing marking the end of its options, on a "
        "git that would honour the marker",
        (
            "test_check_citations.AskingTheClone.test_the_question_carries_end_of_options_where_git_honours_it",
        ),
    ),
    Mutation(
        "citation link: a dot-dot path is asked of git",
        "check_citations.py",
        '        if any(segment in ("", ".", "..") for segment in path.split("/")):\n'
        "            return ABSENT\n",
        "",
        "git answers a path climbing out of the repository with a `fatal:` line, and a "
        "path that names no file is put to a subprocess at all",
        ("test_check_citations.AskingTheClone.test_a_path_climbing_with_dot_dot_is_never_asked",),
    ),
    Mutation(
        "citation link: quiet git still prints its refusals",
        "sync_repositories.py",
        "        stderr=subprocess.DEVNULL if quiet else None,\n",
        "        stderr=None,\n",
        "git's `fatal:` line for every refused question lands in the middle of the gate's report",
        (
            "test_check_citations.TheQuietRunner.test_a_quiet_git_call_prints_nothing_when_git_refuses",
        ),
    ),
    Mutation(
        "citation link: an unpinned link also reports an unknown file",
        "check_citations.py",
        "    if not holding and not undecided and is_full_sha(link.sha):\n",
        "    if not holding and not undecided:\n",
        "a slashed branch shifts where the path starts, so `unknown file` sends the reader "
        "to fix a path that was never the problem",
        ("test_check_citations.VerdictsOnSourceLinks.test_an_unpinned_link_reports_the_sha_alone",),
    ),
    Mutation(
        "citation link: answers raises on a kind it does not name",
        "answers.py",
        '    return _VERDICT_CLASS.get(resolve(text, vocabulary).kind, "skipped")\n',
        "    return _VERDICT_CLASS[resolve(text, vocabulary).kind]\n",
        "the next verdict kind `check_citations` adds ends the whole measurement with a KeyError",
        ("test_answers.EveryVerdictHasAClass.test_an_unknown_kind_is_skipped_rather_than_raising",),
    ),
    Mutation(
        "citation link: answers forgets the unchecked kind",
        "answers.py",
        '    "unchecked": "skipped",\n',
        "",
        "`unchecked` falls to the default silently, so the map no longer names every kind "
        "and the next one added is not noticed either",
        ("test_answers.EveryVerdictHasAClass.test_every_kind_check_citations_returns_is_mapped",),
    ),
    Mutation(
        "source url stage: only the plain graph is rewritten",
        "build_source_urls.py",
        '    with io.gzip_text(config.GRAPH_PATH.with_suffix(".json.gz")) as out:\n'
        "        out.write(serialised)\n",
        "",
        "every store gitignores `graph.json` and commits `graph.json.gz`, so links written "
        "only into the plain file never reach a reader and the stage still reports them",
        (
            "test_archive_writer_ordering.ArchiveWriterOrderingGuidanceTest.test_the_documented_stage_set_is_the_set_that_writes_the_archive",
            "test_build_source_urls.SourceUrlsStageTest.test_a_rerun_is_byte_identical",
            "test_build_source_urls.SourceUrlsStageTest.test_both_graph_files_are_written_and_agree",
        ),
    ),
    Mutation(
        "source url stage: the stale-graph guard is skipped",
        "build_source_urls.py",
        "    refusal = graph_files.stale_refusal(config.GRAPH_PATH)\n",
        '    refusal = ""\n',
        "the fourth stage that rewrites both graph files would overwrite the committed "
        "archive from a stale `graph.json`, losing the newer clustering (#198)",
        ("test_stale_graph_refusal.StaleRefusalTest.test_source_urls_refuses_and_writes_nothing",),
    ),
    Mutation(
        "source url stage: an unreadable repository list reads as an empty estate",
        "build_source_urls.py",
        '        print(f"Cannot read {config.REPOSITORIES_CONFIG}: {error}", file=sys.stderr)\n'
        "        return 1\n",
        "        urls = SourceUrls({}, {})\n",
        "one malformed line in `repositories.txt` would be read as an estate of nothing",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_a_malformed_repository_list_is_refused_and_nothing_is_written",
        ),
    ),
    Mutation(
        "source url stage: skipped nodes are not reported",
        "build_source_urls.py",
        "            outcome.skipped[found.reason] += 1\n",
        "            pass\n",
        "a store with no provenance would print `0 of N linked` with no reason beside it, "
        "which is the silent-zero shape this stage's report exists to prevent",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_a_file_name_that_cannot_be_encoded_is_left_unlinked_and_counted",
            "test_build_source_urls.SourceUrlsStageTest.test_a_missing_repository_list_with_estate_nodes_is_refused",
            "test_build_source_urls.SourceUrlsStageTest.test_a_repository_list_naming_none_of_the_graphs_repositories_is_refused",
            "test_build_source_urls.SourceUrlsStageTest.test_a_run_that_would_remove_every_link_is_refused",
            "test_build_source_urls.SourceUrlsStageTest.test_a_run_whose_shas_are_all_unusable_keeps_every_link",
            "test_build_source_urls.SourceUrlsStageTest.test_an_estate_configured_on_another_host_still_passes",
            "test_build_source_urls.SourceUrlsStageTest.test_the_report_counts_what_was_linked_and_why_the_rest_was_not",
        ),
    ),
    Mutation(
        "source url stage is not registered",
        "cli.py",
        '    "source-urls": (\n'
        '        "build_source_urls",\n'
        '        "give each graph node a `url` to its file at the commit the store was built from",\n'
        "    ),\n",
        "",
        "the documented `knowledgestore source-urls` would answer `unknown stage`, and no "
        "store could produce the links its skill tells agents to cite",
        (
            "test_archive_writer_ordering.ArchiveWriterOrderingGuidanceTest.test_the_documented_stage_set_is_the_set_that_writes_the_archive",
            "test_documented_stages.DocumentedStagesExist.test_every_documented_stage_is_a_real_stage",
            "test_repo_list_and_sync.CliTest.test_stages_are_listed_in_pipeline_order",
        ),
    ),
    Mutation(
        "source url: the context file stops forbidding corpus citations",
        "build_knowledge_context.py",
        "off `#L<line>` when no line is known. Never cite `repositories/<repo>/...`:\n"
        "no reader can open that path.\n",
        "off `#L<line>` when no line is known.\n",
        "every agent answering from a store reads the context file; without the rule it "
        "cites the uncommitted corpus path a reader cannot open",
        (
            "test_build_knowledge_context.BuildOutputsTest.test_context_tells_agents_to_cite_a_link_and_never_the_corpus",
        ),
    ),
    Mutation(
        "source url: the question skill goes back to citing source_file",
        "skills/knowledge-store/SKILL.md",
        "- In an answer or an exported finding, cite a node's `url` for specific claims.",
        "- Cite `source_file` / `source_location` for specific claims.",
        "the skill is where an answering agent reads its rules (CLAUDE.md 3); the old line "
        "is the one that produced `repositories/...` citations",
        (
            "test_source_urls.TheSkillCarriesTheCitationRule.test_the_question_skill_says_cite_the_url_and_never_the_corpus",
        ),
    ),
    Mutation(
        "a node's ticket list takes in its file's proposed tickets",
        "build_explorer.py",
        '    return list(entry["tickets"].keys())[:MAX_TICKETS] if entry else []',
        '    return list({**entry["tickets"], **entry.get("proposed", {})})[:MAX_TICKETS] if entry else []',
        "#425: the explorer's per-node list is what the page calls the code's history, "
        "and the graph is of the default branch - so a ticket found only on a branch "
        "would be published as what changed this code, the reading the label exists to stop",
        ("test_build_explorer.NodeTicketsTest.test_proposed_tickets_stay_off_a_code_node",),
    ),
    Mutation(
        "ticket refresh: a kept refresh is not re-dated",
        "fetch_tickets.py",
        "                cache[key] = dict(sorted({**previous, CHECKED: today}.items()))\n",
        "                pass\n",
        "review of #421: a refresh the tracker did not answer stays due and oldest, heads the queue on every run, and once the unreadable records fill the cap no other record is ever refreshed",
        (
            "test_fetch_tickets.RefreshKeepsTest.test_a_kept_refresh_waits_its_interval_and_does_not_starve_the_queue",
            "test_fetch_tickets.RefreshTest.test_a_refresh_the_tracker_does_not_answer_keeps_the_earlier_record",
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_refresh_counts_its_follow_ups_in_its_cost",
        ),
    ),
    Mutation(
        "ticket refresh: a refresh replaces the record wholesale",
        "fetch_tickets.py",
        "        cache[key] = merged(previous, record)\n",
        "        cache[key] = record\n",
        "a refresh with descriptions off erases every stored description, and a transport returning a recent page of comments cuts a thread another fetch stored whole",
        (
            "test_fetch_tickets.RefreshKeepsTest.test_a_refresh_never_leaves_a_record_holding_less",
            "test_fetch_tickets.ThreadTruncationTest.test_a_kept_longer_thread_is_marked_when_the_tracker_reports_more",
        ),
    ),
    Mutation(
        "ticket refresh: refreshes are counted as new fetches",
        "fetch_tickets.py",
        "            if _settle(cache, key, record, report, today) == STORED:\n                report.fetched += 1\n",
        "            _settle(cache, key, record, report, today)\n            report.fetched += 1\n",
        "the run's first line then mixes new tickets with refreshed ones, and neither number says what it claims",
        (
            "test_fetch_tickets.RefreshTest.test_an_open_ticket_is_fetched_again_once_its_record_is_a_week_old",
        ),
    ),
    Mutation(
        "twg transport: follow-up calls are not counted",
        "fetch_tickets.py",
        "        return Response(200, body, calls=session.calls, unanswered=tuple(unanswered))\n",
        "        return Response(200, body, unanswered=tuple(unanswered))\n",
        "review of #420: a page of a hundred unreturned keys is a hundred and one calls and was reported as one, so the run under-reported its own cost",
        (
            "test_fetch_tickets.TwgBoundaryTest.test_a_refused_key_costs_only_itself",
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_follow_up_is_counted_and_paced_like_a_request",
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_refresh_counts_its_follow_ups_in_its_cost",
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_follow_up_that_fails_as_a_process_costs_only_its_key",
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_thread_completion_that_fails_as_a_process_leaves_the_thread_marked",
            "test_fetch_tickets.TwgThreadTest.test_a_partial_thread_is_completed_by_one_counted_follow_up",
        ),
    ),
    Mutation(
        "twg transport: follow-up calls are not paced",
        "twg_transport.py",
        "        if self.calls:\n            self.sleep(self.delay)\n",
        "",
        "follow-ups fired back to back are the burst the page delay exists to prevent",
        (
            "test_fetch_tickets.TwgFollowUpCostTest.test_a_follow_up_is_counted_and_paced_like_a_request",
        ),
    ),
    Mutation(
        "twg transport: follow-up calls are unbounded",
        "twg_transport.py",
        "        if self.followups <= 0:\n            return UNANSWERED\n",
        "",
        "without a per-run budget one page of unreturned keys becomes a call per key",
        ("test_fetch_tickets.TwgFollowUpCostTest.test_follow_ups_stop_at_the_per_run_budget",),
    ),
    Mutation(
        "twg transport: any get error is read as not-found",
        "twg_transport.py",
        '        if isinstance(error, dict) and error.get("statusCode") == NOT_FOUND:\n',
        "        if isinstance(error, dict):\n",
        "review of #420: a throttled, signed-out or failed get prints the same error envelope as a missing ticket, and cached as absent - final - it is lost for good",
        ("test_fetch_tickets.TwgMovedTicketTest.test_only_a_not_found_from_get_is_absence",),
    ),
    Mutation(
        "twg transport: an inline node renders its attrs.text",
        "twg_transport.py",
        '    return render(node, attrs) if render else ""\n',
        '    return render(node, attrs) if render else str(attrs.get("text", ""))\n',
        "review of #420, privacy: the helpful fallback for a status or a date publishes a name from any node carrying one in attrs.text",
        ("test_fetch_tickets.TwgPrivacyTest.test_no_node_s_attrs_text_reaches_the_flattened_text",),
    ),
    Mutation(
        "twg transport: a comment author is handed to the library",
        "twg_transport.py",
        '            {"body": flatten(comment.get("body"))}\n',
        '            {**comment, "body": flatten(comment.get("body"))}\n',
        "the transport promises the author is gone before the library sees the comment; kept, one change to the library's reader would publish it",
        (
            "test_fetch_tickets.TwgPrivacyTest.test_the_transport_hands_the_library_no_comment_author",
        ),
    ),
    Mutation(
        "twg transport: an emoji renders nothing",
        "twg_transport.py",
        '    return _string(attrs.get("text") or attrs.get("shortName"))\n',
        '    return ""\n',
        "an emoji carries meaning in a short comment (a tick, a cross) and is not person data",
        ("test_fetch_tickets.TwgPrivacyTest.test_an_emoji_and_a_link_card_render",),
    ),
    Mutation(
        "twg transport: a link card renders nothing",
        "twg_transport.py",
        '    return _string(attrs.get("url"))\n',
        '    return ""\n',
        "a link card is often the only reference in a line, and its URL is the content",
        ("test_fetch_tickets.TwgPrivacyTest.test_an_emoji_and_a_link_card_render",),
    ),
    Mutation(
        "twg transport: a key is not checked before the command line",
        "twg_transport.py",
        "    keys = [key for key in entries if TICKET_KEY.fullmatch(key)]\n",
        "    keys = list(entries)\n",
        "keys come from commit messages through a configurable pattern, and anything that is not a ticket key should never reach a process's argument list",
        (
            "test_fetch_tickets.TwgBoundaryTest.test_a_key_with_a_non_ascii_letter_or_digit_is_refused",
            "test_fetch_tickets.TwgBoundaryTest.test_a_refused_key_costs_only_itself",
            "test_fetch_tickets.TwgBoundaryTest.test_something_that_is_not_a_ticket_key_never_reaches_twg",
        ),
    ),
    Mutation(
        "twg transport: keys can be read as options",
        "twg_transport.py",
        '        "--",\n        *keys,\n',
        "        *keys,\n",
        "without the end-of-options marker a key shaped like a flag is read as one",
        (
            "test_fetch_tickets.TwgBoundaryTest.test_a_refused_key_costs_only_itself",
            "test_fetch_tickets.TwgMovedTicketTest.test_a_ticket_moved_to_an_allowlisted_project_is_filed_under_the_cited_key",
            "test_fetch_tickets.TwgRefreshTest.test_a_refresh_through_twg_is_capped_and_batched_like_any_other",
            "test_fetch_tickets.TwgRequestTest.test_a_page_is_one_bulk_get_answered_in_the_v2_shape",
            "test_fetch_tickets.TwgThreadTest.test_a_partial_thread_is_completed_by_one_counted_follow_up",
        ),
    ),
    Mutation(
        "twg transport: output is decoded strictly",
        "twg_transport.py",
        '            errors="replace",\n',
        '            errors="strict",\n',
        "one undecodable byte in a ticket's text raises out of the stage and stops the run",
        ("test_fetch_tickets.TwgBoundaryTest.test_output_that_is_not_valid_utf_8_is_still_read",),
    ),
    Mutation(
        "twg transport: a key with no answer is cached as absent",
        "fetch_tickets.py",
        "    answered = [key for key in keys if key not in response.unanswered]\n",
        "    answered = list(keys)\n",
        "a key the transport could not answer, cached as absent, is final and lost for good",
        (
            "test_fetch_tickets.TwgBoundaryTest.test_a_refused_key_costs_only_itself",
            "test_fetch_tickets.TwgFollowUpCostTest.test_follow_ups_stop_at_the_per_run_budget",
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_follow_up_that_fails_as_a_process_costs_only_its_key",
            "test_fetch_tickets.TwgMovedTicketTest.test_a_follow_up_reply_that_is_not_twg_s_envelope_is_no_answer",
            "test_fetch_tickets.TwgMovedTicketTest.test_an_unreadable_follow_up_is_no_answer_not_absence",
            "test_fetch_tickets.TwgMovedTicketTest.test_only_a_not_found_from_get_is_absence",
        ),
    ),
    Mutation(
        "comment thread: a partial thread is not completed",
        "twg_transport.py",
        "            issue = self.completed(found, issue, fields)\n",
        "",
        "#431: bulk-get returns only the most recent page of a long thread, so a ticket's earliest comments - usually where the problem is stated - never reached the store",
        (
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_thread_completion_that_fails_as_a_process_leaves_the_thread_marked",
            "test_fetch_tickets.TwgThreadTest.test_a_partial_thread_is_completed_by_one_counted_follow_up",
        ),
    ),
    Mutation(
        "comment thread: completion ignores the follow-up budget",
        "twg_transport.py",
        '        if "comment" not in fields or not partial_thread(issue) or self.followups <= 0:\n',
        '        if "comment" not in fields or not partial_thread(issue):\n',
        "a completion outside the budget is the unbounded per-ticket call the budget exists to stop",
        ("test_fetch_tickets.TwgThreadTest.test_a_thread_left_partial_is_marked_truncated",),
    ),
    Mutation(
        "comment thread: completion runs with comments off",
        "twg_transport.py",
        '        if "comment" not in fields or not partial_thread(issue) or self.followups <= 0:\n',
        "        if not partial_thread(issue) or self.followups <= 0:\n",
        "a thread fetched with comments switched off is narrative nobody enabled, and spends follow-ups on it",
        (
            "test_fetch_tickets.TwgThreadTest.test_no_thread_is_fetched_unless_comments_are_requested",
        ),
    ),
    Mutation(
        "comment thread: a truncated thread is not marked",
        "fetch_tickets.py",
        '        record[COMMENTS_TRUNCATED] = thread_truncated(fields.get("comment"))\n',
        "",
        "#431: a thread stored as returned reads as the whole conversation when its start is missing",
        (
            "test_fetch_tickets.ThreadTruncationTest.test_a_thread_the_tracker_returned_part_of_is_marked_on_any_transport",
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_thread_completion_that_fails_as_a_process_leaves_the_thread_marked",
            "test_fetch_tickets.TwgThreadTest.test_a_thread_left_partial_is_marked_truncated",
        ),
    ),
    Mutation(
        "comment thread: the transport drops the thread total",
        "twg_transport.py",
        '    if isinstance(value.get("total"), int):\n        out["total"] = value["total"]\n',
        "",
        "without the tracker's total the library cannot tell a partial thread from a whole one, and the mark silently never appears on the twg route",
        (
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_thread_completion_that_fails_as_a_process_leaves_the_thread_marked",
            "test_fetch_tickets.TwgThreadTest.test_a_thread_left_partial_is_marked_truncated",
        ),
    ),
    Mutation(
        "comment thread: the mark describes the wrong thread",
        "fetch_tickets.py",
        "        out.pop(COMMENTS_TRUNCATED, None)\n",
        "",
        "a refresh that keeps the longer stored thread would otherwise carry the shorter refreshed thread's mark, describing comments the record does not hold",
        (
            "test_fetch_tickets.ThreadTruncationTest.test_a_kept_longer_thread_is_marked_when_the_tracker_reports_more",
        ),
    ),
    Mutation(
        "comment thread: the page loses the mark",
        "build_explorer.py",
        '            entry["ct"] = True\n',
        "            pass\n",
        "#431: the explorer is where comments are read, and without the mark it shows a partial thread as if it were whole",
        ("test_build_explorer.TrackerEvidenceTest.test_a_partial_thread_is_marked_for_the_page",),
    ),
    Mutation(
        "comment thread: a kept thread ignores the tracker's newer count",
        "fetch_tickets.py",
        "    return latest_total > held\n",
        "    return False\n",
        "second review of #431: a stored thread kept over a shorter refresh was shown as whole while the tracker had just reported more comments than it held",
        (
            "test_fetch_tickets.ThreadTruncationTest.test_a_kept_longer_thread_is_marked_when_the_tracker_reports_more",
        ),
    ),
    Mutation(
        "comment thread: the tracker's count is not stored",
        "fetch_tickets.py",
        '        record[COMMENTS_TOTAL] = thread_total(fields.get("comment"))\n',
        "",
        "without the count a later refresh cannot tell whether the thread it keeps is short of what the tracker reports",
        (
            "test_fetch_tickets.ThreadTruncationTest.test_a_comment_dropped_as_noise_is_not_read_as_missing",
            "test_fetch_tickets.ThreadTruncationTest.test_a_kept_longer_thread_is_marked_when_the_tracker_reports_more",
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_thread_completion_that_fails_as_a_process_leaves_the_thread_marked",
        ),
    ),
    Mutation(
        "twg transport: a follow-up that fails as a process escapes the page",
        "twg_transport.py",
        "        except OSError:\n            # A timeout or a missing executable: counted, and no answer for\n",
        "        except ValueError:\n            # A timeout or a missing executable: counted, and no answer for\n",
        "second review of #420: a timed-out get discarded the bulk-get answers for every other key in its page, and the run counted one call where twg ran two",
        (
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_follow_up_that_fails_as_a_process_costs_only_its_key",
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_thread_completion_that_fails_as_a_process_leaves_the_thread_marked",
        ),
    ),
    Mutation(
        "comment thread: a completion that fails as a process escapes the page",
        "twg_transport.py",
        "        except OSError:\n            return issue\n",
        "        except ValueError:\n            return issue\n",
        "the same loss through the thread completion: one failed get --comments would cost every ticket in its page",
        (
            "test_fetch_tickets.TwgFollowUpFailureTest.test_a_thread_completion_that_fails_as_a_process_leaves_the_thread_marked",
        ),
    ),
    Mutation(
        "twg transport: one refused key refuses its page",
        "twg_transport.py",
        "    return keys, fields, refused\n",
        '    if refused:\n        raise NoAnswer("a page held something that is not a ticket key")\n    return keys, fields, refused\n',
        "second review of #420: keys are sorted into pages, so the same page failed every run and the valid tickets beside a refused key were never fetched",
        (
            "test_fetch_tickets.TwgBoundaryTest.test_a_key_with_a_non_ascii_letter_or_digit_is_refused",
            "test_fetch_tickets.TwgBoundaryTest.test_a_refused_key_costs_only_itself",
            "test_fetch_tickets.TwgBoundaryTest.test_something_that_is_not_a_ticket_key_never_reaches_twg",
        ),
    ),
    Mutation(
        "twg transport: a page of refused keys still runs twg",
        "twg_transport.py",
        '        if not keys:\n            return json.dumps({"issues": []}), refused\n',
        "",
        "a page with nothing to ask about would still run twg and be counted as a request",
        ("test_fetch_tickets.TwgBoundaryTest.test_a_refused_key_costs_only_itself",),
    ),
    Mutation(
        "twg transport: a refused key is cached as absent",
        "twg_transport.py",
        "        issues, unanswered = [], list(refused)\n",
        "        issues, unanswered = [], []\n",
        "a refused key read as missing from the reply is cached as absent, which is final",
        ("test_fetch_tickets.TwgBoundaryTest.test_a_refused_key_costs_only_itself",),
    ),
    Mutation(
        "twg transport: the key pattern matches Unicode letters and digits",
        "twg_transport.py",
        'TICKET_KEY = re.compile(r"[A-Za-z]\\w*-\\d+", re.ASCII)\n',
        'TICKET_KEY = re.compile(r"[A-Za-z]\\w*-\\d+")\n',
        "Sonar S6353 asks for \\w and \\d; without re.ASCII they match Unicode letters and digits in Python 3, widening what reaches twg's command line",
        (
            "test_fetch_tickets.TwgBoundaryTest.test_a_key_with_a_non_ascii_letter_or_digit_is_refused",
        ),
    ),
    Mutation(
        "twg transport: a follow-up reply that is not twg's envelope is absence",
        "twg_transport.py",
        "            # Unparseable, or JSON that is not twg's envelope: no answer.\n            return UNANSWERED\n",
        "            # Unparseable, or JSON that is not twg's envelope: no answer.\n            return ABSENT\n",
        "JSON that parses but is not the envelope says nothing about the ticket, and cached as absent - which is final - the ticket is lost for good",
        (
            "test_fetch_tickets.TwgMovedTicketTest.test_a_follow_up_reply_that_is_not_twg_s_envelope_is_no_answer",
            "test_fetch_tickets.TwgMovedTicketTest.test_an_unreadable_follow_up_is_no_answer_not_absence",
        ),
    ),
    Mutation(
        "branch-only text is pooled with a landed ticket's evidence",
        "build_intent_index.py",
        "        _pool_text(info, *texts, text_landed=text_landed)",
        "        _pool_text(info, *texts)",
        "a landed ticket quoted a draft nobody merged as what it changed (#429)",
        (
            "test_build_intent_index.BranchTextTest.test_a_landed_tickets_branch_text_is_kept_apart",
            "test_build_intent_index.BranchTextTest.test_branch_text_that_also_landed_is_not_called_branch_only",
            "test_build_intent_index.BranchTextTest.test_the_run_counts_tickets_carrying_branch_text",
            "test_build_intent_index.DocumentedRecordShapeTest.test_a_cap_the_stage_does_not_keep_is_reported",
            "test_build_intent_index.DocumentedRecordShapeTest.test_a_documented_view_the_record_does_not_carry_is_reported",
            "test_build_intent_index.DocumentedRecordShapeTest.test_a_singular_description_is_reported",
            "test_build_intent_index.DocumentedRecordShapeTest.test_a_wrong_presence_claim_is_reported",
            "test_build_intent_index.DocumentedRecordShapeTest.test_an_always_present_field_not_declared_as_one_is_reported",
            "test_build_intent_index.DocumentedRecordShapeTest.test_only_d_is_written_on_a_ticket_with_nothing_to_store",
            "test_build_intent_index.DocumentedRecordShapeTest.test_the_caps_are_the_hand_derived_ones_on_both_sides",
            "test_build_intent_index.DocumentedRecordShapeTest.test_the_forged_block_this_gate_is_driven_with_is_itself_clean",
            "test_build_intent_index.DocumentedRecordShapeTest.test_the_skill_agrees_with_the_record_it_documents",
            "test_build_intent_index.RedactedValuesTest.test_branch_only_text_is_redacted_like_landed_text",
        ),
    ),
    Mutation(
        "squash-landed text is kept apart as branch-only",
        "build_intent_index.py",
        '    text_landed = is_landed or any(f.get("landed") for f in commit.get("files", []))',
        "    text_landed = is_landed",
        "a squash merge's own account of its work was labelled as never landed (#429)",
        (
            "test_build_intent_index.BranchTextTest.test_a_branch_commit_whose_content_landed_is_landed_text",
        ),
    ),
    Mutation(
        "branch-only text the landed commits also said stays in branch-only",
        "build_intent_index.py",
        "            repeated = [text for text in branch[name] if text in main[name]]",
        "            repeated = []",
        "a rebased branch's stale commits labelled landed words as branch-only (#429)",
        (
            "test_build_intent_index.BranchTextTest.test_branch_copies_of_landed_text_keep_their_count",
            "test_build_intent_index.BranchTextTest.test_branch_text_that_also_landed_is_not_called_branch_only",
        ),
    ),
    Mutation(
        "branch copies of landed text are dropped with their count",
        "build_intent_index.py",
        "                main[name][text] += branch[name].pop(text)",
        "                branch[name].pop(text)",
        "separating branch text re-ranked a landed ticket's own fields (#429 review)",
        (
            "test_build_intent_index.BranchTextTest.test_branch_copies_of_landed_text_keep_their_count",
        ),
    ),
    Mutation(
        "the run stops recording how many tickets carry branch-only text",
        "build_intent_index.py",
        '            "intent.tickets_with_branch_text": branch_texted,',
        '            "intent.tickets_with_branch_text": 0,',
        "a rebuild that moved text into `ps`/`pb` reported nothing of it (#429 review)",
        (
            "test_build_intent_index.BranchTextTest.test_the_run_counts_tickets_carrying_branch_text",
        ),
    ),
    Mutation(
        "a proposed-only ticket's branch text is left out of its main fields",
        "build_intent_index.py",
        "            main[name][text] = main[name].get(text, 0) + count",
        "            pass",
        "a ticket found only on a branch lost the only evidence it had (#429)",
        (
            "test_build_intent_index.BranchTextTest.test_a_proposed_only_ticket_keeps_its_text_in_the_main_fields",
        ),
    ),
    Mutation(
        "check-evidence is blind to branch-only text",
        "sensitive.py",
        'EVIDENCE_FIELDS = ("d", "s", "b", "ps", "pb")',
        'EVIDENCE_FIELDS = ("d", "s", "b")',
        "a committed identifier in branch-only text passed the gate and reached the page (#429)",
        ("test_build_intent_index.CheckEvidenceStageTest.test_branch_only_text_is_checked_too",),
    ),
    Mutation(
        "explorer source links: a base is guessed for a repository the graph cannot link",
        "build_explorer.py",
        "    bases = {repo: base for repo in repos if (base := urls.base(repo))}\n",
        '    bases = {repo: urls.base(repo) or f"https://github.com/{repo}/blob/main/" '
        "for repo in repos}\n",
        "#423: rows of a repository on another host or with no recorded commit would link "
        "a guessed file at a moving branch, where the rule is to write no link at all",
        (
            "test_explorer_source_links.SourceLinksOnThePage.test_each_row_joined_to_its_base_is_the_graph_url_for_its_node",
            "test_explorer_source_links.SourceLinksOnThePage.test_the_page_carries_a_base_for_each_repository_its_rows_can_link",
        ),
    ),
    Mutation(
        "explorer source links: the single-root repositories are not named",
        "build_explorer.py",
        "    if single:\n",
        "    if False:\n",
        "#423: rows of a repository built by the single-root route would keep the "
        "`repositories/<repo>/` prefix when joined, linking a path that does not exist",
        (
            "test_explorer_source_links.SourceLinksOnThePage.test_each_row_joined_to_its_base_is_the_graph_url_for_its_node",
            "test_explorer_source_links.SourceLinksOnThePage.test_the_page_carries_a_base_for_each_repository_its_rows_can_link",
        ),
    ),
    Mutation(
        "explorer source links: rows lose their line anchor",
        "build_explorer.py",
        '    return line_anchor(location).removeprefix("#") if found.url else ""\n',
        '    return ""\n',
        "#423: every row would link the top of its file rather than the line the graph's "
        "`url` names, so the page and the graph would cite one node two ways",
        (
            "test_explorer_source_links.SourceLinksOnThePage.test_a_file_name_that_cannot_be_encoded_gets_no_line_and_stops_nothing",
            "test_explorer_source_links.SourceLinksOnThePage.test_a_row_that_gets_no_link_carries_no_line",
            "test_explorer_source_links.SourceLinksOnThePage.test_each_row_joined_to_its_base_is_the_graph_url_for_its_node",
        ),
    ),
    Mutation(
        "explorer source links: a row that cannot be linked still carries its line",
        "build_explorer.py",
        '    return line_anchor(location).removeprefix("#") if found.url else ""\n',
        '    return line_anchor(location).removeprefix("#")\n',
        "#423: the anchor is the one per-row cost of linking, and every row the page will "
        "never link would pay it for nothing",
        (
            "test_explorer_source_links.SourceLinksOnThePage.test_a_file_name_that_cannot_be_encoded_gets_no_line_and_stops_nothing",
            "test_explorer_source_links.SourceLinksOnThePage.test_a_row_that_gets_no_link_carries_no_line",
        ),
    ),
    Mutation(
        "explorer source links: an unreadable repository list stops the page",
        "build_explorer.py",
        '    except ValueError as error:\n        print(f"No source links',
        '    except OSError as error:\n        print(f"No source links',
        "#423: a malformed `config/repositories.txt` would refuse the whole page over its "
        "links, when the page is still worth having without them",
        (
            "test_explorer_source_links.SourceLinksOnThePage.test_an_unreadable_repository_list_builds_the_page_without_links",
            "test_explorer_source_links.SourceLinksOnThePage.test_unreadable_provenance_is_named_as_the_file_that_failed",
        ),
    ),
    Mutation(
        "source url: a file name that cannot be encoded raises",
        "source_urls.py",
        "        except UnicodeEncodeError:\n",
        "        except UnicodeDecodeError:\n",
        "#423 review: a lone surrogate in a `source_file` - a non-UTF-8 file name under "
        "surrogateescape - would stop both the source-urls stage and the explorer page "
        "rather than leave that one node unlinked",
        (
            "test_build_source_urls.SourceUrlsStageTest.test_a_file_name_that_cannot_be_encoded_is_left_unlinked_and_counted",
            "test_explorer_source_links.SourceLinksOnThePage.test_a_file_name_that_cannot_be_encoded_gets_no_line_and_stops_nothing",
            "test_source_urls.BuildingTheUrl.test_no_url_is_written_where_one_cannot_be_right",
        ),
    ),
    Mutation(
        "explorer: the config block is written without the </ escape",
        "build_explorer.py",
        'json.dumps(page_config, ensure_ascii=False, separators=SEPARATORS).replace("</", "<\\\\/")',
        "json.dumps(page_config, ensure_ascii=False, separators=SEPARATORS)",
        "#423 review: repository names are #config keys and are not validated, so one "
        "holding `</script>` would close the block and run what follows as page script",
        (
            "test_explorer_source_links.SourceLinksOnThePage.test_a_repository_name_cannot_close_the_config_block",
        ),
    ),
    Mutation(
        "source url: unreadable provenance is blamed on the repository list",
        "source_urls.py",
        '            raise ValueError(f"{config.PROVENANCE_PATH}: {error}") from error\n',
        '            raise ValueError(f"{config.REPOSITORIES_CONFIG}: {error}") from error\n',
        "#423 review: an operator told the repository list is unreadable looks in the "
        "wrong file while the malformed one is provenance.json",
        (
            "test_explorer_source_links.SourceLinksOnThePage.test_unreadable_provenance_is_named_as_the_file_that_failed",
        ),
    ),
    Mutation(
        "explorer source links: a sync after source-urls passes silently",
        "build_explorer.py",
        "    if not differing:\n",
        "    if True:\n",
        "#423 review: the page would link the newly synced commit while the graph's `url` "
        "cites the old one, and nothing would say the two now cite one node two ways",
        (
            "test_explorer_source_links.SourceLinksOnThePage.test_a_sync_after_source_urls_is_warned_about",
        ),
    ),
    Mutation(
        "explorer source links: unreadable inputs are reported as unlinkable hosts",
        "build_explorer.py",
        '    if urls is None:\n        print("Source links: none',
        '    if False:\n        print("Source links: none',
        "#423 review: the summary would say every repository lacks a github.com clone URL "
        "or a sha when the real cause was a file that could not be read",
        (
            "test_explorer_source_links.SourceLinksOnThePage.test_an_unreadable_repository_list_builds_the_page_without_links",
        ),
    ),
    # #437. None of these removes the migration or the retry themselves: the
    # real-git test observes those only where the filesystem folds case, so an
    # entry for them would name different observers on macOS and on Linux.
    Mutation(
        "sync case collision: an unrelated fetch failure migrates the clone",
        "sync_repositories.py",
        '    if not any(symptom in (error.stderr or "") for symptom in _COLLISION_SYMPTOMS):\n'
        "        return []\n",
        "",
        "#437: an unreachable remote would rewrite the clone's ref store and then fail anyway, "
        "where it used to fail and touch nothing",
        (
            "test_sync_case_collision.CaseCollisionDecisionTest.test_an_unrelated_fetch_failure_behaves_as_before",
        ),
    ),
    Mutation(
        "sync case collision: git's wording alone decides",
        "sync_repositories.py",
        "    return case_colliding_branches(listing)\n",
        '    return case_colliding_branches(listing) or [["(unconfirmed)"]]\n',
        "#437: `cannot lock ref` is also a branch named like another's directory, which "
        "reftable does not fix, so the clone is migrated for nothing",
        (
            "test_sync_case_collision.CaseCollisionDecisionTest.test_a_lock_failure_without_a_case_collision_is_not_migrated",
        ),
    ),
    Mutation(
        "sync case collision: git 2.46 is trusted to migrate a clone",
        "git_version.py",
        "FLOOR = (2, 48)\n",
        "FLOOR = (2, 46)\n",
        "#437: `refs migrate` exists from 2.46 but refuses a repository holding reflogs until "
        "2.48, and every clone holds them, so the operator gets git's refusal instead of the fix",
        (
            "test_git_version_floor.StagesRefuseAnOldGit.test_every_git_using_stage_refuses",
            "test_git_version_floor.StagesRefuseAnOldGit.test_git_2_47_is_refused_naming_the_version_and_the_floor",
            "test_git_version_floor.StagesRefuseAnOldGit.test_unreadable_version_output_is_refused_clearly",
            "test_git_version_floor.StatusReportsAnOldGit.test_status_reports_an_old_git_and_still_exits_zero",
            "test_git_version_floor.VersionFlagReportsGit.test_version_names_git_and_the_floor",
            "test_sync_case_collision.CaseCollisionDecisionTest.test_git_too_old_to_migrate_names_the_branches_and_the_command",
        ),
    ),
    Mutation(
        "sync case collision: a reftable clone is migrated again",
        "sync_repositories.py",
        '    if run(["-C", str(repo_dir), "rev-parse", "--show-ref-format"]).strip() == "reftable":\n',
        "    if False:\n",
        "#437: a clone the migration cannot help fails on git's error rather than one naming "
        "the colliding branches",
        (
            "test_sync_case_collision.CaseCollisionDecisionTest.test_a_clone_already_on_reftable_is_not_migrated_again",
        ),
    ),
    Mutation(
        "sync case collision: the migration does not name the repository",
        "sync_repositories.py",
        '        f"{repo.name}: branches differing only in case ({named}) - "\n',
        '        f"branches differing only in case ({named}) - "\n',
        "#437: sync rewrites a clone's ref store and the log cannot say which one",
        (
            "test_sync_case_collision.CaseCollisionDecisionTest.test_a_collision_on_current_git_migrates_the_clone_and_syncs",
        ),
    ),
    Mutation(
        "git floor: the CLI does not gate the git-using stages",
        "cli.py",
        "    refused = git_version.refusal() if stage in GIT_STAGES else None\n",
        "    refused = None\n",
        "#437 follow-up: sync converts clones that an older git, here or on a CI runner, "
        "cannot read, and no stage says why",
        (
            "test_git_version_floor.StagesRefuseAnOldGit.test_every_git_using_stage_refuses",
            "test_git_version_floor.StagesRefuseAnOldGit.test_git_2_47_is_refused_naming_the_version_and_the_floor",
            "test_git_version_floor.StagesRefuseAnOldGit.test_unreadable_version_output_is_refused_clearly",
        ),
    ),
    Mutation(
        "git floor: a git-using stage is left out of the gate",
        "cli.py",
        'GIT_STAGES = frozenset({"sync", "drift", "export-history", "check-citations"})\n',
        'GIT_STAGES = frozenset({"sync", "drift", "export-history"})\n',
        "#437 follow-up: check-citations asks the clones with an old git and reads its "
        "failures as unresolved citations",
        ("test_git_version_floor.StagesRefuseAnOldGit.test_every_git_using_stage_refuses",),
    ),
    Mutation(
        "git floor: git 2.48 itself is refused",
        "git_version.py",
        "    if version < FLOOR:\n        return (\n",
        "    if version <= FLOOR:\n        return (\n",
        "#437 follow-up: the first git that can read a converted clone is turned away",
        ("test_git_version_floor.StagesAcceptACurrentGit.test_git_2_48_runs_the_stage",),
    ),
    Mutation(
        "git floor: only the minor version is compared",
        "git_version.py",
        "    if version < FLOOR:\n        return (\n",
        "    if version[1] < FLOOR[1]:\n        return (\n",
        "#437 follow-up: git 3.0 would be refused as older than 2.48",
        (
            "test_git_version_floor.StagesAcceptACurrentGit.test_a_major_version_above_two_runs_the_stage",
        ),
    ),
    Mutation(
        "git floor: an unreadable version passes",
        "git_version.py",
        '    if version is None:\n        return (\n            f"Cannot tell',
        '    if version is None:\n        return None\n        return (\n            f"Cannot tell',
        "#437 follow-up: a git whose version cannot be read is trusted with converted clones",
        (
            "test_git_version_floor.StagesRefuseAnOldGit.test_unreadable_version_output_is_refused_clearly",
        ),
    ),
    Mutation(
        "git floor: status stops reporting git",
        "status.py",
        '    print(f"Git: {git_version.describe()}")\n',
        "",
        "#437 follow-up: the health report cannot say that this machine's git is below the floor",
        (
            "test_git_version_floor.StatusReportsAnOldGit.test_status_reports_an_old_git_and_still_exits_zero",
        ),
    ),
    Mutation(
        "git floor: --version stops naming git",
        "cli.py",
        "        print(git_version.describe())\n",
        "",
        "#437 follow-up: the command that answers which environment runs a stage cannot "
        "say which git it would use",
        ("test_git_version_floor.VersionFlagReportsGit.test_version_names_git_and_the_floor",),
    ),
    Mutation(
        "sync case collision: the colliding branches follow the remote's order",
        "sync_repositories.py",
        "    return [sorted(names) for _, names in sorted(groups.items()) if len(names) > 1]\n",
        "    return [sorted(names) for _, names in groups.items() if len(names) > 1]\n",
        "#437: the failure message for one repository changes between runs (CLAUDE.md 1.3)",
        (
            "test_sync_case_collision.CollidingBranchesTest.test_groups_names_equal_once_case_folded_in_a_stable_order",
        ),
    ),
    Mutation(
        "twg flatten: a code block goes through the per-line strip",
        "twg_transport.py",
        '        if child.get("type") == "codeBlock":\n            _code(child, out)\n',
        "        if False:\n            _code(child, out)\n",
        "#436 regression found on a real tracker: stack traces, config and SQL lost their "
        "indentation and blank lines, because a code block's own newlines were split and stripped",
        (
            "test_fetch_tickets.TwgCodeAndTaskTest.test_a_code_block_beside_prose_keeps_both_shapes",
            "test_fetch_tickets.TwgCodeAndTaskTest.test_a_code_block_in_a_list_item_is_indented_with_the_item",
            "test_fetch_tickets.TwgCodeAndTaskTest.test_a_code_block_keeps_its_indentation_and_blank_lines",
        ),
    ),
    Mutation(
        "twg flatten: every action item reads as done",
        "twg_transport.py",
        '        done = isinstance(raw, dict) and raw.get("state") == "DONE"\n',
        "        done = True\n",
        "an open action item would be published as finished",
        ("test_fetch_tickets.TwgCodeAndTaskTest.test_task_items_are_marked_done_or_not",),
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


@dataclass(frozen=True)
class Record:
    """What the gate leaves on disk while a file is mutated: the file, the original
    bytes, and enough about the run to tell a live one from an abandoned one from
    outside the process that started it."""

    module: str
    original: bytes
    root: str
    pid: int
    host: str
    started: str


def run_ps(pid: int) -> str:
    """The start time `ps` reports for one pid, or "" when it names no process.

    `ps` because there is no portable way to read a process start time - macOS has
    no `/proc` - and that start time is the whole difference between "the process
    that wrote this record is still running" and "something else holds its pid
    now". A missing or failing `ps` returns "", which reads through as undecidable
    rather than as a dead process: this must never be why a gate run cannot start.
    """
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _header(module: str) -> bytes:
    """The identity line written above the original bytes: which file, which
    checkout, which process. One line, because everything after the first newline
    is the file, and sorted so two records of the same run are byte-identical."""
    identity = {
        "module": module,
        "root": str(ROOT),
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started": run_ps(os.getpid()),
    }
    return json.dumps(identity, sort_keys=True).encode("utf-8")


def read_record() -> Record | None:
    """The record on disk, or None when there is nothing a caller can act on.

    None for absent, for a header that will not parse, and for one naming a path
    outside the tree. The callers refuse on all three rather than reading past
    them: something wrote the record before mutating a file, so a record that
    cannot be read still means a file may hold a deliberately introduced defect.
    """
    if not RECOVERY_PATH.is_file():
        return None
    header, separator, original = RECOVERY_PATH.read_bytes().partition(b"\n")
    if not separator:
        return None
    try:
        parsed = json.loads(header)
        record = Record(
            module=str(parsed["module"]),
            original=original,
            root=str(parsed.get("root", "")),
            pid=int(parsed.get("pid", 0)),
            host=str(parsed.get("host", "")),
            started=str(parsed.get("started", "")),
        )
    # ValueError covers the JSON itself (JSONDecodeError is one) and a pid that is
    # not a number; TypeError and KeyError cover a header that parses to something
    # other than an object with a module in it.
    except (KeyError, TypeError, ValueError):
        return None
    return None if _traversal(record.module) else record


def liveness(record: Record, run=run_ps) -> str:
    """Whether the run a record names is still going, as far as this machine can
    tell - and a named answer where it cannot tell, because both wrong readings
    are expensive and opposite: a dead run reported live sends a session to wait
    for something that will never finish, and a live one reported dead gets it
    killed. `ps` is the seam, so a reused pid can be exercised without one.
    """
    if record.root != str(ROOT) or record.host != socket.gethostname():
        return ELSEWHERE
    if not record.started:
        return UNDECIDABLE
    return LIVE if run(record.pid) == record.started else ABANDONED


def recover() -> int:
    """Restore a file a killed run left mutated. Non-zero when a run must not start.

    Called before the pre-check below, because a mutation still applied makes
    the suite fail for a reason that has nothing to do with the working tree,
    and the pre-check would report that as a real failure.
    """
    if not RECOVERY_PATH.is_file():
        return 0
    record = read_record()
    target = target_of(record.module) if record else None
    if record is None or target is None or not target.is_file():
        print(
            f"{RECOVERY_PATH} records a mutation from an earlier run but does not name a "
            f"file this gate can restore, so a source file may still hold a deliberately "
            f"introduced defect. Restore it from git, then delete {RECOVERY_PATH}.",
            file=sys.stderr,
        )
        return 1
    target.write_bytes(record.original)
    RECOVERY_PATH.unlink()
    print(
        f"Recovered {record.module}: an earlier run was killed with a mutation applied, and "
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

# Run by `_run` in the suite's own directory. It reports which tests failed
# rather than only whether the run passed, because the observers in the table
# above are derived from that set and checked against it. The name comes off the
# test object rather than out of unittest's printed failure header: a header is a
# display format, and reading a mapping this gate depends on out of one would be
# a parser that breaks silently. `test.id()` is not it either - for a subTest it
# carries the parameters of the case that failed, which vary from run to run.
REPORTER = """
import json
import sys
import unittest


def observer_of(test):
    # A subTest failure is reported through a _SubTest, whose own module is
    # unittest's, with the real case hanging off it - so unwrap before asking
    # anything about the test. A failure inside a test class then carries its
    # module and class on the class and its name on the instance. A module that
    # failed to import, or whose setUpModule raised, is reported through
    # unittest's other stand-ins, which have no test behind them at all:
    # _FailedTest keeps the module name where a method name would go, and
    # _ErrorHolder puts it in brackets after the name of the hook that raised.
    # Those name the module alone, because there is no test to name.
    case = getattr(test, "test_case", None)
    if case is not None:
        test = case
    module = type(test).__module__
    if not module.startswith("unittest"):
        return f"{module}.{type(test).__qualname__}.{test._testMethodName}"
    described = getattr(test, "description", "")
    if described:
        return described.partition("(")[2].rstrip(")").split(".")[0]
    return str(getattr(test, "_testMethodName", "")).split(".")[0]


names = json.loads(sys.argv[1])
marker = sys.argv[2]
loader = unittest.TestLoader()
suite = loader.loadTestsFromNames(names) if names else loader.discover(".")
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
failed = {observer_of(test) for test, _ in result.failures + result.errors} - {""}
print(marker + json.dumps({"passed": result.wasSuccessful(), "observers": sorted(failed)}))
"""


@dataclass(frozen=True)
class SuiteRun:
    """What one suite run reported: whether it passed, and which tests failed."""

    passed: bool
    observers: tuple[str, ...]


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
    return SuiteRun(bool(parsed["passed"]), tuple(parsed["observers"]))


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
    tests that failed: a red suite the reader cannot see from where they are
    standing is the case that costs, and the names are already in hand.
    """
    return _run(None)


def module_of(observer: str) -> str:
    """The test module holding an observer: everything before the first dot.

    An observer is `module.Class.test` for a failure inside a test, and the
    module alone for one that is not inside a test at all - an import that
    raised, or a `setUpModule`. Both name their module the same way.
    """
    return observer.partition(".")[0]


def isolable(observer: str) -> bool:
    """Whether `observer` names a test that can be run by itself.

    A `module.Class.test` can; a name carrying no test - the module alone, which
    is what unittest reports for an import that raised or a `setUpModule` that
    did - cannot, because selecting it runs every test the module holds. That is
    module scope by another route, so `--isolate` reports such a name as
    unchecked rather than deciding it either way. No shipped entry has one, and
    the branch is here so that the first one is visible rather than measured in a
    scope the mode's own sentence denies.
    """
    return module_of(observer) != observer


def run_alone(observer: str) -> bool:
    """Whether `observer` failed when it was the only test in the run.

    The discriminator this gate lacked. Every other run shape here selects at
    module granularity - `run_observers` runs the modules an entry names, and
    `failing_observers` discovers everything - so a test that fails only because
    a sibling ran first is indistinguishable from one that observes the defect
    (#334). Selecting the single test is what separates them.

    Alone means no sibling test, not no fixture: `setUpModule` and `setUpClass`
    still run, because they are part of what the test is. A test that needs a
    sibling's leftovers is the thing being looked for; a test that needs its own
    class's setup is not.

    Whether the *named* test failed rather than whether the run passed, for the
    same reason `run_observers` asks that: a `tearDownModule` that raises fails
    the run while naming the module, and reading the verdict would count that as
    an observation (#274).
    """
    return observer in _run((observer,)).observers


def run_observers(observers: Sequence[str]) -> tuple[str, ...]:
    """The named observers that failed, from a run of the modules holding them.

    Whole modules are run, because a module costs no more than one test of it and
    that is what the mapping was derived from - but the answer is which *named*
    tests failed, not whether the run passed. A module fails for reasons an entry
    does not describe: the guards over the table above fail for every entry
    targeting this file, and reading the run's verdict reported four such entries
    as caught while nothing had observed them (#274).

    An empty selection is refused rather than run. `unittest` reports a suite of
    no tests as a success, so an entry that named nothing would run no test at
    all and be reported as `caught` - a gate saying it had observed a defect it
    never looked for, which is worse than the whole-suite run this replaces.
    """
    if not observers:
        raise SystemExit(
            "a mutation reached the runner naming no observing test. Running nothing "
            "reports success, which this gate would print as `caught`."
        )
    modules = tuple(dict.fromkeys(module_of(observer) for observer in observers))
    failed = set(_run(modules).observers)
    return tuple(observer for observer in observers if observer in failed)


def failing_observers() -> tuple[str, ...]:
    """The tests that fail with the current tree, from a whole-suite run."""
    return _run(None).observers


def behavioural(observers: Iterable[str]) -> tuple[str, ...]:
    """`observers` without the table guards, which observe the table and nothing else.

    Applied wherever a set is derived from or compared against a suite run, so
    that a guard firing because this file changed cannot be read as a test having
    seen the defect. `sweep` needs no such filter: it asks whether a *named*
    observer failed, and `check_mapping` refuses an entry that names a guard.
    """
    return tuple(observer for observer in observers if observer not in TABLE_GUARDS)


def observer_path(observer: str) -> Path:
    """The file holding an entry's observer."""
    return ROOT / "tests" / f"{module_of(observer)}.py"


@contextmanager
def excusing_unmapped(mutations: Sequence[Mutation]) -> Iterator[None]:
    """Let `check_mapping` past the unmapped entries, in this process and its children.

    Narrow in three ways on purpose, because the refusal it suspends is what stops
    a gate printing `caught` from a run that looked for nothing: only entries that
    name no observer are excused, only those entries by name, and only while the
    block runs. Everything else `check_mapping` refuses still refuses, and
    `run_modules` still refuses an empty selection - so a mode that runs what an
    entry names stops at an excused entry rather than passing it.

    Entered by every mode, with no names by all but one. An empty list excuses
    nothing and overwrites whatever the environment arrived holding, which is what
    makes the variable a channel rather than a switch.
    """
    excused = sorted(mutation.name for mutation in mutations if not mutation.observers)
    os.environ[DERIVING] = json.dumps(excused)
    try:
        yield
    finally:
        os.environ.pop(DERIVING, None)


def excused_from_mapping() -> frozenset[str]:
    """The entries a `--derive-mapping` run has asked `check_mapping` to let past."""
    recorded = os.environ.get(DERIVING)
    return frozenset(json.loads(recorded)) if recorded else frozenset()


def check_mapping(mutations: Sequence[Mutation]) -> None:
    """Refuse a table whose entries do not name tests this tree can observe with.

    All three refusals close the same hole, which is the failure mode naming
    observers introduced. An entry naming nothing runs nothing; an entry naming a
    module that was renamed or misspelt loads a `_FailedTest` and fails for that
    reason rather than for the mutation; an entry naming a guard over this table
    names something that fails whenever this file changes. Each would print
    `caught` from a run that proved nothing, and none is visible in a pass.

    The first refusal is suspended for the entries a `--derive-mapping` run is
    mapping, and only for those. This check runs inside the suite, so refusing an
    unmapped entry made the suite red, and a red suite blocked the mode named in
    the refusal - the remedy could not be run (#287).
    """
    excused = excused_from_mapping()
    for mutation in mutations:
        if not mutation.observers:
            if mutation.name in excused:
                continue
            raise SystemExit(
                f"mutation '{mutation.name}' names no observing test. Run "
                f"`--derive-mapping --only '{mutation.name}'` and paste what it prints: "
                "an unmapped entry cannot be run, because running nothing passes."
            )
        for observer in mutation.observers:
            if observer in TABLE_GUARDS:
                raise SystemExit(
                    f"mutation '{mutation.name}' names the table guard '{observer}' as an "
                    "observer. That check reads this table rather than any behaviour, and "
                    "it fails for every entry targeting this file whatever the mutation "
                    "did, so it protects nothing. Derive the set again with "
                    f"`--derive-mapping --only '{mutation.name}'`: if it comes back empty, "
                    "the behaviour has no test and that is the finding."
                )
            if not observer_path(observer).is_file():
                raise SystemExit(
                    f"mutation '{mutation.name}' names '{observer}' as an observer and "
                    f"{observer_path(observer)} does not exist. A module unittest cannot "
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
    RECOVERY_PATH.write_bytes(_header(mutation.module) + b"\n" + original)
    path.write_bytes(source.replace(mutation.find, mutation.replace, 1).encode("utf-8"))
    return original


def _remedy(record: Record, state: str) -> str:
    """What to do about the record, which differs by state and is the whole point
    of naming the run: the same words for a live run and an abandoned one send
    half the readers at the wrong action."""
    if state == LIVE:
        return (
            f"A run is live: pid {record.pid} in {record.root}. Wait for it, or stop it "
            f"with `kill -TERM {record.pid}`, which restores the file on the way out. Not "
            "`kill -9`, which cannot be handled and leaves the mutation applied, and not a "
            "`pgrep` on this script's name, which matches every checkout on this machine. "
            "To find which checkout any gate holds, read each one's working directory "
            "rather than its arguments: `for p in $(pgrep -f mutation_gate.py); do lsof "
            "-a -p $p -d cwd -Fn | grep '^n'; done`."
        )
    if state == ABANDONED:
        return (
            f"No run holds it: pid {record.pid} in {record.root} is gone, so an earlier run "
            f"was killed with the mutation applied. `PYTHONPATH=src python3 "
            f"tests/mutation_gate.py` in that checkout restores the file from this record "
            "before it does anything else."
        )
    if state == ELSEWHERE:
        return (
            f"This record was written by pid {record.pid} on {record.host} in "
            f"{record.root}, and this is {socket.gethostname()} in {ROOT}, so nothing here "
            f"can say whether that run is still going. Ask in that checkout, or restore "
            f"{record.module} from git and delete {RECOVERY_PATH}."
        )
    return (
        f"The record names pid {record.pid} but no start time for it, so a pid that exists "
        f"proves nothing - it is reused. Check by hand whether a gate run is going in "
        f"{record.root}, and if none is, run `PYTHONPATH=src python3 "
        f"tests/mutation_gate.py` there to restore the file from this record."
    )


def status(*, as_json: bool = False, run=run_ps) -> int:
    """Report whether a mutation is applied in this tree. Non-zero when one is.

    The readable half of the record, and the only one anything outside the gate
    has: a person about to commit and a job deciding whether to trust the tree
    both got this wrong from `git status`, which reports a mutation as an ordinary
    edit and a mutated tree as merely dirty. `--json` carries the pid so a caller
    that wants to stop the run can signal that process rather than matching
    command lines, which finds every checkout on the machine.
    """
    present = RECOVERY_PATH.is_file()
    if not present:
        if as_json:
            print(json.dumps({"applied": False}, sort_keys=True))
        else:
            print(f"No mutation is applied in {ROOT}.")
        return 0

    record = read_record()
    if record is None:
        if as_json:
            print(json.dumps({"applied": True, "readable": False}, sort_keys=True))
        print(
            f"{RECOVERY_PATH} cannot be read as a mutation record, so a file in {ROOT} may "
            f"hold a deliberately introduced defect and nothing here can say which. "
            f"Restore the tree from git, then delete {RECOVERY_PATH}.",
            file=sys.stderr,
        )
        return 1

    state = liveness(record, run=run)
    path = target_of(record.module)
    if as_json:
        print(
            json.dumps(
                {
                    "applied": True,
                    "readable": True,
                    "state": state,
                    "path": str(path),
                    "module": record.module,
                    "root": record.root,
                    "pid": record.pid,
                    "host": record.host,
                    "started": record.started,
                },
                sort_keys=True,
            )
        )
    print(
        f"{path} is mutated: the mutation gate has a deliberately introduced defect in it "
        f"right now, so nothing read from this tree means what it says - and committing it "
        f"commits the defect.\n{_remedy(record, state)}",
        file=sys.stderr,
    )
    return 1


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
    """Apply each entry and check that the tests it names failed. The gate proper."""
    check_mapping(mutations)
    survived: list[Mutation] = []
    silent: list[str] = []

    def observe(mutation: Mutation) -> None:
        observed = run_observers(mutation.observers)
        print(f"  {'caught ' if observed else 'SURVIVED'}  {mutation.name}")
        if not observed:
            survived.append(mutation)
        elif len(observed) < len(mutation.observers):
            quiet = sorted(set(mutation.observers) - set(observed))
            silent.append(f"{mutation.name}: {', '.join(quiet)} is named and did not fail")

    interrupted = each_mutation(mutations, observe)
    if interrupted is not None:
        return interrupted
    return _sweep_verdict(mutations, survived, silent)


def _sweep_verdict(
    mutations: Sequence[Mutation], survived: list[Mutation], silent: list[str]
) -> int:
    """Report what `sweep` saw. A named test that did not fail fails the run as well.

    `run_observers` already returns which named tests failed, so this costs nothing:
    an entry still caught by one name while another has stopped observing it used
    to print `caught` here and go red only in the nightly `--verify-mapping`, after
    the merge. That half of the mapping check needs no whole-suite run, so it runs
    on every pull request. The other half - a test that fails and is not named -
    does, and stays with `--verify-mapping` and `observer_staleness.py`.
    """

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
            "the entry before changing the code: the tests it names may no longer be the "
            "ones that observe it, and a named test that was renamed or deleted is a "
            "survivor here whatever the rest of its module did. If they nearly all "
            "survived, the suite is not reading this tree at all - see `_run` on a "
            "relative PYTHONPATH.",
            file=sys.stderr,
        )
    for entry in silent:
        print(f"  NAMED, DID NOT FAIL: {entry}", file=sys.stderr)
    if silent:
        print(
            "\nThe entry is still caught, but it claims protection from a test that no "
            "longer gives it, so the mapping overstates what guards that behaviour. If the "
            "test changed on purpose, take the set from `--derive-mapping --only` on the "
            "entry rather than editing by hand; if it did not, the test has lost what it "
            "was checking.",
            file=sys.stderr,
        )
    return 1 if survived or silent else 0


def derive_mapping(mutations: Sequence[Mutation]) -> int:
    """Print the observers of each entry, from a whole-suite run per entry.

    What the table's `observers` are filled in from. Slow by construction - one
    full suite per entry - and that is the trade the field exists to take: paid
    once when an entry is written rather than on every run.

    The table guards are subtracted before anything is printed, so an entry whose
    only failures were theirs derives as observed by nothing rather than as
    protected by the check that noticed this file had changed (#274).

    An entry nothing observes is a survivor of the whole suite, so this refuses
    rather than printing an empty tuple to be pasted in.
    """
    unobserved: list[Mutation] = []

    def observe(mutation: Mutation) -> None:
        observers = behavioural(failing_observers())
        if not observers:
            unobserved.append(mutation)
        print(json.dumps({"name": mutation.name, "observers": list(observers)}), flush=True)
        print(f"  {len(observers) or 'NOTHING'} <- {mutation.name}", file=sys.stderr)

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
    each entry makes - these tests and no others observe this defect - is checked
    here in both directions. A test that failed and is not named means the entry
    under-describes what protects it; a named test that did not fail means the
    entry describes protection that is not there. The table guards are subtracted
    from what failed, so an entry whose behavioural observer was deleted
    disagrees here instead of agreeing with a guard (#274).
    """
    check_mapping(mutations)
    wrong: list[str] = []
    disagreed: set[str] = set()

    def observe(mutation: Mutation) -> None:
        complaints = len(wrong)
        failed = set(behavioural(failing_observers()))
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
            "\nEvery run of this gate concluded from only the tests named above, so a "
            "mapping that disagrees with the suite means those runs proved less than they "
            "reported. Take the set from `--derive-mapping` rather than editing by hand.",
            file=sys.stderr,
        )
    return 1 if wrong else 0


def already_red_alone(observers: Sequence[str]) -> tuple[str, ...]:
    """The observers that fail alone against the tree as it stands. Run unmutated.

    The control for `--isolate`, and the reason its number means what its
    sentence says. The mode reads "this test fails alone under the mutation" as
    "this test observes the mutation", which is only true of a test that passes
    alone without it. Order dependence has the other polarity too: a test that
    needs a sibling's leftovers to pass fails alone whatever the tree holds, and
    would be counted sound by a mode that only looked at the mutated run - the
    same interference, scored as its own absence.

    Run once over the distinct names rather than per entry, on the unmutated
    tree, before anything is applied. One subprocess per distinct observer, and
    an entry's observer is named by several entries on average.
    """
    return tuple(observer for observer in observers if isolable(observer) and run_alone(observer))


def isolate_mapping(mutations: Sequence[Mutation]) -> int:
    """Re-run each named observer alone under its entry's mutation. The third slow mode.

    What `--verify-mapping` cannot do. Both of the gate's other run shapes select
    at module granularity, so an observer that fails only because a sibling ran
    first is recorded as an observer, and one that would fail alone but passes in
    module scope is recorded as not one. Verification compares a module-scope
    answer against a module-scope answer, so the artefact is stable and agrees
    with itself (#334). Only running the test by itself can tell them apart.

    An observer that fails in module scope and passes alone is an interference
    artefact: the entry's claim about that test is not true of that test. The
    mutation is usually still caught, by a different test in the same module, so
    the gate's headline number stays honest - what degrades is the part a later
    reader is invited to falsify.

    Its own mode rather than a flag on `--verify-mapping`, which is the shape the
    issue proposed, because the two questions cost three orders of magnitude
    apart. Verification runs a whole suite per entry, 43 seconds against 0.07 for
    a single test, so bolting isolation onto it would have made a four-minute
    question cost hours and put the measurement the decision needs out of reach
    of one sitting. They are also different questions over different populations:
    verification asks whether the named set matches what the whole suite
    reported, and this asks whether each named test is individually sufficient. A
    run can want either without the other, and this one shards and selects with
    `--shard` and `--only` exactly as the other two do.

    Isolating inside `--derive-mapping` was the other candidate, and it stays
    available without new code: derive an entry, paste the set, then
    `--isolate --only <name>` on it. Deliberately two commands rather than a
    filter inside the deriver - a set silently narrowed on the way out is a table
    nobody can reconcile against a run, and the deriver's output is what an
    author pastes in.

    Three outcomes per observer rather than two, because a mode that can only say
    sound or artefact cannot report that it decided nothing: an observer that
    already fails alone unmutated, and one naming no test at all, are unchecked.
    They are counted and named separately, and a pass that decided nothing is a
    refusal rather than a green `0 of 0`.
    """
    check_mapping(mutations)
    named = sorted({observer for mutation in mutations for observer in mutation.observers})
    unisolable = tuple(observer for observer in named if not isolable(observer))
    # Before the control pass rather than after it, because that pass is one
    # subprocess per name and prints nothing while it runs - and because the
    # sentence at the end is about these names and no others. A reader who is told
    # afterwards that 753 of 759 claims hold cannot tell whether the six that did
    # not were six of the table's or six of a selection.
    print(
        f"  {len(named)} named observers over {len(mutations)} entries, checked one at a "
        "time; the control pass over them runs first, unmutated",
        file=sys.stderr,
        flush=True,
    )
    red_alone = already_red_alone(named)
    unchecked = frozenset(unisolable) | frozenset(red_alone)
    artefacts: list[str] = []
    decided = 0

    def observe(mutation: Mutation) -> None:
        nonlocal decided
        passing = [
            observer
            for observer in mutation.observers
            if observer not in unchecked and not run_alone(observer)
        ]
        decided += sum(1 for observer in mutation.observers if observer not in unchecked)
        artefacts.extend(f"{mutation.name}: {observer}" for observer in passing)
        if passing:
            verdict = "ARTEFACT"
        elif all(observer in unchecked for observer in mutation.observers):
            verdict = "unchecked"
        else:
            verdict = "isolated"
        print(f"  {verdict:<9} {mutation.name}", flush=True)

    interrupted = each_mutation(mutations, observe)
    if interrupted is not None:
        return interrupted

    # Observer claims rather than entries, because an entry naming twelve tests
    # makes twelve claims and one of them can be an artefact while the other
    # eleven hold. Counting entries would answer a question nobody asked and read
    # as a far worse result than it is.
    print(f"\n{decided - len(artefacts)} of {decided} observer claims hold in isolation.")
    for observer in red_alone:
        print(
            f"  UNCHECKED: {observer} fails alone against the unmutated tree, so its "
            "failing alone under a mutation says nothing about the mutation. That is "
            "order dependence in the other direction and worth its own look.",
            file=sys.stderr,
        )
    for observer in unisolable:
        print(
            f"  UNCHECKED: {observer} names no test - a module that could not be imported, "
            "or a setUpModule that raised - so selecting it runs the whole module and "
            "there is no isolation to run.",
            file=sys.stderr,
        )
    for artefact in artefacts:
        print(f"  INTERFERENCE ARTEFACT: {artefact}", file=sys.stderr)
    if artefacts:
        print(
            "\nEach of those tests fails with the entry applied when its module runs and "
            "passes when it runs alone, so what the entry names as observing the defect is "
            "a sibling's leftovers rather than that test. The mutation may well still be "
            "caught by something else in the module - re-derive the set with "
            "`--derive-mapping --only <name>` and read what is left, and look for shared "
            "class or module state in the module holding it.\n\nBefore any of that, "
            "confirm nothing else was writing this tree while the run was going. A second "
            "process editing a target file produces artefacts that do not reproduce, and "
            "the first measurement taken with this mode reported six that way. `git diff` "
            "over the targets afterwards is the check; a run whose tree is byte-identical "
            "before and after is the one to believe.",
            file=sys.stderr,
        )
    if not decided:
        print(
            f"\nNothing was decided: all {len(named)} named observers were unchecked, so "
            "this run concluded nothing about any entry rather than confirming them. An "
            "empty pass over an empty selection reports as a pass, which is why this is "
            "not one.",
            file=sys.stderr,
        )
        return 1
    return 1 if artefacts else 0


def shard(mutations: Sequence[Mutation], index: int, count: int) -> tuple[Mutation, ...]:
    """The `index`th of `count` interleaved slices of `mutations`, counting from one.

    Interleaved rather than contiguous blocks: the partition is then one expression
    to get right rather than two bounds to keep in step, and there is nothing for a
    contiguous split to balance, because every entry here costs the same - one whole
    suite run. The mode this exists for shards cleanly for the same reason (#285):
    every entry runs the same thing, so there is no ordering, no accumulation and no
    cross-entry state, and one runner per shard means no two of them are mutating
    one tree.

    This expression is covered by the entry `a shard of the table drops an entry`
    above, which replaces it with the off-by-one and requires
    `test_mutation_gate_shards` to notice: a later reader can delete that entry and
    watch the four tests it names go quiet, which is what makes the claim checkable
    rather than a note saying this is checked.
    """
    return tuple(mutations[index - 1 :: count])


def check_shards(mutations: Sequence[Mutation], count: int) -> None:
    """Refuse unless the `count` slices hold every entry exactly once.

    The check on the sharding, and it has to run before anything is applied,
    because a slice that drops entries is invisible from the run that took it: the
    leg counts its verdicts against the entries it held, so it prints `N of N` over
    a smaller N and passes. The only thing that disagrees is the table.

    Counted *and* compared as a set, which is two checks rather than one written
    twice. A sum against `len(mutations)` catches a drop and catches a duplicate,
    but not one of each - two shards overlapping while a third entry falls out
    comes to the same total, and the entry nobody applied is the one whose mapping
    nobody checked.

    What it cannot see is how many legs actually ran: it checks the slices `1..M`
    of the count it was given, and a matrix that ran three of them is not a
    question it was asked. That belongs to the workflow, where the divisor is
    `strategy.job-total` so a leg cannot disagree with it, and to
    `test_mutation_gate_shards.WorkflowShardsTest`.
    """
    held = Counter(
        entry.name for index in range(1, count + 1) for entry in shard(mutations, index, count)
    )
    total = sum(held.values())
    missing = [entry.name for entry in mutations if not held[entry.name]]
    twice = sorted(name for name, times in held.items() if times > 1)
    if not missing and not twice:
        return

    def few(names: Sequence[str]) -> str:
        listed = ", ".join(names[:3])
        return listed if len(names) <= 3 else f"{listed} and {len(names) - 3} more"

    raise SystemExit(
        f"the {count} shards hold {total} entries between them and this table has "
        f"{len(mutations)}, so they are not a partition of it"
        + (f"; no shard holds {few(missing)}" if missing else "")
        + (f"; more than one shard holds {few(twice)}" if twice else "")
        + ". A shard that drops an entry reports `N of N` over a smaller N and reads as a "
        "pass, and a duplicate pays for a drop in the total - so the union is compared as "
        "a set rather than only summed."
    )


def shard_argument(specification: str) -> tuple[int, int]:
    """`N/M` as (index, count), refusing anything that would run a slice nobody chose.

    Refused rather than defaulted, because every wrong form is quiet. `M` alone and
    `0/M` run an empty slice, and every mode here reports an empty table as a pass.
    `5/4` is worse than empty: `MUTATIONS[4::4]` is a real slice of real entries
    that overlaps shard 1 and misses three quarters of the table, and
    `check_shards` cannot see it - the four slices it reconciles are the four the
    count names, not the fifth the run took.
    """
    index, separator, count = specification.partition("/")
    if not separator or not index.isdigit() or not count.isdigit():
        raise SystemExit(
            f"--shard takes N/M, the Nth of M slices of the table: {specification!r} is "
            "neither, and a wrong one runs a slice nobody chose over a table that reports "
            "an empty selection as a pass."
        )
    number, total = int(index), int(count)
    if not 1 <= number <= total:
        raise SystemExit(
            f"--shard {specification} asks for shard {number} of {total}, and the shards "
            f"run from 1 to {total}. An index outside them is still a slice of real "
            "entries - it overlaps another shard and misses most of the table - and the "
            "reconciliation cannot see it, because it is not one of the slices it checks."
        )
    return number, total


def sharded(mutations: Sequence[Mutation], specification: str) -> tuple[tuple[Mutation, ...], str]:
    """One shard of `mutations`, and the sentence reconciling it with all of them."""
    index, count = shard_argument(specification)
    check_shards(mutations, count)
    slice_ = shard(mutations, index, count)
    return slice_, (
        f"That was shard {index} of {count}: {len(slice_)} of {len(mutations)} entries. "
        f"The {count} shards hold all {len(mutations)} between them, each in exactly one."
    )


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
        "--status",
        action="store_true",
        help="report whether a mutation is applied in this tree, non-zero while one is",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="with --status, print the record so a caller can read the pid",
    )
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
    parser.add_argument(
        "--isolate",
        action="store_true",
        help="re-run each entry's observers one at a time and refuse one that passes alone",
    )
    parser.add_argument(
        "--shard",
        metavar="N/M",
        help="run the Nth of M interleaved slices of the entries, one runner per shard",
    )
    arguments = parser.parse_args(argv)
    # Refused rather than ordered, unlike the two modes above it, because the issue
    # that asked for this proposed it as `--verify-mapping --isolate` - so that is
    # the command an operator will type, and silently running one of the two would
    # answer a question they did not ask while printing a count that reads like an
    # answer to it.
    if arguments.isolate and (arguments.derive_mapping or arguments.verify_mapping):
        raise SystemExit(
            "--isolate is its own mode rather than a modifier of the other two. It runs "
            "one test per subprocess where they run a whole suite per entry, which is "
            "minutes against hours, and it answers a different question: whether each "
            "named test fails by itself, not whether the named set matches what the suite "
            "reported. Run them separately."
        )
    mutations = selected(arguments.only)
    # Before `--list`, before the recovery record and before the pre-check: a slice
    # that is not a partition means nothing this run prints can be believed, and the
    # cheapest place to say so is with the tree untouched.
    reconciliation = ""
    if arguments.shard:
        mutations, reconciliation = sharded(mutations, arguments.shard)

    if arguments.list:
        for mutation in mutations:
            # The observers on their own lines: a full test name runs to about a
            # hundred characters, so joining three of them onto the entry's line
            # puts the escape - the reason the entry exists - past any width a
            # terminal has.
            print(f"{mutation.name:<38} {mutation.module:<32} {mutation.escaped_as}")
            for observer in mutation.observers:
                print(f"    observed by {observer}")
        return 0

    if arguments.status:
        return status(as_json=arguments.json)

    if recover():
        return 1

    # Before the pre-check below, so that refusal is unreachable for this cause.
    # A child reading an installed release of a branch that adds functions fails
    # the suite, and the message would then send the reader to tests that pass
    # when run directly - correct about the suite, wrong about the suspect.
    if check_import_path():
        return 1

    # The whole table rather than the selection, because the guard inside the suite
    # reads the whole table: excusing only `--only`'s entries would leave the child
    # red over an entry this run is not touching, and every derived set would then
    # name the guard's own module alongside the real observers.
    with excusing_unmapped(MUTATIONS if arguments.derive_mapping else ()):
        baseline = run_suite()
        if not baseline.passed:
            # Named, because the reader is not always looking at the failure: a guard
            # over the table below fails this run while every test in the module they
            # are editing passes, and an anonymous refusal sends them there.
            blamed = (
                f"these failed: {', '.join(baseline.observers)}"
                if baseline.observers
                else "the run named no failing test, so it did not fail inside one - run the "
                "suite directly and read what it printed"
            )
            print(
                "The suite is already failing, so nothing can be concluded about any "
                f"mutation. Fix that first - {blamed}.",
                file=sys.stderr,
            )
            return 1

        if arguments.derive_mapping:
            code = derive_mapping(mutations)
        elif arguments.verify_mapping:
            code = verify_mapping(mutations)
        elif arguments.isolate:
            code = isolate_mapping(mutations)
        else:
            code = sweep(mutations)
        if reconciliation:
            # After the mode's own count rather than before it, because that count is
            # over the shard and reads exactly like one over the table: `47 of 47
            # entries name what observes them` is what a leg prints and what the whole
            # check prints, and only this line beside it says which.
            print(reconciliation)
        return code


if __name__ == "__main__":
    raise SystemExit(main())
