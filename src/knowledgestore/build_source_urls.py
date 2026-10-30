"""Give every estate node in the graph a `url` a reader can open.

Answers from a store cited `repositories/<repo>/<path>`: a path into the build
corpus, which no store commits, so no reader could follow the citation. This
stage writes the followable form onto each node once, at build time,

    https://github.com/<org>/<repo>/blob/<provenance sha>/<path>#L<line>

so an agent answering from the graph copies a link rather than assembling one.
`source_urls` builds it; this stage only applies it and accounts for every node.

A repository node - kind `repository`, no `source_file` - gets the tree of the
build commit, `https://github.com/<org>/<repo>/tree/<provenance sha>`, from the same
organisation and sha the file links use. A node with neither a `repo` nor a
`source_file` has nothing to link and is counted under its own reason.

**Every node is accounted for.** The report names how many nodes were linked and
how many were not, by reason: a node with no `source_file` (a Java package
node, a module referenced from outside the estate), no `repo`, a repository on
another host, one the store does not configure, a missing or malformed
provenance sha, or a path that is not inside its own repository.

**A node that cannot be linked correctly loses any link it had, and the loss is
counted.** A repository that has left the configuration, or moved host, still
carries the link an earlier build wrote; leaving it would cite a commit this build
did not read. Per node that is right. Across the estate it is not: a missing
local file must not strip every link a store has published and exit 0. So:

- removed links are printed on their own line, never folded into the skip counts;
- the stage refuses, writing nothing, when `knowledge/provenance.json` is missing
  or records no repository - every node would fall to "no provenance sha";
- it refuses, writing nothing, when the graph carried links and this run would
  link none, whatever the reason;
- it refuses, writing nothing, when the graph holds estate nodes - a `repo` and a
  `source_file` - and `config/repositories.txt` is missing or configures none of
  their repositories. Run from the wrong root, the stage would otherwise link
  nothing and exit 0 over a graph that was never linked, where the refusal above
  cannot fire. An estate whose list configures it on another host links nothing
  by design and is not refused.

Run after `gherkin`, `packages` and `deployments`, so the nodes they add are
linked too, and before clustering: like them it rewrites both graph files, and
it refuses through `graph_files.stale_refusal` when the two disagree.
Re-running rewrites every link from the current provenance (idempotent).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass, field

from . import config, graph_files, io, kinds, provenance, telemetry
from .source_urls import (
    NO_REPO_OR_FILE,
    Resolution,
    NOT_CONFIGURED,
    SourceUrls,
    single_root_repositories,
)


@dataclass
class Outcome:
    """What one pass did to the graph's links."""

    total: int = 0
    linked: int = 0
    anchored: int = 0
    carried: int = 0  # nodes that held a `url` before this pass
    estate: int = 0  # nodes naming a repository and a file in it
    removed: int = 0  # of those, the ones left without one
    skipped: Counter = field(default_factory=Counter)


def _resolve(node: dict, urls: SourceUrls, single_root: set[str]):
    """The link for one node: its file, its repository's tree, or the reason for none."""
    repo, path = node.get("repo"), node.get("source_file")
    has_file = isinstance(path, str) and bool(path)
    if not has_file and kinds.is_kind(node, kinds.REPOSITORY):
        return urls.resolve_repository(repo)
    if not has_file and not (isinstance(repo, str) and repo):
        return Resolution(None, NO_REPO_OR_FILE)
    return urls.resolve(repo, path, node.get("source_location"), repo in single_root)


def annotate(graph: dict, urls: SourceUrls) -> Outcome:
    """Set or clear `url` on every node, and account for each one."""
    nodes = graph["nodes"]
    single_root = single_root_repositories(nodes)
    outcome = Outcome(total=len(nodes))
    for node in nodes:
        # Popped first so a link lands in the same key position on every run.
        had = node.pop("url", None) is not None
        outcome.carried += had
        repo, path = node.get("repo"), node.get("source_file")
        outcome.estate += bool(isinstance(repo, str) and repo and isinstance(path, str) and path)
        found = _resolve(node, urls, single_root)
        if found.url is None:
            outcome.skipped[found.reason] += 1
            outcome.removed += had
            continue
        node["url"] = found.url
        outcome.linked += 1
        outcome.anchored += "#L" in found.url
    return outcome


def report(outcome: Outcome) -> str:
    lines = [
        f"Source URLs: {outcome.linked} of {outcome.total} nodes linked "
        f"({outcome.anchored} with a line anchor)"
    ]
    if outcome.removed:
        lines.append(f"  removed {outcome.removed} links an earlier build wrote")
    if outcome.skipped:
        lines.append(f"  not linked: {sum(outcome.skipped.values())}")
        # Largest first, then by name, so two runs print the same lines.
        for reason, count in sorted(outcome.skipped.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {count} {reason}")
    return "\n".join(lines)


def unconfigured_refusal(outcome: Outcome) -> str:
    """Why a run whose repository list configures none of the estate writes nothing."""
    listed = config.REPOSITORIES_CONFIG
    found = (
        "is missing"
        if not listed.is_file()
        else "names none of the repositories those nodes belong to"
    )
    return (
        f"Refusing to write: {listed}, looked for under {config.ROOT}, {found}, and the "
        f"graph holds {outcome.estate} nodes with a repository and a source file, none "
        "of which can be linked without it. Nothing was written. Run from the store "
        "root, or set KSB_ROOT to it; `knowledgestore discover` writes the list."
    )


def main() -> int:
    if not config.GRAPH_PATH.exists():
        print(f"Graph not found: {config.GRAPH_PATH} (gunzip -k graph.json.gz first)")
        return 1
    refusal = graph_files.stale_refusal(config.GRAPH_PATH)
    if refusal:
        print(refusal, file=sys.stderr)
        return 1
    if not provenance.read():
        print(
            f"Refusing to run: {config.PROVENANCE_PATH} is missing or records no "
            "repository, so no node can be linked and every link the graph carries "
            "would be removed. Run `knowledgestore sync`, which records it, then re-run.",
            file=sys.stderr,
        )
        return 1
    try:
        urls = SourceUrls.from_store()
    except ValueError as error:
        # Read as an empty estate, an unreadable list would strip every link.
        print(f"Cannot read {config.REPOSITORIES_CONFIG}: {error}", file=sys.stderr)
        return 1

    graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
    outcome = annotate(graph, urls)
    if outcome.carried and not outcome.linked:
        # Flushed so the reasons land above the refusal that points at them.
        print(report(outcome), flush=True)
        print(
            f"Refusing to write: the graph carried {outcome.carried} links and this run "
            "would link none, so every one would be removed. Nothing was written; the "
            "reasons above say why no node could be linked.",
            file=sys.stderr,
        )
        return 1
    if outcome.estate and outcome.skipped[NOT_CONFIGURED] == outcome.estate:
        print(report(outcome), flush=True)
        print(unconfigured_refusal(outcome), file=sys.stderr)
        return 1

    serialised = json.dumps(graph, ensure_ascii=False)
    config.GRAPH_PATH.write_text(serialised, encoding="utf-8")  # NOSONAR(S2083)
    with io.gzip_text(config.GRAPH_PATH.with_suffix(".json.gz")) as out:
        out.write(serialised)

    print(report(outcome))
    # The store manifest's `graph.source_urls` flag reads this; a store that never
    # ran the stage has no key, which the manifest reports as unknown.
    telemetry.record({"source_urls.linked": outcome.linked})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
