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

**Deterministic** (CLAUDE.md 1.3). The one time-dependent field, `built`, is the
newest modification time among the layer files present, never the clock, so
computing twice over an unchanged store gives the same manifest.

The format version: the major number changes when a reader written for the old
version would misread the store - a field changes meaning or a file moves. The
minor number changes when a layer or a field is added.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import __version__, config
from .git_env import git_env

FORMAT: tuple[int, int] = (1, 0)

# Declared by a store, not by this library: its own layers, such as a service
# registry or written journeys. Read from the store root a manifest is computed
# for, like every other path here.
EXTRA_LAYERS_CONFIG = "config/store-extra-layers.json"

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


def run_git(arguments: list[str]) -> str:
    """Run git and return stdout. The IO boundary; tests may replace it.

    stderr is dropped because the expected failure - no `origin` remote - is
    answered by the refusal `store_identity` raises, which names both remedies.
    """
    return subprocess.run(
        ["git", *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=git_env(),
    ).stdout


def _repository_name(url: str) -> str:
    """`example-store` from an https, ssh or scp-like clone URL; "" if none."""
    trimmed = url.strip().rstrip("/")
    if trimmed.endswith(".git"):
        trimmed = trimmed[: -len(".git")]
    # `/` separates path segments in every form; `:` also separates the host from
    # the path in the scp-like form `git@host:org/repo`.
    return re.split(r"[/:]", trimmed)[-1]


def store_identity(root: Path, run=run_git) -> tuple[str, str]:
    """The store's id and GitHub org.

    The id is `KSB_STORE_ID` when set, else the repository name of the `origin`
    remote. Never the directory name: a reader identifies a store by this id, and
    a clone may be checked out under any directory name at all.
    """
    store_id = os.environ.get("KSB_STORE_ID", "").strip()
    if not store_id:
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


def _present(path: Path, kind: str) -> bool:
    if kind == "dir":
        # A directory a stage created and left empty has built nothing.
        return path.is_dir() and any(path.iterdir())
    return path.is_file()


def _listed(rel: str, kind: str) -> str:
    return rel + "/" if kind == "dir" else rel


def _measurements(root: Path) -> dict[str, int] | None:
    """What the stages recorded, or None when there is no readable record.

    A malformed record is unknown rather than a failure: the file is committed,
    so it is hand-edited and merge-resolved, and a damaged one must not stop the
    manifest saying what the files show.
    """
    path = root / _rel(config.TELEMETRY_PATH)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    measurements = document.get("measurements") if isinstance(document, dict) else None
    if not isinstance(measurements, dict):
        return None
    return {
        metric: value
        for metric, value in measurements.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _flag(measurements: dict[str, int] | None, metric: str, test) -> bool | None:
    """`test(value)` for a recorded metric; None when nothing recorded it."""
    if measurements is None or metric not in measurements:
        return None
    return test(measurements[metric])


def _newest(path: Path, kind: str) -> float:
    if kind == "dir":
        return max(
            (entry.stat().st_mtime for entry in path.rglob("*") if entry.is_file()),
            default=path.stat().st_mtime,
        )
    return path.stat().st_mtime


def _layers(root: Path) -> tuple[dict, float | None]:
    """Each layer's entry, and the newest modification time among those present."""
    layers: dict[str, dict] = {}
    newest: float | None = None
    for layer in LAYERS:
        found = [rel for rel in layer.paths if _present(root / rel, layer.kind)]
        chosen = found[0] if found else layer.paths[0]
        ordered = [chosen, *(rel for rel in layer.paths if rel != chosen)]
        layers[layer.name] = {
            "paths": [_listed(rel, layer.kind) for rel in ordered],
            "present": bool(found),
        }
        for rel in found:
            mtime = _newest(root / rel, layer.kind)
            newest = mtime if newest is None else max(newest, mtime)
    return layers, newest


def _within(root: Path, rel: str) -> bool:
    resolved_root = root.resolve()
    target = (root / rel).resolve()
    return target == resolved_root or resolved_root in target.parents


def extra_layers(root: Path) -> dict:
    """The store's own layers, from `config/store-extra-layers.json` when it exists.

    Each declared path is held inside the store root: a reader resolves it
    against its own clone, so an absolute path or one that climbs out names a
    file no other clone holds - or one that is not the store's to publish.
    """
    path = root / EXTRA_LAYERS_CONFIG
    if not path.is_file():
        return {}
    try:
        declared = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise UsageError(f"{EXTRA_LAYERS_CONFIG} is not valid JSON: {error}") from error
    if not isinstance(declared, dict):
        raise UsageError(f"{EXTRA_LAYERS_CONFIG} must map each layer name to its declaration")
    result: dict[str, dict] = {}
    for name, entry in sorted(declared.items()):
        if not isinstance(entry, dict):
            raise UsageError(f"extra layer {name!r} must be an object with a path and description")
        rel, description = entry.get("path"), entry.get("description")
        if not isinstance(rel, str) or not rel:
            raise UsageError(f"extra layer {name!r} declares no path")
        if not isinstance(description, str) or not description:
            raise UsageError(f"extra layer {name!r} declares no description")
        # One test covers both: joining an absolute path to the root yields the
        # absolute path, which then resolves outside it.
        if not _within(root, rel):
            raise UsageError(
                f"extra layer {name!r} points outside the store: {rel!r}. Declare a path "
                "relative to the store root, inside it."
            )
        described = {"path": rel, "description": description, "present": (root / rel).exists()}
        if "kind" in entry:
            if entry["kind"] not in EXTRA_KINDS:
                raise UsageError(
                    f"extra layer {name!r} declares kind {entry['kind']!r}; the only kind is "
                    + ", ".join(repr(kind) for kind in EXTRA_KINDS)
                )
            described["kind"] = entry["kind"]
        result[name] = described
    return result


def _timestamp(mtime: float | None) -> str | None:
    if mtime is None:
        return None
    moment = datetime.datetime.fromtimestamp(int(mtime), tz=datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def compute(root: Path) -> dict:
    """The full manifest for the store at `root`."""
    store_id, github_org = store_identity(root)
    layers, newest = _layers(root)
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
    }
    return {
        "format": {"major": FORMAT[0], "minor": FORMAT[1]},
        "store": {"id": store_id, "github_org": github_org},
        "library_version": __version__,
        "built": _timestamp(newest),
        "layers": layers,
        "extra_layers": extra_layers(root),
    }
