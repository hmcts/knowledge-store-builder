"""Size the content cuts an estate is choosing between, by what survives them.

    knowledgestore size-cuts                     # every repositories/*/graphify-out/graph.json
    knowledgestore size-cuts one.json two.json.gz

An estate's AST layer is not a fixed multiple of its semantic layer. Java and
TypeScript emit a node per *symbol* where Terraform and YAML emit one per
*resource*, so adding application repositories to an infrastructure estate can
multiply the layer by an order of magnitude from a small multiple of the
repository count - and the same arithmetic runs the other way on a document-heavy
estate. Two estates measured AST-to-semantic node ratios roughly a factor of a
hundred apart.

**So there is no cap, no ratio, no threshold and no per-language rule in this
module, and there cannot be**: any constant this library shipped would be wrong
by two orders of magnitude on one of those estates. What a library can do is
measure the candidates an operator declared, report them beside the layer's own
totals, and record the counts so the *next* refresh of that store compares
against its own history rather than against a constant.

## Why surviving edges, and not node counts

An edge survives a cut only when **both** its endpoints do, so node reduction and
edge reduction are not proportional and neither predicts the other. On the estate
that reported this, a cut keeping only file-level nodes kept tens of thousands of
them joined by **low hundreds** of edges: graphify's AST edges connect symbols,
not files, so dropping the symbols leaves the files as disconnected dots. That cut
looks the most attractive of any on its node count. Nothing but the edge count
reveals that it is not a graph at all.

Hence the report: nodes kept, edges kept, and edges per node kept beside the same
figure for the uncut layer. The comparison is against the layer's own density
rather than against a number this module believes in.

## The design this deliberately does not implement

The obvious structural prune - drop every node whose edges all stay inside its own
file, keep everything with a cross-file edge - was proposed on the issue and
**withdrawn on it after measurement**, by the person who proposed it. Cross-file
connectivity does not separate symbol-level noise from resource-level substance:
on that estate roughly three quarters of Java nodes had a cross-file edge and
rather fewer Terraform nodes did, so Java looked *more* connected than the
infrastructure the store existed to describe, and any threshold keeping one keeps
the other. The prune also removed about a quarter of a layer that needed reducing
by an order of magnitude. An ordinary import reaches across files; the property is
real and simply not diagnostic of relevance.

What survived that measurement is a **content cut** - a statement of what the
store is *for*, written down by whoever owns it: this store holds the estate's
infrastructure and deployment surface, and not application symbol graphs. A
structural heuristic has to be true of the graph, and a policy only has to be true
of the intent, which is why the policy survives contact with data that refutes the
heuristic. It is also honest in a way the prune was not, because it can be stated
in the store's own documentation, so a reader knows the shape of what is missing.

**This stage sizes cuts; it does not apply one.** Applying it means writing a
different graph, which is an artefact consumers commit, and the measurement worth
having before that is not the node count - it is whether any question the estate
needs answered depended on the excluded nodes. That baseline is what
`check-answers` is for, and it has to exist before the cut, not after.

## Measure the layer you store, not the layer you publish

With no arguments this reads the **per-repository** graphs under
`repositories/*/graphify-out/`, which is the same glob the documented
`graphify merge-graphs` command takes, before anything has been merged or cut.
That is deliberate. A post-cut measurement cannot characterise the raw layer,
because a cut removes content for reasons unrelated to it being noise: on a
document-heavy estate, vendored code was concentrated in exactly the repositories
the cut discarded anyway, so the surviving layer showed a few thousand vendored
nodes where the raw layer held two orders of magnitude more. Sizing tells you what
a cut *retains*; it says nothing about what the raw layer *contains*, and both get
reported as a percentage of "the layer".

Naming graph files as arguments measures whatever you name, including a merged
graph - which is a different and also legitimate question, as long as nobody
reads the answer as a statement about the raw layer.

**The default input is the glob, deliberately, and not the estate manifest.**
Extraction is manifest-driven and the merge is directory-driven, and nothing
reconciles the two: a store measured 164 per-repository graphs on disk against
163 declared repositories, because a repository was discovered, cloned and
extracted and the manifest naming it was discarded when that refresh aborted.
The clone and its graph stayed, so the merge reads an input the store's own
provenance cannot name a commit for. A stage that walked the manifest would be
structurally blind to exactly that input and would report a clean result, so this
one counts what `merge-graphs` would read and names every file it counted.

## Declaring candidates

`config/content-cuts.txt`, one `cut <name>` per candidate followed by its rules:

    # Keep the estate's infrastructure surface wherever it lives, including a
    # component's own /infrastructure directory inside an application repository.
    cut iac-anywhere
    file *.tf
    file *.tfvars
    file *.hcl

    # For comparison: the same intent expressed by repository, which discards
    # every application repository's own component-level infrastructure.
    cut infra-repositories-only
    repo *-infrastructure

Three axes - `file` (the node's `source_file`), `kind` (its declaration kind) and
`repo` (the repository it came from, falling back to the `repositories/<name>/`
segment of the graph file's own path, because `merge-graphs` is what adds the
`repo` attribute and the layer as extracted has none) - each with a `not-` form.
A node is kept when it matches at least one rule on **every axis the cut
constrains** and no `not-` rule. So rules on one axis widen a cut and rules on a
second narrow it, which is what lets `file *.java` + `not-kind method` express
"Java declarations without the callables".

Globs are `fnmatch`, where `*` crosses directory separators: `*.tf` already means
"any `.tf` at any depth". `**` is refused rather than silently read as one `*`,
because `**/*.tf` would then quietly exclude a file at a repository's root - a rule
that matches less than it says is this pipeline's most expensive class of defect.

## What it reports and what it refuses

Every input file is named with its own counts, because a tool's own total is not
verification: the reconciliation an operator needs is against the files they
believed they were reading. A rule that matched no node anywhere is named with its
line number - an unmatched rule looks exactly like a rule that had nothing to do -
and so is a cut that kept nodes and no edges.

It reports; it does not adjudicate a cut. The one thing it refuses is measuring
nothing: no graph file found, or no node read from the files it did find, exits
non-zero and records nothing, because writing zeros over a previous refresh's
counts would destroy the only baseline there is.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import config, graph_files, graph_stream, kinds, merge_layers, telemetry

# What a rule can select on, and where a node records it. `file` and `repo` are
# graphify's own attributes; `kind` is read through `_kind` because three
# generations of node shape carry it in three places.
AXES = ("file", "kind", "repo")

# A cut name becomes a segment of a telemetry metric, so it is restricted to what
# that document accepts - and to hyphens rather than underscores, so that the
# `-` -> `_` translation into a metric name cannot map two cuts onto one key.
NAME = re.compile(r"^[a-z][a-z0-9-]*$")


class CutError(ValueError):
    """A cuts file that cannot be read as written. Named back with its line."""


@dataclass(frozen=True)
class Rule:
    """One line of a cuts file: an axis, a glob, and whether it excludes."""

    axis: str
    glob: str
    negative: bool
    line: int

    def describe(self) -> str:
        return f"{'not-' if self.negative else ''}{self.axis} {self.glob}"


@dataclass
class Cut:
    """One candidate cut: a name to report it under and the rules that define it."""

    name: str
    rules: list[Rule] = field(default_factory=list)

    @property
    def metric(self) -> str:
        """The cut's name as a telemetry metric segment."""
        return self.name.replace("-", "_")


