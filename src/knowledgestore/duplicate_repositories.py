"""Which repositories are near-copies of each other, by their (label, path) pairs.

A measurement, not a verdict. A copy can be entirely deliberate - a vendored
fork, a migration part-way through, a template instantiated twice - and only a
person looking at the two names can say which. So this reports an overlap and
ranks it, and never declares anything a duplicate.

    knowledgestore status --duplicates

## Why the pipeline could not see this

On one estate two repositories were near-identical to two others by these pairs,
together contributing a noticeable share of the graph's nodes, and **no count the
pipeline reported made it visible**. They were found because subagents authoring
community summaries independently noticed they were writing the same description
twice.

Node ids are why. `merge-graphs` namespaces every id as `<repo>::<id>`, so two
copies of one file produce two ids that cannot collide by construction - the
de-duplication that protects the merge is exactly what hides the duplication from
every count downstream of it. Labels and paths are not namespaced, so the pair
`(label, source_file)` is the cheapest thing in the graph that a copy preserves.

The cost is not the wasted nodes. It is duplicate community structure, so
clustering spends communities on a second copy; duplicate authored prose, paid
for twice by an LLM; and two equally good citations for one question, differing
only in which copy they name, which is the shape that erodes trust in a cited
answer.

## The ranking denominator, and why both readings are printed

Overlap is directional and both directions are true. A small repository wholly
inside a large one is 100% of itself and a few percent of the other.

The **ranking key is `shared / max(|A|, |B|)`** - the smaller of the two readings.
Ranking by the larger reading would put every small repository whose files all
appear somewhere else at the top of the list, which is common and usually
uninteresting; dividing by the larger set ranks by how much of the *pair* is one
copy, which is the case that buys a second set of communities and a second set of
summaries. Neither reading is hidden: every line prints the shared count and the
percentage of each side, so containment is visible as `100.0% of alpha` beside
`4.1% of beta`.

## The cost, and why the pruning is exact rather than a threshold

All pairs over a few hundred repositories is quadratic, and a similarity
threshold to prune it would be a constant that is wrong by orders of magnitude on
some estate - skipping real duplicates in silence, which is the failure this
report exists to end.

So the pruning is an **exact upper bound**, computed from set sizes before any
intersection: `|A n B| <= min(|A|, |B|)`, therefore
`shared / max(|A|, |B|) <= min(|A|, |B|) / max(|A|, |B|)`. Candidates are visited
in descending bound order, so once a bound falls strictly below the current
Nth-best actual overlap, every remaining candidate is below it too and none can
enter the list. **It cannot skip a pair that would have been reported.** The next
reader will assume this is a heuristic and try to fix it; it is not.

**The saving only exists once the list is full.** There is no Nth-best to bound
against until `top` pairs have been found, so an estate with fewer overlapping
pairs than that intersects every pair. That is the direction to be wrong in - the
estate with many near-copies is the expensive one and the one where the walk stops
early - but it means the two figures on the header are not decoration. Read them
rather than assuming the pruning fired.

Measured through `status --duplicates` on two synthetic estates, each 300
repositories with per-repository node counts drawn from 40 to 2,000 and a fixed
number of deliberate near-copy pairs, uncompressed graph, default `top`:

    179,095 nodes, 15 near-copy pairs   44,850 bounded   6,403 intersected   0.4s  0.08 GB
    211,550 nodes,  3 near-copy pairs   45,753 bounded  45,753 intersected   0.6s  0.08 GB

The second row is the case with nothing to bound against, and it is still
affordable: intersecting two sets costs the smaller of them, not the graph. These
are synthetic, and no estate here is as large as the largest real one - what they
size is the shape of the cost, not a promise about a specific store.

Two streamed passes are not needed - one is. The signature scan holds one node at
a time from `graph_stream`, and what it accumulates is one `(label, path)` tuple
per node rather than the node. Peak memory therefore tracks the graph's distinct
pairs rather than its parsed size, which is what makes this affordable at all -
loading the same graph costs several GB.

## What it cannot see

**Paths are compared as the graph carries them.** Extraction is meant to run from
inside each repository, so `source_file` is repo-relative and two copies of a file
carry the identical value. A graph extracted with a path like
`repositories/<name>` prefixes every `source_file` with the repository name - the
same defect that silently breaks the file-to-ticket join - and two copies then
share no pair at all. This report would read as a clean estate. It normalises
nothing, because stripping a guessed prefix would quietly invent overlap that the
graph does not hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from . import graph_stream

# What a signature is: one (label, source_file) pair per node. Tuples rather than
# a joined string, which would halve the memory: a separator has to be a character
# that cannot occur in either field, and JSON string values may legally hold any of
# them. An exact comparison is worth more here than the bytes.
Signature = tuple[str, str]


@dataclass(frozen=True)
class Scan:
    """The signature sets, and enough context to say what was read.

    `nodes` counts nodes carrying a `repo` and at least one of label or path -
    what the comparison actually saw. `unattributed` counts nodes with no `repo`,
    because a graph built without that attribute produces no signatures at all,
    and "nothing to report" would then read as a clean estate rather than a broken
    precondition (the #104 class).

    `repositories` counts every distinct `repo` value in the graph, including one
    whose nodes are all structural and so carry no signature. Without it the
    report's own "compared" figure would be the same number twice, which says
    nothing about what was left out.
    """

    by_repo: dict[str, set[Signature]]
    nodes: int
    unattributed: int
    repositories: int


@dataclass(frozen=True)
class Overlap:
    """One pair, with both directional readings derivable from the raw counts.

    `left` is always the name that sorts first, so a pair has one representation
    and two runs cannot print it two ways.
    """

    left: str
    right: str
    shared: int
    left_size: int
    right_size: int

    @property
    def fraction(self) -> float:
        """The ranking key: the shared pairs over the larger of the two sets."""
        return self.shared / max(self.left_size, self.right_size)

    @property
    def left_fraction(self) -> float:
        return self.shared / self.left_size

    @property
    def right_fraction(self) -> float:
        return self.shared / self.right_size


@dataclass(frozen=True)
class Report:
    """The ranking, and the cost it took to produce.

    `considered` is the pairs whose bound was computed - every pair of comparable
    repositories. `intersected` is the pairs whose sets were actually intersected.
    The gap between them is the pruning, stated rather than asserted.

    `compared` is the repositories that carried at least one signature - the only
    ones a pair can be formed from. `repositories`, `nodes` and `unattributed` are
    carried through from the scan so the report can say what it did not compare and
    can tell "no near-copies" from "nothing to group by". They are 0 on a report
    ranked from signatures the caller already held.
    """

    overlaps: tuple[Overlap, ...]
    compared: int
    considered: int
    intersected: int
    repositories: int = 0
    nodes: int = 0
    unattributed: int = 0


def scan(path: Path) -> Scan:
    """Per-repository signature sets, in one streamed pass over the nodes.

    A node with neither label nor path contributes nothing: newer graphify emits
    package-hierarchy nodes with neither, and `("", "")` is a pair every
    repository would hold exactly once, which is overlap the estate does not have.
    One of the two is enough - a shared path with no label is still a shared file.
    """
    by_repo: dict[str, set[Signature]] = {}
    seen: set[str] = set()
    nodes = 0
    unattributed = 0
    for node in graph_stream.iter_array(path):
        if not isinstance(node, dict):
            continue
        repo = str(node.get("repo") or "")
        if not repo:
            unattributed += 1
            continue
        seen.add(repo)
        label = str(node.get("label") or "")
        source = str(node.get("source_file") or "")
        if not label and not source:
            continue
        by_repo.setdefault(repo, set()).add((label, source))
        nodes += 1
    return Scan(by_repo, nodes, unattributed, len(seen))


def rank(by_repo: dict[str, set[Signature]], top: int = 10) -> Report:
    """The `top` pairs by overlap, most overlapping first, with the cost.

    Only pairs sharing at least one signature are reported: a pair sharing nothing
    is not an overlap, and there is deliberately no percentage below which a real
    one is suppressed.

    Ties break by name, both sides, and the candidate order is fully sorted rather
    than dict order. Stage output is a committed artefact here and hash
    randomisation across processes has broken byte-identical reruns before,
    invisibly until someone diffed two builds.

    `top` is what makes the pruning possible and also what limits it: the bound has
    nothing to compare against until `top` pairs are held. See the module docstring.
    """
    sizes = {name: len(pairs) for name, pairs in by_repo.items() if pairs}
    # The bound, negated so one ascending sort gives descending bound and then
    # ascending names.
    candidates = sorted(
        (-(min(sizes[a], sizes[b]) / max(sizes[a], sizes[b])), a, b)
        for a, b in combinations(sorted(sizes), 2)
    )

    best: list[Overlap] = []
    intersected = 0
    for negated_bound, left, right in candidates:
        # Strictly below, never at: a pair whose bound equals the Nth-best actual
        # overlap can still tie it and take its place on the name tiebreak.
        if len(best) >= top and -negated_bound < best[-1].fraction:
            break
        shared = len(by_repo[left] & by_repo[right])
        intersected += 1
        if not shared:
            continue
        best.append(Overlap(left, right, shared, sizes[left], sizes[right]))
        best.sort(key=lambda o: (-o.fraction, o.left, o.right))
        del best[top:]

    return Report(tuple(best), len(sizes), len(candidates), intersected)


def near_duplicates(path: Path, top: int = 10) -> Report:
    """`rank` over `scan`: the report for one graph file."""
    scanned = scan(path)
    # Constructed rather than `replace`d: `dataclasses.replace` is typed as
    # returning a `DataclassInstance` rather than the class it was handed, so the
    # declared return type and the inferred one disagree (Sonar S5886). An
    # annotation does not settle it - it only moves the complaint to the
    # assignment (S5890) - so the four ranked fields are named explicitly and the
    # three scan-carried ones alongside them.
    ranked = rank(scanned.by_repo, top=top)
    return Report(
        overlaps=ranked.overlaps,
        compared=ranked.compared,
        considered=ranked.considered,
        intersected=ranked.intersected,
        repositories=scanned.repositories,
        nodes=scanned.nodes,
        unattributed=scanned.unattributed,
    )


def lines(report: Report, described: str = "") -> list[str]:
    """The report as printed, or nothing at all when there is no overlap to state.

    Silence is the answer for an estate whose repositories share no (label, path)
    pair. A report that always finds something is a report nobody reads.

    The one thing it will not stay silent about is having had nothing to compare:
    a graph with nodes but no `repo` attribute produces no signatures, and that is
    a broken precondition rather than a clean estate.

    `described` is the graph file the caller read, and it belongs on the header
    rather than on a line of its own: a store holds two graphs which can disagree,
    so the same overlap figure means different things depending on which was read -
    but printing the name unconditionally would end the silence above. Empty for a
    caller ranking signatures it already held, where there is no file to name.
    """
    where = f" in {described}" if described else ""
    if report.unattributed and not report.nodes:
        return [
            f"Repository overlap: nothing compared - none of the {report.unattributed:,} "
            f"node(s){where} carries a `repo` attribute, so the graph cannot be grouped by "
            "repository. Not a clean result."
        ]
    if not report.overlaps:
        return []
    # Naming what was not compared, not just what was. A repository whose nodes are
    # all structural carries no (label, path) pair, so no pair can be formed from it
    # and this report says nothing about it either way.
    silent = report.repositories - report.compared
    left_out = f" ({silent:,} carried no (label, path) pair)" if silent > 0 else ""
    out = [
        f"Repository (label, path) overlap{where} - a measurement, not a verdict: a copy may "
        "be deliberate, and only you can say. Ranked by the shared pairs over the larger "
        f"repository. {report.compared:,} repositories compared{left_out}, "
        f"{report.considered:,} pair(s) bounded, {report.intersected:,} intersected:"
    ]
    for overlap in report.overlaps:
        out.append(
            f"  {overlap.fraction:>6.1%}  {overlap.left} / {overlap.right}: "
            f"{overlap.shared:,} shared pair(s) - {overlap.left_fraction:.1%} of "
            f"{overlap.left} ({overlap.left_size:,}), {overlap.right_fraction:.1%} of "
            f"{overlap.right} ({overlap.right_size:,})"
        )
    return out
