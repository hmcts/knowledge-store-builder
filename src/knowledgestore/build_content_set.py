r"""Expose the content set, so a corpus search reads corpus.

The graph is the product and the graph is clean. This stage is about the
**fallback**: what a person sees when the graph has already failed to answer them
and they reach for `grep`. On one estate a naive recursive search over the corpus
reads roughly eight times as many files as the store considers content, so about
88 per cent of what arrives on the fallback path is not corpus at all - a third of
it the pipeline's own output, written into the tree the pipeline reads and present
in every repository. A second estate has the same ratio from a different dominant
source. One recorded question was answered correctly by reading fifty-three
matches one at a time, every one a false positive.

    knowledgestore content-set

        knowledge/corpus/content-files.txt   the set, one store-relative path a line
        knowledge/corpus/content-set.json    the counts, and where the noise lives

Both are committed. The path list is the point: its consumer is `grep`, and a
consumer that has to parse a manifest first re-derives the set badly instead.

    tr '\n' '\0' < knowledge/corpus/content-files.txt \
      | xargs -0 grep -nIs -- '<term>'

## Four things this stage refuses to do

**It never writes a set no clone on disk contributed to.** `detect` honours
`.gitignore` and `repositories/` is gitignored, so a pass run at the store root
classifies the store's own files and stops - and the set that comes back is small
rather than empty, so the refusal below is stepped around rather than triggered.
Clones that contributed nothing are otherwise reported and named, not refused: a
repository created and never populated legitimately contributes nothing.

**It never exposes a file in a format that holds resolved secret values.** A
Terraform state file holds provider outputs verbatim, and there "report it and
let a human judge" is the wrong shape: by the time the report is read the content
is extracted, and extracted content persists in the extraction cache and in each
clone's own `graphify-out/`, which `sync` deliberately preserves. So this is an
exit code that names every offending path, overridable per file in
`config/content-set-allowed.txt` or with `--allow`, because whether a named file
is safe is a ruling the pipeline cannot derive.

**On the current peer version this refusal cannot fire, and it is documented as
dormant rather than as a guard.** graphify's detect pass does not classify
`.tfstate`, so no state file reaches the content set - it is defence-in-depth
against a peer change, one extension-map entry away given detect already
classifies a plain `.json` as code. `content_set` carries the reasoning, and
`DetectClassificationOfNamedFormatsTest` pins the premise against the real detect
pass so the change is noticed rather than assumed.

**Storage-emulator dumps are the lesser half and this half is live.** Those files
*are* classified, so they do reach the content set, and they stay a separate,
lesser response - excluded from the set and counted in the report, since they are
wasted work rather than a leak.

**It never writes an empty set.** A store whose detect result is missing gets an
exit code and a sentence, not a file with nothing in it: an empty path list makes
every search return no matches, which reads as a confident answer about the estate
rather than as a missing input.

**It never reports a noise figure it did not measure.** Without the corpus on disk
- a sparse clone, or a store that has not synced - the tree side of the comparison
is zero, and zero would render as "no noise", the most flattering possible reading
of the least measured case. The manifest records `measured: false` and the report
says which artefact was and was not read.
"""

from __future__ import annotations

import argparse

from . import config, content_set, io, store_paths


def _corpus_measurement(content: list[str]) -> dict:
    """The tree side of the comparison, or an explicit refusal to claim one."""
    tree = content_set.tree_files(config.REPOSITORIES_DIR)
    if not tree:
        return {"measured": False}
    held = set(content)
    roots = content_set.noise_roots(tree, content)
    non_content = len([path for path in tree if path not in held])
    return {
        "measured": True,
        "tree_files": len(tree),
        "non_content_files": non_content,
        "content_files_absent_from_the_tree": len(held - set(tree)),
        "noise_roots": [
            {"path": root.path, "files": root.files, "repositories": root.repositories}
            for root in roots
        ],
    }