@dataclass
class Sizing:
    """What was read, and what each candidate would keep of it."""

    graphs: list[tuple[Path, int, int]] = field(default_factory=list)
    nodes: int = 0
    edges: int = 0
    kept_nodes: dict[str, int] = field(default_factory=dict)
    kept_edges: dict[str, int] = field(default_factory=dict)
    # rule line number -> how many nodes the rule itself matched, across every
    # graph read. A rule's own reach, not the cut's verdict: a rule can match
    # plenty and still keep nothing once another axis narrows it, and the two
    # failures need different fixes.
    hits: dict[int, int] = field(default_factory=dict)


def _kind(node: Mapping) -> str:
    """A node's declaration kind, from wherever its generation recorded it.

    Three shapes are in circulation and all three reach this stage: the library
    writes `metadata.kind`, graphify writes a top-level `type`, and a store's own
    extraction has written a top-level `kind`. Reading one of them and treating
    the absence as "no kind" would make a `kind` rule silently select nothing on
    two thirds of the graphs it is pointed at.
    """
    for value in (kinds.node_kind(dict(node)), node.get("type"), node.get("kind")):
        if isinstance(value, str) and value:
            return value
    return ""


def _values(node: Mapping, repository: str) -> dict[str, str]:
    """The node's value on each axis, absent attributes read as empty.

    Structural nodes carry neither a label nor a `source_file`, so absence is
    normal rather than exceptional. Empty matches no glob that names an extension,
    which is the right answer: a node with no file is not a `.tf` file's node.

    `repository` is the fallback for the `repo` axis, and it is why this stage can
    be pointed at the per-repository layer at all: **`merge-graphs` is what adds
    the `repo` attribute**, so on the layer as extracted no node carries one, and
    every `repo` rule would select nothing and be reported as a rule with nothing
    to do. The fallback comes from the graph file's own path, not from the node.
    """
    return {
        "file": str(node.get("source_file") or ""),
        "kind": _kind(node),
        "repo": str(node.get("repo") or "") or repository,
    }


