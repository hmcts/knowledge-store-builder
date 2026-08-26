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

    python3 tests/mutation_gate.py          # all mutations
    python3 tests/mutation_gate.py --list   # names only

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
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
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
    """One real defect, and the escape it represents."""

    name: str
    module: str
    find: str
    replace: str
    escaped_as: str


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
    ),
    Mutation(
        "stale counterpart no longer named",
        "record_clustering.py",
        "        disagreement = counterpart_disagreement(config.GRAPH_PATH, (communities, clustered))",
        '        disagreement = ""',
        "the same operator had to diff two graph files by hand to find that the "
        "record described a stale leftover; every count in it reconciled",
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
    ),
    Mutation(
        "summaries remap no longer names a stale graph",
        "build_community_summaries.py",
        '    print(_graph_disagreement(_membership({"nodes": nodes})), end="", file=sys.stderr)',
        "    pass",
        "the second call site of the same warning, and the one that rewrites the "
        "committed summaries file - the snapshot half being covered is exactly how "
        "a half-wired check reads as done",
    ),
    Mutation(
        "explorer built from a stale graph in silence",
        "build_explorer.py",
        '    print(\n        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "explorer.html"),\n        end="",\n        file=sys.stderr,\n    )',
        "    pass",
        "the page is tracked and ships to readers who have no graph and no CLI to "
        "check it against; an operator hit the same shape when a refresh embedded a "
        "stale semantic layer beside a newly built graph and every gate passed",
    ),
    Mutation(
        "deep dives built from a stale graph in silence",
        "build_deep_dives.py",
        '    print(\n        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "dives.json"),\n        end="",\n        file=sys.stderr,\n    )',
        "    pass",
        "dives.json is a committed artefact, so a stale read is published rather than merely reported",
    ),
    Mutation(
        "semantic layer built from a stale graph in silence",
        "build_semantic_index.py",
        '    print(\n        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "the semantic layer"),\n        end="",\n        file=sys.stderr,\n    )',
        "    pass",
        "the layer is committed and the explorer page embeds it, so the stale read reaches the page twice over",
    ),
    Mutation(
        "the estate check runs against a stale graph in silence",
        "build_community_summaries.py",
        '    print(\n        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "the estate check"),\n        end="",\n        file=sys.stderr,\n    )',
        "    pass",
        "the strongest form of the class: a truthfulness gate reading the wrong artefact passes on the wrong data, and its silence then licenses a claim about something it never looked at",
    ),
    Mutation(
        "upward write paths accepted again",
        "io.py",
        '    if any(part == ".." for part in Path(path).parts):',
        "    if False:",
        "reported by SonarCloud as pythonsecurity:S8707 on write_json: a path "
        "assembled from CLI arguments reached write_text unvalidated, so whatever "
        "built those arguments chose where the process wrote",
    ),
    Mutation(
        "the write guard applied to the reads",
        "io.py",
        '    if not path.exists():\n        return default\n    if path.suffix == ".gz":',
        "    checked_write_target(path)\n    if not path.exists():\n"
        '        return default\n    if path.suffix == ".gz":',
        "the one-line answer to the same rule's second report - S8707 on read_json, "
        "code-scanning alert 53 - which this library rejects: the guard is in the "
        "module already, so calling it on a read is one line, and it then refuses "
        "every relative path an operator types from a subdirectory. Confining reads "
        "instead of suppressing was measured twice on the write side, at 48 and 4 "
        "failing tests, and nothing on the read path observed either until "
        "test_read_path_policy; with this applied the whole suite failed only there",
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
    ),
    Mutation(
        "layer merge keeps an edge whose endpoint is in neither layer",
        "merge_layers.py",
        '        if str(moved["source"]) not in by_id or str(moved["target"]) not in by_id:',
        "        if False:",
        "guessing at a dangling endpoint is how a concatenation invents "
        "relationships; dropping it silently would be the same defect one step on, "
        "which is why the count is asserted too",
    ),
    Mutation(
        "layer merge accepts an empty layer",
        "merge_layers.py",
        "    if not ast_nodes or not sem_nodes:",
        "    if False:",
        "every stage in this library that shipped doing nothing did so with a "
        "passing suite, and an empty input layer merges to a smaller graph that "
        "looks like a successful run",
    ),
    Mutation(
        "most-connected report unwired",
        "status.py",
        "    _report_central(arguments.central)",
        "    pass",
        "#112: reporting through the function while nothing drives the CLI is the "
        "most repeated escape in this repository, and three existing entries in this "
        "module are the same shape",
    ),
    Mutation(
        "most-connected loses its edge-key fallback",
        "graph_files.py",
        "    if not found:",
        "    if False:",
        "graphify writes `links` in node-link JSON and `edges` in its extract files, "
        "so reading one key silently ranks nothing on half the artefacts this is "
        "pointed at - indistinguishable from a graph with no edges",
    ),
    Mutation(
        "estate check stops matching name segments",
        "build_community_summaries.py",
        "            if len(normalised) >= MIN_SEGMENT_MATCH and normalised in segments:",
        "            if False:",
        "#179: a whole-label match reported `NgRx` absent while the estate held it "
        "under eight scoped package names across 228 labels - a check that fires on "
        "an entire ecosystem's naming convention gets switched off",
    ),
    Mutation(
        "the segment floor is removed",
        "build_community_summaries.py",
        "MIN_SEGMENT_MATCH = 3",
        "MIN_SEGMENT_MATCH = 1",
        "without a floor the looser match becomes a substring match in effect, and "
        "a two-character segment would corroborate a two-character claim - the "
        "false-negative direction, which fails reassuringly",
    ),
    Mutation(
        "the stem basis option stops affecting ids",
        "merge_chunks.py",
        "    stem = spec_stem(basis, keep_extension=keep_extension)",
        "    stem = spec_stem(basis)",
        "#115: threading an option through and having it change nothing is the "
        "wiring escape this gate already records four times, and here it would "
        "leave a store believing it had adopted the extension basis",
    ),
    Mutation(
        "the migration cost stops being counted",
        "merge_chunks.py",
        "    if spec_stem(basis, keep_extension=not keep_extension) != stem:",
        "    if False:",
        "#115 can only be decided on each estate's own number; the issue's figures "
        "are one estate's, and a cost nobody can measure locally is one nobody acts "
        "on",
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
    ),
    Mutation(
        "a cross-repository AST edge is attributed to one side",
        "merge_layers.py",
        "        if source_repo and target_repo and source_repo != target_repo:",
        "        if False:",
        "the fix is exact only while both endpoints belong to one repository; "
        "attributing an edge that spans two is the guess the rewrite exists to "
        "avoid",
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
    ),
    Mutation(
        "summaries with no snapshot entry silently excluded",
        "build_community_summaries.py",
        '        "unsnapshotted": sorted(wanted - set(snap_sets), key=_by_id),',
        '        "unsnapshotted": [],',
        "the `if cid in snapshot` shape: prose that can be neither checked nor re-keyed - a "
        "remap cannot even withdraw it - dropped from the population, after which the count "
        "reads as though it had covered everything",
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
    ),
    Mutation(
        "an id-space mismatch reported as moved membership",
        "build_community_summaries.py",
        "    if (graph_share >= NAMESPACED_SHARE) == (snapshot_share >= NAMESPACED_SHARE):",
        "    if True:",
        "a first implementation of this check elsewhere reported 58 communities adrift of "
        "which 57 were not, because the snapshot's ids were bare and the graph's carried a "
        "`<repo>::` prefix; naming it is what keeps the fix from being a looser comparison",
    ),
    Mutation(
        "status leaves its summary count to be read as a verdict",
        "status.py",
        "    pointer = snapshot_pointer()",
        '    pointer = ""',
        "the count is identical whether the prose still describes its community or not, and an "
        "operator read exactly that line as healthy; `status` cannot read the graph, so naming "
        "the blind spot is the only honest thing it can do there",
    ),
    Mutation(
        "declared boundary never reaches the manifest",
        "build_knowledge_context.py",
        "        *boundary.manifest_section(boundary.read()),",
        "        *[],",
        "the unwired-check class this library has shipped twice - the parse works, "
        "the rendering works, and the committed artefact a reader opens says none of "
        "it, which is indistinguishable from an estate that declared nothing",
    ),
    Mutation(
        "store stops saying it does not claim completeness",
        "boundary.py",
        '    return lines + [NO_COMPLETENESS, ""]',
        "    return lines",
        "a declaration that reads as `this is all of it` is a new false claim "
        "replacing the old silent one; the estate that prompted this had enumerated "
        "its hosts and was still hunting services with no locatable repository",
    ),
    Mutation(
        "declared repository with no ruling vanishes from the manifest",
        "boundary.py",
        "    subjects = sorted({*declared.rulings, *declared.snapshots, *declared.aliases.values()})",
        "    subjects = sorted(declared.rulings)",
        "written this way first, and found by re-reading the artefact rather than by a "
        "test: a repository declared only by a snapshot date or only by an alias parsed "
        "cleanly, was counted in the status summary, and reached no reader at all",
    ),
    Mutation(
        "status no longer says the boundary is undeclared",
        "status.py",
        "    _report_boundary(recorded)",
        "    pass",
        "silence is the state every store starts in, so a report that speaks only "
        "for the configured case never reaches the stores that most need telling "
        "what their own absences mean",
    ),
    Mutation(
        "off-host name stops resolving to the repository held",
        "boundary.py",
        "        target = aliases.get(name, name)",
        "        target = name",
        "the false absence the declaration exists to remove, reintroduced inside it: "
        "a ruling written under the off-host name keys itself under a name no store "
        "holds, so `status` reports a held repository as missing",
    ),
    Mutation(
        "declaration stops being reconciled against disk",
        "status.py",
        "    disagreements = boundary.reconciliation(declared, set(recorded))",
        "    disagreements = {k: [] for k in ('active_absent', 'ruled_out_held', 'alias_absent')}",
        "a declaration nothing checks is a second artefact that can be quietly "
        "wrong, and a repository ruled live and not held is the exact shape of the "
        "published finding that was drawn honestly and was false",
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
    ),
    Mutation(
        "progress estimated with no plan to measure against",
        "chunk_status.py",
        "    if not plan:",
        "    if False:",
        "the plan is the only map from chunk number to file list, so without it "
        "there is no denominator and nothing to name as missing. Reporting `0 of 0` "
        "reads as a finished fan-out",
    ),
    Mutation(
        "content set written empty",
        "build_content_set.py",
        "    if not content:",
        "    if False:",
        "an empty path list makes every search over it return no matches, and no "
        "matches reads as a confident answer about the estate rather than as a "
        "missing detect result - the exact failure mode the artefact exists to end",
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
    ),
    Mutation(
        "noise figure claimed from a tree nobody measured",
        "build_content_set.py",
        '    if not tree:\n        return {"measured": False}',
        '    if False:\n        return {"measured": False}',
        "a sparse clone has no corpus, so the tree count is zero and zero renders "
        "as 'no noise' - the most flattering reading of the least measured case, on "
        "the one artefact whose whole purpose is to say the tree is mostly noise",
    ),
    Mutation(
        "content files counted as noise",
        "content_set.py",
        "        if path in held:",
        "        if path in bearing:",
        "written and caught during #213: `bearing` holds directories, so no file "
        "path is ever in it and every content file was tallied as noise. The "
        "percentage came out higher, which is the direction that gets believed",
    ),
    Mutation(
        "noise attributed to the file's own directory",
        "content_set.py",
        "    for depth in range(2, len(parts)):",
        "    for depth in range(len(parts) - 1, len(parts)):",
        "the shallowest contentless directory is the finding; the deepest one is "
        "thousands of content-hash directories holding one file each, which turns a "
        "single dominant noise source into an unreadable list",
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
    ),
    Mutation(
        "directory symlinks walked again",
        "content_set.py",
        "    for directory, _, names in os.walk(corpus, followlinks=False):",
        "    for directory, _, names in os.walk(corpus, followlinks=True):",
        "a followed link reports the same files twice under two paths, inflating "
        "the tree count that is the denominator of every percentage this stage "
        "prints; symlink duplication has already produced three wrong figures here",
    ),
    Mutation(
        "content set report unwired",
        "status.py",
        "    _report_content_set()",
        "    pass",
        "the fourth instance of this repository's most repeated escape, and #213 "
        "itself is the consequence: the pipeline knew what it considered content "
        "and nothing said so, so every consumer re-derived it badly",
    ),
    Mutation(
        "a set that cannot be judged reported as stale",
        "status.py",
        '"current": None if not config.DETECT_PATH.is_file() else recorded == here,',
        '"current": recorded == here,',
        "detect is a graphify working file a store need not keep, so a store "
        "without one would be told its content set was stale on every single run - "
        "which is how a real warning stops being read",
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
    ),
    Mutation(
        "an unmatched cut rule reported as a rule with nothing to do",
        "size_cuts.py",
        "            if not sizing.hits.get(rule.line):",
        "            if False:",
        "#116: a glob written for a path form the graph does not use selects nothing and "
        "reads exactly like a clean run - the same silence a `match` rule that selected "
        "no repository already has a reporter for, one artefact along",
    ),
    Mutation(
        "a cut that strands every node it keeps reported as a smaller graph",
        "size_cuts.py",
        "        elif not edges:",
        "        elif False:",
        "#116: mass without structure is the failure the issue found counter-intuitive, "
        "and clustering, centrality and summaries are all degree-driven, so the estate "
        "prose is generated from a layer nothing joins",
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
    ),
    Mutation(
        "the cut sizing measured and discarded",
        "size_cuts.py",
        "        telemetry.record(measurements(sizing, cuts))",
        "        measurements(sizing, cuts)",
        "#116 over #154: without the record a candidate's counts live in one afternoon's "
        "scrollback, so the next refresh cannot say whether the cut it applied still "
        "keeps what it kept - the wiring escape this gate already records six times",
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
    ),
    Mutation(
        "provenance closure no longer checked",
        "merge_inputs.py",
        "        ungrounded=tuple(sorted(on_disk - set(recorded))),",
        "        ungrounded=(),",
        "the sharp half of the same report: provenance records what was read, and a "
        "glob-driven merge can read something it has no entry for, so an answer citing "
        "those nodes cannot name the commit they were read at",
    ),
    Mutation(
        "an undeclared input is counted but not named",
        "merge_inputs.py",
        "    if report.undeclared:",
        "    if False:",
        "the divergence the issue asked to have named rather than counted - an operator "
        "given a number knows something is wrong and not which repository to look at",
    ),
    Mutation(
        "an empty merge glob reads as a clean run",
        "merge_inputs.py",
        "    if not report.inputs or report.declared is None:\n        return 1",
        "    if False:\n        return 1",
        "the vacuity this library keeps meeting: a check over an empty set has nothing "
        "to report and exits 0, so a build that produced no per-repository graphs at "
        "all passes the gate meant to notice it",
    ),
    Mutation(
        "status stops reporting the merge inputs",
        "status.py",
        "    for line in merge_inputs.lines(merge_inputs.reconcile(), limit=5):\n        print(line)",
        "    return",
        "the operator who reported this was reading `status`, which described an "
        "entirely healthy store over a graph it could not account for; a stage nobody "
        "runs is how a reconciliation ships and changes nothing",
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
    ),
    Mutation(
        "graph-report check unwired",
        "status.py",
        "    _report_graph_report(arguments.verify_graph)",
        "    pass",
        "behaviour tested through the function; nothing drove `main()`",
    ),
    Mutation(
        "contentless check unwired",
        "status.py",
        "    _report_contentless(nodes)",
        "    pass",
        "same escape as above, three days later, in a different check",
    ),
    Mutation(
        "manifest scope statement unwired",
        "build_knowledge_context.py",
        "            *scope_statement(len(repository_dirs)),",
        "",
        "the statement was tested; that it reached the written file was not",
    ),
    Mutation(
        "precision floor defaults to off",
        "build_community_summaries.py",
        "DEFAULT_PRECISION = 0.2",
        "DEFAULT_PRECISION = 0.0",
        "every test passed the floor explicitly, so the shipped default was unheld",
    ),
    Mutation(
        "symlink exclusions ignored",
        "status.py",
        "        elif ignored(path):",
        "        elif False:",
        "a check that could not see its own mitigation, reported by an operator",
    ),
    Mutation(
        "distinct-target count dropped",
        "status.py",
        'found["targets"] = len(targets)',
        'found["targets"] = 0',
        "the test drove a stub, so it pinned the wording and not the computation",
    ),
    Mutation(
        "verify finding renamed back to 'unsupported'",
        "build_community_summaries.py",
        '"  [not in digest] community',
        '"  [unsupported] community',
        "a label that claimed 98% more than it measured, so the gate got ignored",
    ),
    Mutation(
        "estate pass never narrows anything",
        "build_community_summaries.py",
        "            if normalised in estate:\n                continue",
        "            if True:\n                continue",
        "the wider check exists to narrow; a pass-through would look identical. "
        "Retargeted when #179 replaced the comprehension with an explicit loop - "
        "the gate refused to run rather than quietly stop testing this",
    ),
    Mutation(
        "partitioner check unwired",
        "status.py",
        "    _report_clustering()",
        "    pass",
        "the third instance of this escape: reported through the function, never through main()",
    ),
    Mutation(
        "unrecorded partitioner defaults to a partitioner",
        "record_clustering.py",
        "    return named if named in PARTITIONER_NAMES else None",
        "    return named or LOUVAIN",
        "an absent measurement reading as a clean result - shipped twice in one week",
    ),
    Mutation(
        "a broken graspologic reported as the fallback",
        "record_clustering.py",
        '        return None, f"importing graspologic raised',
        '        return LOUVAIN, f"importing graspologic raised',
        "graphify does not catch that import error, so Louvain there is a guess, not a probe",
    ),
    Mutation(
        "seed state not recorded",
        "record_clustering.py",
        '"hash_randomised": hash_randomisation(),',
        "",
        "a store that clustered unseeded then looks identical to one that did not",
    ),
    Mutation(
        "unpinned hashes not reported at record time",
        "record_clustering.py",
        "    if hash_randomisation():",
        "    if False:",
        "the record would say unseeded while the run said nothing",
    ),
    Mutation(
        "record does not say which file it described",
        "record_clustering.py",
        '"described": config.GRAPH_PATH.name,',
        "",
        "a reader then guesses which of a store's two graph files it refers to",
    ),
    Mutation(
        "absolute-path check unwired",
        "status.py",
        "    _report_absolute_paths(arguments.paths)",
        "    pass",
        "#176: the fifth instance of this repository's most repeated escape - four "
        "entries above are the same shape, in the same module, and each was written "
        "after the previous one was fixed",
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
    ),
    Mutation(
        "unreadable tracked files reported as a clean store",
        "status.py",
        '    if not scan["files"]:',
        "    if False:",
        "the '0 checked, none dangling' defect the corpus-citation check in this same "
        "module already shipped once - a measurement of nothing paired with a clean "
        "verdict, which reads as a pass",
    ),
    Mutation(
        "absolute paths lost at a read-block boundary",
        "status.py",
        "        if match.end() > end:",
        "        if False:",
        "the scan streams because a store's tracked artefacts run to gigabytes "
        "decompressed, and a path cut in half by a block boundary is the silent half "
        "of that trade: no count can show what it failed to see",
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
    ),
    Mutation(
        "entity names matched against the whole path-qualified id",
        "measure_dangling_endpoints.py",
        '    return str(value).strip().rsplit("::", 1)[-1].casefold()',
        "    return str(value).strip().casefold()",
        "#162, and the shape this codebase has shipped most often: a name matched "
        "against a path-qualified id cannot match, so the rate comes back a clean "
        "0.0% and reads as an estate with nothing to fix",
    ),
    Mutation(
        "recovery stops looking at local_id",
        "measure_dangling_endpoints.py",
        '    keys = {entity_name(node.get("id")), entity_name(node.get("local_id"))}',
        '    keys = {entity_name(node.get("id"))}',
        "#162's whole claim is that local_id carries the entity name; without it "
        "the estate whose endpoints dangle because a chunk named an id defined in "
        "another chunk measures zero, which is the answer that closes the issue",
    ),
    Mutation(
        "an empty walk reports a clean rate",
        "measure_dangling_endpoints.py",
        '    return (graphs, "") if graphs else ([], _no_graphs_message(repositories))',
        '    return graphs, ""',
        "#162: every stage in this library that shipped doing nothing did so with "
        "a passing suite, and '0 dangling endpoints' over a walk that read no "
        "files is indistinguishable from an estate that has none",
    ),
    Mutation(
        "graphs that could not be measured reported as a measured zero",
        "measure_dangling_endpoints.py",
        "    if not any(m.measurable for m in measurements):",
        "    if False:",
        "#162: a graph with no edges has no endpoints to dangle, so every count "
        "is zero and the rate is undefined - printed as a result it says the "
        "store is clean",
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
    ),
    Mutation(
        "the same graph measured twice from one clone",
        "measure_dangling_endpoints.py",
        "                found.append(candidate)\n                break",
        "                found.append(candidate)",
        "#162: a clone holding both graph forms would have every count doubled "
        "while the rate stayed put - an error that reconciles internally, which "
        "is the kind nobody finds",
    ),
    Mutation(
        "the report stops naming the artefacts it read",
        "measure_dangling_endpoints.py",
        "        *_read_lines(measurements),",
        "",
        "#162: a rate measured downstream of a store's own fix is not a rate, and "
        "the only way a reader can tell which it got is the list of files. A "
        "check's silence licenses a claim only about the artefact it read",
    ),
    Mutation(
        "named endpoints printed in set order",
        "measure_dangling_endpoints.py",
        "    result.classified = {kind: sorted(found) for kind, found in buckets.items()}",
        "    result.classified = {kind: list(found) for kind, found in buckets.items()}",
        "#162: two runs on the same graph must be byte-identical, and hash "
        "randomisation makes an unsorted list invisible until someone diffs two "
        "builds from different processes",
    ),
    Mutation(
        "retained failure double-counted",
        "sync_repositories.py",
        "total = len({*entries, *(name for name, _ in failures)})",
        "total = len(entries) + len(failures)",
        "shipped in v0.11.5; found by an estate, not by this suite",
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
    ),
    Mutation(
        "the join cardinality measured and discarded",
        "build_explorer.py",
        "    telemetry.record(page_measurements(graph, entries, edges, size_bytes))",
        "    page_measurements(graph, entries, edges, size_bytes)",
        "#154 over #149: the join report refuses zero and prints the rate, and the "
        "half-dead case is neither of those. Without a record the rate reaches a terminal "
        "and is gone, which is how a join that should have been three times larger read "
        "as a working join on a sparse estate",
    ),
    Mutation(
        "the indexed inventory measured and discarded",
        "build_intent_index.py",
        "    telemetry.record(summarise(index, commits_seen, report))",
        "    summarise(index, commits_seen, report)",
        "#154: a corpus inventory that collapsed reported a plausible smaller number and "
        "read as a smaller estate; nothing but its predecessor contradicts it",
    ),
    Mutation(
        "the record never reaches an operator",
        "status.py",
        "    _report_telemetry()\n\n    _report_graph_report",
        "    _report_graph_report",
        "#154: reporting through a function while nothing drives the CLI is the most "
        "repeated escape in this repository, and `status` alone accounts for three "
        "existing entries here",
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
    ),
    Mutation(
        "the built side stops being subtracted",
        "report_ingestion_gaps.py",
        "        if coordinate in consumed and coordinate not in evidence.built:",
        "        if coordinate in consumed:",
        "the whole stage reduced to `list your internal dependencies`, which is a "
        "list nobody can act on; it still ranks, still classifies and still prints a "
        "confident table, with the estate's own artefacts at the top of it",
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
    ),
    Mutation(
        "an off-host alias becomes a repository to ingest",
        "report_ingestion_gaps.py",
        "        name = aliases.get(provider, provider)",
        "        name = provider",
        "a false absence invented inside the report whose subject is false absence: "
        "a module consumed under the off-host name of a repository the store already "
        "holds is reported as something to go and find",
    ),
    Mutation(
        "an unscoped package makes every public dependency internal",
        "report_ingestion_gaps.py",
        "    counts = Counter(namespace_of(coordinate.group) for coordinate in built if coordinate.group)",
        "    counts = Counter(namespace_of(coordinate.group) for coordinate in built)",
        "one unscoped npm package published by the estate turns the empty namespace "
        "into an internal one, after which the whole of npm is a candidate to ingest - "
        "a check that cannot fire on Maven and fires on everything under npm",
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
    ),
    Mutation(
        "the refusal to resolve a coordinate stops reaching the reader",
        "report_ingestion_gaps.py",
        '    lines += ["", FOOTER]',
        "    lines += []",
        "an operator who is not told a coordinate is unresolved reads the namespace as "
        "a repository name and searches the forge for it, which returns nothing for an "
        "artefact published to a binary repository and rate-limits while doing so",
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
    ),
    Mutation(
        "the record a SIGKILL cannot destroy is never written",
        "tests/mutation_gate.py",
        '    RECOVERY_PATH.write_bytes(mutation.module.encode("utf-8") ' + '+ b"\\n" + original)',
        "    pass",
        "#227: no signal handler covers SIGKILL, so the only thing that can put a file "
        "back is bytes already on disk when the process died. Without them the tree keeps "
        "the mutation and the next suite run in it reports real-looking failures in real code",
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
    ),
    Mutation(
        "a used recovery record is left on disk",
        "tests/mutation_gate.py",
        "    RECOVERY_PATH.unlink(" + "missing_ok=True)",
        "    pass",
        "#227: a record that outlives its restore describes a file that is no longer "
        "mutated, so the next run overwrites a healthy file with older bytes - a stale "
        "artefact read as current, the same class as the leftover graph refusals above",
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
    ),
    Mutation(
        "the build skill's library floor is compared in the direction that cannot fire",
        "tests/test_documented_stages.py",
        "    if triple(declared) < triple(tag):",
        "    if triple(declared) > triple(tag):",
        "this restores the comparison that shipped. It read the declared minimum against "
        "the INSTALLED version, and `tests.yml` never runs on a tag, so that version is "
        "always a devN derived from the previous release - the check could not fail for a "
        "floor that was behind and failed for one that was ahead. The build skill sat at "
        "0.11.6 while documenting ten stages that release does not have, green throughout: "
        "a comparison pointed at the quantity that cannot be wrong",
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
    ),
    Mutation(
        "bytecode writing is disabled with a value CPython ignores",
        "tests/mutation_gate.py",
        '        env={**os.environ, "PYTHONDONT' + 'WRITEBYTECODE": "1"},',
        '        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "0"},',
        '#228: the variable is read as a flag, so "0" and "" both leave bytecode '
        "writing on while the name sits in the environment looking correct - the defect "
        "class this gate exists for, a check present and doing nothing",
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
    ),
    Mutation(
        "the second interpolation dialect dropped",
        "deploy_values.py",
        "    return _DOLLAR_INTERPOLATION.sub(PLACEHOLDER, stripped)",
        "    return stripped",
        "#88: two deployment layouts interpolate differently, `{{ }}` and `${ }`, and the "
        "second going unstripped publishes the variable names the first withholds. Nothing "
        "counts a name that was not withheld, so both layouts' output reads as healthy",
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
    ),
    Mutation(
        "the second deployment layout unwired",
        "build_deployments.py",
        "    facts = _merged_facts(found, reading, tally.flux)",
        "    facts = _values_facts(found)",
        "#88: the stage read one layout, so a Kustomize/Flux repository returned exactly the "
        "pair count the run returned without it. Every test of the reader itself still passes "
        "with the call site gone, which is the class of escape this gate exists for",
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
    ),
    Mutation(
        "the base layer dropped from an environment's composition",
        "deploy_flux.py",
        "    ordered = [base] if base else []",
        "    ordered = []",
        "#88: an overlay declares only what it overrides, so a fact built from the patches "
        "alone reports a partial configuration as a whole one - the keys it does carry are "
        "right, which is why nothing looks wrong",
    ),
    Mutation(
        "only the first directory a cluster reconciles is read",
        "deploy_flux.py",
        "        if any(_under(rel, directory) for directory in directories):",
        "        if any(_under(rel, directory) for directory in directories[:1]):",
        "#88: a cluster reconciles an environment overlay and its stack overlays together, so "
        "reading one of them loses limits that are really set and reports nothing missing",
    ),
    Mutation(
        "composed values share the parsed document",
        "deploy_flux.py",
        "            base[key] = copy.deepcopy(value)",
        "            base[key] = value",
        "#88: one base declaration is composed into every environment, so overlaying onto the "
        "parsed document instead of a copy gives the environments composed later the earlier "
        "ones' overrides - both facts stay well-formed and one of them is fiction",
    ),
    Mutation(
        "an unparsable Kustomize/Flux file skipped in silence",
        "build_deployments.py",
        "        if documents is None:\n            unparsed.append(rel)\n            continue",
        "        if documents is None:\n            continue",
        "#88's premise is a silent omission: a file this reader cannot parse is a service "
        "absent from every answer, and a walk that drops it reports the same clean run",
    ),
    Mutation(
        "the layout that contributed nothing says nothing",
        "build_deployments.py",
        "    if flux.releases or flux.kustomizations:",
        "    if True:",
        "#88: the finding is a repository adding no nodes and no message. Counting documents "
        "for a tree that declared none reads as a healthy run, and the sensitivity control "
        "that proves this reader can tell the two apart is the report line itself",
    ),
    Mutation(
        "the chart source dropped from a chart reference",
        "deploy_flux.py",
        '    source = _text(reference.get("name")) if reference.get("kind") in CHART_SOURCE_KINDS else ""',
        '    source = ""',
        "#88 and #122: `sourceRef` is a repository-to-repository dependency nothing else in a "
        "store holds. Losing it leaves the chart name and version on the node, so the fact "
        "reads as fully parsed",
    ),
    Mutation(
        "every fact claims one route",
        "build_deployments.py",
        '            "route": fact.route,',
        '            "route": ROUTE_VALUES,',
        "#88: two layouts emit onto one set of environments, so the route is what makes "
        '"which services reach this environment by which route" answerable. The key is still '
        "present and every node still carries a value, which is how a wrong one survives",
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


def run_suite() -> bool:
    """True when the suite passes. Run as a subprocess so imports are fresh.

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
    """
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "."],
        cwd=ROOT / "tests",
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return completed.returncode == 0