def _report_corpus(corpus: dict, content: int, top: int) -> None:
    """What the tree holds against what the store calls content."""
    if not corpus["measured"]:
        print(
            f"  The corpus at {content_set.CORPUS}/ holds no files here, so the noise "
            "figure is NOT measured and none is reported. This store is a sparse or "
            "partial clone, or has not synced yet; the content set above is still the "
            "set the pipeline computed.",
            flush=True,
        )
        return

    tree, noise = corpus["tree_files"], corpus["non_content_files"]
    print(
        f"  the corpus tree holds {tree:,} files, of which {noise:,} "
        f"({noise / tree:.1%}) are not in the content set",
        flush=True,
    )
    absent = corpus["content_files_absent_from_the_tree"]
    if absent:
        print(
            f"  {absent:,} of {content:,} content files are not on disk: detect is older "
            "than the tree. Re-run graphify's detect pass, and pass grep -s meanwhile.",
            flush=True,
        )
    _report_noise_roots(corpus["noise_roots"], noise, top)


def _report_noise_roots(roots: list[dict], noise: int, top: int) -> None:
    """Where the noise lives, and the reconciliation that makes the tally readable.

    The sum is printed rather than assumed. A per-file tally that attributes one
    file to more than one bucket sums to more than the population it describes and
    then reads as a larger problem than the estate has; this repository has shipped
    that shape before.
    """
    if not roots:
        return
    counted = sum(root["files"] for root in roots)
    print(
        f"  where it lives - {len(roots):,} directories hold no content at all, "
        f"accounting for {counted:,} of {noise:,} non-content files:",
        flush=True,
    )
    for root in roots[:top]:
        repositories = root["repositories"]
        print(
            f"    {root['files']:>9,}  {root['path']}  (in {repositories:,} "
            f"{'repository' if repositories == 1 else 'repositories'})",
            flush=True,
        )
    if counted != noise:
        print(
            f"  WARNING: the buckets total {counted:,} against {noise:,} non-content "
            "files, so this attribution is not a partition and its numbers cannot be "
            "added up. Treat the total above, not the buckets, as the measurement.",
            flush=True,
        )
    print(
        "  Those are measured, not classified: each is the shallowest directory in a "
        "repository under which the store found no content. Excluding them from "
        "extraction with a .graphifyignore is cheaper than filtering afterwards.",
        flush=True,
    )


def _report_contribution(contribution: content_set.Contribution) -> None:
    """Which clones on disk the content set holds nothing from, named.

    **A non-zero count here is the healthy case**, and the report says so. A
    repository that was created and never populated - a licence file, an ignore
    file, nothing else - contributes nothing and appears in none of detect's own
    exclusion buckets, so it is indistinguishable from a defect to anyone told
    only the number. Reading one name answers in seconds what the count cannot.

    Every name is printed rather than the largest few: the count is expected to be
    a handful, and truncating would hide precisely the run where it is not.
    """
    if not contribution.silent:
        return
    silent, clones = len(contribution.silent), len(contribution.clones)
    print(
        f"  {silent:,} of {clones:,} cloned "
        f"{'repository' if clones == 1 else 'repositories'} in {content_set.CORPUS}/ "
        "contributed no content file:",
        flush=True,
    )
    for name in contribution.silent:
        print(f"    {name}", flush=True)
    print(
        "  A non-zero count is expected here and is not by itself a defect: a repository "
        "created and never populated genuinely contributes nothing, and detect reports it "
        "under no exclusion of its own. Read the names - one you expected to hold content "
        "means detect never read that clone.",
        flush=True,
    )