def keeps(cut: Cut, values: Mapping[str, str], hits: dict[int, int]) -> bool:
    """Whether `cut` keeps a node with these axis values, counting each rule's reach.

    Union within an axis, intersection across axes: a node must match one of the
    `file` rules *and* one of the `kind` rules when the cut declares both, and
    must match no `not-` rule. An axis the cut says nothing about is unconstrained
    rather than empty - otherwise adding a second axis to a working cut would
    reduce it to nothing.
    """
    required: set[str] = set()
    satisfied: set[str] = set()
    kept = True
    for rule in cut.rules:
        if not rule.negative:
            required.add(rule.axis)
        if fnmatch.fnmatchcase(values[rule.axis], rule.glob):
            hits[rule.line] = hits.get(rule.line, 0) + 1
            if rule.negative:
                kept = False
            else:
                satisfied.add(rule.axis)
    return kept and required <= satisfied


def _rule(line: str, number: int, value: str) -> Rule:
    """One rule line as a `Rule`, or a CutError naming what is wrong with it."""
    keyword = line.split(" ", 1)[0]
    negative = keyword.startswith("not-")
    axis = keyword[4:] if negative else keyword
    if axis not in AXES:
        raise CutError(
            f"line {number}: `{line}` is not a rule. Each line is `cut <name>`, or one of "
            f"{', '.join(AXES)} (and their not- forms) followed by a glob."
        )
    if "**" in value:
        raise CutError(
            f"line {number}: `{line}` uses `**`, which is not a separate wildcard here - a "
            "single `*` already crosses directories, so `*.tf` matches a .tf file at any "
            "depth. Written as `**/*.tf` this rule would require a directory and skip a "
            "file at a repository's root."
        )
    return Rule(axis, value, negative, number)


def _open_cut(name: str, number: int, cuts: list[Cut]) -> Cut:
    """A new named candidate, refusing a name that cannot be reported or recorded."""
    if not NAME.match(name):
        raise CutError(
            f"line {number}: `cut {name}` - a cut name must be lower-case letters, digits "
            "and hyphens, starting with a letter. The name becomes part of a telemetry "
            "metric, and an underscore in it could collide with a hyphenated name."
        )
    if any(cut.name == name for cut in cuts):
        raise CutError(
            f"line {number}: `cut {name}` is declared twice. Two candidates under one name "
            "report as one row and record into one metric, so the second would overwrite "
            "the first silently."
        )
    return Cut(name)


def read_cuts(path: Path) -> list[Cut]:
    """The candidates declared in a cuts file, in the order they were written.

    Declaration order, not sorted: it is the operator's argument, usually widest
    first, and a report that reordered it would be harder to read than the file.
    Missing file means no candidates, which is a legitimate state - the layer's own
    totals are worth having before anybody has proposed a cut.

    The upward-path refusal is the read-side twin of `io.checked_write_target`, and
    it is here for the same reason: `--cuts` is a CLI argument, so whatever built
    it - an operator, a script or an agent - chooses which file this process opens.
    Checked lexically, before any resolution, because `realpath` collapses `..` and
    would launder exactly what this rejects. A store's cuts file lives in the
    store, so nothing legitimate here climbs out of it.
    """
    if any(part == ".." for part in Path(path).parts):
        raise CutError(
            f"refusing to read a cuts file through a path that traverses upward: {path}. "
            "Name the file directly."
        )
    if not path.is_file():
        return []
    cuts: list[Cut] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        value = rest.strip()
        if not value:
            raise CutError(f"line {number}: `{line}` names no value.")
        if keyword == "cut":
            cuts.append(_open_cut(value, number, cuts))
            continue
        if not cuts:
            raise CutError(
                f"line {number}: `{line}` comes before any `cut <name>`, so there is no "
                "candidate for it to belong to."
            )
        cuts[-1].rules.append(_rule(line, number, value))
    for cut in cuts:
        if not cut.rules:
            raise CutError(
                f"`cut {cut.name}` has no rules, so it would keep every node. The layer's "
                "own totals are reported anyway; a candidate has to say what it excludes."
            )
    return cuts


