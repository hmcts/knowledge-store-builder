"""Give every estate node in the graph a `url` a reader can open.

Answers from a store cited `repositories/<repo>/<path>`: a path into the build
corpus, which no store commits, so no reader could follow the citation. This
stage writes the followable form onto each node once, at build time,

    https://github.com/<org>/<repo>/blob/<provenance sha>/<path>#L<line>

so an agent answering from the graph copies a link rather than assembling one.
`source_urls` builds it; this stage only applies it and accounts for every node.

**Every node is accounted for.** The report names how many nodes were linked and
how many were not, by reason: a node with no `source_file` (a Java package
node, a module referenced from outside the estate), no `repo`, a repository on
another host, one the store does not configure or holds no provenance sha for,
or a path that is not inside its own repository. A store whose provenance is
missing reads as "0 linked" with the reason beside it, never as success.

**A node that cannot be linked correctly loses any link it had.** A repository
that has left the configuration, or moved host, still carries the link an
earlier build wrote; leaving it would cite a commit this build did not read.

Run after `gherkin`, `packages` and `deployments`, so the nodes they add are
linked too, and before clustering: like them it rewrites both graph files, and
it refuses through `graph_files.stale_refusal` when the two disagree.
Re-running rewrites every link from the current provenance (idempotent).
"""

from __future__ import annotations

import json
import sys
from collections import Counter

from . import config, graph_files, io
from .source_urls import SourceUrls


def annotate(graph: dict, urls: SourceUrls) -> tuple[int, int, Counter]:
    """Set or clear `url` on every node; return (linked, anchored, skipped by reason)."""
    linked = anchored = 0
    skipped: Counter = Counter()
    for node in graph["nodes"]:
        # Popped first so a link lands in the same key position on every run.
        node.pop("url", None)
        found = urls.resolve(node.get("repo"), node.get("source_file"), node.get("source_location"))
        if found.url is None:
            skipped[found.reason] += 1
            continue
        node["url"] = found.url
        linked += 1
        anchored += "#L" in found.url
    return linked, anchored, skipped


def report(total: int, linked: int, anchored: int, skipped: Counter) -> str:
    lines = [f"Source URLs: {linked} of {total} nodes linked ({anchored} with a line anchor)"]
    if skipped:
        lines.append(f"  not linked: {sum(skipped.values())}")
        # Largest first, then by name, so two runs print the same lines.
        for reason, count in sorted(skipped.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {count} {reason}")
    return "\n".join(lines)


def main() -> int:
    if not config.GRAPH_PATH.exists():
        print(f"Graph not found: {config.GRAPH_PATH} (gunzip -k graph.json.gz first)")
        return 1
    refusal = graph_files.stale_refusal(config.GRAPH_PATH)
    if refusal:
        print(refusal, file=sys.stderr)
        return 1
    try:
        urls = SourceUrls.from_store()
    except ValueError as error:
        # Read as an empty estate, an unreadable list would strip every link.
        print(f"Cannot read {config.REPOSITORIES_CONFIG}: {error}", file=sys.stderr)
        return 1

    graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
    linked, anchored, skipped = annotate(graph, urls)

    serialised = json.dumps(graph, ensure_ascii=False)
    config.GRAPH_PATH.write_text(serialised, encoding="utf-8")  # NOSONAR(S2083)
    with io.gzip_text(config.GRAPH_PATH.with_suffix(".json.gz")) as out:
        out.write(serialised)

    print(report(len(graph["nodes"]), linked, anchored, skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
