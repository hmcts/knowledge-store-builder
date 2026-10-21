"""What a store contains, measured from the files on disk.

A reader of a store - first the MCP server - has to know which layers it can
read before it reads any. Guessing from whichever files happen to exist means
every reader carries its own copy of the store layout and its own idea of what a
missing file means. The manifest says it once: one entry per layer, each with
the store-relative path a reader should open and whether it is there.

**Measured, never assumed.** `present` is a file or directory test made when the
manifest is computed. Flags a file cannot answer - whether source links were
written, whether the history separates landed from proposed work, whether the
deployments stage put anything in the graph - come from `knowledge/telemetry.json`,
which the stages that know write. A flag nothing recorded is `null`, meaning
unknown, never `false`: `false` says the store was measured and lacks it, and a
reader acts differently on the two.

**The graph is never parsed.** It can be 1.6 GB decompressed; its presence is a
file test and its flags are read from telemetry.

**Deterministic** (CLAUDE.md 1.3), and stable across clones. Nothing here reads
a clock or a modification time: a clone and an identical rebuild move every
mtime, so a field built from one would make the committed manifest churn. What
says whether a store changed is `digest` - a content digest per layer, and one
over all of them - and what says how recent its sources are is `sources_as_of`,
the newest commit date recorded in provenance. Hashing a file is not parsing
it, so the graph is still never parsed (CLAUDE.md 6.6).

The format version: the major number changes when a reader written for the old
version would misread the store - a field changes meaning or a file moves. The
minor number changes when a layer or a field is added.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import posixpath
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import __version__, config, io, provenance, telemetry
from .git_env import git_env

FORMAT: tuple[int, int] = (1, 0)

# The one kind an extra layer may declare: written answers a reader can serve
# alongside the library's own topic briefs and deep dives.
EXTRA_KINDS = ("answers",)


class UsageError(Exception):
    """A refusal that ends the run with exit 1 and this message."""


@dataclass(frozen=True)
class Layer:
    """One layer: store-relative candidate paths, any one of which counts."""

    name: str
    paths: tuple[str, ...]
    kind: str = "file"  # or "dir"


def _rel(path: Path) -> str:
    """A config path as the store-relative string a manifest reader opens.

    Relative to `config.ROOT` at import, which is a statement about the store's
    layout rather than a copy of a setting: `configure(root=...)` moves every path
    together, so the relative form is the same for every root, and `compute` joins
    it to the root it is given.
    """
    return path.relative_to(config.ROOT).as_posix()


# In the order a reader meets them. Paths come from `config` wherever it names
# them; `relationships` (#455) and `index` (#457) are written by no stage yet, so
# config does not name them and they are spelled here.
LAYERS: tuple[Layer, ...] = (
    # The committed archive first: every store commits it and gitignores the plain
    # file, which the stages write beside it under the same name.
    Layer("graph", (_rel(config.GRAPH_PATH.with_suffix(".json.gz")), _rel(config.GRAPH_PATH))),
    Layer("labels", (_rel(config.LABELS_PATH),)),
    Layer("provenance", (_rel(config.PROVENANCE_PATH),)),
    Layer("repository_manifest", (_rel(config.MANIFEST_PATH),)),
    Layer("context", (_rel(config.CONTEXT_PATH),)),
    # Committed, and the asker docs tell a reader to grep it.
    Layer("content_files", (_rel(config.CONTENT_FILES_PATH),)),
    Layer("history", (_rel(config.HISTORY_DIR),), "dir"),
    Layer("intent", (_rel(config.INTENT_INDEX_PATH),)),
    Layer("ticket_descriptions", (_rel(config.TICKET_DESCRIPTIONS_PATH),)),
    Layer("ticket_tracker", (_rel(config.TICKET_TRACKER_PATH),)),
    Layer("summaries", (_rel(config.SUMMARIES_PATH),)),
    Layer("topics", (_rel(config.TOPICS_BRIEFS_PATH),)),
    Layer("deep_dives", (_rel(config.DEEPDIVES_PATH),)),
    Layer("semantic", (_rel(config.SYNONYMS_PATH),)),
    Layer("explorer", (_rel(config.EXPLORER_PATH),)),
    Layer("relationships", ("knowledge/relationships/by-repo",), "dir"),
    # The archive first, as for the graph: on a large estate the plain index is
    # over GitHub's 100 MB file limit, so a store commits only the `.gz`.
    Layer("index", ("knowledge/index/store.sqlite.gz", "knowledge/index/store.sqlite")),
)


# Every path `config` names that is not a layer, and why. Held against `config` by
# `tests/test_store_manifest.py`: a path the library writes that is neither a layer
# nor here fails the suite, so adding an artefact forces the decision whether a
# reader should be told about it. Keyed by the config name.
NOT_A_LAYER: dict[str, str] = {
    # inputs a person maintains
    "BOUNDARY_PATH": "a hand-maintained input: the estate's declared boundary",
    "CONTENT_CUTS_PATH": "a hand-maintained input: candidate cuts for size-cuts to measure",
    "CONTENT_SET_ALLOWED_PATH": "a hand-maintained input: files ruled safe despite their format",
    "EXTERNAL_CONFIG": "a hand-maintained input: fetch-only repositories",
    "FILTERS_PATH": "a hand-maintained input: repository filters",
    "QUESTIONS_PATH": "a hand-maintained input: the questions a store is asked",
    "REPOSITORIES_CONFIG": "a hand-maintained input: the repositories to build from",
    "STORE_EXTRA_LAYERS_PATH": "an input the manifest reads to list a store's own layers",
    "TOPICS_CONFIG_PATH": "a hand-maintained input: the topics to write about",
    "REQUIREMENTS_PATH": "a dependency pin, not store content",
    "LOCK_PATH": "a dependency pin, not store content",
    # working directories, regenerable and not committed
    "REPOSITORIES_DIR": "working clones, regenerable and not committed",
    "EXTERNAL_DIR": "working clones of fetch-only repositories, not committed",
    # intermediates a stage hands to the next
    "CHUNK_PLAN_PATH": "a build input: the map from chunk number to file list",
    "DETECT_PATH": "an intermediate of the graph extraction pass",
    "UNCACHED_PATH": "an intermediate of the graph extraction pass",
    "CLUSTERING_RECORD_PATH": "a record of the last clustering's inputs, read back by the next",
    "EXPLORER_INPUTS_PATH": "a record of the explorer's inputs, read back by the next build",
    "SUMMARIES_INPUT_PATH": "the input of the summaries stage, not its answers",
    "TOPICS_INPUT_PATH": "the input of the topics stage, not its answers",
    "DEEPDIVES_INPUT_DIR": "holds the deep-dive inputs beside dives.json, which is the layer",
    "SUMMARIES_SNAPSHOT_PATH": "membership before a re-cluster, kept so a remap can run",
    "SUMMARIES_FILE_SNAPSHOT_PATH": "file-keyed membership before a re-cluster, for the remap",
    "CONTENT_SET_PATH": "a committed record of the content-set counts, read by status",
    # reports and diagnostics
    "GRAPH_REPORT_PATH": "a human-readable report on the graph, not a layer a reader opens",
    "REMAP_REPORT_PATH": "a report on the last remap",
    "SUMMARIES_WITHDRAWN_PATH": "prose a remap would not re-key, awaiting revision",
    "TRACKER_UNDECIDED_PATH": "ticket prefixes nobody has ruled on, a report",
    # documents the topic and deep-dive layers are built from
    "TOPICS_DOCS_DIR": "the written topic pages that briefs.json is built from",
    "DEEPDIVES_DOCS_DIR": "the written deep-dive pages that dives.json is built from",
    # intent-stage sidecar
    "TICKET_TITLES_PATH": "committed titles the explorer build reads as input; not an asker-facing layer",
    # the manifest's own sources and output
    "TELEMETRY_PATH": "what the stages measured; the manifest reads it for flags",
    "STORE_MANIFEST_PATH": "the manifest itself",
}


def run_git(arguments: list[str]) -> str:
    """Run git and return stdout. The IO boundary; tests may replace it.

    stderr rides on the `CalledProcessError`, not the terminal: the expected
    failure - no `origin` remote - is answered by the refusal `store_identity`
    raises, which names both remedies, while a refusal about a layer quotes git.
    """
    # Bytes, decoded as the file system decodes names: text mode translates a
    # carriage return in a file name to a newline, so a listing of paths would
    # name files that are not there.
    return os.fsdecode(
        subprocess.run(
            ["git", *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_env(),
        ).stdout
    )


def _repository_name(url: str) -> str:
    """`example-store` from an https, ssh or scp-like clone URL; "" if none."""
    trimmed = url.strip().rstrip("/")
    if trimmed.endswith(".git"):
        trimmed = trimmed[: -len(".git")]
    # `/` separates path segments in every form; `:` also separates the host from
    # the path in the scp-like form `git@host:org/repo`.
    return re.split(r"[/:]", trimmed)[-1]


def _top_level(root: Path, run) -> Path | None:
    """The top of the git repository holding `root`, or None outside one."""
    try:
        return Path(run(["-C", str(root), "rev-parse", "--show-toplevel"]).strip()).resolve()
    except (OSError, subprocess.CalledProcessError):
        return None


def store_identity(root: Path, run=run_git) -> tuple[str, str]:
    """The store's id and GitHub org.

    The id is `config.STORE_ID` (`KSB_STORE_ID`) when set, else the repository
    name of the `origin` remote. Never the directory name: a reader identifies a
    store by this id, and a clone may be checked out under any directory name.
    """
    store_id = config.STORE_ID
    if store_id:
        return store_id, config.GITHUB_ORG or ""
    top = _top_level(root, run)
    if top is not None and top != root.resolve():
        # The origin of the repository above would name a different store.
        raise UsageError(
            f"Cannot name the store at {root}: it is not the top of its git repository "
            f"({top}), so that repository's `origin` would name another store. Set "
            "KSB_STORE_ID, or point at the top of the store's own clone."
        )
    try:
        store_id = _repository_name(run(["-C", str(root), "remote", "get-url", "origin"]))
    except (OSError, subprocess.CalledProcessError):
        store_id = ""
    if not store_id:
        raise UsageError(
            f"Cannot name the store at {root}: KSB_STORE_ID is not set and the clone has "
            "no `origin` remote to take the repository name from. Set KSB_STORE_ID, or "
            "add the remote with `git remote add origin <url>`."
        )
    return store_id, config.GITHUB_ORG or ""


def _ignored(root: Path, rel: str, run) -> set[str]:
    """The store-relative files under `rel` that git ignores and does not track.

    `ls-files -o -i --exclude-standard` lists exactly those: untracked, so a file
    force-added past a rule is committed and not listed, and ignored, so what a
    clone will not receive. NUL-separated, because a name may hold any other
    character. Outside a repository there are no rules to judge by and nothing is
    ignored. Inside one, a git failure is a refusal and never "none ignored": the
    manifest would offer files a clone may lack and say nothing (a layer below a
    symlinked directory is one such failure).
    """
    if _top_level(root, run) is None:
        return set()
    below = root
    for part in Path(rel).parts:
        below = below / part
        # git does not look beyond a link to a directory: it lists nothing, with
        # no error, which would read as "nothing is ignored" and hash files it
        # should not.
        if below.is_symlink() and below.is_dir():
            raise UsageError(
                f"Cannot tell which files of {rel} git would commit: {below.relative_to(root)} "
                "is a link to a directory, and git does not look through it. Replace the link "
                "with the directory."
            )
    try:
        listing = run(
            ["-C", str(root), "ls-files", "-z", "-o", "-i", "--exclude-standard", "--", rel]
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = os.fsdecode(getattr(error, "stderr", None) or str(error)).strip()
        raise UsageError(f"Cannot tell which files of {rel} git would commit: {detail}") from error
    return {name for name in listing.split("\0") if name}


def _probe(root: Path, rel: str, kind: str, run) -> tuple[bool, bool, list[Path]]:
    """(on disk, received by a clone, the entries a clone receives).

    A directory is received when at least one non-ignored file is in it, and a
    directory a stage created and left empty has built nothing. The entries are
    what its digest reads, so builder and clone hash the same files.
    """
    path = root / rel
    if kind != "dir":
        if not path.is_file():
            return False, False, []
        return True, rel not in _ignored(root, rel, run), []
    if not path.is_dir():
        return False, False, []
    entries = sorted(path.rglob("*"))
    skipped = _ignored(root, rel, run)
    kept = [entry for entry in entries if entry.relative_to(root).as_posix() not in skipped]
    return (
        any(entry.is_file() for entry in entries),
        any(entry.is_file() for entry in kept),
        kept,
    )


def _listed(rel: str, kind: str) -> str:
    return rel + "/" if kind == "dir" else rel


def _measurements(root: Path) -> dict[str, int]:
    """What the stages recorded, or {} when there is no readable record.

    A malformed record is unknown rather than a failure: the file is committed,
    so it is hand-edited and merge-resolved, and a damaged one must not stop the
    manifest saying what the files show.
    """
    try:
        return telemetry.read(root / _rel(config.TELEMETRY_PATH))
    except (OSError, ValueError):
        return {}


def _flag(measurements: dict[str, int], metric: str, test) -> bool | None:
    """`test(value)` for a recorded metric; None when nothing recorded it."""
    if metric not in measurements:
        return None
    return test(measurements[metric])


def _file_digest(path: Path) -> str:
    """The first 16 hex digits of the file's sha256, streamed.

    Streamed in 1 MiB chunks because a plain `graph.json` can be several
    gigabytes. A loop rather than `hashlib.file_digest`, which needs Python 3.11
    while this package supports 3.10. The value is the one `io.layer_digests`
    records for the same file.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _held(path: Path, root: Path) -> None:
    """Refuse a path that resolves outside the store.

    A layer's digest reads the target of any link it holds, so a link out of the
    store would put a file no other clone carries into a manifest that is
    committed and read elsewhere. A link that stays inside is hashed as its
    target. Resolved before comparing, on both sides: a store under a symlinked
    directory (`/tmp` on macOS) is still inside itself.
    """
    if root.resolve() not in path.resolve().parents:
        raise UsageError(
            f"{path.relative_to(root).as_posix()} resolves outside the store "
            f"({path.resolve()}). A layer may hold links that stay inside the store; "
            "copy the file in, or point the link at one that is."
        )


