"""Community summaries - the GraphRAG indexing step, done at build time.

GraphRAG's core technique is LLM-written summaries of each graph community
at *index* time, so query time needs no LLM at all. This script provides
the deterministic halves of that step; the generation itself runs in
Claude Code (maintainers have a licence; consumers never need one):

  1. knowledgestore summaries extract
       -> knowledge/summaries/communities-input.json
       One digest per significant community (label, size, repositories,
       top nodes, business features, Jira tickets) - the raw material -
       and a `coverage` block saying how much of each capped field it
       shows, so the author knows when they are reading a sample.

  2. In Claude Code: generate 2-4 sentence business summaries for each
     digest, as JSON files of {"<community id>": "<summary>", ...}.

  3. knowledgestore summaries snapshot
       Records community membership before a re-cluster moves the ids, as
       knowledge/summaries/membership-snapshot.json.gz (node ids) and
       knowledge/summaries/membership-files.json.gz (the same communities
       keyed by `(repository, source_file)`, for the remap's fallback route).

  4. knowledgestore summaries remap [--carry exact] [--floor 10] [--coverage 0.5]
       After re-clustering, carries a summary onto the new id of the community
       holding exactly the node set it was written about, and withdraws the rest
       to knowledge/summaries/communities-withdrawn.json. Reports retention and
       the withdrawal count together, and gates the write on the same prose
       digest `merge` uses, so an identity remap rewrites nothing.
       Refuses on an unclustered graph.
       `--carry overlap [--bar 0.6] [--precision 0.2]` is the older tolerance.
       Where the node-id route drops a summary because its members are gone
       from the graph entirely, a fallback keyed on `(repository, source_file)`
       gets one attempt at it - see `FALLBACK_ROUTE` for why only there. The
       two routes are counted separately in the output and named per carry in
       the remap report.

  4a. knowledgestore summaries adrift [--bar 0.6] [--precision 0.2] [--coverage 0.5]
       Whether the snapshot still describes the graph: which committed summaries
       are keyed to a community that no longer holds the members they were
       written about. Exit 1 on drift, 2 when the check could not run.

  5. knowledgestore summaries merge <file.json ...>
       -> knowledge/summaries/communities.json  (committed)
       Validates ids and length bounds, merges over any existing file,
       and carries each community's coverage into a reserved `_metadata`
       key beside a digest of the prose. The write is gated on that
       digest: a run that changed no prose rewrites nothing. The block
       also records one digest per summary, and `merge` and `remap` both
       report - without refusing - any committed summary whose prose no
       longer matches the one recorded beside it.

The explorer embeds the merged summaries; Ask answers then include
pre-written prose selected deterministically - no query-time AI.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
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

# The capped fields, in the order a coverage block records them. Listed rather
# than derived from a digest's keys: these blocks are committed bytes, so their
# order must not depend on how a dict happened to be built.
COVERAGE_FIELDS = ("top_nodes", "business_features", "tickets")


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


def validate_coverage(community: object, coverage: dict) -> None:
    """Refuse a coverage block that does not reconcile.

    `shown + unshown == total`, per field, wherever a block is written. A block
    that does not add up is worse than no block at all: it reads as precision, and
    the whole point of recording it is that `summaries verify` and a human reader
    subtract from it. Raises rather than warns, because the artefact is committed
    and a warning on a build log is not a gate.
    """
    for field in COVERAGE_FIELDS:
        entry = coverage.get(field)
        if not isinstance(entry, dict):
            raise ValueError(f"community {community}: coverage records no {field}")
        shown, unshown, total = entry.get("shown"), entry.get("unshown"), entry.get("total")
        if not (isinstance(shown, int) and isinstance(unshown, int) and isinstance(total, int)):
            raise ValueError(f"community {community}: coverage for {field} is not counted: {entry}")
        if shown < 0 or unshown < 0 or shown + unshown != total:
            raise ValueError(
                f"community {community}: coverage for {field} does not reconcile - "
                f"{shown} shown + {unshown} unshown is not {total} total"
            )


def checked_coverage(digest: dict, pools: dict[str, tuple[int, int]]) -> dict:
    """One digest's coverage block, cross-checked against the fields it describes.

    `pools` gives each field `(how many the cap lets through, how many exist)`.
    The first is computed from the cap rather than measured off the emitted list
    on purpose: a count copied from the list it describes can never disagree with
    it, so it would record a cap change instead of catching one. Lower a cap in
    the digest and leave this alone and the two disagree here, at the point of
    writing, rather than in a store's committed prose.
    """
    block: dict[str, dict[str, int]] = {}
    for field in COVERAGE_FIELDS:
        shown, total = pools[field]
        held = len(digest[field])
        if held != shown:
            raise ValueError(
                f"coverage for {field} claims {shown} shown, the digest holds {held} - "
                "the cap and the block that describes it have gone out of step"
            )
        block[field] = {"shown": shown, "unshown": total - shown, "total": total}
    validate_coverage(digest.get("id"), block)
    return block


def community_digest(
    community: int, nodes: list[dict], labels: dict, intent: dict, degree: dict
) -> dict:
    """The raw material one community summary is written from.

    Three fields are capped, and `coverage` records what each cap left out. An
    author reading `12 of 340` writes differently from one reading `12 of 12`,
    and `summaries verify` can then tell a term the digest never showed from one
    the community does not contain - which the totals being computed and
    discarded made indistinguishable.
    """
    nodes.sort(key=lambda n: -degree[n["id"]])
    repos = Counter(n.get("repo", "") for n in nodes)
    # label-less structural nodes (Java package hierarchy) are skipped, so the
    # sample is drawn from twice the cap and then capped. `window` is what that
    # sample may draw on; `labelled` is what the community holds altogether,
    # which is the total the coverage block reports.
    labelled = [n for n in nodes if n.get("label")]
    window = [n for n in nodes[: TOP_NODES * 2] if n.get("label")]
    features = [n["label"] for n in nodes if kinds.is_kind(n, kinds.FEATURE)]
    tickets: Counter = Counter()
    for n in nodes[:30]:
        tickets.update((n.get("metadata") or {}).get("tickets") or [])
        entry = intent.get(n.get("repo", ""), {}).get(n.get("source_file") or "")
        if entry:
            tickets.update(dict(list(entry["tickets"].items())[:3]))
    digest = {
        "id": community,
        "label": (
            labels.get(str(community)) or _derived_label(nodes, repos) or f"Community {community}"
        ),
        "size": len(nodes),
        "repositories": [r for r, _ in repos.most_common(4) if r],
        "top_nodes": [f"{n['label']} ({n.get('source_file') or '?'})" for n in window][:TOP_NODES],
        "business_features": features[:TOP_FEATURES],
        "tickets": [t for t, _ in tickets.most_common(TOP_TICKETS)],
    }
    # `tickets` counts what the digest's own scan found across the 30
    # highest-degree nodes, which is the pool the sample was taken from rather
    # than every ticket the community touches. Said here because a total that
    # means something narrower than it reads is how a coverage block misleads.
    digest["coverage"] = checked_coverage(
        digest,
        {
            "top_nodes": (min(TOP_NODES, len(window)), len(labelled)),
            "business_features": (min(TOP_FEATURES, len(features)), len(features)),
            "tickets": (min(TOP_TICKETS, len(tickets)), len(tickets)),
        },
    )
    return digest


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


def content_digest(body: dict[str, str]) -> str:
    """A hash of the merged artefact's semantic body - the prose, and nothing else.

    The metadata block is excluded deliberately. Coverage counts move whenever the
    graph is re-extracted, so hashing them would make every refresh rewrite every
    summary in every consuming store's diff for reasons no reader can act on.
    Keys are sorted, so the hash describes the content rather than the order it
    happened to be built in.
    """
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _merged_metadata(body: dict[str, str], coverage: dict[str, dict]) -> dict:
    """The reserved block: the digest the write is gated on, and the evidence base.

    Every key is placed explicitly - here, in `COVERAGE_FIELDS` and by sorting the
    community ids - because two runs on the same inputs must be byte-identical and
    dict order is not a contract worth resting that on.

    Coverage is carried only for a community that has a digest. Prose retained for
    a cluster that has fallen below the significance threshold has none, and
    inventing an empty block for it would say the digest showed everything.
    """
    covered = {cid: coverage[cid] for cid in sorted(body, key=lambda k: int(k)) if cid in coverage}
    for cid, block in covered.items():
        validate_coverage(cid, block)
    return {
        "content_digest": content_digest(body),
        # Two digests over the same prose, answering two different questions, and
        # neither is a substitute for the other (#316). `content_digest` covers
        # `{id: prose}` and gates the write: a remap that re-keys prose must
        # rewrite the file, so that digest has to move when an id moves. The
        # per-entry digests exclude the ids for the opposite reason - they are
        # what a later run compares the committed prose against, and a re-key is
        # not an edit.
        io.PROSE_DIGESTS_KEY: io.prose_digests(body.values()),
        "coverage": covered,
    }


def _recorded_content_digest(document: dict) -> str | None:
    """The digest the committed file records, or None if it records no prose digests.

    The write gate skips a run whose prose is unchanged, which is what a store
    upgrading to a version that records something new in the metadata block runs
    into: the prose is identical, the write is skipped, and the new field arrives
    only if somebody happens to change a summary. Withholding the recorded digest
    until the block carries the prose digests makes the first run after the upgrade
    write once and every run after it skip as before - so the check populates
    itself rather than waiting for unrelated work.
    """
    metadata = document.get(io.SUMMARIES_METADATA_KEY) or {}
    return metadata.get("content_digest") if metadata.get(io.PROSE_DIGESTS_KEY) else None


def _write_merged_summaries(
    document: dict, body: dict[str, str], coverage: dict[str, dict]
) -> bool:
    """Write the merged summaries artefact unless the prose in it is already this.

    The one writer of that file and the one gate on it, because both `merge` and
    `remap` write it and while each had its own writer they disagreed twice
    (#313): `remap` rewrote every line of a file whose prose it had not changed,
    and escaped non-ASCII characters `merge` wrote literally, so alternating the
    two stages churned a committed artefact on its own. `document` is the file as
    it stands, which is where the digest to compare against is recorded.

    Returns whether the write was skipped - the only part of this either caller
    reports, and each reports it in its own words.

    Gated on the prose alone. Coverage counts move on every re-extraction, so
    without this a refresh that changed not one word would still rewrite every
    summary - the diff a consuming store reviews would be noise forever, which
    is a worse outcome than not recording coverage at all.

    Also where the committed prose is checked against the digests recorded beside
    it, before this replaces both (#316). Here rather than in each caller for the
    same reason the write is: the file this reads is about to be overwritten, so a
    check anywhere downstream of the write can no longer see what it needs to.
    """
    for line in io.prose_drift(config.SUMMARIES_PATH, document, io.summaries_body(document)):
        print(line)
    metadata = _merged_metadata(body, coverage)
    recorded = _recorded_content_digest(document)
    unchanged = recorded == metadata["content_digest"]
    if not unchanged:
        config.SUMMARIES_PATH.write_text(
            json.dumps({**body, io.SUMMARIES_METADATA_KEY: metadata}, indent=1, ensure_ascii=False),
            encoding="utf-8",
        )
    return unchanged


# The S8707 register. Sonar reports `pythonsecurity:S8707` wherever a path built
# from CLI arguments reaches the filesystem, and this library answers it in two
# ways: a write is validated by `io.checked_write_target`, and a read is
# suppressed on the grounds stated in `merge` below. Every read site cites those
# grounds rather than restating them, so the two cannot drift into two different
# policies. Keep this register complete - `tests/test_read_path_policy.py` fails
# when a module suppresses the rule without appearing here.
#   S8707 policy site: build_community_summaries.py - _take_batches, where the grounds are
#   S8707 policy site: extract_ast.py - read_file_list, citing merge
#   S8707 policy site: chunk_status.py - log_tokens, citing merge
#   S8707 policy site: io.py - every read, citing merge; its two writes are
#     validated by checked_write_target, which is a check rather than grounds
#   S8707 policy site: build_content_set.py - a write, validated the same way
def _take_batches(
    paths: list[str], known_ids: set[str], merged: dict[str, str]
) -> tuple[int, list[str]]:
    """Read each batch into `merged`, returning what was taken and what was refused.

    Lifted out of `merge` so that function stays readable as the four steps it is -
    read, merge, gate the write, report. The refusals are collected rather than
    raised because a batch of many summaries should not be lost to one bad entry,
    and the caller prints every one.
    """
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
    return added, rejected


def merge(paths: list[str]) -> int:
    digests = json.loads(config.SUMMARIES_INPUT_PATH.read_text(encoding="utf-8"))
    known_ids = {str(d["id"]) for d in digests}
    coverage = {str(d["id"]): d["coverage"] for d in digests if isinstance(d.get("coverage"), dict)}
    document = io.read_json_dict(config.SUMMARIES_PATH)
    merged: dict[str, str] = io.summaries_body(document)
    added, rejected = _take_batches(paths, known_ids, merged)

    body = dict(sorted(merged.items(), key=lambda kv: int(kv[0])))
    unchanged = _write_merged_summaries(document, body, coverage)
    for r in rejected:
        print(f"rejected - {r}")
    if unchanged:
        print(
            f"{added} summaries merged ({len(merged)} total); the prose is unchanged, so "
            f"{config.SUMMARIES_PATH} was not rewritten"
        )
    else:
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

# The two routes a carry can arrive by, named in the remap report so the next
# refresh can measure which one carried what.
NODE_ROUTE = "node-ids"
FALLBACK_ROUTE = "source-files"
# Node ids first, always. The rank is a route rank rather than a score
# comparison because the two routes' recall figures are not the same quantity -
# one is a share of nodes, the other a share of files - and picking between two
# claimants on numbers that mean different things is how a coarse measurement
# beats a fine one.
ROUTE_RANK = {NODE_ROUTE: 0, FALLBACK_ROUTE: 1}

# Why the file key is a fallback and not the key (#302).
#
# The diagnosis behind it is not in doubt: on a rebuild that re-runs semantic
# extraction, only a minority of the pre-rebuild node ids exist in the new
# graph, and the survivors are almost exactly the AST population. Structural
# extraction is deterministic so those ids are stable; semantic ids are built
# from labels an extraction authored, so a fresh pass renames essentially all of
# them even where the underlying content is unchanged. When most of a
# community's members are new strings standing for the same content, no
# membership-overlap criterion can carry - set equality, Jaccard and recall fail
# together. Source files do not have this property: they are corpus paths,
# identical whoever extracted them.
#
# Four reasons that diagnosis argues for a fallback rather than a swap.
#
# 1. On the AST population node ids are exact and free of every objection
#    below, so swapping the key wholesale gives up a good key to fix a bad case.
#
# 2. The semantic-rename problem lives precisely in the `members-gone` drops and
#    nowhere else. Everywhere else the old ids are present and answer the
#    question directly.
#
# 3. A fallback cannot loosen a bar the primary route already cleared, and a
#    wholesale swap silently does. `share_of_old = count / len(members)` divides
#    by the member count, so keying on files shrinks the denominator from
#    hundreds of nodes to a handful of files: a three-file community can then
#    only score 0, 0.33, 0.67 or 1.0, and `--bar 0.6` becomes "2 of 3". That is
#    a real loosening applied unevenly - hardest on the file-poor communities -
#    and it would read as retention without anything having matched better. As a
#    fallback the coarseness only ever applies where the node route scored
#    nothing at all, so it cannot relabel a rejection as a pass.
#
# 4. The precision bar exists because of a real escape, recorded at
#    `DEFAULT_PRECISION`: a cluster that grew from 37 members to 458 with recall
#    1.00 and precision 0.08. Counted in files a cluster can grow by an order of
#    magnitude in nodes while barely moving, so a file-counted precision check
#    partly goes blind exactly where it earns its place. So the fallback does
#    not count precision in files at all. Recall is measured in files, because
#    that is the half whose old-side node ids no longer exist; precision is
#    measured in NODES on both sides, as the smaller of two ratios over the
#    target community's node count - the share of it that comes from the old
#    community's files, and the old community's own node count as an upper bound
#    on how much of the target the prose can possibly describe. The denominator
#    never shrinks to a file count, and the 37-to-458 shape scores 0.08 through
#    the fallback exactly as it does through the node route. See `_file_claims`
#    for why the second term is needed and why an upper bound is sound here.
#
# And one thing to confirm rather than assume: structural nodes carry no
# `source_file` - newer graphify emits Java package-hierarchy nodes with neither
# that nor a label - so a structural-heavy community keys on less than it looks
# like it does, and a wholly structural one keys on nothing. Nodes without a
# `source_file` are therefore excluded from every file key on both sides, never
# collapsed onto a `(repo, "")` key that would make a repository's whole
# structural population one matching key. A community with no file key is left
# where the node route put it: an empty key set must never match another empty
# key set, because that is a carry on no evidence that looks like a perfect one.
FALLBACK_OUTCOMES = (
    "carried",
    "collision",
    "no-file-key",
    "no-file-match",
    "not-identical",
    "below-bar",
    "below-precision",
)


def _membership(graph: dict) -> dict[str, list[str]]:
    members: dict[str, list[str]] = {}
    for node in graph.get("nodes", []):
        community = node.get("community")
        if community is None:
            continue
        members.setdefault(str(community), []).append(node["id"])
    return members


def _file_key(node: dict) -> tuple[str, str] | None:
    """A node's `(repository, source_file)`, or None when it has no source file.

    None rather than `(repo, "")`. Structural nodes carry no `source_file`, so a
    falsy one is an absent measurement: keying it as the empty string would make
    every structural node in a repository one key, and one key that every such
    community shares matches wholesale on no evidence at all. `repo` is kept as
    the empty string where it is absent, because the key is the pair and
    `("", "path")` is a different file from `("repo-a", "path")`.
    """
    source = node.get("source_file")
    if not source:
        return None
    return str(node.get("repo") or ""), str(source)


def _file_membership(graph: dict) -> dict[str, list[list[str]]]:
    """Community id -> its `(repository, source_file)` keys, sorted and unique.

    Lists rather than tuples because this is written as JSON, and sorted because
    two runs on the same graph must produce the same bytes.
    """
    files: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for node in graph.get("nodes", []):
        community = node.get("community")
        key = _file_key(node)
        if community is None or key is None:
            continue
        files[str(community)].add(key)
    return {name: [list(key) for key in sorted(keys)] for name, keys in sorted(files.items())}


def _membership_digest(members: dict) -> str:
    """A fingerprint tying the file snapshot to the membership snapshot it was taken with.

    The two files are written by one `snapshot` run, so they can only disagree
    when something wrote one of them separately - an older library's `snapshot`
    refreshing the membership file while leaving a previous rebuild's file keys
    beside it, which is the shape a pinned release produces. That pairing would
    key the fallback on a clustering nobody is remapping, so `remap` compares
    this digest and withholds the fallback rather than trusting it.
    """
    canonical = json.dumps(members, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    # The second half of the baseline, written in the same run so the two cannot
    # be taken from different graphs. See `FALLBACK_ROUTE`: the remap's fallback
    # needs the old side's file keys, and after a rebuild nothing else can
    # supply them, because the old node ids that would have answered are the
    # ones that have gone.
    files = _file_membership(graph)
    with io.gzip_text(config.SUMMARIES_FILE_SNAPSHOT_PATH) as handle:
        json.dump({"members_digest": _membership_digest(members), "communities": files}, handle)
    keyed = sum(len(v) for v in files.values())
    print(
        f"Snapshotted {len(files)} of those communities by (repository, source_file) "
        f"({keyed} file keys) -> {config.SUMMARIES_FILE_SNAPSHOT_PATH}"
    )
    if not keyed:
        # Not an error: a graph can legitimately be all structural. But a remap
        # that reports "0 carried by the fallback" against this graph has not
        # measured the fallback, and nothing in that output would say so.
        print(
            "No node in this graph carries a source_file, so the file keys are empty and "
            "`summaries remap` has no fallback to fall back to. Structural nodes carry no "
            "source_file; if that is not what this graph is, check the extraction.",
            file=sys.stderr,
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
    summaries = io.read_summaries(config.SUMMARIES_PATH)
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


def _file_index(nodes: Iterable[dict]) -> tuple[dict[tuple[str, str], Counter], dict[str, set]]:
    """The new graph keyed by file: nodes per community per file, and each community's files.

    Both are needed and neither derives the other cheaply: the fallback's recall
    counts *files* landing in a community and its precision counts the *nodes*
    those files account for there. Nodes with no `source_file` appear in neither
    - see `_file_key` - so they count towards a community's node total (the
    precision denominator, taken from the graph) and never towards what the
    prose is credited with describing. A structural-heavy community is therefore
    harder to carry onto through the fallback, not easier, which is the honest
    direction for a key it does not participate in.
    """
    by_file: dict[tuple[str, str], Counter] = defaultdict(Counter)
    files_of: dict[str, set] = defaultdict(set)
    for node in nodes:
        community = node.get("community")
        key = _file_key(node)
        if community is None or key is None:
            continue
        name = str(community)
        by_file[key][name] += 1
        files_of[name].add(key)
    return by_file, files_of


def _read_file_snapshot(old_members: dict) -> tuple[dict[str, set], str]:
    """The old side's file keys, or `({}, why not)`.

    Returns a reason rather than raising or defaulting to empty silently. An
    absent or mismatched file snapshot means the fallback did not run, and a
    remap that prints "0 carried by the fallback" for that reason has measured
    nothing - reporting it as a zero would be a clean verdict over an absent
    measurement.
    """
    path = config.SUMMARIES_FILE_SNAPSHOT_PATH
    if not path.exists():
        return {}, (
            f"no file snapshot at {path}, so the (repository, source_file) fallback did not "
            "run. It is written by `summaries snapshot`; a snapshot taken before this "
            "library recorded file keys has only node ids."
        )
    document = io.read_gzip_json_dict(path)
    recorded = document.get("members_digest")
    if recorded != _membership_digest(old_members):
        return {}, (
            f"{path} was taken with a different membership snapshot, so its file keys "
            "describe a clustering this remap is not carrying. The fallback is withheld "
            "rather than keyed on the wrong baseline - re-run `summaries snapshot` against "
            "the graph these summaries were written for, before re-clustering."
        )
    communities = document.get("communities") or {}
    return {str(name): {tuple(key) for key in keys} for name, keys in communities.items()}, ""


def _file_claims(
    displaced: dict[str, dict],
    old_members: dict,
    old_files: dict[str, set],
    by_file: dict[tuple[str, str], Counter],
    files_of: dict[str, set],
    sizes: Counter,
    bar: float,
    precision: float,
    carry: str,
) -> tuple[dict[str, tuple[str, float, float, bool]], dict[str, dict]]:
    """The fallback route: one attempt at the summaries the node-id route lost entirely.

    Reads `displaced` and considers only `members-gone` - the summaries whose
    members are absent from the graph, which is where the semantic-rename problem
    lives and the only place a file key can say something the node ids cannot.
    Everywhere else the old ids are present and have already answered; a summary
    the node route withdrew as `not-identical`, `below-bar` or `below-precision`
    was measured against evidence that still exists, and giving it a second
    criterion to pass would be an over-correction rather than a fallback. See
    `FALLBACK_ROUTE` for the whole argument.

    Returns the claims it won, keyed like `_claim_targets`'s, and the fallback's
    outcome for every summary it considered - carried or not - so the two routes
    can be counted separately.
    """
    claims: dict[str, tuple[str, float, float, bool]] = {}
    outcomes: dict[str, dict] = {}
    for old_id in sorted(displaced, key=_by_id):
        if displaced[old_id]["reason"] != "members-gone":
            continue
        keys = old_files.get(str(old_id), set())
        if not keys:
            # The dangerous case, refused explicitly. An empty old key set
            # intersects an empty community key set perfectly, so every measure
            # would read 1.0 over no evidence whatsoever - and a wholly
            # structural community has exactly that key set.
            outcomes[old_id] = {"outcome": "no-file-key", "files": 0}
            continue
        measured = _file_measure(
            keys, len(old_members.get(str(old_id), [])), by_file, files_of, sizes
        )
        if measured is None:
            outcomes[old_id] = {"outcome": "no-file-match", "files": len(keys)}
            continue
        entry, share_of_old, share_of_new = measured
        outcome = _fallback_verdict(entry, share_of_old, share_of_new, bar, precision, carry)
        outcomes[old_id] = entry if outcome == "carried" else {**entry, "outcome": outcome}
        if outcome == "carried":
            # `identical` is False for every fallback carry, whatever the file
            # sets say. That flag is #296's marker for "the target holds exactly
            # the set the prose was written about", and by construction it does
            # not here - the members are gone. Recording a file-set match as set
            # equality would hand a downstream check a clean bill for the least
            # exact carry the stage makes; the file-set verdict is reported as
            # `files_exact` instead.
            claims[old_id] = (str(entry["best_target"]), share_of_old, share_of_new, False)
    return claims, {old_id: outcomes[old_id] for old_id in sorted(outcomes, key=_by_id)}


def _landed_order(item: tuple[str, int]) -> tuple[int, tuple[int, str]]:
    """Most files landed first, lowest community id to break a tie.

    A named function rather than a lambda over the loop's own `landed` counter:
    the tie has to break the same way on every run, and `Counter.most_common`
    breaks it on insertion order - which here follows a set iteration, so hash
    randomisation would move the chosen target between processes.
    """
    name, count = item
    return -count, _by_id(name)


def _file_measure(
    keys: set,
    old_size: int,
    by_file: dict[tuple[str, str], Counter],
    files_of: dict[str, set],
    sizes: Counter,
) -> tuple[dict, float, float] | None:
    """What the file key says about one community, before any criterion is applied.

    Returns the report entry alongside the two unrounded shares - the entry
    rounds them for reading, and a criterion must be applied to the full value
    or a share of 0.5995 clears a 0.6 bar - or None when none of the files is in
    the new graph at all.
    """
    landed: Counter = Counter()  # files of this community present in each target
    attributable: Counter = Counter()  # nodes of each target that come from those files
    for key in keys:
        for community, count in by_file.get(key, {}).items():
            landed[community] += 1
            attributable[community] += count
    if not landed:
        return None
    target = min(landed.items(), key=_landed_order)[0]
    matched = landed[target]
    share_of_old = matched / len(keys)
    # Precision in NODES on both sides, never in files: see `FALLBACK_ROUTE`
    # reason 4. Two terms, and the smaller of them is the figure, so the
    # floor is never looser than either.
    #
    # - `attributed` is the share of the target's nodes that come from the
    #   old community's files. It sees a target that absorbed material from
    #   elsewhere - the prose describes a corner of a merged cluster.
    # - `bound` is the old community's node count over the target's. It sees
    #   the case the first term cannot: the same files holding an order of
    #   magnitude more nodes after a rebuild that added a layer over them,
    #   where every node is attributable and the prose still describes a
    #   fraction of what a reader now sees. It is an upper bound rather than
    #   a measurement - the prose described `old_size` nodes, so at most
    #   that many of the target's nodes can be things it described - and
    #   rejecting on an upper bound can only ever under-reject.
    #
    # `bound` is also the closest available analogue of the node route's own
    # precision, and it reproduces the recorded escape: 37 old members in a
    # cluster of 458 scores 0.081 here exactly as it does there.
    target_size = sizes[target]
    attributed = attributable[target] / target_size if target_size else 0.0
    bound = old_size / target_size if target_size else 0.0
    share_of_new = min(attributed, bound)
    entry = {
        "outcome": "carried",
        "files": len(keys),
        "matched_files": matched,
        "best_target": target,
        "share": round(share_of_old, 3),
        "precision": round(share_of_new, 3),
        # The three counts the precision figure is a ratio of, so a reader of
        # the report can re-derive it instead of believing it. It is a min of
        # two ratios, which is exactly the kind of number that gets quoted as
        # though it were one.
        "nodes": old_size,
        "target_nodes": target_size,
        "attributed_nodes": attributable[target],
        "files_exact": keys == files_of.get(target, set()),
    }
    return entry, share_of_old, share_of_new


def _fallback_verdict(
    entry: dict, share_of_old: float, share_of_new: float, bar: float, precision: float, carry: str
) -> str:
    """Which criterion a file-keyed match clears, or the name of the one it fails."""
    if carry == CARRY_EXACT and not entry["files_exact"]:
        return "not-identical"
    if carry != CARRY_EXACT and share_of_old < bar:
        return "below-bar"
    # The precision floor is applied under both criteria, where the node route
    # applies it under `overlap` only. Under `exact` the node route's guarantee
    # is node-set equality, and file-set equality is not that: the same files can
    # hold ten times the nodes after a rebuild that added a layer over them. The
    # floor is what stands in for the guarantee that does not transfer.
    if share_of_new < precision:
        return "below-precision"
    return "carried"


def _report_fallback(
    carried: dict, outcomes: dict[str, dict], considered: int, precision: float, note: str
) -> dict:
    """Count the two routes separately, and return the report's `fallback` block.

    Separately reportable is half of what the fallback is for. The measurement
    that decides whether it should stay is its false-carry rate - prose landing
    on a community it does not describe - and that is a question about the
    fallback-only carries, not about retention. Nobody can sample a set the
    output does not distinguish, so the route is named per carry in the report
    and counted on its own line here.
    """
    by_route = Counter(str(entry.get("route") or NODE_ROUTE) for entry in carried.values())
    print(
        f"Carried by route: {by_route[NODE_ROUTE]} on node ids, "
        f"{by_route[FALLBACK_ROUTE]} on (repository, source_file)"
    )
    tally = Counter(str(entry["outcome"]) for entry in outcomes.values())
    block: dict = {
        "available": not note,
        "considered": considered,
        "outcomes": {name: tally[name] for name in FALLBACK_OUTCOMES if tally[name]},
    }
    if note:
        block["reason"] = note
        # On stderr and only when it would have had something to do. An absent
        # file snapshot is the normal state of a store that has not snapshotted
        # since this route existed, and a run with nothing for the fallback to
        # try is not missing anything.
        if considered:
            print(
                f"Fallback on (repository, source_file) did not run for the {considered} "
                f"summaries whose members are gone: {note}",
                file=sys.stderr,
            )
        return block
    if considered:
        print(
            f"Fallback on (repository, source_file): {tally['carried']} of {considered} "
            "summaries whose members are gone were carried by it; "
            f"{tally['no-file-key']} keyed on no file at all (structural nodes carry no "
            f"source_file), {tally['no-file-match']} matched no file in the new graph, "
            f"{tally['not-identical'] + tally['below-bar']} missed the recall criterion, "
            f"{tally['below-precision']} would have described under {int(precision * 100)}% "
            f"of their new cluster in nodes, {tally['collision']} lost a collision"
        )
    return block


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


def _fallback_claims(
    displaced: dict[str, dict],
    old_members: dict,
    nodes: list,
    sizes: Counter,
    bar: float,
    precision: float,
    carry: str,
) -> tuple[dict[str, tuple[str, float, float, bool]], dict[str, dict], str]:
    """The fallback route's claims and outcomes, or nothing and the reason it did not run."""
    old_files, note = _read_file_snapshot(old_members)
    # Not run at all rather than run over an empty old side. Running it would
    # report every candidate as `no-file-key` - "this community keys on no
    # file" - when the truth is that no community keys on anything because the
    # snapshot could not be read. A wrong reason is worse than no reason: it
    # names something to go and look at that is not what happened.
    if note:
        return {}, {}, note
    claims, outcomes = _file_claims(
        displaced,
        old_members,
        old_files,
        *_file_index(nodes),
        sizes,
        bar,
        precision,
        carry,
    )
    return claims, outcomes, note


def _resolve_targets(
    summaries: dict,
    claims: dict[str, tuple[str, float, float, bool]],
    routes: dict[str, str],
    displaced: dict[str, dict],
    fallback_outcomes: dict[str, dict],
) -> tuple[dict[str, str], dict[str, dict]]:
    """Pass 2: one summary per new cluster, and the carried record for each.

    Mutates `displaced` with the losers and `fallback_outcomes` with the ones it
    took back, so the two tallies never disagree about the same summary.
    """
    # A contested new cluster keeps the summary whose old cluster
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
    #
    # A fallback claimant never outranks a node-id claimant for the same target,
    # whatever the two shares say. One is a share of nodes and the other a share
    # of files, so sorting them against each other would let a coarse
    # measurement beat a fine one - and the primary route's claim is the one
    # backed by ids the graph still holds. `ROUTE_RANK` sorts first for that
    # reason; the share rule then decides within a route, as before.
    by_target: dict[str, list[tuple[str, float, float, bool, str]]] = {}
    for old_id, (target, share_of_old, share_of_new, identical) in claims.items():
        by_target.setdefault(target, []).append(
            (old_id, share_of_old, share_of_new, identical, routes[old_id])
        )
    remapped: dict[str, str] = {}
    carried: dict[str, dict] = {}
    for target, claimants in by_target.items():
        claimants.sort(key=lambda c: (ROUTE_RANK[c[4]], -c[1], (len(c[0]), c[0])))
        winner, winner_share, winner_precision, winner_identical, winner_route = claimants[0]
        remapped[target] = summaries[winner]
        carried[target] = {
            "from": winner,
            "share": round(winner_share, 3),
            "precision": round(winner_precision, 3),
            # The mark #296 asks for: under a tolerance this says which carried
            # prose describes a set the graph no longer holds, so a downstream
            # check can find it without recomputing the overlap.
            "exact": winner_identical,
            # Which key carried it. Half the value of the fallback is that the
            # next refresh can measure it: the question the fallback has to
            # answer is its false-carry rate - prose landing on a community it
            # does not describe - and that measurement needs the fallback-only
            # carries picked out of the carried map, not a total.
            "route": winner_route,
        }
        if winner_route == FALLBACK_ROUTE:
            carried[target].update(
                {
                    key: fallback_outcomes[winner][key]
                    for key in (
                        "files",
                        "matched_files",
                        "files_exact",
                        "nodes",
                        "target_nodes",
                        "attributed_nodes",
                    )
                }
            )
        for loser, loser_share, _, _, loser_route in claimants[1:]:
            displaced[loser] = {
                "reason": "collision",
                "best_target": target,
                "share": round(loser_share, 3),
                "route": loser_route,
                "prose": summaries[loser],
            }
            if loser_route == FALLBACK_ROUTE:
                # The fallback claimed it and pass 2 took it away again. Saying
                # "carried" in the fallback's own tally would make the two
                # counts disagree about the same summary, and the tally is what
                # the false-carry measurement will be drawn from.
                fallback_outcomes[loser]["outcome"] = "collision"

    return remapped, carried


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

    The write is gated on a digest of the prose, the way `merge`'s is: a remap
    that carries every summary onto the id it already holds rewrites nothing.

    A second, narrower route runs after the first: for the summaries the node-id
    route lost because their members are gone from the graph entirely, a key of
    `(repository, source_file)` gets one attempt at them. Only there, and it
    never re-decides anything the node ids decided - see `FALLBACK_ROUTE` for
    why the file key is a fallback rather than the key, and how it keeps the
    precision floor honest while measuring recall in files.

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
    # The whole document, not just the prose: the write below is gated on the
    # digest the metadata block records, exactly as `merge`'s is.
    document = io.read_json_dict(config.SUMMARIES_PATH)
    summaries = io.summaries_body(document)
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
    routes = dict.fromkeys(claims, NODE_ROUTE)

    # The fallback, over what the node-id route lost outright. Sequenced here
    # rather than inside `_claim_targets` so the shape of the code says the
    # shape of the rule: the primary route runs to completion and reaches its
    # verdicts, and this reads those verdicts and reopens exactly one of them.
    sizes = Counter(new_community.values())
    # Counted before the fallback runs, because it is the fallback's denominator
    # and the fallback removes its successes from `displaced`. Taken from the
    # verdicts rather than recomputed, so "considered" is the set the route
    # actually looked at.
    considered = sum(1 for entry in displaced.values() if entry["reason"] == "members-gone")
    file_claims, fallback_outcomes, fallback_note = _fallback_claims(
        displaced, old_members, nodes, sizes, bar, precision, carry
    )
    for old_id, claim in file_claims.items():
        claims[old_id] = claim
        routes[old_id] = FALLBACK_ROUTE
        # Removed from `displaced` only once it is genuinely claimed. A fallback
        # that ran and refused must leave the node route's verdict standing,
        # prose and reason intact, or the withdrawn file loses the paragraph
        # somebody is meant to go and re-author.
        del displaced[old_id]

    remapped, carried = _resolve_targets(summaries, claims, routes, displaced, fallback_outcomes)

    body = dict(sorted(remapped.items(), key=lambda kv: int(kv[0])))
    # Through the shared writer, which is gated on the prose (#299, #313). An
    # identity remap carries every summary onto the id it already holds, so the
    # document this stage would write is the document already committed;
    # rewriting it produces a whole-file diff with no semantic change, in an
    # artefact a human reviews and the only review LLM-authored prose gets.
    #
    # No coverage is passed, so none is recorded. A coverage block says how much
    # of a community the digest its prose was written from showed; after a
    # re-cluster the carried prose was written from the digest of the community
    # it left, so re-keying that block onto the new id would state a sampling
    # figure about a community nobody sampled. `merge` records it again the next
    # time prose is merged, and nothing is lost that survived before: this write
    # dropped the metadata block whole.
    unchanged = _write_merged_summaries(document, body, {})
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
    if unchanged:
        print(
            f"Retained {len(remapped)} of {total} summaries ({share}%), "
            f"withdrew {len(displaced)}; the prose is unchanged, so "
            f"{config.SUMMARIES_PATH} was not rewritten"
        )
    else:
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
    fallback = _report_fallback(carried, fallback_outcomes, considered, precision, fallback_note)
    # The fallback's own finding, on the entry it did not rescue, nested rather
    # than merged into it. `best_target` and `share` on a displaced entry are
    # the node route's, measured in nodes; the fallback's near miss is measured
    # in files against a different key. Writing one over the other would put two
    # different quantities under one name, which is how a report starts reading
    # as more comparable than it is.
    for old_id, outcome in fallback_outcomes.items():
        if old_id in displaced:
            displaced[old_id]["fallback"] = outcome
    # The report is the spool: displaced prose is raw material for the
    # backfill (revise against the new digest, never trust unverified), and
    # the carried map is what lets `verify` split its flag rate by
    # carried-versus-authored - remap preserves coverage while degrading
    # grounding, so the two must be measured together.
    io.write_json(
        config.REMAP_REPORT_PATH,
        {
            # Which clustering these ids belong to. Without it the carried map is
            # a set of small integers that resolves against any partition, so
            # `verify` cannot tell a split of these summaries from a split of
            # somebody else's (#305).
            "clustering": _clustering_of_nodes(nodes),
            "carried": dict(sorted(carried.items(), key=lambda kv: int(kv[0]))),
            "displaced": dict(sorted(displaced.items(), key=lambda kv: int(kv[0]))),
            # Whether the second route ran at all, over how many, and what it
            # decided. `available: false` with a reason is not a zero: a remap
            # that could not read the file snapshot has not measured the
            # fallback, and a bare 0 would read as though it had.
            "fallback": fallback,
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


def _report_evidence_split(
    unsupported: list[tuple[str, set[str]]], evidence: dict[str, str | None]
) -> tuple[int, int]:
    """The flagged total split by how much of its evidence each digest showed.

    This is the subtraction the coverage block exists for. A term missing from a
    digest that withheld nothing is missing from the community; one missing from a
    digest that showed twelve of three hundred says nothing either way. Splitting
    them is what lets an operator act on the first without reading the second.

    Returns `(sampled, unrecorded)` so the caller can decide whether the older
    paragraph about unsampled content still applies. The arithmetic is printed
    because a split that does not add up is how a count stops being a finding.
    """
    complete = [cid for cid, _ in unsupported if evidence.get(cid) == ""]
    sampled = [cid for cid, _ in unsupported if evidence.get(cid)]
    unrecorded = [cid for cid, _ in unsupported if evidence.get(cid) is None]
    print(
        f"  Of those, {len(complete)} of them cite a digest that withheld nothing, so the "
        "community holds no such node, feature or ticket and that is a finding to act on; "
        f"{len(sampled)} cite one that did not show everything, which proves nothing either "
        f"way; {len(unrecorded)} cite one that recorded no coverage, so nothing can say which "
        "of the two it is - re-run `summaries extract` to record it."
    )
    # "the split reconciles" rather than "reconciled", which the estate pass's own
    # arithmetic line already owns: two different reconciliations sharing a word is
    # how a check asserting one of them starts passing on the other.
    print(
        f"    the split reconciles: {len(complete)} + {len(sampled)} + {len(unrecorded)} = "
        f"{len(unsupported)} flagged."
    )
    return len(sampled), len(unrecorded)


def _report_verify_totals(
    unsupported: list[tuple[str, set[str]]],
    speculative: list[tuple[str, list[str]]],
    orphaned: list[str],
    evidence: dict[str, str | None],
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
    unexplained = sum(_report_evidence_split(unsupported, evidence)) if unsupported else 0
    if absent is None:
        if unexplained:
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
    counts: tuple[int, int],
    unsupported: list[tuple[str, set[str]]],
    speculative: list[tuple[str, list[str]]],
    orphaned: list[str],
    evidence: dict[str, str | None],
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

    `evidence` carries what each flagged digest recorded about its own sampling,
    which turns that inference into a subtraction: absence from a digest that
    withheld nothing is absence from the community, and the label says so rather
    than leaving every finding to be discounted at the same rate.
    """
    checked, total = counts
    print(f"Verified {checked} of {total} summaries against their digests.")
    for cid, extra in unsupported:
        cited = ", ".join(sorted(extra))
        sampled = evidence.get(cid)
        if sampled is None:
            print(f"  [not in digest] community {cid} cites: {cited}")
        elif sampled:
            print(f"  [not in digest, {sampled} sampled] community {cid} cites: {cited}")
        else:
            print(f"  [not in digest, nothing withheld] community {cid} cites: {cited}")
    for cid, hedges in speculative:
        words = sorted({hedge.lower() for hedge in hedges})
        print(f"  [speculation] community {cid}: {', '.join(words)}")
    for cid in orphaned:
        print(f"  [no digest] community {cid} has prose but no evidence to check it against")
    if absent:
        _report_absent_terms(absent, classified)
    _report_verify_totals(unsupported, speculative, orphaned, evidence, absent, classified)


def _unshown(entry: object) -> int:
    """How many a coverage field held back, or 0 when it does not say."""
    if not isinstance(entry, dict) or not isinstance(entry.get("unshown"), int):
        return 0
    return entry["unshown"]


def truncated_fields(digest: dict) -> list[str] | None:
    """Which of the digest's capped fields held something back.

    `[]` and `None` are deliberately different answers, and confusing them is the
    one way this whole change makes things worse. `[]` is a measured "the digest
    showed everything there was", which is what lets a term absent from it be
    called a finding. `None` is "nothing recorded what was sampled" - every store
    built before the coverage block, and any hand-edited digest - and it must
    never read as an excuse, or the check goes green on those stores without
    having examined anything.
    """
    coverage = digest.get("coverage")
    if not isinstance(coverage, dict):
        return None
    return [field for field in COVERAGE_FIELDS if _unshown(coverage.get(field)) > 0]


def evidence_base(digest: dict) -> str | None:
    """What the digest sampled, as a phrase; `""` when it withheld nothing.

    `None` when the digest records no coverage - see `truncated_fields` for why
    that is not the same answer as `""`.
    """
    fields = truncated_fields(digest)
    if fields is None:
        return None
    coverage = digest.get("coverage") or {}
    return ", ".join(
        f"{field} {coverage[field]['shown']} of {coverage[field]['total']}" for field in fields
    )


def _ungrounded(text: str, digest: dict) -> set[str]:
    """Identifiers the prose cites that the evidence does not contain."""
    evidence = {_normalise(item) for item in _digest_identifiers(digest)}
    return {
        cited
        for cited in prose_identifiers(text)
        if _normalise(cited) and _normalise(cited) not in evidence
    }


def _clustering_fingerprint(sizes: dict[str, int]) -> str:
    """An identity for one clustering: its significant communities and their sizes.

    What `remap` records in its report and what `verify` recomputes, so the two
    can be compared. Timestamps cannot answer "was this report written for this
    partition" - see `io.layer_digests` for the same argument about pages and the
    layers they embed - and the answer matters here because every id in a report
    written for a previous partition still resolves against the current
    summaries.

    Sorted before hashing: a fingerprint that depended on dict order would differ
    between two runs over the same graph, and a freshness check that reports stale
    at random is worse than none.
    """
    payload = ";".join(f"{cid}:{sizes[cid]}" for cid in sorted(sizes))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _clustering_of_nodes(nodes: list[dict]) -> str:
    """The fingerprint of the clustering a graph holds, counted as `extract` counts it.

    Mirrors `extract`'s bucketing on purpose - the same default for a node
    carrying no community, the same significance threshold - because the two
    values are compared: this is recorded in the remap report and `verify`
    recomputes it from the digests `extract` wrote. The mirror is checked rather
    than asserted; `tests/test_summaries_provenance_freshness.py` runs both
    stages over one graph.

    "" when nothing clears the threshold, which reads through as "no identity
    recorded" rather than as the identity of an empty clustering.
    """
    sizes = Counter(str(node.get("community", -1)) for node in nodes)
    significant = {cid: n for cid, n in sizes.items() if n >= config.MIN_COMMUNITY_SIZE}
    return _clustering_fingerprint(significant) if significant else ""


def _clustering_of_digests(digests: dict[str, dict]) -> str:
    """The same fingerprint, from the digests `verify` has already loaded.

    Free, and that is why the check is affordable in `verify`: `extract` writes
    each community's size into its digest, so nothing has to read the graph to
    answer whether the clustering has moved.

    "" when any digest carries no size. A digest file this library did not write
    cannot be compared, and a fingerprint over the ones that do carry a size
    would answer a different question - it would report every hand-made fixture
    as a different clustering.
    """
    sizes: dict[str, int] = {}
    for community, digest in digests.items():
        size = digest.get("size")
        if not isinstance(size, int):
            return ""
        sizes[community] = size
    return _clustering_fingerprint(sizes) if sizes else ""


def _mtime(path: Path) -> float:
    """A file's modification time, or 0.0 when it cannot be read - which reads
    through as "older than anything", so an unreadable report is treated as one
    that cannot be shown to be current."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _stale_provenance(report: dict, digests: dict[str, dict]) -> str:
    """Why the carried-versus-authored split must not be printed, or "" when it may.

    Two checks, because a report written before this one was recorded still has
    to be judged:

    - the recorded clustering against the current one, which is the real answer:
      a re-cluster moves community sizes, so a report written for a previous
      partition fingerprints differently whatever the filesystem says;
    - failing that, the report's age against the digests'. `extract` rewrites the
      digests for a new partition, so a report older than them may describe a
      previous one. A proxy, and it fails towards withholding the number: it
      fires on a re-extract that changed nothing, and it misses a checkout that
      rewrote both timestamps.
    """
    recorded = report.get("clustering")
    current = _clustering_of_digests(digests)
    if isinstance(recorded, str) and recorded and current:
        if recorded == current:
            return ""
        return (
            "  grounding by provenance: not reported. "
            f"{config.REMAP_REPORT_PATH.name} was written for clustering {recorded} and these "
            f"digests are {current}, so its carried and authored groups are a different "
            "partition of this estate. Community ids are small integers and are reused across "
            "re-clusters, so its ids still resolve against these summaries and the split would "
            "have read as ordinary. Re-run `summaries remap` to rewrite the report for this "
            "clustering."
        )
    if _mtime(config.REMAP_REPORT_PATH) >= _mtime(config.SUMMARIES_INPUT_PATH):
        return ""
    return (
        "  grounding by provenance: not reported. "
        f"{config.REMAP_REPORT_PATH.name} records no clustering and is older than "
        f"{config.SUMMARIES_INPUT_PATH.name}, so it may have been written for a previous "
        "partition - and one would still resolve, because community ids are reused across "
        "re-clusters. Re-run `summaries remap` to rewrite the report for this clustering."
    )


# The three states `remap` can leave a carried summary in, in the order the split
# prints them: the group a reader should slow down for first. Named once because
# each label appears both in the line and in the note under it, and a label that
# drifted between the two would describe a group nobody could find.
_CARRIED_MOVED = "carried across a move"
_CARRIED_UNCHANGED = "carried unchanged"
_CARRIED_UNRECORDED = "carried with no record of the move"


def _provenance_groups(report: dict, checked: list[str]) -> dict[str, list[str]]:
    """`checked` split by what the last remap actually did to each summary.

    Three carried states rather than one, because "present in the last remap
    report" and "written for a different cluster and carried across the move"
    are different quantities and coincide only when the remap moved something.
    An identity remap - unchanged clustering, every summary retained - puts every
    summary in the report, and the split called them all carried: the alarming
    reading printed for the harmless case, which is how a figure gets discounted
    and then the alarming one with it (#314).

    `remap` already records the distinction per summary, so this reads it rather
    than recomputing it: `"exact"` is whether the target holds exactly the set the
    prose was written about (#296). Membership that did not move means the prose
    still describes what it was written about, whatever community id it now sits
    under - a re-keying moves the label, not the nodes the prose cites, and the
    digest the grounding check reads is the same digest either way.

    A missing `exact` is not a true one. A report written before #296 recorded it
    carries no such field, and calling those "unchanged" would print the
    reassuring reading of a measurement nobody took, so they get a state of their
    own and the line says how to record it.
    """
    carried = report.get("carried", {})
    groups: dict[str, list[str]] = {
        _CARRIED_MOVED: [],
        _CARRIED_UNCHANGED: [],
        _CARRIED_UNRECORDED: [],
        "authored": [],
    }
    for cid in checked:
        if cid not in carried:
            groups["authored"].append(cid)
            continue
        entry = carried[cid]
        exact = entry.get("exact") if isinstance(entry, dict) else None
        if exact is True:
            groups[_CARRIED_UNCHANGED].append(cid)
        elif exact is False:
            groups[_CARRIED_MOVED].append(cid)
        else:
            groups[_CARRIED_UNRECORDED].append(cid)
    return groups


def _report_provenance_split(
    checked: list[str], unsupported: list[tuple[str, set[str]]], digests: dict[str, dict]
) -> None:
    """Grounding flag rate split by provenance, when a remap report exists.

    Remap preserves coverage while degrading grounding (measured: 9% flagged
    for prose authored on its own digest, 37% for prose carried across a
    re-cluster), so retention must never be read without this line beside it.

    That degradation belongs to prose carried *across a move*, which is why the
    line names the three states of `_provenance_groups` rather than one carried
    group: a summary that went through a remap which did not move its community
    grounds like authored prose, and reporting it beside the 37% figure invites
    the opposite conclusion (#314).

    Which is why the split is withheld rather than qualified when the report
    describes another clustering. A line that load-bearing gets read, and a
    reader invited to conclude that carried prose grounds better than authored
    prose has no way to see that the two groups are not the groups being
    described: nothing fails and nothing comes back empty on a stale report,
    because community ids are small integers and get reused, so every id in a
    report written for a previous partition still resolves against the current
    summaries. The reason goes to stderr in the line's place, so a withheld
    number is visible rather than silently absent.
    """
    report = io.read_json_dict(config.REMAP_REPORT_PATH)
    if not report.get("carried", {}):
        return
    # `withheld` rather than `stale`: the guard of that name in `adrift` is a
    # mutation-gate site, and an entry's `find` must match one line of the file.
    withheld = _stale_provenance(report, digests)
    if withheld:
        print(withheld, file=sys.stderr)
        return
    flagged = {cid for cid, _ in unsupported}

    def rate(group: list[str]) -> str:
        if not group:
            return "n/a (0 checked)"
        hit = sum(1 for cid in group if cid in flagged)
        return f"{100 * hit // len(group)}% ({hit} of {len(group)})"

    groups = _provenance_groups(report, checked)
    # An empty group still prints: "n/a (0 checked)" is what tells a reader the
    # group is empty rather than unmeasured. The unrecorded one is the exception,
    # because no report this library writes now can populate it and a permanent
    # zero on the line would train the eye past the rest of it.
    parts = [
        f"{label} {rate(members)}"
        for label, members in groups.items()
        if members or label != _CARRIED_UNRECORDED
    ]
    print(
        f"  grounding by provenance: {', '.join(parts)} - only prose carried "
        "across a move describes a set it was not written about, so a gap there "
        "is expected and is the signal to revise rather than trust; prose whose "
        "community did not move is evidence about this clustering exactly as "
        "authored prose is"
    )
    if groups[_CARRIED_UNRECORDED]:
        print(
            f"  ({len(groups[_CARRIED_UNRECORDED])} of those carried summaries predate the "
            "record of whether the remap moved them - re-run `summaries remap` to record it.)"
        )


def _verify_exit(
    strict: bool,
    estate: bool,
    digest_findings: list,
    orphaned: list,
    absent: dict | None,
) -> int:
    """Whether to fail, and on which finding.

    Under `--estate`, fail on what is genuinely unbacked rather than on what a
    12-node sample failed to mention.

    Without it, `digest_findings` is the flagged set minus what the coverage block
    explains: a term the digest never showed no longer fails a run, and one absent
    from a digest that withheld nothing still does. A digest recording no coverage
    keeps its previous meaning and stays blocking, so an existing CI invocation
    against an existing store does not silently turn green.
    """
    if not strict:
        return 0
    blocking = (absent or orphaned) if estate else (digest_findings or orphaned)
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

# What separates one name from the next *inside a descriptive label*. Semantic and
# document nodes are labelled with a phrase rather than a bare name - a widget
# word, the field it is bound to and the wording a user reads, in one string - and
# an identifier in the middle of one is unreachable while the prose either side of
# it stays attached.
#
# Defined as the complement of the characters a name is spelled with, rather than
# as a list of the punctuation seen so far. The two characters the report named
# were a space and an `=`, but the same label wrapped its user-facing wording in a
# parenthesis and two apostrophes, and the next label will use something else; a
# complement needs no further amendment, and it says what it means - a name is
# letters, digits and the separators above, so everything else is prose.
_PHRASE_SEPARATORS = re.compile(r"[^A-Za-z0-9/@.\-_:]+")


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

    A phrase label is a sequence of names with prose between them, so it is cut
    twice: into words on the phrase separators, and then each word into segments
    on the name separators. `Radio someField=A_CONSTANT ('some wording')` offers
    `someField` and `A_CONSTANT` as well as `CONSTANT`, where before it offered
    only the two fragments the name separators happened to fall on. That is the
    whole of #303: the identifier was in the citing community's own node and the
    report said the store could not speak about it.

    Both cuts, not the finer one alone. The segments of the *whole* label are kept
    as well, so `Feature: My Widget` still offers ` My Widget` for prose citing
    `MyWidget`. A fix for false absences must not create new ones by narrowing
    somewhere else.

    And it stops at the phrase. Case transitions are deliberately not split on:
    offering `delivery` and `Window` out of `deliveryWindow` would corroborate a
    term against any label that merely mentions the other half of it, which is the
    substring match this rule exists instead of.

    This deliberately loosens a check whose job is not lying, so it trades false
    positives for false negatives, which fail in the reassuring direction.
    `MIN_SEGMENT_MATCH` and the count reported by `absent_from_estate` are what
    keep that trade visible rather than assumed.
    """
    # `part != identifier` so a bare name is not offered as its own segment: a
    # whole identifier is already matched as one, and adding it here would credit
    # the segment counter for matches the strict rule was making anyway.
    words = {part for part in _PHRASE_SEPARATORS.split(identifier) if part and part != identifier}
    parts = words | {
        part for value in (identifier, *words) for part in _SEGMENT_SEPARATORS.split(value) if part
    }
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

    The digest check is also split, by what each digest recorded about its own
    sampling. A term absent from a digest that withheld nothing is absent from the
    community, and `--strict` fails on it; one absent from a digest that showed a
    fraction of its nodes proves nothing and no longer fails a run; one whose
    digest recorded no coverage keeps the previous meaning and still fails,
    because unknown must not read as excused.
    """
    loaded = io.read_json(config.SUMMARIES_INPUT_PATH, default=[]) or []
    digests = {str(d["id"]): d for d in loaded if isinstance(d, dict) and "id" in d}
    prose = io.read_summaries(config.SUMMARIES_PATH)
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
            "whole identifier (scoped packages, Java packages, module addresses, "
            "identifiers inside a descriptive label)"
        )
    # What each flagged digest recorded about its own sampling. A term the digest
    # never showed is excused; one absent from a digest that withheld nothing is a
    # finding; one whose digest recorded nothing is neither, and stays blocking
    # because unknown must not read as excused.
    evidence = {cid: evidence_base(digests[cid]) for cid, _ in unsupported}
    excused = {cid for cid, _ in unsupported if evidence[cid]}
    conclusive = [(cid, terms) for cid, terms in unsupported if cid not in excused]
    _report_verify(
        (len(checked), len(prose)), unsupported, speculative, orphaned, evidence, absent, classified
    )
    _report_provenance_split(checked, unsupported, digests)
    # Under --estate, fail on what is genuinely unbacked rather than on what a
    # 12-node sample failed to mention. Without it, the old behaviour stands -
    # sharpened by the coverage block - so an existing CI invocation does not
    # silently change meaning.
    return _verify_exit(strict, estate, conclusive, orphaned, absent)


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