def _count_nodes(path: Path, cuts: Sequence[Cut], sizing: Sizing) -> tuple[int, dict[str, int]]:
    """This file's node count, and a bit per candidate keeping each surviving id.

    The table holds only the nodes at least one candidate keeps, which is what
    makes the second pass affordable on a layer of half a million nodes: on the
    estate that reported this, a cut worth applying kept a few percent of them.
    """
    # Exact rather than inferred, and shared with the layer merge: the path either
    # carries a `repositories/<name>/` segment or the `repo` axis has nothing to
    # fall back on for this file.
    repository = merge_layers.repository_of(str(path))
    kept: dict[str, int] = {}
    nodes = 0
    for node in graph_stream.iter_array(path, key="nodes"):
        nodes += 1
        values = _values(node, repository)
        mask = 0
        for index, cut in enumerate(cuts):
            if keeps(cut, values, sizing.hits):
                mask |= 1 << index
                sizing.kept_nodes[cut.name] = sizing.kept_nodes.get(cut.name, 0) + 1
        if mask:
            kept[str(node.get("id"))] = mask
    return nodes, kept


def _count_edges(path: Path, cuts: Sequence[Cut], kept: dict[str, int], sizing: Sizing) -> int:
    """This file's edge count, crediting a candidate only where both endpoints survive."""
    edges = 0
    for edge in graph_files.iter_edges(path):
        edges += 1
        # An edge survives only where both endpoints do, which is the whole
        # measurement: `&`, never `|`. With `|` a cut that strands its nodes
        # reports the edges it broke as edges it kept.
        surviving = kept.get(str(edge.get("source")), 0) & kept.get(str(edge.get("target")), 0)
        for index, cut in enumerate(cuts):
            if surviving & (1 << index):
                sizing.kept_edges[cut.name] = sizing.kept_edges.get(cut.name, 0) + 1
    return edges


def size(paths: Sequence[Path], cuts: Sequence[Cut]) -> Sizing:
    """Count the layer and what each candidate keeps of it, one file at a time.

    Streamed, never loaded: the largest thing this pipeline handles is a graph, and
    a store that cannot read its own build product cannot size a cut for the next
    one.

    Each file is counted with its own id table, because an edge's endpoints are in
    the same per-repository graph as the edge. Sharing one table across files would
    let an id that two repositories happen to reuse make an edge look as though it
    survived a cut that dropped the file it belongs to.
    """
    sizing = Sizing()
    for path in paths:
        nodes, kept = _count_nodes(path, cuts, sizing)
        edges = _count_edges(path, cuts, kept, sizing)
        sizing.graphs.append((path, nodes, edges))
        sizing.nodes += nodes
        sizing.edges += edges
    return sizing


def _share(part: int, whole: int, width: int = 0) -> str:
    """`4 of 15 (26.7%)`, padded so a column of them can be read down.

    Both counts, never the percentage alone: a rate whose population is not beside
    it cannot be re-derived, and every derived figure in this report has to follow
    from integers printed on the same line.
    """
    share = f"{part:,} of {whole:,} ({part / whole:.1%})" if whole else f"{part:,} of 0"
    return f"{share:<{width}}"


def _density(edges: int, nodes: int) -> str:
    return f"{edges / nodes:.2f}" if nodes else "-"


def _relative(path: Path) -> str:
    """The path as an operator would type it, absolute only when it has to be."""
    try:
        return str(path.relative_to(config.ROOT))
    except ValueError:
        return str(path)


def _report_layer(sizing: Sizing, out) -> None:
    """Name every file read, with its own counts, then the total.

    Naming the inputs is the reconciliation: a total is the tool agreeing with
    itself, and the failure this catches is a glob that read 61 of the 81 graphs
    somebody thought it read.
    """
    print(
        f"Layer as extracted, before any cut - {len(sizing.graphs)} graph file(s) read:",
        file=out,
    )
    width = max(len(f"{sizing.nodes:,}"), len(f"{sizing.edges:,}"))
    for path, nodes, edges in sizing.graphs:
        print(f"  {nodes:>{width},} nodes  {edges:>{width},} edges  {_relative(path)}", file=out)
    print(
        f"  {sizing.nodes:>{width},} nodes  {sizing.edges:>{width},} edges  total, at "
        f"{_density(sizing.edges, sizing.nodes)} edges per node",
        file=out,
    )


