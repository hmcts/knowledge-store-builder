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

  4. knowledgestore summaries remap [--bar 0.6] [--floor 10] [--coverage 0.5]
       After re-clustering, carries summaries onto the new ids by membership
       overlap and reports retention. Refuses on an unclustered graph.

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
from pathlib import Path


from . import config
from . import graph_files
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
        # Sonar S8707: reading a caller-supplied path is this maintainer CLI's
        # purpose; it runs offline against a local clone with no privilege
        # boundary to cross.
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


# The share of an old cluster's members that must land in one new cluster before
# its summary is carried across. Below this the summary is dropped: prose on the
# wrong cluster reads as authoritative and is worse than no prose. 0.6 has been
# used across several estate refreshes and behaved sensibly.
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

# How much of the NEW cluster a carried summary must describe. The carry bar
# above measures recall - how much of the old cluster stayed together - and says
# nothing about how much of what a reader now sees the prose covers. Measured on
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
    """
    counts = (len(members), sum(len(ids) for ids in members.values()))
    note = graph_files.disagreement(
        config.GRAPH_PATH,
        counts,
        "Snapshots key the remap, so a snapshot of the wrong graph mis-keys every "
        "carried summary. Decompress the committed graph over graph.json, or remove "
        "the stale graph.json, and re-run.",
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
    precision: float = 0.0,
) -> tuple[dict, dict, list, list, list]:
    """Each summary's best new cluster, and what fell out on the way.

    Returns the surviving claims plus the ways a summary is lost: its members
    gone from the graph entirely, its best overlap short of `bar` (recall), or
    the target cluster so much larger that the prose describes a corner of it
    (`precision`). Sorted for a deterministic tiebreak.
    """
    sizes = Counter(new_community.values())
    claims: dict[str, tuple[str, float, float]] = {}
    displaced: dict[str, dict] = {}
    below_bar: list[str] = []
    members_gone: list[str] = []
    below_precision: list[str] = []
    for old_id in sorted(summaries, key=lambda k: (len(k), k)):
        members = old_members.get(str(old_id))
        if not members:
            members_gone.append(old_id)
            displaced[old_id] = {
                "reason": "members-gone",
                "best_target": None,
                "share": None,
                "prose": summaries[old_id],
            }
            continue
        landed = Counter(new_community[m] for m in members if m in new_community)
        if not landed:
            members_gone.append(old_id)
            displaced[old_id] = {
                "reason": "members-gone",
                "best_target": None,
                "share": None,
                "prose": summaries[old_id],
            }
            continue
        target, count = landed.most_common(1)[0]
        share_of_old = count / len(members)
        if share_of_old < bar:
            below_bar.append(old_id)
            displaced[old_id] = {
                "reason": "below-bar",
                "best_target": target,
                "share": round(share_of_old, 3),
                "prose": summaries[old_id],
            }
            continue
        share_of_new = count / sizes[target] if sizes[target] else 0.0
        if share_of_new < precision:
            below_precision.append(old_id)
            displaced[old_id] = {
                "reason": "below-precision",
                "best_target": target,
                "share": round(share_of_old, 3),
                "precision": round(share_of_new, 3),
                "prose": summaries[old_id],
            }
            continue
        claims[old_id] = (target, share_of_old, share_of_new)
    return claims, displaced, below_bar, members_gone, below_precision


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
) -> int:
    """Carry committed summaries onto new community ids after a re-cluster.

    For each summary, find the new cluster holding the largest share of its old
    members and carry it there when that share meets `bar`. Drop it otherwise,
    and report retention so the cost of the re-cluster is a measured number.
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

    claims, displaced, below_bar, members_gone, below_precision = _claim_targets(
        summaries, old_members, new_community, bar, precision
    )

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
    by_target: dict[str, list[tuple[str, float, float]]] = {}
    for old_id, (target, share_of_old, share_of_new) in claims.items():
        by_target.setdefault(target, []).append((old_id, share_of_old, share_of_new))
    remapped: dict[str, str] = {}
    carried: dict[str, dict] = {}
    collisions: list[str] = []
    for target, claimants in by_target.items():
        claimants.sort(key=lambda c: (-c[1], (len(c[0]), c[0])))
        winner, winner_share, winner_precision = claimants[0]
        remapped[target] = summaries[winner]
        carried[target] = {
            "from": winner,
            "share": round(winner_share, 3),
            "precision": round(winner_precision, 3),
        }
        for loser, loser_share, _ in claimants[1:]:
            collisions.append(loser)
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
    total = len(summaries)
    share = (100 * len(remapped) // total) if total else 0
    print(f"Retained {len(remapped)} of {total} summaries ({share}%) -> {config.SUMMARIES_PATH}")
    print(
        f"Dropped: {len(below_bar)} below {int(bar * 100)}% overlap, "
        f"{len(members_gone)} whose members are gone, "
        f"{len(collisions)} merged-cluster collisions, "
        f"{len(below_precision)} describing under {int(precision * 100)}% of their new cluster"
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


def _report_verify_totals(
    unsupported: list[tuple[str, set[str]]],
    speculative: list[tuple[str, list[str]]],
    orphaned: list[str],
    absent: dict[str, set[str]] | None,
) -> None:
    """The counts, and what they do and do not mean."""
    if not (unsupported or speculative or orphaned):
        print("  nothing cited beyond its digest.")
        return
    print(
        f"  {len(unsupported)} citing beyond their digest, {len(speculative)} speculative, "
        f"{len(orphaned)} without a digest."
    )
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
        for cid, terms in sorted(absent.items()):
            print(f"  [not in graph] community {cid} cites: {', '.join(sorted(terms))}")
    _report_verify_totals(unsupported, speculative, orphaned, absent)


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
    if estate and matched_by_segment:
        print(
            f"  {matched_by_segment} cited terms matched a name segment rather than a "
            "whole identifier (scoped packages, Java packages, module addresses)"
        )
    _report_verify(len(checked), len(prose), unsupported, speculative, orphaned, absent)
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
    if arguments[:1] == ["remap"]:
        parser = argparse.ArgumentParser(prog="knowledgestore summaries remap")
        parser.add_argument("--bar", type=float, default=DEFAULT_BAR)
        parser.add_argument("--floor", type=int, default=DEFAULT_FLOOR)
        parser.add_argument("--coverage", type=float, default=DEFAULT_COVERAGE)
        parser.add_argument("--precision", type=float, default=DEFAULT_PRECISION)
        options = parser.parse_args(arguments[1:])
        return remap(
            bar=options.bar,
            floor=options.floor,
            coverage=options.coverage,
            precision=options.precision,
        )
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
