"""Community summaries - the GraphRAG indexing step, done at build time.

GraphRAG's core technique is LLM-written summaries of each graph community
at *index* time, so query time needs no LLM at all. This script provides
the deterministic halves of that step; the generation itself runs in
Claude Code (maintainers have a licence; consumers never need one):

  1. knowledgestore summaries extract
       -> knowledge/summaries/communities-input.json
       One digest per significant community (label, size, repositories,
       top nodes, business features, Jira tickets) - the raw material.

  2. In Claude Code: generate 2-4 sentence business summaries for each
     digest, as JSON files of {"<community id>": "<summary>", ...}.

  3. knowledgestore summaries snapshot
       Records community membership before a re-cluster moves the ids.

  4. knowledgestore summaries remap [--carry exact] [--floor 10] [--coverage 0.5]
       After re-clustering, carries a summary onto the new id of the community
       holding exactly the node set it was written about, and withdraws the rest
       to knowledge/summaries/communities-withdrawn.json. Reports retention and
       the withdrawal count together. Refuses on an unclustered graph.
       `--carry overlap [--bar 0.6] [--precision 0.2]` is the older tolerance.

  4a. knowledgestore summaries adrift [--bar 0.6] [--precision 0.2] [--coverage 0.5]
       Whether the snapshot still describes the graph: which committed summaries
       are keyed to a community that no longer holds the members they were
       written about. Exit 1 on drift, 2 when the check could not run.

  5. knowledgestore summaries merge <file.json ...>
       -> knowledge/summaries/communities.json  (committed)
       Validates ids and length bounds, merges over any existing file.

The explorer embeds the merged summaries; Ask answers then include
pre-written prose selected deterministically - no query-time AI.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path


from . import config
from . import graph_files
from . import graph_stream
from . import io
from . import kinds


TOP_NODES = 12
TOP_FEATURES = 5
TOP_TICKETS = 8
MIN_SUMMARY_LEN = 60
MAX_SUMMARY_LEN = 700


def _derived_label(nodes: list[dict], repos: Counter) -> str:
    """Name a community from evidence already in its digest.

    Only communities graphify clustered appear in the labels file, so anything a
    store adds through its own extractor arrives unnamed and falls back to an
    ordinal. `Community 40862` gives the summary author nothing to check an
    inference against and the reader nothing to hold on to.

    Derived from the dominant repository and the highest-degree node — both drawn
    from the graph, so the name stays checkable rather than invented. Returns ""
    when there is neither, because an ordinal is more honest than a wrong name.
    """
    top = next((n["label"] for n in nodes if n.get("label")), "")
    repo = next((r for r, _ in repos.most_common() if r), "")
    if top and repo:
        return f"{repo}: {top}"
    return top or repo


def community_digest(
    community: int, nodes: list[dict], labels: dict, intent: dict, degree: dict
) -> dict:
    """The raw material one community summary is written from."""
    nodes.sort(key=lambda n: -degree[n["id"]])
    repos = Counter(n.get("repo", "") for n in nodes)
    features = [n["label"] for n in nodes if kinds.is_kind(n, kinds.FEATURE)]
    tickets: Counter = Counter()
    for n in nodes[:30]:
        tickets.update((n.get("metadata") or {}).get("tickets") or [])
        entry = intent.get(n.get("repo", ""), {}).get(n.get("source_file") or "")
        if entry:
            tickets.update(dict(list(entry["tickets"].items())[:3]))
    return {
        "id": community,
        "label": (
            labels.get(str(community)) or _derived_label(nodes, repos) or f"Community {community}"
        ),
        "size": len(nodes),
        "repositories": [r for r, _ in repos.most_common(4) if r],
        "top_nodes": [
            # label-less structural nodes (Java package hierarchy) are skipped
            f"{n['label']} ({n.get('source_file') or '?'})"
            for n in nodes[: TOP_NODES * 2]
            if n.get("label")
        ][:TOP_NODES],
        "business_features": features[:TOP_FEATURES],
        "tickets": [t for t, _ in tickets.most_common(TOP_TICKETS)],
    }


def extract() -> int:
    graph = io.load_graph(config.GRAPH_PATH)
    labels = io.load_labels(config.LABELS_PATH)
    intent = io.read_gzip_json_dict(config.INTENT_INDEX_PATH)

    degree: dict[str, int] = defaultdict(int)
    for edge in graph["links"]:
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1

    members: dict[int, list[dict]] = defaultdict(list)
    for node in graph["nodes"]:
        members[node.get("community", -1)].append(node)

    if graph["nodes"] and not any(n.get("repo") for n in graph["nodes"]):
        # Without it every digest still looks well-formed - the right count, top
        # nodes populated - and simply has no repositories, no tickets and no
        # label. That reads as a thin estate rather than a broken precondition,
        # so it is said once, loudly, rather than left to be inferred.
        print(
            f"WARNING: no node in {config.GRAPH_PATH} carries a `repo` attribute. "
            "Digests will have empty `repositories` and no tickets, and any summary "
            "written from them will be guesswork. Rebuild the graph before authoring.",
            file=sys.stderr,
        )

    digests = [
        community_digest(community, nodes, labels, intent, degree)
        for community, nodes in sorted(members.items(), key=lambda kv: -len(kv[1]))
        if len(nodes) >= config.MIN_COMMUNITY_SIZE
    ]

    config.SUMMARIES_INPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Sonar S2083 misfires here: config.SUMMARIES_INPUT_PATH is a module constant derived from
    # configuration, not untrusted input; this is offline build tooling.
    config.SUMMARIES_INPUT_PATH.write_text(  # NOSONAR(S2083)
        json.dumps(digests, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print(f"{len(digests)} community digests -> {config.SUMMARIES_INPUT_PATH}")
    return 0


# The S8707 register. Sonar reports `pythonsecurity:S8707` wherever a path built
# from CLI arguments reaches the filesystem, and this library answers it in two
# ways: a write is validated by `io.checked_write_target`, and a read is
# suppressed on the grounds stated in `merge` below. Every read site cites those
# grounds rather than restating them, so the two cannot drift into two different
# policies. Keep this register complete - `tests/test_read_path_policy.py` fails
# when a module suppresses the rule without appearing here.
#   S8707 policy site: build_community_summaries.py - merge, where the grounds are
#   S8707 policy site: extract_ast.py - read_file_list, citing merge
#   S8707 policy site: chunk_status.py - log_tokens, citing merge
#   S8707 policy site: io.py - every read, citing merge; its two writes are
#     validated by checked_write_target, which is a check rather than grounds
#   S8707 policy site: build_content_set.py - a write, validated the same way
def merge(paths: list[str]) -> int:
    known_ids = {
        str(d["id"]) for d in json.loads(config.SUMMARIES_INPUT_PATH.read_text(encoding="utf-8"))
    }
    merged: dict[str, str] = (
        json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))
        if config.SUMMARIES_PATH.exists()
        else {}
    )
    added, rejected = 0, []
    for path in paths:
        # Sonar S8707, and the grounds the register above points at: reading a
        # caller-supplied path is this maintainer CLI's purpose; it runs offline
        # against a local clone with no privilege boundary to cross. No check is
        # added above it, deliberately - the write-side guard rejects an upward
        # component because no caller in this library needs to climb out of an
        # output path it named, and that argument does not transfer to a read
        # whose entire purpose is to open a path the caller chose.
        batch = json.loads(Path(path).read_text(encoding="utf-8"))  # NOSONAR(S8707)
        for community_id, summary in batch.items():
            summary = " ".join(str(summary).split())
            if str(community_id) not in known_ids:
                rejected.append(f"{community_id}: unknown community id")
            elif not MIN_SUMMARY_LEN <= len(summary) <= MAX_SUMMARY_LEN:
                rejected.append(f"{community_id}: length {len(summary)} outside bounds")
            else:
                merged[str(community_id)] = summary
                added += 1

    config.SUMMARIES_PATH.write_text(
        json.dumps(
            dict(sorted(merged.items(), key=lambda kv: int(kv[0]))), indent=1, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    for r in rejected:
        print(f"rejected - {r}")
    print(f"{added} summaries merged ({len(merged)} total) -> {config.SUMMARIES_PATH}")
    missing = len(known_ids - set(merged))
    if missing:
        print(f"{missing} significant communities still lack a summary")
    retained_below = len(set(merged) - known_ids)
    if retained_below:
        print(
            f"{retained_below} summaries cover clusters now below the significance "
            "threshold - retained; they cost nothing and may become significant again"
        )
    return 1 if rejected else 0


# How `remap` decides a summary may be re-keyed.
#
# `exact` carries a summary only onto a community whose member set is identical
# to the one it was written about. A summary is a specific claim about a specific
# set of nodes, so a community that gained a node is a different set and
# therefore a different claim.
#
# `overlap` is the previous criterion - the recall bar below, then the precision
# floor - kept because a tolerance is a judgement an estate is entitled to make,
# and defaulted off because it was making that judgement silently. Recall alone
# cannot see a new community that swallows an old one whole: every old member is
# still together, so the share is 1.00 however much unrelated material came with
# it, and the prose is re-attached to something it does not describe. Reported on
# a real rebuild as the large majority of everything carried. The shape is what
# makes it dangerous rather than the size - every summary still has a community
# and every community still has prose, so the store looks healthy and the
# retention figure reads as reassurance (#296).
#
# Anything `overlap` carries that is not the set the prose was written about is
# marked `"exact": false` in the remap report, so a downstream check can find it.
CARRY_EXACT = "exact"
CARRY_OVERLAP = "overlap"
CARRY_CRITERIA = (CARRY_EXACT, CARRY_OVERLAP)
DEFAULT_CARRY = CARRY_EXACT

# Under `--carry overlap` only: the share of an old cluster's members that must
# land in one new cluster before its summary is carried across. Below this the
# summary is withdrawn: prose on the wrong cluster reads as authoritative and is
# worse than no prose. 0.6 has been used across several estate refreshes and
# behaved sensibly. It is not the shipped criterion - see `DEFAULT_CARRY` for why.
DEFAULT_BAR = 0.6
# Refuse to remap when fewer summaries than this are loaded. A mis-specified path
# reads as "almost nothing to do" rather than failing, and the run then writes an
# empty file over a good one.
DEFAULT_FLOOR = 10
# Refuse to remap when less than this share of the graph's nodes carry a
# community. A clustering step that reports success without persisting its result
# leaves a graph that is readable and almost entirely unclustered; remap then
# finds nothing to map onto, reports it as legitimate churn, and overwrites the
# committed summaries. This has happened on a real refresh: the clustering tool
# printed "Done - N communities. graph.json updated" while writing nothing, a
# tiny fraction of nodes kept a community, and remap carried a handful of
# summaries out of thousands. The wrong-snapshot check cannot catch it, because
# those few surviving communities still share node ids with the snapshot.
DEFAULT_COVERAGE = 0.5

# Under `--carry overlap` only: how much of the NEW cluster a carried summary
# must describe. The carry bar above measures recall - how much of the old
# cluster stayed together - and says nothing about how much of what a reader now
# sees the prose covers. Measured on
# a real refresh: of 5,405 carried summaries, one describes a cluster that grew
# from 37 members to 458 with every old member retained. Recall 1.00, clearing a
# 60% bar comfortably; precision 0.08. Not stale and not unsupported -
# confidently describing a small corner of something much larger.
#
# Deliberately low. On that estate 93.5% of carried summaries already sit at 80%
# precision or better, so this rejects the unambiguous cases only and leaves the
# judgement calls carried. Re-authoring costs real money, so the default errs
# towards keeping prose and reporting the distribution; --precision tightens it
# once an operator has looked at their own numbers.
DEFAULT_PRECISION = 0.2


def _membership(graph: dict) -> dict[str, list[str]]:
    members: dict[str, list[str]] = {}
    for node in graph.get("nodes", []):
        community = node.get("community")
        if community is None:
            continue
        members.setdefault(str(community), []).append(node["id"])
    return members


def _graph_disagreement(members: dict[str, list[str]]) -> str:
    """A trailing-newline note when the store's other graph file disagrees with this one.

    `config.GRAPH_PATH` is the *uncompressed* graph, which every store gitignores
    while committing the `.gz`. So the file these two stages read is either absent
    or whatever a discarded run left behind, and both stages used to say nothing
    about which one they got. `record-clustering` already carried this warning; the
    same class reached here, where it costs more - a snapshot is the remap's
    baseline, and `_remap_refusal` cannot catch it because a snapshot taken from
    the stale file shares every node id with that same stale file. Consistent and
    wrong is the one case that guard is blind to.

    Reports, never refuses: the `.gz` is tracked, so both files exist on every
    refresh after the first, and refusing would fire on the normal case.

    The remedy names both stale directions rather than one, via
    `graph_files.stale_direction`: this note used to say to decompress the archive
    over `graph.json` or remove it, which discards the fresh merge whenever the
    stale file is the archive - the state for the whole of a full rebuild (#243).
    """
    counts = (len(members), sum(len(ids) for ids in members.values()))
    other = graph_files.counterpart(config.GRAPH_PATH)
    if other is None:
        return ""  # nothing to disagree with; `disagreement` would say the same
    note = graph_files.disagreement(
        config.GRAPH_PATH,
        counts,
        "Snapshots key the remap, so a snapshot of the wrong graph mis-keys every "
        f"carried summary. {graph_files.stale_direction(config.GRAPH_PATH, other)}",
    )
    return f"{note}\n" if note else ""


def snapshot() -> int:
    """Record community membership before a re-cluster moves the ids."""
    graph = io.read_json_dict(config.GRAPH_PATH)
    members = _membership(graph)
    print(_graph_disagreement(members), end="", file=sys.stderr)
    if not members:
        print(
            f"No communities in {config.GRAPH_PATH}. Cluster the graph before snapshotting - "
            "an empty snapshot makes every later remap drop everything.",
            file=sys.stderr,
        )
        return 1
    with io.gzip_text(config.SUMMARIES_SNAPSHOT_PATH) as handle:
        json.dump(members, handle)
    print(
        f"Snapshotted {len(members)} communities "
        f"({sum(len(v) for v in members.values())} member nodes) -> {config.SUMMARIES_SNAPSHOT_PATH}"
    )
    return 0


# How many adrift or narrowed communities the report names individually before it
# stops and gives a count. A re-cluster can strand thousands, and a wall of ids
# reads as noise rather than as a finding.
REPORT_LIMIT = 20

# Above this share, a population of node ids counts as namespaced `<repo>::<id>`.
# Half, because the question is only which id space a side is *mostly* in - two
# sides on opposite answers are not comparable, whatever the exact proportions.
NAMESPACED_SHARE = 0.5


def _by_id(name: str) -> tuple[int, str]:
    """Deterministic order for community ids that are numeric strings.

    Length first so `9` sorts before `10`, then lexically so two ids of the same
    length never swap between runs. The tiebreak `_claim_targets` already uses.
    """
    return len(name), name


def membership_drift(
    summaries: dict,
    snapshot: dict,
    nodes: Iterable[dict],
    bar: float = DEFAULT_BAR,
    precision: float = DEFAULT_PRECISION,
    coverage: float = DEFAULT_COVERAGE,
    described: str = "",
) -> dict:
    """Which committed summaries no longer describe the community they are keyed to.

    `described` names the graph file the nodes came from, for the messages. A
    store has two graph files and the caller decides which it read, so naming
    `config.GRAPH_PATH` here would put the wrong one in the message on any
    checkout where only the committed `.gz` exists.

    Community ids are positional, so a summary is bound to a *number*; only the
    snapshot binds it to a member *set*. Rebuild or re-cluster without refreshing
    the snapshot and every summary stays attached to a community it no longer
    describes, with no outward sign: every community still has a summary and every
    summary still has a community, so the coverage `status` reports is the same
    either way. `_remap_refusal` cannot see it, because a snapshot taken from a
    stale graph shares every node id with that same stale file - consistent and
    wrong is the one case that guard is blind to.

    **Graded, where `remap`'s carry is binary.** For each summary, the share of
    its snapshot members the graph still files under that same community id -
    recall - and the share of the community those members now make up -
    precision. Both, because recall alone was measured on a real refresh to
    report prose describing a corner of something much larger as sound.

    These bars were once `remap`'s own, and are no longer: since #296 `remap`
    carries only onto an identical member set. So anything this reports as
    narrowed or adrift will certainly be withdrawn, and some of what it passes
    will be too - it answers "does the committed prose still describe the graph,
    and how badly not", which is a question with degrees, whereas "may this prose
    be re-keyed" is not. Reading a clean run here as a prediction of retention is
    the one mistake to avoid; `remap` reports that number itself.

    Ids are compared exactly, which is also what `remap` does - `_claim_targets`
    looks each snapshot member up in a dict keyed by raw `node["id"]`. Stripping a
    `<repo>::` prefix to compare would be *looser* than the `remap` this guards,
    reporting prose as sound that `remap` will drop; and in a merged estate two
    repositories can hold the same local id, so collapsing the prefix silently
    merges distinct nodes into one member. Where the two sides are in different id
    spaces, that is named as its own cause or note rather than reported as drift.

    Four causes stop the comparison instead of returning a verdict, because each
    makes *every* summary compare as adrift and each needs the opposite response
    to "membership moved":

    - `no-graph` - nothing was read at all.
    - `no-membership` - the nodes carry no `community`. graphify holds the
      assignment in that key, so a renamed key, or a clustering step that printed
      success without persisting its result, reads as total drift.
    - `stale-graph` - the graph read disagrees with its committed counterpart, so
      the community ids it carries may not be the ones the store ships. Checked
      before any count is printed, because stating a count from the wrong file as
      though it were the store's is the same error one step earlier.
    - `wrong-snapshot` - the snapshot and the graph share no node ids, the same
      condition `_remap_refusal` refuses on.

    Summaries with no snapshot entry are returned as their own population, never
    filtered out. They can be neither checked nor re-keyed - a remap cannot even
    withdraw them - so excluding them narrows the population and then reports a
    count as though it had covered everything.
    """
    wanted = {str(key) for key in summaries}
    # An empty member list joins the unsnapshotted rather than scoring 0.0: an
    # absent measurement must not read as a measured total loss.
    snap_sets = {str(c): set(ids) for c, ids in snapshot.items() if str(c) in wanted and ids}
    snapshot_ids: set[str] = set().union(*snap_sets.values()) if snap_sets else set()
    counts, kept, sizes = _tally_membership(nodes, snap_sets, snapshot_ids)
    cause, message = _blocking_cause(counts, snapshot_ids, coverage, described)
    result: dict = {
        "cause": cause,
        "message": message,
        "nodes": counts["nodes"],
        "clustered": counts["clustered"],
        "communities": counts["communities"],
        "attached": [],
        "adrift": [],
        "narrowed": [],
        "unsnapshotted": sorted(wanted - set(snap_sets), key=_by_id),
        "snapshot_ids": len(snapshot_ids),
        "namespaced_graph_ids": counts["namespaced_graph_ids"],
        "namespaced_snapshot_ids": sum(1 for node_id in snapshot_ids if "::" in node_id),
    }
    if cause:
        return result
    result.update(_classify(snap_sets, kept, sizes, bar, precision))
    return result


def _tally_membership(
    nodes: Iterable[dict], snap_sets: dict[str, set[str]], snapshot_ids: set[str]
) -> tuple[dict, Counter, Counter]:
    """One streamed pass, counting only what the comparison needs.

    Deliberately holds no graph ids of its own: `kept` needs
    `|snapshot members that the graph still files here|`, which is answered by
    testing each streamed id against the snapshot's set as it goes. On the largest
    estate available a loaded read of the same question was 5.3s and 3.75 GB
    against 2.2s and 0.04 GB streamed.
    """
    kept: Counter = Counter()
    sizes: Counter = Counter()
    communities: set[str] = set()
    total = clustered = overlap = namespaced = 0
    for node in nodes:
        total += 1
        node_id = node.get("id")
        if node_id is not None and "::" in str(node_id):
            namespaced += 1
        if node_id in snapshot_ids:
            overlap += 1
        community = node.get("community")
        if community is None:
            continue
        clustered += 1
        name = str(community)
        communities.add(name)
        if name in snap_sets:
            sizes[name] += 1
            if node_id in snap_sets[name]:
                kept[name] += 1
    counts = {
        "nodes": total,
        "clustered": clustered,
        "communities": len(communities),
        "overlap": overlap,
        "namespaced_graph_ids": namespaced,
    }
    return counts, kept, sizes


def _blocking_cause(
    counts: dict, snapshot_ids: set[str], coverage: float, described: str
) -> tuple[str | None, str]:
    """Why the comparison must not return a verdict, or `(None, "")` when it may.

    Each of these makes *every* summary compare as adrift, and each needs the
    opposite response to "membership moved" - so they are named rather than
    counted. See `membership_drift` for what each one means.
    """
    total, clustered = counts["nodes"], counts["clustered"]
    if not total:
        return "no-graph", (
            f"Cannot check membership: no nodes were read from {described or 'the graph'}. "
            "Nothing was compared, so nothing here says the prose is sound."
        )
    if clustered / total < coverage:
        return "no-membership", (
            f"Cannot check membership: only {clustered} of {total} nodes carry a community "
            f"({clustered / total:.1%}, floor {coverage:.0%}). Every summary would compare as "
            "adrift, and that is no membership having been read rather than membership having "
            "moved - graphify carries the assignment in `community`, so a renamed key, or a "
            "clustering step that printed success without writing its result, looks exactly "
            "like this. Do not re-author prose on this result. Pass --coverage to lower the "
            "floor for a deliberately sparse graph."
        )
    if snapshot_ids and not counts["overlap"]:
        return "wrong-snapshot", (
            f"Cannot check membership: the snapshot and {described or 'the graph'} share no "
            "node ids, so this is the wrong snapshot - the same condition `summaries remap` "
            "refuses on. Every summary would compare as adrift. Re-take the snapshot from "
            "this graph rather than re-authoring prose."
        )
    return None, ""


def _classify(
    snap_sets: dict[str, set[str]],
    kept: Counter,
    sizes: Counter,
    bar: float,
    precision: float,
) -> dict[str, list]:
    """Each checked community filed under attached, adrift or narrowed.

    Adrift and narrowed are separate populations because the responses differ:
    adrift prose is keyed to a set that moved, narrowed prose is still about its
    members and no longer about most of what a reader sees.
    """
    populations: dict[str, list] = {"attached": [], "adrift": [], "narrowed": []}
    for name in sorted(snap_sets, key=_by_id):
        members = snap_sets[name]
        size = sizes[name]
        entry = {
            "id": name,
            "was": len(members),
            "size": size,
            "share": round(kept[name] / len(members), 3),
            "precision": round(kept[name] / size, 3) if size else 0.0,
        }
        if entry["share"] < bar:
            populations["adrift"].append(entry)
        elif entry["precision"] < precision:
            populations["narrowed"].append(entry)
        else:
            populations["attached"].append(entry)
    return populations


def _namespace_note(result: dict) -> str:
    """A line naming an id-space mismatch, when that is what the drift is.

    The trap a first implementation of this check fell into: the snapshot held
    bare ids, the graph held `<repo>::<id>`, and an exact comparison reported
    communities as adrift that were not. `wrong-snapshot` catches the
    whole-population case; this catches the partial one, where enough ids still
    match for the comparison to look legitimate.

    Named rather than corrected. Stripping the prefix would make this check looser
    than the `remap` it guards - see `membership_drift` - so the honest move is to
    say the measurement may not mean what it appears to and leave re-taking the
    snapshot to the operator.
    """
    if not result["nodes"] or not result["snapshot_ids"]:
        return ""
    graph_share = result["namespaced_graph_ids"] / result["nodes"]
    snapshot_share = result["namespaced_snapshot_ids"] / result["snapshot_ids"]
    if (graph_share >= NAMESPACED_SHARE) == (snapshot_share >= NAMESPACED_SHARE):
        return ""
    return (
        f"  Note: {graph_share:.0%} of the graph's node ids are namespaced `<repo>::<id>` "
        f"against {snapshot_share:.0%} of the snapshot's, so the two are in different id "
        "spaces and this may be a mismatch rather than moved membership. Re-take the snapshot "
        "from this graph before re-authoring anything. Comparing with the prefix stripped is "
        "not the fix: `summaries remap` matches ids exactly, so a looser check here would "
        "report prose as sound that remap will drop."
    )


def _report_population(label: str, entries: list, note: str = "") -> None:
    if not entries:
        return
    print(f"{label}: {len(entries)}")
    for entry in entries[:REPORT_LIMIT]:
        print(
            f"  community {entry['id']}: {entry['share']:.0%} of its {entry['was']} snapshot "
            f"members are still filed there; the community now holds {entry['size']} nodes, "
            f"{entry['precision']:.0%} of them described"
        )
    if len(entries) > REPORT_LIMIT:
        print(f"  ... and {len(entries) - REPORT_LIMIT} more")
    if note:
        print(note)


def adrift(
    bar: float = DEFAULT_BAR,
    precision: float = DEFAULT_PRECISION,
    coverage: float = DEFAULT_COVERAGE,
) -> int:
    """Report which committed summaries no longer describe the community they name.

    Exit codes are three-valued on purpose. `2` is "the check could not run", and
    it is separate from `1` because the two need opposite responses: drift means
    re-key or re-author, an unreadable membership means fix the graph and run
    again. Collapsing them is how a one-line read failure gets answered by
    rewriting an estate's prose.

    **Vacuous immediately after `summaries snapshot`**, which is worth saying
    because nothing in a green result can say it: the snapshot is then taken from
    the graph it is compared against, so every summary matches by construction.
    The check means something on a checkout, in CI, or at the start of a refresh -
    wherever the two committed artefacts have had an opportunity to go out of
    step.
    """
    summaries = io.read_json_dict(config.SUMMARIES_PATH)
    if not summaries:
        print(
            f"No summaries at {config.SUMMARIES_PATH}, so there was nothing to check. "
            "This is not a clean result.",
            file=sys.stderr,
        )
        return 2
    if not config.SUMMARIES_SNAPSHOT_PATH.exists():
        print(
            f"No membership snapshot at {config.SUMMARIES_SNAPSHOT_PATH}, so nothing can say "
            f"whether the {len(summaries)} committed summaries still describe their "
            "communities. Run `summaries snapshot`.",
            file=sys.stderr,
        )
        return 2
    with gzip.open(config.SUMMARIES_SNAPSHOT_PATH, "rt", encoding="utf-8") as handle:
        snapshot: dict = json.load(handle)
    if not snapshot:
        print(
            f"The membership snapshot at {config.SUMMARIES_SNAPSHOT_PATH} is empty, so nothing "
            "was compared. Re-run `summaries snapshot` against a clustered graph.",
            file=sys.stderr,
        )
        return 2
    graph = graph_files.graph_to_read(config.GRAPH_PATH)
    if graph is None:
        print(
            f"No graph at {config.GRAPH_PATH} or its .gz counterpart, so the snapshot cannot be "
            "compared against anything.",
            file=sys.stderr,
        )
        return 2
    result = membership_drift(
        summaries,
        snapshot,
        graph_stream.iter_array(graph),
        bar=bar,
        precision=precision,
        coverage=coverage,
        described=graph.name,
    )
    # A disagreeing counterpart is the fourth blocking cause, and it is checked
    # before any count is stated - reported by an operator whose run printed "One of
    # them is stale" and then returned a verdict of `1`, whose documented response
    # is to re-take the snapshot and re-author. On their store that instruction
    # would have destroyed five thousand correct summaries.
    #
    # It is the cause an operator is most likely to meet, because `graph_to_read`
    # prefers the gitignored file - the one most likely to be a stale leftover -
    # over the committed archive, which by definition is not.
    stale = graph_files.disagreement(
        graph,
        (result["communities"], result["clustered"]),
        "Community ids are positional, so every summary would be compared against "
        "communities from a graph the store may not ship.",
    )
    if stale:
        print(
            f"Cannot check membership: {graph.name} and its counterpart hold different "
            "graphs, so the counts read here may not describe what this store ships.",
            file=sys.stderr,
        )
        print(stale.rstrip("\n"), file=sys.stderr)
        print(
            "Do not re-take the snapshot and do not re-author prose on this result - both "
            "would answer a read failure by rewriting an estate's prose. Read the graph the "
            "store ships and run again.",
            file=sys.stderr,
        )
        return 2
    print(
        f"Read {result['nodes']} nodes and {result['communities']} communities from "
        f"{graph.name}; the snapshot holds {len(snapshot)} communities."
    )
    if result["cause"]:
        print(result["message"], file=sys.stderr)
        return 2
    checked = len(result["attached"]) + len(result["adrift"]) + len(result["narrowed"])
    print(
        f"Membership: {len(result['attached'])} of {checked} checked summaries still describe "
        f"the community they name (recall bar {bar:.0%}, precision floor {precision:.0%})"
    )
    print(
        f"Reconciles: {checked} checked + {len(result['unsnapshotted'])} with no snapshot entry "
        f"= {len(summaries)} committed summaries"
    )
    note = _namespace_note(result)
    _report_population(
        "Adrift - too few of its members are still filed under that id, so the prose is keyed "
        "to a set that moved",
        result["adrift"],
        note,
    )
    _report_population(
        "Narrowed - the members stayed but the community grew around them, so the prose "
        "describes a corner of it",
        result["narrowed"],
    )
    if result["unsnapshotted"]:
        named = ", ".join(result["unsnapshotted"][:REPORT_LIMIT])
        more = (
            f" ... and {len(result['unsnapshotted']) - REPORT_LIMIT} more"
            if len(result["unsnapshotted"]) > REPORT_LIMIT
            else ""
        )
        print(
            f"No snapshot entry: {len(result['unsnapshotted'])} summaries can be neither "
            f"checked nor re-keyed - a remap cannot even withdraw them. {named}{more}"
        )
    if result["adrift"] or result["narrowed"] or result["unsnapshotted"]:
        return 1
    return 0


def _remap_refusal(
    summaries: dict,
    nodes: list,
    new_community: dict,
    old_members: dict,
    floor: int,
    coverage: float,
) -> str | None:
    """Why this remap must not run, or None when it may.

    These are refusals rather than warnings because each one produces a
    plausible-looking 0% retention rather than an error: an empty result that
    reads as legitimate churn is how a remap destroys a good summaries file
    without anyone noticing.
    """
    if len(summaries) < floor:
        return (
            f"Refusing to remap: only {len(summaries)} summaries loaded from "
            f"{config.SUMMARIES_PATH} (floor {floor}). A mis-specified path looks like this. "
            "Pass --floor to lower it for a genuinely small store."
        )
    # Counted over nodes, not over `new_community`, which is keyed by id: a merged
    # graph can repeat an id, and the dict would collapse those and understate
    # coverage enough to refuse a healthy graph.
    clustered = sum(1 for node in nodes if node.get("community") is not None)
    if nodes and clustered / len(nodes) < coverage:
        return (
            f"Refusing to remap: only {clustered} of {len(nodes)} nodes in "
            f"{config.GRAPH_PATH} carry a community "
            f"({clustered / len(nodes):.1%}, floor {coverage:.0%}). The "
            "graph is effectively unclustered, so every summary would be dropped "
            "and reported as legitimate churn. A clustering step that printed "
            "success without writing its result looks exactly like this — check "
            "the graph's community coverage before re-running. Pass --coverage to "
            "lower the floor for a deliberately sparse graph."
        )
    snapshot_ids = {node for ids in old_members.values() for node in ids}
    if snapshot_ids and not (snapshot_ids & set(new_community)):
        return (
            "Refusing to remap: the snapshot and the graph share no node ids, so "
            "this is the wrong snapshot. Proceeding would drop every summary and "
            "report it as legitimate 0% retention."
        )
    return None


def _claim_targets(
    summaries: dict,
    old_members: dict,
    new_community: dict,
    bar: float,
    precision: float,
    carry: str,
) -> tuple[dict, dict]:
    """Each summary's best new cluster, and what was withdrawn on the way.

    Returns the surviving claims - target, recall, precision and whether the
    target holds exactly the set the prose was written about - alongside every
    summary that was not claimed, keyed by its old id and carrying its reason,
    its near-miss target and the prose itself. The two partition `summaries`, so
    a caller can reconcile what it carried against what it read.

    Under `CARRY_EXACT` the only claim is set equality. Under `CARRY_OVERLAP` a
    summary is withdrawn when its best overlap falls short of `bar` (recall) or
    the target cluster is so much larger that the prose describes a corner of it
    (`precision`). Either way its members can have gone from the graph entirely.

    Sorted for a deterministic tiebreak.
    """
    members_of: dict[str, set[str]] = defaultdict(set)
    for node, community in new_community.items():
        members_of[community].add(node)
    claims: dict[str, tuple[str, float, float, bool]] = {}
    displaced: dict[str, dict] = {}
    for old_id in sorted(summaries, key=lambda k: (len(k), k)):
        # An absent snapshot entry and an entry whose members have all left the
        # graph are the same finding: nothing to place the prose against.
        members = old_members.get(str(old_id), [])
        landed = Counter(new_community[m] for m in members if m in new_community)
        if not landed:
            displaced[old_id] = {
                "reason": "members-gone",
                "best_target": None,
                "share": None,
                "prose": summaries[old_id],
            }
            continue
        target, count = landed.most_common(1)[0]
        share_of_old = count / len(members)
        share_of_new = count / len(members_of[target])
        # Set equality, not recall 1.0 and precision 1.0. A merged graph can
        # repeat a node id, so the snapshot's member list can too - and then both
        # ratios reach 1.0 over a target holding a member the prose never
        # described. The ratios are a neighbour of the quantity being claimed.
        identical = set(members) == members_of[target]
        if carry == CARRY_EXACT:
            if not identical:
                displaced[old_id] = {
                    "reason": "not-identical",
                    "best_target": target,
                    "share": round(share_of_old, 3),
                    "precision": round(share_of_new, 3),
                    "prose": summaries[old_id],
                }
                continue
        elif share_of_old < bar:
            displaced[old_id] = {
                "reason": "below-bar",
                "best_target": target,
                "share": round(share_of_old, 3),
                "prose": summaries[old_id],
            }
            continue
        elif share_of_new < precision:
            displaced[old_id] = {
                "reason": "below-precision",
                "best_target": target,
                "share": round(share_of_old, 3),
                "precision": round(share_of_new, 3),
                "prose": summaries[old_id],
            }
            continue
        claims[old_id] = (target, share_of_old, share_of_new, identical)
    return claims, displaced


def _report_precision(carried: dict) -> None:
    """The distribution of how much of its cluster each carried summary describes.

    Reported rather than gated beyond the floor, because where to draw the line
    is an estate's judgement and re-authoring costs real money. What the operator
    needs is the shape: on one refresh 93.5% of carried summaries described 80%
    or more of their cluster, which says the recall bar is mostly right - and 51
    landed on a cluster more than twice their old size, which is where the prose
    quietly stops describing what a reader sees.
    """
    if not carried:
        return
    # A missing precision is not a perfect one. Entries written before this was
    # recorded have no such field, and defaulting them to 1.0 printed "5,405 at
    # 80%+" for a report that measured nothing - a clean verdict over an absent
    # measurement, which is the failure this whole check exists to prevent.
    values = [
        entry["precision"]
        for entry in carried.values()
        if isinstance(entry, dict) and entry.get("precision") is not None
    ]
    if not values:
        print(
            f"Carried prose describes its new cluster: not recorded for any of "
            f"{len(carried)} carried summaries - this report predates the measurement. "
            "Re-run `summaries remap` to record it."
        )
        return
    if len(values) < len(carried):
        print(
            f"  (precision recorded for {len(values)} of {len(carried)} carried summaries; "
            "the rest predate the measurement.)"
        )
    bands = [(0.8, "80%+"), (0.5, "50-80%"), (0.2, "20-50%"), (0.0, "under 20%")]
    counts = []
    for lower, label in bands:
        # `min`, not `next`: the bands are listed descending, so `next` returned
        # the first bound in LIST order - 0.8 for every band below it - making
        # the lower three nested rather than adjacent. Each summary below 80%
        # was then counted three times, and the reported distribution summed to
        # more than the population it described.
        upper = min((b[0] for b in bands if b[0] > lower), default=1.01)
        n = sum(1 for v in values if lower <= v < upper)
        counts.append(f"{n} at {label}")
    print(f"Carried prose describes its new cluster: {', '.join(counts)}")


def remap(
    bar: float = DEFAULT_BAR,
    floor: int = DEFAULT_FLOOR,
    coverage: float = DEFAULT_COVERAGE,
    precision: float = DEFAULT_PRECISION,
    carry: str = DEFAULT_CARRY,
) -> int:
    """Carry committed summaries onto new community ids after a re-cluster.

    For each summary, find the new cluster holding the largest share of its old
    members and carry it there when that cluster holds exactly the set the prose
    was written about. Withdraw it otherwise, to a file beside the summaries so
    the writing survives, and report the withdrawal count beside retention so the
    cost of the re-cluster is a measured number in both directions.

    `carry=CARRY_OVERLAP` restores the previous tolerance - see `DEFAULT_CARRY`.
    """
    if not config.SUMMARIES_SNAPSHOT_PATH.exists():
        print(
            f"No membership snapshot at {config.SUMMARIES_SNAPSHOT_PATH}. Run `summaries snapshot` "
            "before re-clustering.",
            file=sys.stderr,
        )
        return 1
    with gzip.open(config.SUMMARIES_SNAPSHOT_PATH, "rt", encoding="utf-8") as handle:
        old_members: dict[str, list[str]] = json.load(handle)
    summaries = io.read_json_dict(config.SUMMARIES_PATH)
    nodes = io.read_json_dict(config.GRAPH_PATH).get("nodes", [])
    print(_graph_disagreement(_membership({"nodes": nodes})), end="", file=sys.stderr)
    new_community = {
        node["id"]: str(node["community"]) for node in nodes if node.get("community") is not None
    }
    refusal = _remap_refusal(summaries, nodes, new_community, old_members, floor, coverage)
    if refusal:
        print(refusal, file=sys.stderr)
        return 1

    claims, displaced = _claim_targets(summaries, old_members, new_community, bar, precision, carry)

    # Pass 2: a contested new cluster keeps the summary whose old cluster
    # contributed the largest share of itself - it describes more of the merged
    # result than a summary that barely arrived. Lowest old id only breaks
    # ties, deterministically. (Winner-by-lowest-id regardless of fit was the
    # old rule; measured on a real refresh, the share rule chose a
    # better-fitting summary for 36 of 86 contested clusters, median +16.7%
    # overlap, with identical retention.)
    #
    # Greedy per-target, and full bipartite assignment is rejected rather than
    # deferred. The obvious objection to greedy is that a loser here might have
    # been the best available summary for some other cluster, which optimal
    # matching would have found. Evaluated on a real refresh's own evidence
    # before this was written, it rescued zero summaries beyond the share rule:
    # a loser's second choice never cleared the 60% carry bar, because a summary
    # that contributed most of one cluster has little left for another. The
    # measurement is what makes this greedy loop correct, not an assumption that
    # optimal matching is too complex - do not "upgrade" it without repeating
    # the measurement and finding a different answer.
    by_target: dict[str, list[tuple[str, float, float, bool]]] = {}
    for old_id, (target, share_of_old, share_of_new, identical) in claims.items():
        by_target.setdefault(target, []).append((old_id, share_of_old, share_of_new, identical))
    remapped: dict[str, str] = {}
    carried: dict[str, dict] = {}
    for target, claimants in by_target.items():
        claimants.sort(key=lambda c: (-c[1], (len(c[0]), c[0])))
        winner, winner_share, winner_precision, winner_identical = claimants[0]
        remapped[target] = summaries[winner]
        carried[target] = {
            "from": winner,
            "share": round(winner_share, 3),
            "precision": round(winner_precision, 3),
            # The mark #296 asks for: under a tolerance this says which carried
            # prose describes a set the graph no longer holds, so a downstream
            # check can find it without recomputing the overlap.
            "exact": winner_identical,
        }
        for loser, loser_share, _, _ in claimants[1:]:
            displaced[loser] = {
                "reason": "collision",
                "best_target": target,
                "share": round(loser_share, 3),
                "prose": summaries[loser],
            }

    config.SUMMARIES_PATH.write_text(
        json.dumps(dict(sorted(remapped.items(), key=lambda kv: int(kv[0]))), indent=1),
        encoding="utf-8",
    )
    # Withdrawn rather than dropped: the prose is a written artefact, so it goes
    # to a file shaped like the one it left and can be revised and merged back.
    # Rewritten on every run, including when it is empty - a file left behind by
    # an earlier remap reads as this run's finding, over prose somebody is meant
    # to go and re-author.
    withdrawn = {old_id: entry["prose"] for old_id, entry in displaced.items()}
    io.write_json(
        config.SUMMARIES_WITHDRAWN_PATH,
        dict(sorted(withdrawn.items(), key=lambda kv: int(kv[0]))),
        indent=1,
    )

    total = len(summaries)
    share = (100 * len(remapped) // total) if total else 0
    # The withdrawal count sits on the retention line deliberately. Retention
    # read as reassurance while being the opposite, so the figure now carries
    # its own contradiction rather than leaving it a line further down (#296).
    print(
        f"Retained {len(remapped)} of {total} summaries ({share}%), "
        f"withdrew {len(displaced)} -> {config.SUMMARIES_PATH}"
    )
    reasons = Counter(entry["reason"] for entry in displaced.values())
    print(
        f"Withdrawn: {reasons['not-identical']} not identical to their new community, "
        f"{reasons['below-bar']} below {int(bar * 100)}% overlap, "
        f"{reasons['members-gone']} whose members are gone, "
        f"{reasons['collision']} merged-cluster collisions, "
        f"{reasons['below-precision']} describing under {int(precision * 100)}% of their cluster"
    )
    inexact = sum(1 for entry in carried.values() if not entry["exact"])
    if inexact:
        print(
            f"Carried below set equality: {inexact} of {len(carried)} carried summaries "
            "describe a community whose membership has changed since they were written, "
            f'marked "exact": false in {config.REMAP_REPORT_PATH}'
        )
    _report_precision(carried)
    # The report is the spool: displaced prose is raw material for the
    # backfill (revise against the new digest, never trust unverified), and
    # the carried map is what lets `verify` split its flag rate by
    # carried-versus-authored - remap preserves coverage while degrading
    # grounding, so the two must be measured together.
    io.write_json(
        config.REMAP_REPORT_PATH,
        {
            "carried": dict(sorted(carried.items(), key=lambda kv: int(kv[0]))),
            "displaced": dict(sorted(displaced.items(), key=lambda kv: int(kv[0]))),
        },
        indent=1,
    )
    print(
        f"Remap report: {len(carried)} carried, {len(displaced)} displaced "
        f"(prose kept for revision) -> {config.REMAP_REPORT_PATH}"
    )
    print(f"Withdrawn prose -> {config.SUMMARIES_WITHDRAWN_PATH}")
    return 0


# A token is treated as a claim about code only if it is shaped like one. This is
# structural rather than a blocklist, because ordinary prose is full of
# capitalised words - "Welsh", "Angular", "Service Manual" - that are not claims
# about code, and a blocklist of them would never be complete. Requiring an
# internal case change, a separator, a file extension or a ticket shape excludes
# English capitalisation and all-caps acronyms without naming any of them.
_CAMEL = re.compile(r"\b[A-Za-z][a-z0-9]*[a-z0-9][A-Z][A-Za-z0-9]*\b")
_SNAKE = re.compile(r"\b[A-Za-z]\w*_\w+\b")
_DASHED = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+){2,}\b")
# English compound adjectives match _DASHED too - "police-to-courtroom",
# "end-to-end", "point-in-time" - and flagging them is noise that would train
# readers to ignore the report. A hyphenated token joined by a preposition or
# conjunction is prose, not a repository or file name.
_PROSE_JOINERS = {
    "to",
    "in",
    "of",
    "and",
    "or",
    "for",
    "with",
    "the",
    "a",
    "an",
    "at",
    "on",
    "by",
    "from",
    "as",
    "per",
    "vs",
}
_FILE = re.compile(
    r"\b[\w.-]+\.(?:java|ts|js|py|json|yaml|yml|xml|raml|csv|feature|sql|html|tsx)\b"
)
_TICKET = re.compile(r"\b[A-Z]{2,}-\d+\b")
# Words that mark a claim the author had no evidence for. A factual description
# layer should contain almost none.
_SPECULATION = re.compile(
    r"\b(probably|likely|presumably|possibly|perhaps|appears to|seems to|"
    r"suggests that|may be|might be|could be)\b",
    re.I,
)


def prose_identifiers(text: str) -> set[str]:
    """Tokens in `text` shaped like a claim about code."""
    found: set[str] = set()
    for pattern in (_FILE, _TICKET, _CAMEL, _SNAKE):
        found.update(pattern.findall(text))
    for token in _DASHED.findall(text):
        if not (set(token.split("-")) & _PROSE_JOINERS):
            found.add(token)
    return found


def _normalise(identifier: str) -> str:
    """Reduce an identifier to what makes two spellings the same thing.

    Graph node labels carry method decoration (`.saveDecision()`) that prose
    naturally drops, and this estate names the same concept in kebab-case as a
    schema and CamelCase as a class (`result-prompt-word-synonym`,
    `ResultPromptWordSynonym`). Comparing on letters and digits alone treats
    those as grounded, while a genuinely different name - or a longer one like
    `FooProcessor` against `Foo` - still differs.
    """
    return re.sub(r"[^a-z0-9]", "", identifier.lower())


def _node_identifiers(node) -> set[str]:
    """Identifiers a single digest node contributes.

    Real digests write a node as "Label (source/file.ext)" in one string; the
    dict form is also accepted. Handling only the dict form crashed on a real
    store despite a full passing test suite, because every fixture used dicts.
    """
    if isinstance(node, dict):
        label = str(node.get("label") or "")
        source = str(node.get("source_file") or "")
    else:
        label, _, tail = str(node).partition(" (")
        source = tail.rstrip(")") if tail else ""
    found: set[str] = set()
    if label:
        found.add(label.strip())
        found.update(word for word in re.split(r"[\s,]+", label) if word)
    if source:
        found.add(source)
        found.update(part for part in re.split(r"[/\\]", source) if part)
    return found


def _spelling_variants(identifier: str) -> set[str]:
    """Other spellings of the same thing, so prose need not match character for
    character: a bare stem for a filename, the parts of a dotted name, and the
    test/subject pairing."""
    variants = {f"{identifier}.java"}
    stem = identifier.rsplit(".", 1)[0]
    if stem and stem != identifier:
        variants.add(stem)
    # Schema and event contracts are filed under dotted names
    # (`<domain>.event.<event-name>.json`) while prose cites the event itself,
    # which is both shorter and more readable. Each dot-separated part is
    # therefore a spelling of the same thing. This does not loosen the check into
    # substring matching: parts are whole segments, so an event the evidence does
    # not hold stays unsupported, and `prose_identifiers` only extracts
    # structural tokens, so a bare part like `json` or `event` is never cited.
    variants.update(part for part in identifier.split(".") if len(part) > 2)
    # A digest showing FooTest is evidence that Foo exists; describing the class
    # rather than its test is interpretation, not invention.
    if identifier.endswith("Test") and len(identifier) > 4:
        variants.add(identifier[:-4])
    else:
        variants.add(f"{identifier}Test")
    return variants


def _digest_identifiers(digest: dict) -> set[str]:
    """Every identifier the evidence contains, with its spelling variants."""
    evidence: set[str] = set(digest.get("repositories", []))
    if digest.get("label"):
        evidence.add(str(digest["label"]))
    for node in digest.get("top_nodes", []):
        evidence |= _node_identifiers(node)
    for feature in digest.get("business_features", []):
        label = feature.get("label") if isinstance(feature, dict) else feature
        if isinstance(label, str):
            evidence.add(label)
            evidence.update(word for word in re.split(r"[\s,]+", label) if word)
    evidence.update(str(ticket) for ticket in digest.get("tickets", []))
    for identifier in tuple(evidence):
        evidence |= _spelling_variants(identifier)
    return evidence


def _sample_ids(ids: list[str], sample: int | None) -> list[str]:
    """A deterministic subset, so a finding can be re-examined without it moving."""
    if not sample or sample >= len(ids):
        return ids
    step = len(ids) / sample
    return [ids[int(i * step)] for i in range(sample)]


# What the dashed rule is, printed where an author meets a dashed finding. The
# shape - three or more segments and a lowercase initial - was stated nowhere an
# author could read it, so authors rewrote correct English defensively, most of it
# two-segment compounds the rule never looks at (#248). The last sentence is the
# operative half: the residual false positives are irreducible, because an
# estate's identifiers are built from the same ordinary English words its prose
# is, so no segment count and no dictionary separates them. Rephrasing is the
# author's move; distrusting the check is not.
_DASHED_RULE = (
    "  Dashed terms: a term is checked as an identifier only if it has three or more "
    "segments and a lowercase initial, so same-named and JDBC-backed are never flagged, "
    "and a compound joined by a preposition or conjunction (end-to-end, point-in-time) is "
    "exempt as well. A three-segment lowercase compound is flagged whether it is an "
    "identifier or ordinary English, because an estate's identifiers are built from "
    "ordinary English words. If a flagged term is English, rephrase it; do not assume the "
    "check is wrong."
)


def _report_dashed_rule(flagged: set[str]) -> None:
    """State the dashed rule when a dashed term is among the findings.

    Conditional rather than always printed: a paragraph about dashed terms under
    findings that hold none is noise inside the block the findings are in, and
    noise in this report is what got its predecessor ignored.
    """
    if any(_DASHED.fullmatch(term) for term in flagged):
        print(_DASHED_RULE)


def _report_absent_classes(
    total_absent: int,
    classified: tuple[dict[str, set[str]], dict[str, set[str]]] | None,
) -> None:
    """The flagged total split by where each term was found, and the arithmetic.

    One count answered two questions until #249, and they need different actions:
    an invented identifier means the prose is wrong and has to be rewritten, while
    a ticket the history datasets record and the graph does not means the summary
    is keyed to a community that has moved. Measured on a large store, the second
    class was nearly half of what the estate pass reported, so an operator was
    told to act on a figure inflated by that much and could not tell which half
    without chasing single terms by hand.

    The arithmetic is printed because a breakdown that does not add up is worse
    than none: `total_absent` is counted from the flagged terms and the two class
    counts from the classification, so a term dropped from the split or counted
    twice shows up in the line rather than in nobody's reading of it.
    """
    if classified is None:
        print(
            f"    no history datasets under {config.HISTORY_DIR}, so these are not split by "
            "where they were found - run knowledgestore export-history, and a term a commit "
            "records is separated from one nothing in the store has ever mentioned."
        )
        return
    in_history, nowhere = classified
    invention = sum(len(terms) for terms in nowhere.values())
    recorded = sum(len(terms) for terms in in_history.values())
    print(
        f"    absent from the graph AND from history: {invention} term(s) - the only class that "
        "can contain invention, and the one to act on: read the prose and rewrite or drop the "
        "term."
    )
    print(
        f"    in the history datasets but not the graph: {recorded} term(s) - the graph holds a "
        "ticket node only for what the intent index mined, so a summary keyed to a community "
        "that has moved cites a real ticket the graph does not hold. A remap question, not an "
        "authoring one."
    )
    print(f"    reconciled: {invention} + {recorded} = {total_absent} flagged.")


def _report_absent_terms(
    absent: dict[str, set[str]],
    classified: tuple[dict[str, set[str]], dict[str, set[str]]] | None,
) -> None:
    """Which summary cites which absent term, under the label its class earns.

    The class that can contain invention is printed first, because it is the one
    an operator acts on. Both labels contain "not in graph", which is the claim
    the estate pass can make about every term here; only the classification adds
    to it.
    """
    if classified is None:
        for cid, terms in sorted(absent.items()):
            print(f"  [not in graph] community {cid} cites: {', '.join(sorted(terms))}")
        return
    in_history, nowhere = classified
    for cid, terms in sorted(nowhere.items()):
        print(f"  [not in graph or history] community {cid} cites: {', '.join(sorted(terms))}")
    for cid, terms in sorted(in_history.items()):
        print(f"  [not in graph, in history] community {cid} cites: {', '.join(sorted(terms))}")


def _report_verify_totals(
    unsupported: list[tuple[str, set[str]]],
    speculative: list[tuple[str, list[str]]],
    orphaned: list[str],
    absent: dict[str, set[str]] | None,
    classified: tuple[dict[str, set[str]], dict[str, set[str]]] | None = None,
) -> None:
    """The counts, and what they do and do not mean."""
    if not (unsupported or speculative or orphaned):
        print("  nothing cited beyond its digest.")
        return
    print(
        f"  {len(unsupported)} citing beyond their digest, {len(speculative)} speculative, "
        f"{len(orphaned)} without a digest."
    )
    _report_dashed_rule({term for _, terms in unsupported for term in terms})
    if absent is None:
        print(
            "  A term absent from a 12-node digest is usually real content the digest did "
            "not sample - on one estate 98% of them existed in the corpus. Run with "
            "--estate to check the graph instead, which is the truthfulness gate."
        )
        return
    total_absent = sum(len(terms) for terms in absent.values())
    if total_absent:
        print(
            f"  Of those, {total_absent} term(s) in {len(absent)} summary(ies) are absent from "
            "the graph as well. The graph is a wider evidence base than a 12-node digest and a "
            "NARROWER one than the corpus, so these are candidates to read rather than proven "
            "fabrications - a term can be real content in a file nothing extracted. Measured on "
            "one estate: this narrowed 9 flagged summaries to 1, and that one cited two terms "
            "that did exist in the corpus."
        )
        _report_absent_classes(total_absent, classified)
    else:
        print(
            "  Every one of them exists somewhere in the graph, so none is citing something "
            "the store cannot speak about."
        )


def _report_verify(
    checked: int,
    total: int,
    unsupported: list[tuple[str, set[str]]],
    speculative: list[tuple[str, list[str]]],
    orphaned: list[str],
    absent: dict[str, set[str]] | None = None,
    classified: tuple[dict[str, set[str]], dict[str, set[str]]] | None = None,
) -> None:
    """Report the digest check for what it is, and the estate check for what it is.

    The old wording called a term the digest did not mention "unsupported",
    which reads as an unbacked claim. A digest carries 12 `top_nodes` and a
    community can span dozens of files, so most such terms are real content the
    digest simply did not sample. Measured on one estate: of 244 distinct flagged
    terms, **239 (98%) existed in the corpus** and 5 did not.

    A gate whose headline is 98% false positives gets ignored, and then stops
    catching the 2% that are real. So the digest finding is now named for what it
    measures, and `absent` - populated only by the estate-wide pass - carries the
    finding that actually means a summary cites something that does not exist.
    """
    print(f"Verified {checked} of {total} summaries against their digests.")
    for cid, extra in unsupported:
        print(f"  [not in digest] community {cid} cites: {', '.join(sorted(extra))}")
    for cid, hedges in speculative:
        words = sorted({hedge.lower() for hedge in hedges})
        print(f"  [speculation] community {cid}: {', '.join(words)}")
    for cid in orphaned:
        print(f"  [no digest] community {cid} has prose but no evidence to check it against")
    if absent:
        _report_absent_terms(absent, classified)
    _report_verify_totals(unsupported, speculative, orphaned, absent, classified)


def _ungrounded(text: str, digest: dict) -> set[str]:
    """Identifiers the prose cites that the evidence does not contain."""
    evidence = {_normalise(item) for item in _digest_identifiers(digest)}
    return {
        cited
        for cited in prose_identifiers(text)
        if _normalise(cited) and _normalise(cited) not in evidence
    }


def _report_provenance_split(checked: list[str], unsupported: list[tuple[str, set[str]]]) -> None:
    """Grounding flag rate split carried-versus-authored, when a remap report exists.

    Remap preserves coverage while degrading grounding (measured: 9% flagged
    for prose authored on its own digest, 37% for prose carried across a
    re-cluster), so retention must never be read without this line beside it.
    """
    carried_ids = set(io.read_json_dict(config.REMAP_REPORT_PATH).get("carried", {}))
    if not carried_ids:
        return
    flagged = {cid for cid, _ in unsupported}

    def rate(group: list[str]) -> str:
        if not group:
            return "n/a (0 checked)"
        hit = sum(1 for cid in group if cid in flagged)
        return f"{100 * hit // len(group)}% ({hit} of {len(group)})"

    carried = [cid for cid in checked if cid in carried_ids]
    authored = [cid for cid in checked if cid not in carried_ids]
    print(
        f"  grounding by provenance: carried {rate(carried)}, "
        f"authored {rate(authored)} - carried prose cites the cluster "
        "it was written for, so a gap here is expected and is the signal to "
        "revise rather than trust"
    )


def _verify_exit(
    strict: bool,
    estate: bool,
    unsupported: list,
    orphaned: list,
    absent: dict | None,
) -> int:
    """Whether to fail, and on which finding.

    Under `--estate`, fail on what is genuinely unbacked rather than on what a
    12-node sample failed to mention. Without it the previous behaviour stands,
    so an existing CI invocation does not silently change meaning.
    """
    if not strict:
        return 0
    blocking = (absent or orphaned) if estate else (unsupported or orphaned)
    return 1 if blocking else 0


# A cited term shorter than this is not matched against a name *segment*, only
# against a whole identifier. Segments are short and common - `api`, `ui`, `db` -
# so a floor is what stops the looser match turning "the estate contains a segment
# spelled like your term" into "your term is corroborated". Three characters keeps
# `ngrx`, `hmcts` and `terraform` and rejects the noise.
MIN_SEGMENT_MATCH = 3

# What a name is composed of, across the ecosystems this has to serve: scoped npm
# packages (`@scope/name`), Java packages (`uk.gov.example.thing`), Terraform module
# addresses (`module.name`) and hyphenated repository names.
_SEGMENT_SEPARATORS = re.compile(r"[/@.\-_:]+")


def estate_vocabulary() -> tuple[set[str], set[str]]:
    """Every identifier the graph holds, normalised - the estate's own vocabulary.

    The wider evidence base a digest is a 12-node sample of. Node labels and
    `local_id`s together are what a summary could legitimately be about, so a
    cited term absent from all of them is not merely uncorroborated: nothing in
    the store can answer a question about it.

    Returns whole identifiers and the name *segments* they are composed of. A
    cited term matching only a segment is corroborated - `NgRx` against
    `@ngrx/store` - and counted separately, so the looser rule stays measurable.

    Loads the graph, which is why it is opt-in. `status` must stay cheap; this
    stage is already an authoring-time check and can afford it.
    """
    graph = io.read_json_dict(config.GRAPH_PATH)
    print(
        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "the estate check"),
        end="",
        file=sys.stderr,
    )
    identifiers: set[str] = set()
    segments: set[str] = set()
    for node in graph.get("nodes", []):
        for field in ("label", "local_id"):
            value = node.get(field)
            if value:
                raw = str(value)
                identifiers.add(_normalise(raw))
                # Split the RAW value. `_normalise` reduces to letters and digits,
                # so by then `@ngrx/store` is `ngrxstore` and there is nothing left
                # to split on - the first version of this collected segments after
                # normalising and matched nothing it did not already match.
                segments |= {_normalise(part) for part in name_segments(raw)}
        source = node.get("source_file")
        if source:
            # The filename alone, because prose cites `AddressPipe` and
            # `address.pipe.ts` rather than the whole repo-relative path.
            filename = str(source).rsplit("/", 1)[-1]
            identifiers.add(_normalise(filename))
            segments |= {_normalise(part) for part in name_segments(filename)}
    identifiers.discard("")
    segments.discard("")
    return identifiers, {part for part in segments if len(part) >= MIN_SEGMENT_MATCH}


def estate_identifiers() -> set[str]:
    """The whole identifiers only. Kept because a caller wanting the strict set
    should not have to discard the segments to get it."""
    return estate_vocabulary()[0]


def name_segments(identifier: str) -> set[str]:
    """The parts of a name a cited term may legitimately refer to.

    `NgRx` is reported absent while the estate holds `@ngrx/store`, `@ngrx/effects`
    and six more scoped packages across 228 labels. A whole-label match can never
    match a scoped package name, and scoped names are the norm in JS/TS - so that
    check cried wolf on an entire ecosystem's naming convention, and a check that
    does that gets switched off and then protects nothing.

    Segments rather than substrings. Substring matching would also match a term
    against the middle of an unrelated word, and case-insensitively it matches far
    more than intended. Segments are explainable in one sentence and cover the two
    other ecosystems this will arrive from next - a Java package and a Terraform
    module address are the same shape - without a second special case.

    This deliberately loosens a check whose job is not lying, so it trades false
    positives for false negatives, which fail in the reassuring direction.
    `MIN_SEGMENT_MATCH` and the count reported by `absent_from_estate` are what
    keep that trade visible rather than assumed.
    """
    parts = {part for part in _SEGMENT_SEPARATORS.split(identifier) if part}
    return {part for part in parts if len(part) >= MIN_SEGMENT_MATCH}


def absent_from_estate(
    unsupported: list[tuple[str, set[str]]],
) -> tuple[dict[str, set[str]], int]:
    """Terms the graph does not hold, and how many were matched only by a segment.

    The second value is the measurement this change is not safe without. The issue
    asking for a looser match was explicit that it had to be measured against a
    real estate both ways, and a one-off count on one estate would not have
    travelled. Reporting it on every run makes the trade visible wherever the check
    runs: a large number means the whole-label match was hiding a great deal, and a
    number close to the finding count means the looser rule is doing most of the
    work and deserves a read.
    """
    if not unsupported:
        return {}, 0
    estate, segments = estate_vocabulary()
    if not estate:
        return {}, 0
    absent: dict[str, set[str]] = {}
    by_segment = 0
    for cid, terms in unsupported:
        missing = set()
        for term in terms:
            normalised = _normalise(term)
            if normalised in estate:
                continue
            if len(normalised) >= MIN_SEGMENT_MATCH and normalised in segments:
                by_segment += 1
                continue
            missing.add(term)
        if missing:
            absent[cid] = missing
    return absent, by_segment


# The characters a name is spelled with. A flagged term is credited to history
# only when it stands alone between them, so `svc-alpha` is not recorded by a
# commit naming `svc-alpha-api`. That is stricter than `_normalise`, which the
# graph check uses: a spelling difference therefore leaves a term in the class
# that can contain invention, which is a candidate to read rather than a term
# quietly explained away.
_HISTORY_TOKEN_CHARS = r"A-Za-z0-9_.\-"


def _history_token(term: str) -> re.Pattern[str]:
    """One term as a whole token, case-insensitively - the graph check ignores case too."""
    return re.compile(
        rf"(?<![{_HISTORY_TOKEN_CHARS}]){re.escape(term)}(?![{_HISTORY_TOKEN_CHARS}])",
        re.IGNORECASE,
    )


def _substring_alternation(terms: Iterable[str]) -> re.Pattern[str]:
    """A prefilter over a raw dataset line: one automaton for every outstanding term.

    Deliberately looser than `_history_token`, and every line it passes is then
    confirmed field by field. A dataset line is JSON, so a commit body's newlines
    arrive as `\\n` and a token check against the raw line would miss a term at the
    start of a body - a prefilter that can say "no" wrongly is a defect, one that
    says "maybe" too often costs a `json.loads`.
    """
    return re.compile("|".join(re.escape(term) for term in sorted(terms)), re.IGNORECASE)


def _cites(record: object, pattern: re.Pattern[str]) -> bool:
    """Whether one commit cites the term, in the fields `export-history` writes.

    The message it was committed with, and the paths it touched. Confined to those
    so the claim stays "history records this term" rather than "these bytes contain
    it": a repository URL and an author's address are in the same line and neither
    is a citation. Paths match a whole segment, because prose cites `AddressPipe`
    or `address.pipe.ts` and not the repo-relative path around it.
    """
    if not isinstance(record, dict):
        return False
    for field in ("subject", "body"):
        if pattern.search(str(record.get(field) or "")):
            return True
    for entry in record.get("files") or ():
        path = entry.get("path") if isinstance(entry, dict) else entry
        if any(pattern.fullmatch(part) for part in str(path or "").split("/")):
            return True
    return False


def _cited_in_dataset(dataset: Path, outstanding: dict[str, re.Pattern[str]]) -> set[str]:
    """One streaming pass over one repository's commits, for every outstanding term.

    Deletes what it finds from `outstanding` and returns it, so the next dataset
    scans for less and the whole lookup ends as soon as the last term is located.
    """
    found: set[str] = set()
    candidates = _substring_alternation(outstanding)
    with dataset.open(encoding="utf-8") as lines:
        for line in lines:
            if not candidates.search(line):
                continue
            record = json.loads(line)
            cited = {term for term, pattern in outstanding.items() if _cites(record, pattern)}
            if not cited:
                continue
            found |= cited
            for term in cited:
                del outstanding[term]
            if not outstanding:
                break
            candidates = _substring_alternation(outstanding)
    return found


def cited_in_history(terms: set[str]) -> set[str] | None:
    """Which of `terms` the history datasets record. `None` when there are none to read.

    The graph holds a ticket node only for what the intent index mined, so a
    summary citing a ticket real for the community it was written for reads as
    absent from the estate. The datasets under `config.HISTORY_DIR` are where that
    ticket does exist, and separating the two is what stops a remap question being
    reported as an authoring one.

    What this costs, because these datasets hold every commit of every repository
    and a load per term would read them once per term: one pass, line by line, so
    no dataset is ever held in memory; one alternation shared by every outstanding
    term, so a line costs the same whether one term is outstanding or fifty;
    `json.loads` only on a line that contains one of them; and the pass returns as
    soon as the last term is located. The worst case - nothing found anywhere - is
    one read of the datasets, and it runs only when the graph check left terms
    unexplained.

    `None` rather than an empty set when there is nothing to read: absent from
    history and never looked for are different findings, and only one of them makes
    a term a candidate for invention.
    """
    datasets = sorted(config.HISTORY_DIR.glob("*/commits.ndjson"))
    if not datasets:
        return None
    outstanding = {term: _history_token(term) for term in terms if term}
    found: set[str] = set()
    for dataset in datasets:
        if not outstanding:
            break
        found |= _cited_in_dataset(dataset, outstanding)
    return found


def classify_absent(
    absent: dict[str, set[str]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]] | None:
    """The terms the graph does not hold, split by where else they were found.

    Returns `(cited by the history datasets, found in neither)`, keyed by
    community as `absent` is, or `None` when there are no datasets to read - the
    split must not be claimed over an artefact nothing looked at.

    Precedence is the order the places are checked, and it is deliberate: the graph
    first, in `absent_from_estate`, and the history datasets second. The first
    place a term is found decides its class, so the classes cannot overlap, they
    sum to the flagged total, and the second class is exactly "found in neither" -
    the only set that can contain invention.
    """
    cited = cited_in_history({term for terms in absent.values() for term in terms})
    if cited is None:
        return None
    in_history: dict[str, set[str]] = {}
    nowhere: dict[str, set[str]] = {}
    for cid, terms in absent.items():
        if terms & cited:
            in_history[cid] = terms & cited
        if terms - cited:
            nowhere[cid] = terms - cited
    return in_history, nowhere


def verify(sample: int | None = None, strict: bool = False, estate: bool = False) -> int:
    """Check authored summaries cite only what their digests contain.

    Coverage checks confirm every digest got prose; this confirms the prose is
    grounded. Reports rather than fails, so it can be run over a whole store
    without blocking; `strict` is for CI.

    The digest check answers "was the author's own evidence enough to support
    this?" - useful for authoring discipline and a poor truthfulness gate, since
    a digest samples 12 nodes of a community that may span dozens of files.
    `estate=True` widens the evidence to the whole graph. That is a much better
    filter than the digest - it narrowed 9 flagged summaries to 1 on a real
    sample - but it is not proof of fabrication: the graph is narrower than the
    corpus, and the one summary it isolated cited two terms that existed in the
    corpus in files nothing had extracted. Treat its findings as the shortlist
    worth a human read. Checking the corpus itself is the only true gate and is
    not implemented here.

    Those findings are then split by where the term was found: recorded in the
    history datasets but not held by the graph, which is a remap question, or
    absent from both, which is the only class that can contain invention. The two
    counts sum to the flagged total in the output.
    """
    loaded = io.read_json(config.SUMMARIES_INPUT_PATH, default=[]) or []
    digests = {str(d["id"]): d for d in loaded if isinstance(d, dict) and "id" in d}
    prose = io.read_json_dict(config.SUMMARIES_PATH)
    if not digests or not prose:
        print(
            f"Nothing to verify: {len(digests)} digests in {config.SUMMARIES_INPUT_PATH}, "
            f"{len(prose)} summaries in {config.SUMMARIES_PATH}. Run `summaries extract` first.",
            file=sys.stderr,
        )
        return 1

    unsupported: list[tuple[str, set[str]]] = []
    speculative: list[tuple[str, list[str]]] = []
    orphaned: list[str] = []
    checked = _sample_ids(sorted(prose, key=lambda k: (len(k), k)), sample)
    for cid in checked:
        digest = digests.get(cid)
        if digest is None:
            orphaned.append(cid)
            continue
        extra = _ungrounded(prose[cid], digest)
        if extra:
            unsupported.append((cid, extra))
        hedges = _SPECULATION.findall(prose[cid])
        if hedges:
            speculative.append((cid, hedges))

    absent, matched_by_segment = absent_from_estate(unsupported) if estate else (None, 0)
    # What the graph does not hold, split by where else the term was found. The
    # flagging predicate is untouched: this classifies what is already flagged.
    classified = classify_absent(absent) if absent else None
    if estate and matched_by_segment:
        print(
            f"  {matched_by_segment} cited terms matched a name segment rather than a "
            "whole identifier (scoped packages, Java packages, module addresses)"
        )
    _report_verify(len(checked), len(prose), unsupported, speculative, orphaned, absent, classified)
    _report_provenance_split(checked, unsupported)
    # Under --estate, fail on what is genuinely unbacked rather than on what a
    # 12-node sample failed to mention. Without it, the old behaviour stands so
    # an existing CI invocation does not silently change meaning.
    return _verify_exit(strict, estate, unsupported, orphaned, absent)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["extract"]:
        return extract()
    if arguments[:1] == ["merge"] and len(arguments) >= 2:
        return merge(arguments[1:])
    if arguments[:1] == ["snapshot"]:
        return snapshot()
    if arguments[:1] == ["verify"]:
        parser = argparse.ArgumentParser(prog="knowledgestore summaries verify")
        parser.add_argument("--sample", type=int, default=None)
        parser.add_argument("--strict", action="store_true")
        parser.add_argument(
            "--estate",
            action="store_true",
            help="re-check terms the digest did not corroborate against the whole graph, "
            "which is what distinguishes an unbacked claim from an unsampled one",
        )
        options = parser.parse_args(arguments[1:])
        return verify(sample=options.sample, strict=options.strict, estate=options.estate)
    if arguments[:1] == ["adrift"]:
        parser = argparse.ArgumentParser(prog="knowledgestore summaries adrift")
        parser.add_argument("--bar", type=float, default=DEFAULT_BAR)
        parser.add_argument("--precision", type=float, default=DEFAULT_PRECISION)
        parser.add_argument("--coverage", type=float, default=DEFAULT_COVERAGE)
        options = parser.parse_args(arguments[1:])
        return adrift(bar=options.bar, precision=options.precision, coverage=options.coverage)
    if arguments[:1] == ["remap"]:
        parser = argparse.ArgumentParser(prog="knowledgestore summaries remap")
        parser.add_argument("--bar", type=float, default=DEFAULT_BAR)
        parser.add_argument("--floor", type=int, default=DEFAULT_FLOOR)
        parser.add_argument("--coverage", type=float, default=DEFAULT_COVERAGE)
        parser.add_argument("--precision", type=float, default=DEFAULT_PRECISION)
        parser.add_argument(
            "--carry",
            choices=CARRY_CRITERIA,
            default=DEFAULT_CARRY,
            help="exact: carry only onto a community holding the same node set. "
            "overlap: carry on --bar recall and --precision, marking anything "
            'carried below equality as "exact": false',
        )
        options = parser.parse_args(arguments[1:])
        return remap(
            bar=options.bar,
            floor=options.floor,
            coverage=options.coverage,
            precision=options.precision,
            carry=options.carry,
        )
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