def _report_cuts(sizing: Sizing, cuts: Sequence[Cut], where: Path, out) -> None:
    if not cuts:
        print(
            f"\nNo candidate cuts are declared in {where}, so only the "
            "layer above was measured. Declare one per candidate to size them:\n"
            "\n    cut iac-anywhere\n    file *.tf\n    file *.tfvars\n",
            file=out,
        )
        return
    print(
        "\nCandidate cuts. An edge survives only when both its endpoints do, so a cut's "
        "node count does not\npredict its edge count - compare each edges-per-node against "
        f"the layer's own {_density(sizing.edges, sizing.nodes)}:",
        file=out,
    )
    name_width = max(len(cut.name) for cut in cuts)
    # Both share columns padded to the widest they can be, so the rows can be read
    # down as a column. The report is the whole deliverable of this stage: a table
    # nobody can compare at a glance is a measurement nobody compares.
    share_width = max(
        len(_share(sizing.nodes, sizing.nodes)), len(_share(sizing.edges, sizing.edges))
    )
    for cut in cuts:
        nodes = sizing.kept_nodes.get(cut.name, 0)
        edges = sizing.kept_edges.get(cut.name, 0)
        print(
            f"  {cut.name:<{name_width}}  nodes {_share(nodes, sizing.nodes, share_width)}"
            f"  edges {_share(edges, sizing.edges, share_width)}"
            f"  {_density(edges, nodes):>5} edges per node kept",
            file=out,
        )
    print(
        "\nNothing here recommends a cut. Which nodes an estate can do without is a "
        "statement about what\nthe store is for, and the counts above are what makes it a "
        "decision rather than a guess.",
        file=out,
    )


def _report_unmatched(sizing: Sizing, cuts: Sequence[Cut], err) -> None:
    """Name every rule that matched no node at all, with the line it was written on.

    A rule that selects nothing is indistinguishable from a rule that had nothing
    to select, and both read as a clean run. The usual causes are a glob written
    for a path form the graph does not use and an axis the layer does not record.
    """
    for cut in cuts:
        for rule in cut.rules:
            if not sizing.hits.get(rule.line):
                print(
                    f"WARNING: cut `{cut.name}` rule `{rule.describe()}` (line {rule.line}) "
                    f"matched no node in the {sizing.nodes:,} read. It is selecting nothing "
                    "rather than excluding nothing.",
                    file=err,
                )


def _report_collapsed(sizing: Sizing, cuts: Sequence[Cut], err) -> None:
    """Name a cut that keeps nodes and no edges, or keeps nothing at all.

    Zero, not a threshold: this library cannot say how few edges is too few on an
    estate it has never seen, but a layer with mass and no structure at all is not
    a graph, and the file-level cut that behaved that way looked the best of its
    generation on node count alone.
    """
    for cut in cuts:
        nodes = sizing.kept_nodes.get(cut.name, 0)
        edges = sizing.kept_edges.get(cut.name, 0)
        if not nodes:
            print(
                f"WARNING: cut `{cut.name}` keeps no node at all. Every rule it declares may "
                "be matching, and still intersect to nothing across axes.",
                file=err,
            )
        elif not edges:
            print(
                f"WARNING: cut `{cut.name}` keeps {nodes:,} nodes joined by no edge at all. "
                "Clustering, centrality and community summaries are all degree-driven, so "
                "this is mass without structure rather than a smaller graph.",
                file=err,
            )