def _digest(path: Path, kind: str, root: Path, entries: list[Path]) -> str:
    """A content digest: the file's, or for a directory one over its files.

    A directory's is taken over `relative path NUL file digest` lines, sorted, so
    a renamed file changes it as surely as an edited one. `entries` are the ones
    a clone receives (see `_probe`). Every link met on the way must stay inside
    `root` (see `_held`).
    """
    _held(path, root)
    if kind != "dir":
        return _file_digest(path)
    for entry in entries:
        _held(entry, root)
    lines = sorted(
        f"{entry.relative_to(path).as_posix()}\0{_file_digest(entry)}"
        for entry in entries
        if entry.is_file()
    )
    return _text_digest(lines)


def _text_digest(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def _layers(root: Path, run) -> dict:
    """Each layer's entry: its paths, whether a clone receives it, and its digest.

    `present` says a clone of the store has the layer. One that exists only on
    this machine - ignored by git, or an uncompressed twin of a committed archive
    - is `local_only` with no digest.
    """
    layers: dict[str, dict] = {}
    for layer in LAYERS:
        probes = {rel: _probe(root, rel, layer.kind, run) for rel in layer.paths}
        found = [rel for rel in layer.paths if probes[rel][1]]
        chosen = found[0] if found else layer.paths[0]
        ordered = [chosen, *(rel for rel in layer.paths if rel != chosen)]
        described = {
            "paths": [_listed(rel, layer.kind) for rel in ordered],
            "present": bool(found),
            "digest": _digest(root / chosen, layer.kind, root, probes[chosen][2])
            if found
            else None,
        }
        if not found and any(probes[rel][0] for rel in layer.paths):
            described["local_only"] = True
        layers[layer.name] = described
    return layers


def _within(root: Path, rel: str) -> bool:
    """Strictly below the root once resolved: `.` names the whole store."""
    return root.resolve() in (root / rel).resolve().parents


def extra_layers(root: Path, run=run_git) -> dict:
    """The store's own layers, from `config/store-extra-layers.json` when it exists.

    Each declared path is held inside the store root: a reader resolves it
    against its own clone, so an absolute path or one that climbs out names a
    file no other clone holds - or one that is not the store's to publish.
    """
    declared_at = _rel(config.STORE_EXTRA_LAYERS_PATH)
    path = root / declared_at
    if not path.is_file():
        return {}
    try:
        declared = io.read_json(path)
    except ValueError as error:
        raise UsageError(f"{declared_at} is not valid JSON: {error}") from error
    if not isinstance(declared, dict):
        raise UsageError(f"{declared_at} must map each layer name to its declaration")
    result: dict[str, dict] = {}
    library = {layer.name for layer in LAYERS} | {"deployments"}
    for name, entry in sorted(declared.items()):
        if name in library:
            # The store digest is keyed by layer name across both tables.
            raise UsageError(f"extra layer {name!r} takes the name of a library layer")
        if not isinstance(entry, dict):
            raise UsageError(f"extra layer {name!r} must be an object with a path and description")
        rel, description = entry.get("path"), entry.get("description")
        if not isinstance(rel, str) or not rel:
            raise UsageError(f"extra layer {name!r} declares no path")
        if not isinstance(description, str) or not description:
            raise UsageError(f"extra layer {name!r} declares no description")
        # Absolute is refused even when it resolves inside this clone: it names
        # this checkout's directory, which no other clone has.
        if Path(rel).is_absolute() or not _within(root, rel):
            raise UsageError(
                f"extra layer {name!r} must name a path below the store root, relative "
                f"to it: {rel!r} does not."
            )
        # Written the way library layers are: normalised, with a directory marked by
        # a trailing slash, so `./answers` and `answers/../answers` name one layer.
        rel = posixpath.normpath(rel)
        target = root / rel
        # The library's own rule: a file, or a directory holding something.
        kind = "dir" if target.is_dir() else "file"
        exists, present, entries = _probe(root, rel, kind, run)
        described = {
            "path": _listed(rel, kind),
            "description": description,
            "present": present,
            "digest": _digest(target, kind, root, entries) if present else None,
        }
        if exists and not present:
            described["local_only"] = True
        if "kind" in entry:
            if entry["kind"] not in EXTRA_KINDS:
                raise UsageError(
                    f"extra layer {name!r} declares kind {entry['kind']!r}; the only kind is "
                    + ", ".join(repr(kind) for kind in EXTRA_KINDS)
                )
            described["kind"] = entry["kind"]
        result[name] = described
    return result


def _commit_moment(recorded) -> datetime.datetime | None:
    if not isinstance(recorded, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(recorded)
    except ValueError:
        return None
    # `git log --format=%cI` always carries an offset; a hand-edited date
    # without one is read as UTC rather than as this machine's local time.
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def sources_as_of(root: Path) -> str | None:
    """The newest commit date provenance records for the estate, as recorded.

    Repositories only: fetch-only `external` sources are not in the graph. Dates
    are compared as instants, because `%cI` keeps each committer's offset and two
    offsets do not sort as text. None when nothing is recorded.
    """
    try:
        recorded = provenance.read(root / _rel(config.PROVENANCE_PATH))
    except (OSError, ValueError):
        return None
    dated = [
        (moment, entry["committed"])
        for entry in recorded.values()
        if isinstance(entry, dict)
        and (moment := _commit_moment(entry.get("committed"))) is not None
    ]
    return max(dated)[1] if dated else None


def store_digest(layers: dict, extra: dict) -> str:
    """One digest over every layer's, extra layers included.

    An absent layer contributes `-` rather than nothing, so a layer appearing or
    vanishing moves it as surely as one changing.
    """
    entries = {**layers, **extra}
    return _text_digest(
        sorted(f"{name}\0{entry.get('digest') or '-'}" for name, entry in entries.items())
    )


def compute(root: Path, run=run_git) -> dict:
    """The full manifest for the store at `root`."""
    store_id, github_org = store_identity(root, run)
    layers = _layers(root, run)
    extra = extra_layers(root, run)
    measurements = _measurements(root)
    layers["graph"]["source_urls"] = _flag(measurements, "source_urls.linked", lambda n: n > 0)
    layers["intent"]["reach"] = _flag(
        measurements, "intent.repositories_without_reach", lambda n: n == 0
    )
    # Deployments are nodes inside the graph, so no file says whether the stage
    # added any; only the count it recorded can.
    layers["deployments"] = {
        "paths": [],
        "present": _flag(measurements, "deployments.pairs", lambda n: n > 0),
        "digest": None,
    }
    return {
        "format": {"major": FORMAT[0], "minor": FORMAT[1]},
        "store": {"id": store_id, "github_org": github_org},
        "library_version": __version__,
        "digest": store_digest(layers, extra),
        "sources_as_of": sources_as_of(root),
        "layers": layers,
        "extra_layers": extra,
    }
