"""The content set: which corpus files the pipeline decided were content.

The graph is the product. This module is about what happens **after the graph has
already failed to answer somebody** — they fall back to grepping the corpus, and
on a real estate the overwhelming majority of what they then read is not corpus
at all. Measured on one estate, the store's own content set against what a naive
recursive search sees:

    files the store calls content      ~23,000
    files a naive search sees         ~190,000
    noise                                  88%

Roughly a third of that noise was **the pipeline's own output**, present in every
repository, because the pipeline writes into the tree it reads and nothing
distinguishes what it wrote from what it was given. Confirmed on a second estate
with an entirely different dominant source — vendored dependency bundles and VCS
pack files rather than pipeline cache. The mechanism is the same either way.

The failure lands at the worst moment: on the fallback, on someone who has no
reason to suspect the tree. One recorded case answered a question correctly by
reading fifty-three matches one at a time, every one of them a false positive
from a vendored bundle or a pack file.

## Why this exposes a computed set rather than shipping an exclusion list

Every store working around this keeps a hand-maintained list of directories to
skip, and that list is **a second model of what the tool produces**. It is correct
on the day it is written and silently wrong the next time the pipeline emits
something new — it fails by omission, which is invisible. One store's list
excluded dependency bundles, build output and state files, and not the pipeline's
own directory, so hundreds of its own JSON artefacts were being fed back to the
extractor. That was found by watching a run parse a graph file as a source file.

The pipeline already knows the answer, positively: graphify's detect pass wrote
down every file it classified. Anything derived from what the pipeline *actually
saw* cannot drift by omission, because a new artefact it did not classify is
simply not in the set. So this module reads that, and everything else here —
including which directories the noise lives in — is **derived from it** rather
than described alongside it.

Two consequences worth stating, because they are what keep the derivation honest:

- **Every category detect reports is content**, including one this library has
  never heard of. `build_chunk_plan.KNOWN_KINDS` is deliberately a closed list
  there, because refusing `documnet` is better than planning nothing for it. Here
  a closed list would be the drift this module exists to avoid: a graphify release
  adding a category would quietly move those files into the noise.
- **The noise is never classified, only located.** `noise_roots` names the
  shallowest directory in each repository under which the store found no content
  at all. That is a measurement of the content set, not a judgement about
  directory names, so it needs no list to maintain and it reports whatever
  actually dominates a given estate.

## The one named list here, and why it is not the drift above

Two formats are named rather than derived, and the difference from an exclusion
list is the direction of the claim. An exclusion list says "these are the
uninteresting places on this estate", which is an estate-shaped judgement that
goes stale by omission. `SECRET_BEARING_SUFFIXES` and `EMULATOR_DUMP_NAMES` say
"this generated format contains this" - a statement about the format, true
wherever the format occurs, and wrong only if the format changes.

**The two formats are not in the same state today, and the difference matters
more than the rule.** Measured against the installed peer version:

    __azurite_db_*.json    classified, so the exclusion below does work
    *.tfstate              NOT classified, so `secret_bearing` returns nothing

So the emulator exclusion is live, and the secret-bearing refusal is
**defence-in-depth against a change in a peer tool, not a fix for a live
exposure**. No state file reaches the content set on the current peer version,
because detect has no entry for the format; a state file is one extension-map
entry away from being classified, since detect already classifies a plain `.json`
as code. `tests/test_documented_graphify_behaviour.py` pins both halves against
the real detect pass, and the day the first one fails the refusal becomes live
and this paragraph has to be rewritten.

Stated that way because the alternative is worse than saying nothing: a check
that cannot fire, described as though it were guarding something, reads as
protection and is believed because it is present.

What the refusal is for, if it fires. A Terraform state file the pipeline
classified as **content** is *in* the content set, so it is never a contentless
directory and it is not a high-degree node either - neither reporting surface
above can reach it. And for a file holding resolved secret values, reporting is
the wrong shape whatever surface it appears on: by the time a person reads the
report the content is extracted, and extracted content persists in the extraction
cache (keyed by content hash) and in each clone's own `graphify-out/`, which
`sync` deliberately preserves. Filtering the published graph afterwards does not
reach either copy - which is itself asserted against the peer package rather than
described, in `ExtractionCacheRetainsContentTest`.

So the secret-bearing case is a refusal that names every path, overridable by
name, and the emulator dumps are excluded and counted. The two stay separate:
collapsed together, the secret case becomes as ignorable as the wasted parse.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Container
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import store_paths

# The corpus subtree, relative to the store root. Shared with `store_paths`, which
# relativises a foreign absolute path at the same boundary.
CORPUS = "repositories"

# Where a non-content file is attributed when every directory above it does hold
# content somewhere: it is loose among content rather than sitting in a tree that
# has none. Named rather than dropped, because the buckets must sum to the
# population they describe - a tally that does not is how a bucket count comes to
# exceed the thing it counted.
LOOSE = "(loose beside content)"

# The attribution key for a repository that holds no content at all.
WHOLE_REPOSITORY = "(the whole repository)"

# Two named formats, and they are named formats on purpose.
#
# **Not a size rule and not a per-language ban.** Size is not the signal: an
# estate's largest content files are routinely its schemas and its variable
# files, and any byte threshold is wrong by orders of magnitude on some estate.
# A per-language ban is worse - `.tf` and `.tfvars` are an estate's own
# infrastructure surface and among the most valuable content it holds. The claim
# made here is narrower and checkable in each case: *this named format is
# generated, and what it contains is known*.

# Terraform / OpenTofu state. The format holds provider outputs **verbatim**,
# resolved, including passwords, keys and connection strings, so the format is
# the evidence and no inspection of the file is needed to know it. Matched on the
# final path component at any depth, because a state file in a subdirectory holds
# the same resolved values as one beside the README.
#
# **This cannot fire on the current peer version, and that is the honest
# description of it.** graphify's detect pass does not classify `.tfstate` at all,
# so no state file reaches the content set and `secret_bearing` returns nothing on
# any real estate. This is defence-in-depth against a peer change - one
# extension-map entry, given detect already classifies a plain `.json` as code -
# and not a fix for a live exposure. The premise is pinned against the real detect
# pass by `DetectClassificationOfNamedFormatsTest`, so the day it changes a test
# fails and says so; without that test this comment would be the only record that
# the rule is dormant, and a dormant rule described as an active one is worse than
# no rule.
SECRET_BEARING_SUFFIXES = (".tfstate", ".tfstate.backup")

# Storage-emulator dumps. The **lesser** category, deliberately kept separate:
# these are wasted work rather than a leak - graphify itself reports that they
# produce zero nodes - so they cost a parse and yield nothing. Matched on the
# generated filename rather than on the emulator's name anywhere in the path,
# which would take out an estate's own emulator wiring: a client module, a
# compose file, a fixture directory, all of them authored content.
EMULATOR_DUMP_NAMES = ("__azurite_db_*.json",)


@dataclass(frozen=True)
class NoiseRoot:
    """A directory, repository-relative, under which the store found no content.

    `files` is how many non-content files it holds across every repository where
    it occurs; `repositories` is how many of them have one.
    """

    path: str
    files: int
    repositories: int


def content_paths(detect: dict) -> list[str]:
    """Every file detect classified, store-relative, sorted and deduplicated.

    Sorted and deduplicated because the artefact this writes is committed: two
    runs on the same detect result must produce the same bytes, and detect can
    name one path under two categories.

    **Reads whatever categories detect reported**, rather than a list of the ones
    this library knows about. A closed list would drop a category a later graphify
    adds, and dropping it moves real content into the noise silently - which is
    the drift this module exists not to have.
    """
    files = detect.get("files") or {}
    if not isinstance(files, dict):
        return []
    found: set[str] = set()
    for paths in files.values():
        if isinstance(paths, list):
            found.update(store_paths.relative(p) for p in paths if isinstance(p, str) and p)
    return sorted(found)


def read_allowed(path: Path) -> set[str]:
    """Paths this estate has declared safe, store-relative. Absent means none.

    Hand-maintained and optional, in the style of the other `config/`
    declarations: whether a named file is safe is a ruling, and nothing in the
    pipeline can derive a ruling. Blank lines and `#` comments are skipped.

    Every declared path is relativised, because anything copied out of a graphify
    command line is absolute - and a declaration that silently matches nothing
    reads exactly like a declaration that was honoured, since the refusal simply
    stands and the obvious conclusion is that the file is genuinely unsafe.
    """
    if not path.is_file():
        return set()
    declared: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            declared.add(store_paths.relative(line))
    return declared


def secret_bearing(content: list[str], allowed: Container[str] = ()) -> list[str]:
    """Content-set paths in a format that holds resolved secret values, sorted.

    **Returns nothing on the current peer version**: detect does not classify
    `.tfstate`, so no state file reaches a real content set. Every test that
    exercises the refusal therefore drives a constructed content set, and the
    premise itself is pinned separately, against the real detect pass, by
    `DetectClassificationOfNamedFormatsTest`.

    If it does fire, "report it and let a human judge" is the wrong shape and
    neither reporting surface in this module can see the file anyway: a state file
    classified as content is *in* the content set, so it is never a `noise_roots`
    row, and it is not a high-degree node either.
    """
    return sorted(
        path
        for path in content
        if path not in allowed and PurePosixPath(path).name.endswith(SECRET_BEARING_SUFFIXES)
    )


def emulator_dumps(content: list[str], allowed: Container[str] = ()) -> list[str]:
    """Content-set paths that are storage-emulator dumps, sorted.

    Excluded and counted rather than refused. Collapsing this into
    `secret_bearing` would make the secret case as ignorable as this one.
    """
    return sorted(
        path
        for path in content
        if path not in allowed
        and any(
            fnmatch.fnmatchcase(PurePosixPath(path).name, pattern)
            for pattern in EMULATOR_DUMP_NAMES
        )
    )


def kind_counts(detect: dict) -> dict[str, int]:
    """How many files each detect category named, category order sorted."""
    files = detect.get("files") or {}
    if not isinstance(files, dict):
        return {}
    return {
        kind: len(paths)
        for kind, paths in sorted(files.items())
        if isinstance(paths, list) and isinstance(kind, str)
    }


def tree_files(corpus: Path) -> list[str]:
    """Every file under the corpus, store-relative and sorted - what grep sees.

    Nothing is skipped, deliberately: this is the population a naive recursive
    search reads, so excluding anything from it would understate the very number
    this exists to report. Symlinks count as files for the same reason, and
    directory symlinks are not followed, because a search that walks them reports
    the same content twice.
    """
    if not corpus.is_dir():
        return []
    found: list[str] = []
    for directory, _, names in os.walk(corpus, followlinks=False):
        base = Path(directory)
        found.extend(store_paths.relative(base / name) for name in names)
    return sorted(found)


def _bearing_directories(content: set[str]) -> set[str]:
    """Every directory with content somewhere beneath it, content included."""
    bearing: set[str] = set()
    for path in content:
        for parent in Path(path).parents:
            text = str(parent)
            if text in bearing:
                break
            bearing.add(text)
    return bearing


def _attribution(path: str, bearing: set[str]) -> str:
    """Where one non-content file is attributed: exactly one key, always.

    The shallowest directory above it that holds no content anywhere beneath.
    Walking down from the corpus root rather than up from the file is what makes
    the answer the *shallowest* such directory: `graphify-out/cache/ast/<sha>.json`
    is attributed to `graphify-out`, not to the hash directory it sits in, so an
    estate's dominant noise source reads as one line instead of thousands.
    """
    parts = Path(path).parts
    # parts[0] is the corpus directory and parts[1] the repository, so the first
    # candidate for a noise root is the repository itself.
    for depth in range(2, len(parts)):
        candidate = str(Path(*parts[:depth]))
        if candidate not in bearing:
            return WHOLE_REPOSITORY if depth == 2 else str(Path(*parts[2:depth]))
    return LOOSE


def noise_roots(tree: list[str], content: list[str]) -> list[NoiseRoot]:
    """Where the non-content files are, aggregated across repositories.

    Ordered by size, then by path, so the report is reproducible: sorting on the
    count alone leaves ties to whatever order the walk produced.

    The counts sum to the number of non-content files exactly. That is asserted by
    the caller and it is not a formality - a per-file tally that attributes one
    file to several buckets sums to more than the population it describes, and then
    reads as a larger problem than the estate has.
    """
    held = set(content)
    bearing = _bearing_directories(held)
    files: dict[str, int] = {}
    repositories: dict[str, set[str]] = {}
    for path in tree:
        # Content is not noise. Testing membership of the content set rather than
        # of `bearing`: `bearing` holds directories, so a file path is never in it
        # and the same-looking check would have attributed every content file to a
        # noise root - inflating the very figure this reports.
        if path in held:
            continue
        key = _attribution(path, bearing)
        files[key] = files.get(key, 0) + 1
        parts = Path(path).parts
        repositories.setdefault(key, set()).add(parts[1] if len(parts) > 2 else "")
    return sorted(
        (NoiseRoot(key, count, len(repositories[key])) for key, count in files.items()),
        key=lambda root: (-root.files, root.path),
    )