def _no_contributor_refusal(contribution: content_set.Contribution) -> str:
    """Why nothing is written when no clone on disk contributed anything.

    States what the signal cannot distinguish rather than dodging it. A store
    holding one all-but-empty repository satisfies "all of them" and is refused on
    correct data; naming that reading is cheaper than either alternative, because
    a count threshold makes the check tunable and an escape flag would be taken
    without being read - this fires on a first build, where nobody has a baseline
    to check a skip against.
    """
    clones = len(contribution.clones)
    return (
        f"No content file comes from any of the {clones:,} cloned "
        f"{'repository' if clones == 1 else 'repositories'} in {content_set.CORPUS}/, so "
        "this content set describes something other than the corpus and nothing was "
        "written. A scan run at the store root produces exactly this: "
        f"{content_set.CORPUS}/ is gitignored, so detect reads the store's own files and "
        "reports a set that is small rather than empty - which is why the empty-set "
        "refusal cannot catch it. Re-scan with the corpus visible to detect, then run "
        "this stage again.\n"
        "  This cannot tell 'no clone has content' from 'the only clone has no content'. A "
        "store whose single clone is an all-but-empty repository reads identically here, "
        "and for that store this is a false alarm and the content set was correct. There "
        "is deliberately no flag to skip this: on a first build there is no baseline to "
        "check the skip against."
    )


def _report_absolute(content: list[str]) -> None:
    """Paths that could not be relativised, counted and named.

    Committing an absolute path records one build machine's directory layout in a
    tracked artefact, and relativising is silent when it cannot be done - a corpus
    outside the store root simply stays absolute and the file looks fine. The chunk
    plan carried tens of thousands of these before it was counted.
    """
    absolute = [path for path in content if path.startswith("/")]
    if not absolute:
        return
    print(
        f"  WARNING: {len(absolute):,} of {len(content):,} paths could not be made "
        f"relative and are absolute, starting with {absolute[0]}.\n"
        "  They are outside the store root, so this list carries this machine's layout "
        "and will not survive a clone. Point graphify at a corpus inside the store.",
        flush=True,
    )


def _refuse_secret_bearing(refused: list[str], classified: int) -> None:
    """Every offending path, then the two ways past the refusal.

    Named rather than counted, because the remedy is per file: each one is
    removed from the corpus or ruled safe by name, and a count names nothing to
    act on. The opt-out is printed with the refusal for the same reason - a
    refusal whose escape is undocumented is worked around outside the library.

    Unreachable on the current peer version, since detect classifies no
    `.tfstate`; see `content_set.SECRET_BEARING_SUFFIXES`.
    """
    print(
        f"REFUSING to write a content set: {len(refused):,} of the {classified:,} files "
        "the pipeline classified as content are in a format that holds resolved secret "
        "values. Nothing was written.",
        flush=True,
    )
    for path in refused:
        print(f"    {path}", flush=True)
    print(
        "  This is a refusal rather than a report because a report arrives too late. "
        "Extracted content persists in graphify-out/cache/ast/ keyed by content hash, "
        "and in each clone's own graphify-out/, which sync deliberately preserves - so "
        "filtering the published graph afterwards reaches neither copy.\n"
        "  Remove each file from the corpus and re-run the detect pass. If this estate "
        "has decided a named file is safe, declare it in "
        f"{store_paths.relative(config.CONTENT_SET_ALLOWED_PATH)} (one store-relative "
        "path a line, # comments allowed), or pass --allow <path> once per file for a "
        "single run.",
        flush=True,
    )