def apply(mutation: Mutation) -> bytes:
    """Apply one mutation, returning the original bytes for restoration.

    Bytes rather than text, and recorded on disk before the file is touched: a
    re-decoded restore is a re-derivation of the original rather than the
    original, and a process that is killed cannot restore anything at all.
    """
    path = target_of(mutation.module)
    original = path.read_bytes()
    source = original.decode("utf-8")
    if mutation.find not in source:
        raise SystemExit(
            f"mutation '{mutation.name}' no longer applies: its target is absent from "
            f"{mutation.module}. Either the code moved - update the mutation - or the "
            "behaviour was removed, in which case decide deliberately rather than "
            "letting this gate quietly stop testing it."
        )
    RECOVERY_PATH.write_bytes(mutation.module.encode("utf-8") + b"\n" + original)
    path.write_bytes(source.replace(mutation.find, mutation.replace, 1).encode("utf-8"))
    return original


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the mutations and exit")
    arguments = parser.parse_args(argv)

    if arguments.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name:<38} {mutation.module:<32} {mutation.escaped_as}")
        return 0

    if recover():
        return 1

    if not run_suite():
        print(
            "The suite is already failing, so nothing can be concluded about any "
            "mutation. Fix that first.",
            file=sys.stderr,
        )
        return 1

    survived = []
    with restoring_on_termination():
        for mutation in MUTATIONS:
            original = apply(mutation)
            try:
                caught = not run_suite()
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
            print(f"  {'caught ' if caught else 'SURVIVED'}  {mutation.name}")
            if not caught:
                survived.append(mutation)

    print(f"\n{len(MUTATIONS) - len(survived)} of {len(MUTATIONS)} mutations caught.")
    for mutation in survived:
        print(f"  SURVIVED: {mutation.name} - {mutation.escaped_as}", file=sys.stderr)
    if survived:
        print(
            "\nA surviving mutation means that behaviour could be removed today with the "
            "suite still green. It is not a curiosity.",
            file=sys.stderr,
        )
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