def report(sizing: Sizing, cuts: Sequence[Cut], where: Path, out=None, err=None) -> None:
    """The whole report: statistics to stdout, what nobody would have looked for to stderr."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    _report_layer(sizing, out)
    _report_cuts(sizing, cuts, where, out)
    _report_unmatched(sizing, cuts, err)
    _report_collapsed(sizing, cuts, err)


def measurements(sizing: Sizing, cuts: Sequence[Cut]) -> dict[str, int]:
    """The counts to record, as integers - never a ratio.

    The layer's own three counts are recorded whether or not any candidate is
    declared, and they are the point of recording at all: a store's own previous
    refresh is the only thing a layer size can be compared against. `layer_graphs`
    is there because a layer that halved because a re-sync deleted per-repository
    graphs looks exactly like a layer that halved because an estate shrank.

    Renaming a candidate leaves its old pair of metrics in the record, because the
    record carries forward everything it holds - each stage writing it must not
    erase another's. Delete those two lines in the same commit as the rename: the
    artefact is committed precisely so that its diff is reviewable, and a metric
    nothing writes any more is one whose last value nobody can date.
    """
    recorded = {
        "size_cuts.layer_graphs": len(sizing.graphs),
        "size_cuts.layer_nodes": sizing.nodes,
        "size_cuts.layer_edges": sizing.edges,
    }
    for cut in cuts:
        recorded[f"size_cuts.{cut.metric}.nodes"] = sizing.kept_nodes.get(cut.name, 0)
        recorded[f"size_cuts.{cut.metric}.edges"] = sizing.kept_edges.get(cut.name, 0)
    return recorded


def default_graphs() -> tuple[list[Path], int]:
    """Every per-repository graph under `repositories/`, and how many had both forms.

    The uncompressed file wins where both exist, and the count is reported rather
    than adjudicated: which of a store's two graph files is the real one is the
    operator's knowledge, not this stage's.
    """
    found: list[Path] = []
    both = 0
    for repository in sorted(config.REPOSITORIES_DIR.glob("*")):
        graph = repository / "graphify-out" / "graph.json"
        archive = repository / "graphify-out" / "graph.json.gz"
        if graph.is_file():
            found.append(graph)
            both += archive.is_file()
        elif archive.is_file():
            found.append(archive)
    return found, both


def _inputs(named: list[str], err) -> list[Path]:
    """The graph files to read, from the arguments or from the corpus."""
    if named:
        return [Path(name) for name in named]
    found, both = default_graphs()
    if both:
        print(
            f"Note: {both} repository graph(s) exist in both forms; the uncompressed "
            "graph.json was read. Name the file to read the other.",
            file=err,
        )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledgestore size-cuts",
        description="Size each declared content cut by the nodes and edges it would keep.",
    )
    parser.add_argument(
        "graph",
        nargs="*",
        help="graph files to measure (default: every repositories/*/graphify-out/graph.json, "
        "which is the layer as extracted rather than after a merge)",
    )
    parser.add_argument(
        "--cuts", type=Path, help=f"default: {config.CONTENT_CUTS_PATH.relative_to(config.ROOT)}"
    )
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="do not write the counts to the telemetry record. Use it while trying candidates "
        "out: recording replaces the last refresh's counts, which are what the next refresh "
        "would have been compared against",
    )
    arguments = parser.parse_args(argv)

    where = config.CONTENT_CUTS_PATH
    if arguments.cuts is not None:
        # Confined to the store, at the CLI boundary rather than inside the reader.
        # `--cuts` is where an operator, a script or an agent chooses which file
        # this process opens, and this is the one place the boundary is known -
        # `io.checked_write_target` says the same thing about the write side, and
        # says why it could not enforce it there. Resolved before comparing,
        # because the check is about where the file *is*, not how it was spelled.
        named = Path(arguments.cuts).resolve()
        root = Path(config.ROOT).resolve()
        if not named.is_relative_to(root):
            print(
                f"refusing to read a cuts file outside the store: {named} is not under "
                f"{root}. A store's candidate cuts are its own configuration; use --root to "
                "name a different store.",
                file=sys.stderr,
            )
            return 1
        where = named

    try:
        cuts = read_cuts(where)
    except CutError as refusal:
        print(f"{where}: {refusal}", file=sys.stderr)
        return 1

    paths = _inputs(arguments.graph, sys.stderr)
    if not paths:
        print(
            "No graph file to measure. Extraction writes one per repository to "
            f"{config.REPOSITORIES_DIR}/<name>/graphify-out/graph.json; name a file to "
            "measure something else.",
            file=sys.stderr,
        )
        return 1

    missing = [path for path in paths if not path.is_file()]
    if missing:
        # Named and absent, rather than absent and unnamed: `iter_array` yields
        # nothing for a path that does not exist, so a mistyped argument would
        # otherwise be reported as a graph that holds no nodes.
        print("No such graph file: " + ", ".join(str(path) for path in missing), file=sys.stderr)
        return 1

    try:
        sizing = size(paths, cuts)
    except graph_stream.TruncatedJson as truncated:
        print(f"{truncated}. Re-extract that repository and run this again.", file=sys.stderr)
        return 1

    report(sizing, cuts, where)
    if not sizing.nodes:
        print(
            f"\nNo node was read from the {len(paths)} file(s) named above, so nothing was "
            "measured and nothing was recorded. A count of zero is not a small estate here: "
            "these files hold no `nodes` array.",
            file=sys.stderr,
        )
        return 1
    if not arguments.no_record:
        telemetry.record(measurements(sizing, cuts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