def _report_emulator_dumps(dumps: list[str], classified: int, exposed: int, top: int) -> None:
    """What was dropped from the set, counted and named, with the arithmetic.

    Deliberately not a refusal: this is wasted work rather than a leak. Equally
    deliberately not silent - an exclusion nobody sees is indistinguishable from
    content that was never there, which is the whole shape of this failure class.
    """
    if not dumps:
        return
    print(
        f"  excluded {len(dumps):,} emulator "
        f"{'dump' if len(dumps) == 1 else 'dumps'} from the set: detect classified "
        f"{classified:,} files and {exposed:,} are exposed. They are a named generated "
        "format that yields zero nodes, so they cost a parse and return nothing - "
        "wasted work rather than anything unsafe, which is why they are dropped here "
        "and a secret-bearing format is refused outright:",
        flush=True,
    )
    for path in dumps[:top]:
        print(f"    {path}", flush=True)
    if len(dumps) > top:
        print(
            f"  ...and {len(dumps) - top:,} more; all of them are named in "
            f"{store_paths.relative(config.CONTENT_SET_PATH)}.",
            flush=True,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore content-set",
        description="Write the content set a corpus search should read instead of the tree.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="how many noise directories to name in the report (default 10). The full "
        "list is always written to content-set.json",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="PATH",
        help="a store-relative path in a named format this stage refuses or excludes, "
        "that this estate has decided to expose anyway, repeatable. For a lasting "
        "decision write it into config/content-set-allowed.txt instead, where the "
        "ruling is reviewable",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    if arguments.top < 1:
        print("--top must be at least 1", flush=True)
        return 2

    detect = io.read_json_dict(config.DETECT_PATH)
    content = content_set.content_paths(detect)
    if not content:
        # Refused rather than written empty. An empty path list makes every search
        # over it return no matches, and no matches reads as an answer about the
        # estate rather than as a missing input.
        print(
            f"No files classified in {config.DETECT_PATH}, so there is no content set to "
            "expose and nothing was written. graphify writes that file when it scans the "
            "corpus - run its detect pass first. An empty list would make every search "
            "over it come back clean, which is the wrong answer, not a small one.",
            flush=True,
        )
        return 2

    contribution = content_set.contributions(config.REPOSITORIES_DIR, content)
    # Before the format rules, and measured on `content` rather than on what
    # survives them. This asks whether detect saw the corpus at all - if it did
    # not, the set is describing something other than the estate and searching it
    # for named formats answers a question about the wrong files.
    #
    # All or nothing, with no threshold, and the reason belongs here rather than in
    # a review comment: on a real corpus the gap between "some clones contributed
    # nothing" and "every clone contributed nothing" is orders of magnitude, so
    # nothing legitimate sits near the line. A percentage would be a tuned constant
    # - the obvious "why not warn at half of them?" - while *all of them* is a
    # structural impossibility for a corpus that was scanned clone by clone.
    if contribution.clones and len(contribution.silent) == len(contribution.clones):
        print(_no_contributor_refusal(contribution), flush=True)
        return 2

    # The two named formats, before anything is written. Both consult the same
    # declaration, so one place records every ruling this estate has made.
    allowed = content_set.read_allowed(config.CONTENT_SET_ALLOWED_PATH) | {
        store_paths.relative(path) for path in arguments.allow
    }
    refused = content_set.secret_bearing(content, allowed)
    if refused:
        _refuse_secret_bearing(refused, len(content))
        return 2
    dumps = content_set.emulator_dumps(content, allowed)
    dropped = set(dumps)
    exposed = [path for path in content if path not in dropped]

    corpus = _corpus_measurement(exposed)
    kinds = content_set.kind_counts(detect)
    manifest = {
        "generated_from": io.layer_digests([config.DETECT_PATH], config.ROOT),
        # Both numbers, so the exclusion reconciles in the artefact and not only
        # in the report: classified = content_files + excluded, exactly.
        "classified_files": len(content),
        "content_files": len(exposed),
        "excluded_emulator_dumps": dumps,
        "kinds": kinds,
        "corpus": corpus,
    }
    config.CONTENT_FILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    io.checked_write_target(config.CONTENT_FILES_PATH)
    config.CONTENT_FILES_PATH.write_text(  # NOSONAR(S2083, S8707) - validated above
        "".join(f"{path}\n" for path in exposed), encoding="utf-8"
    )
    io.write_json(config.CONTENT_SET_PATH, manifest, indent=2)

    print(
        f"{len(exposed):,} content files -> {config.CONTENT_FILES_PATH}\n"
        "  by detect category: "
        + (", ".join(f"{kind} {count:,}" for kind, count in kinds.items()) or "none reported"),
        flush=True,
    )
    _report_emulator_dumps(dumps, len(content), len(exposed), arguments.top)
    _report_corpus(corpus, len(exposed), arguments.top)
    _report_contribution(contribution)
    _report_absolute(exposed)
    # The store-relative path, not the basename: the command is copied and pasted
    # from the store root, where the basename alone names nothing.
    print(
        f"  Search this list, not the tree: tr '\\n' '\\0' < "
        f"{store_paths.relative(config.CONTENT_FILES_PATH)} | xargs -0 grep -nIs -- '<term>'",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
