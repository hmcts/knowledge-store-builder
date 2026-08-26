"""What the merge will actually read, reconciled against what the store declares.

Extraction is **manifest-driven**: `discover` writes `config/repositories.txt`
and the per-repository extraction route reads it. The merge is **glob-driven**:
`graphify merge-graphs repositories/*/graphify-out/graph.json` reads whatever
is on disk. Nothing reconciled the two, and the gap between them is not
symmetric with the one already documented.

`docs/refreshing-a-store.md` records the **removal** direction - delete a
repository from the estate and its clone lingers in the merged graph. The
**addition** direction is the same glob and was documented nowhere. A store
measured a small handful more graphs on disk than its own configuration
declared, because a repository was discovered, cloned and extracted and then
the refresh aborted, discarding the configuration change that named it. The
clone and its per-repository graph stayed.

That supports a sharper statement than lingering data:

    `knowledge/provenance.json` records what was read, and a glob-driven merge
    can read something provenance has no entry for. So a store's own record of
    what it read is not closed over its inputs.

**This module iterates the glob, never the declaration.** That is the whole
design constraint, not a detail: the input that makes this fail is precisely
the one the declaration omits, so any check walking `config/repositories.txt`
skips it - and a skipped input is indistinguishable from an unchanged one. Such
a check reports clean, which is worse than not existing, because a clean report
is read as an answer. `status.unsynced` walks the declaration, correctly, for a
different question; the two are complements and neither substitutes.

Four divergences, each **named** rather than counted:

    undeclared        a merge input `config/repositories.txt` does not name
    ungrounded        a merge input `knowledge/provenance.json` cannot date
    missing           a declared repository with no merge input on disk
    compressed_only   a repository holding only `graph.json.gz`, which the
                      documented glob names `graph.json` and will not read

The first two are closure violations: the merge reads what the store neither
declares nor can name a commit for. `missing` is the documented removal
direction seen from the merge's side. `compressed_only` is its mirror - an
extraction that happened and will be silently omitted.

It **reports; it does not refuse**, because a tree caught mid-refresh is a
normal state and a stage that fails on it would fail on the normal case. Two
conditions are exceptions, and both are the check being unable to answer rather
than answering badly: no merge inputs at all, and a declaration file that
cannot be read. A reconciliation over an empty set has nothing to report and
must not read as a clean one.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config, provenance
from .export_git_history import read_repository_config


# The two names graphify writes a per-repository graph under. `GRAPH_NAME` is
# the one the documented merge command globs for; the archive is named here so
# a repository holding only that form can be reported rather than vanishing.
GRAPH_NAME = "graph.json"
ARCHIVE_NAME = "graph.json.gz"


def _shown(path: Path) -> str:
    """A path as the store names it: relative to the root where it lies inside it.

    Report only. The store-paths rule is that anything a store persists or shows
    about itself is store-relative, and an absolute build-machine path in every
    line of this report buries the repository names the report exists to give.
    `--paths` is the opposite case and stays absolute: that output is handed to
    graphify, which requires it.
    """
    try:
        return str(path.relative_to(config.ROOT))
    except ValueError:
        return str(path)


def merge_glob() -> str:
    """The glob the documented merge command is given, for messages.

    Built from `config.REPOSITORIES_DIR` rather than written out, so a store
    that repoints the directory is told about its own layout instead of the
    default one.
    """
    return f"{_shown(config.REPOSITORIES_DIR)}/*/graphify-out/{GRAPH_NAME}"


def discovered() -> tuple[list[Path], list[str]]:
    """(merge inputs on disk, repositories holding only the compressed form).

    Walks `repositories/` and nothing else. Sorted, so two runs over the same
    tree produce byte-identical output: `iterdir()` yields in directory order,
    which is neither sorted nor stable across filesystems.
    """
    if not config.REPOSITORIES_DIR.is_dir():
        return [], []
    inputs: list[Path] = []
    compressed_only: list[str] = []
    for repository in sorted(config.REPOSITORIES_DIR.iterdir()):
        if not repository.is_dir():
            continue
        out = repository / "graphify-out"
        if (out / GRAPH_NAME).is_file():
            inputs.append(out / GRAPH_NAME)
        elif (out / ARCHIVE_NAME).is_file():
            compressed_only.append(repository.name)
    return inputs, compressed_only


def declared_repositories() -> list[str] | None:
    """Every repository `config/repositories.txt` names, or None when it cannot be read.

    None rather than an empty list on purpose. An unreadable declaration makes
    every merge input undeclared, which is true and useless: the finding is that
    the store has no declaration, not that it has a hundred rogue clones. The
    caller reports that condition on its own terms.
    """
    path = config.REPOSITORIES_CONFIG
    if not path.is_file():
        return None
    try:
        return [repo.name for repo in read_repository_config(path)]
    except (OSError, ValueError):
        return None


def repository_of(graph: Path) -> str:
    """The repository a merge input belongs to: `repositories/<name>/graphify-out/graph.json`."""
    return graph.parent.parent.name


@dataclass(frozen=True)
class Reconciliation:
    """Merge inputs set against the estate declaration and provenance."""

    inputs: tuple[Path, ...]
    declared: tuple[str, ...] | None
    recorded: tuple[str, ...]
    undeclared: tuple[str, ...]
    ungrounded: tuple[str, ...]
    missing: tuple[str, ...]
    compressed_only: tuple[str, ...]

    @property
    def closed(self) -> bool:
        """True when every merge input is both declared and dated.

        This is the property the issue is about, and it is deliberately not
        "nothing to report": `missing` and `compressed_only` describe inputs the
        merge will *omit*, which loses content but never puts an undeclared node
        into the graph. Folding them in here would make a mid-refresh tree
        report a closure failure it does not have.

        An empty input set is not closed. There is nothing to be closed over,
        and reporting a vacuous pass is the failure this module exists to stop.
        """
        return (
            bool(self.inputs)
            and self.declared is not None
            and not (self.undeclared or self.ungrounded)
        )


def reconcile() -> Reconciliation:
    """Set what the merge would read against what the store declares and recorded."""
    inputs, compressed_only = discovered()
    names = [repository_of(graph) for graph in inputs]
    declared = declared_repositories()
    recorded = provenance.read()

    on_disk = set(names)
    undeclared = () if declared is None else tuple(sorted(set(names) - set(declared)))
    missing = () if declared is None else tuple(sorted(set(declared) - on_disk))
    return Reconciliation(
        inputs=tuple(inputs),
        declared=None if declared is None else tuple(sorted(declared)),
        recorded=tuple(sorted(recorded)),
        undeclared=undeclared,
        ungrounded=tuple(sorted(on_disk - set(recorded))),
        missing=missing,
        compressed_only=tuple(compressed_only),
    )


def _named(names: tuple[str, ...], limit: int | None) -> str:
    """The names, all of them unless a caller asks for a dashboard-sized list."""
    if limit is None or len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" and {len(names) - limit} more"


def _repositories(count: int) -> str:
    """ "repository" or "repositories". A line an operator reads should read."""
    return "repository" if count == 1 else "repositories"


def _cannot_answer(report: Reconciliation) -> list[str]:
    """Lines for the two states where the reconciliation could not run at all.

    Kept apart from the divergence lines because they are a different claim. A
    divergence says the store is inconsistent; these say this check learned
    nothing, and the one thing they must never do is read like a clean result.
    """
    lines: list[str] = []
    if not report.inputs:
        lines.append(
            f"Merge inputs: none found under {merge_glob()} - so nothing was reconciled "
            "and this is not a clean result. Run the per-repository extraction first."
        )
    if report.declared is None:
        lines.append(
            f"Merge inputs: {_shown(config.REPOSITORIES_CONFIG)} could not be read, so nothing "
            f"declares the {len(report.inputs)} graph(s) the merge would read. "
            "Run `knowledgestore discover`."
        )
    return lines


def _divergences(report: Reconciliation, limit: int | None) -> list[str]:
    """One line per divergence, each naming the repositories it found."""
    lines: list[str] = []
    total = len(report.inputs)
    if report.undeclared:
        lines.append(
            f"Undeclared merge input: {len(report.undeclared)} of {total} graph(s) the merge "
            f"would read are not named in {_shown(config.REPOSITORIES_CONFIG)} - "
            f"{_named(report.undeclared, limit)}. The merged graph would carry nodes this "
            "store does not declare."
        )
    if report.ungrounded:
        lines.append(
            f"Ungrounded merge input: {len(report.ungrounded)} of {total} graph(s) the merge "
            f"would read have no entry in {_shown(config.PROVENANCE_PATH)} - "
            f"{_named(report.ungrounded, limit)}. An answer citing them cannot name the "
            "commit they were read at."
        )
    if report.missing:
        declared_total = len(report.declared or ())
        lines.append(
            f"Declared but not extracted: {len(report.missing)} of {declared_total} declared "
            f"repositories with no {GRAPH_NAME} on disk - {_named(report.missing, limit)}. "
            "The merge will omit them."
        )
    if report.compressed_only:
        lines.append(
            f"Extracted but not merged: {len(report.compressed_only)} "
            f"{_repositories(len(report.compressed_only))} with only {ARCHIVE_NAME} and no "
            f"{GRAPH_NAME} - {_named(report.compressed_only, limit)}. The documented merge "
            f"glob names {GRAPH_NAME}, so it will not read them."
        )
    return lines


def lines(report: Reconciliation, limit: int | None = None) -> list[str]:
    """The operator-facing report: a summary line, then every divergence found.

    `limit` caps how many names each line prints. `status` passes one because it
    is a dashboard; the stage passes none, because naming every divergence is
    the whole ask - a count tells an operator that something is wrong and not
    which repository to look at.
    """
    if not report.inputs or report.declared is None:
        return _cannot_answer(report)
    summary = (
        f"Merge inputs: {len(report.inputs)} graph(s) under {merge_glob()}; "
        f"{len(report.declared)} repositories declared, {len(report.recorded)} recorded "
        "in provenance"
    )
    divergences = _divergences(report, limit)
    if not divergences:
        return [f"{summary} - every input is declared and dated"]
    return [summary, *divergences]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledgestore merge-inputs",
        description=(
            "Reconcile the graphs `graphify merge-graphs` would read against "
            "config/repositories.txt and knowledge/provenance.json, naming every divergence."
        ),
        epilog=(
            "Feed the merge the names rather than a glob: "
            "`knowledgestore merge-inputs --paths > inputs.txt` writes one path per line on "
            "stdout and every divergence on stderr, so a merge that proceeds with an "
            "undeclared input says so even when its output is piped."
        ),
    )
    parser.add_argument(
        "--paths",
        action="store_true",
        help="print one merge input path per line on stdout; the report goes to stderr",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when a merge input is undeclared or has no provenance entry",
    )
    arguments = parser.parse_args(argv)

    report = reconcile()
    # With --paths, stdout is the merge's argument list and nothing else may
    # reach it. The report still has to be seen, so it goes to stderr - which a
    # redirect of stdout leaves on the operator's terminal.
    destination = sys.stderr if arguments.paths else sys.stdout
    for line in lines(report):
        print(line, file=destination)
    if arguments.paths:
        # Every input, including the undeclared ones. Silently dropping them
        # would change what the merge reads on this stage's own judgement, and
        # the report above has already named them.
        for graph in report.inputs:
            print(graph)

    if not report.inputs or report.declared is None:
        return 1
    return 1 if arguments.strict and not report.closed else 0


if __name__ == "__main__":
    raise SystemExit(main())
