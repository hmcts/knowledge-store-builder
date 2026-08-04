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
from . import io
from . import kinds


TOP_NODES = 12
TOP_FEATURES = 5
TOP_TICKETS = 8
MIN_SUMMARY_LEN = 60
MAX_SUMMARY_LEN = 700


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
        "label": labels.get(str(community), f"Community {community}"),
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


def _membership(graph: dict) -> dict[str, list[str]]:
    members: dict[str, list[str]] = {}
    for node in graph.get("nodes", []):
        community = node.get("community")
        if community is None:
            continue
        members.setdefault(str(community), []).append(node["id"])
    return members


def snapshot() -> int:
    """Record community membership before a re-cluster moves the ids."""
    graph = io.read_json_dict(config.GRAPH_PATH)
    members = _membership(graph)
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


def remap(
    bar: float = DEFAULT_BAR,
    floor: int = DEFAULT_FLOOR,
    coverage: float = DEFAULT_COVERAGE,
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
    if len(summaries) < floor:
        print(
            f"Refusing to remap: only {len(summaries)} summaries loaded from "
            f"{config.SUMMARIES_PATH} (floor {floor}). A mis-specified path looks like this. "
            "Pass --floor to lower it for a genuinely small store.",
            file=sys.stderr,
        )
        return 1
    nodes = io.read_json_dict(config.GRAPH_PATH).get("nodes", [])
    new_community = {
        node["id"]: str(node["community"]) for node in nodes if node.get("community") is not None
    }
    # Counted over nodes, not over `new_community`, which is keyed by id: a merged
    # graph can repeat an id, and the dict would collapse those and understate
    # coverage enough to refuse a healthy graph.
    clustered = sum(1 for node in nodes if node.get("community") is not None)
    if nodes and clustered / len(nodes) < coverage:
        print(
            f"Refusing to remap: only {clustered} of {len(nodes)} nodes in "
            f"{config.GRAPH_PATH} carry a community "
            f"({clustered / len(nodes):.1%}, floor {coverage:.0%}). The "
            "graph is effectively unclustered, so every summary would be dropped "
            "and reported as legitimate churn. A clustering step that printed "
            "success without writing its result looks exactly like this — check "
            "the graph's community coverage before re-running. Pass --coverage to "
            "lower the floor for a deliberately sparse graph.",
            file=sys.stderr,
        )
        return 1
    snapshot_ids = {node for ids in old_members.values() for node in ids}
    if snapshot_ids and not (snapshot_ids & set(new_community)):
        print(
            "Refusing to remap: the snapshot and the graph share no node ids, so "
            "this is the wrong snapshot. Proceeding would drop every summary and "
            "report it as legitimate 0% retention.",
            file=sys.stderr,
        )
        return 1

    # Pass 1: every old summary's best new cluster and the share of its
    # members that landed there. Sorted for a deterministic tiebreak.
    claims: dict[str, tuple[str, float]] = {}
    displaced: dict[str, dict] = {}
    below_bar: list[str] = []
    members_gone: list[str] = []
    for old_id in sorted(summaries, key=lambda k: (len(k), k)):
        members = old_members.get(str(old_id))
        landed = (
            Counter(new_community[m] for m in members if m in new_community) if members else None
        )
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
        claims[old_id] = (target, share_of_old)

    # Pass 2: a contested new cluster keeps the summary whose old cluster
    # contributed the largest share of itself - it describes more of the merged
    # result than a summary that barely arrived. Lowest old id only breaks
    # ties, deterministically. (Winner-by-lowest-id regardless of fit was the
    # old rule; measured on a real refresh, the share rule chose a
    # better-fitting summary for 36 of 86 contested clusters, median +16.7%
    # overlap, with identical retention.)
    by_target: dict[str, list[tuple[str, float]]] = {}
    for old_id, (target, share_of_old) in claims.items():
        by_target.setdefault(target, []).append((old_id, share_of_old))
    remapped: dict[str, str] = {}
    carried: dict[str, dict] = {}
    collisions: list[str] = []
    for target, claimants in by_target.items():
        claimants.sort(key=lambda c: (-c[1], (len(c[0]), c[0])))
        winner, winner_share = claimants[0]
        remapped[target] = summaries[winner]
        carried[target] = {"from": winner, "share": round(winner_share, 3)}
        for loser, loser_share in claimants[1:]:
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
        f"{len(collisions)} merged-cluster collisions"
    )
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
# capitalised words - "Welsh", "Angular", "Common Platform" - that are not claims
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


def _report_verify(
    checked: int,
    total: int,
    unsupported: list[tuple[str, set[str]]],
    speculative: list[tuple[str, list[str]]],
    orphaned: list[str],
) -> None:
    print(f"Verified {checked} of {total} summaries against their digests.")
    for cid, extra in unsupported:
        print(f"  [unsupported] community {cid} cites: {', '.join(sorted(extra))}")
    for cid, hedges in speculative:
        words = sorted({hedge.lower() for hedge in hedges})
        print(f"  [speculation] community {cid}: {', '.join(words)}")
    for cid in orphaned:
        print(f"  [no digest] community {cid} has prose but no evidence to check it against")
    if unsupported or speculative or orphaned:
        print(
            f"  {len(unsupported)} unsupported, {len(speculative)} speculative, "
            f"{len(orphaned)} without a digest."
        )
    else:
        print("  nothing unsupported.")


def _ungrounded(text: str, digest: dict) -> set[str]:
    """Identifiers the prose cites that the evidence does not contain."""
    evidence = {_normalise(item) for item in _digest_identifiers(digest)}
    return {
        cited
        for cited in prose_identifiers(text)
        if _normalise(cited) and _normalise(cited) not in evidence
    }


def verify(sample: int | None = None, strict: bool = False) -> int:
    """Check authored summaries cite only what their digests contain.

    Coverage checks confirm every digest got prose; this confirms the prose is
    grounded. Reports rather than fails, so it can be run over a whole store
    without blocking; `strict` is for CI.
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

    _report_verify(len(checked), len(prose), unsupported, speculative, orphaned)

    report = io.read_json_dict(config.REMAP_REPORT_PATH)
    carried_ids = set(report.get("carried", {}))
    if carried_ids:
        flagged = {cid for cid, _ in unsupported}
        carried_checked = [cid for cid in checked if cid in carried_ids]
        authored_checked = [cid for cid in checked if cid not in carried_ids]

        def rate(group: list[str]) -> str:
            if not group:
                return "n/a (0 checked)"
            hit = sum(1 for cid in group if cid in flagged)
            return f"{100 * hit // len(group)}% ({hit} of {len(group)})"

        print(
            f"  grounding by provenance: carried {rate(carried_checked)}, "
            f"authored {rate(authored_checked)} - carried prose cites the cluster "
            "it was written for, so a gap here is expected and is the signal to "
            "revise rather than trust"
        )
    return 1 if (strict and (unsupported or orphaned)) else 0


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
        options = parser.parse_args(arguments[1:])
        return verify(sample=options.sample, strict=options.strict)
    if arguments[:1] == ["remap"]:
        parser = argparse.ArgumentParser(prog="knowledgestore summaries remap")
        parser.add_argument("--bar", type=float, default=DEFAULT_BAR)
        parser.add_argument("--floor", type=int, default=DEFAULT_FLOOR)
        parser.add_argument("--coverage", type=float, default=DEFAULT_COVERAGE)
        options = parser.parse_args(arguments[1:])
        return remap(bar=options.bar, floor=options.floor, coverage=options.coverage)
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
