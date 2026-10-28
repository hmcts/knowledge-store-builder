"""The library's stages over a store root, in the order its docs give.

    python pipeline.py <root> [--no-index] [--skip <stage>]... [--retire <name>]...
                           [--local-only <layer>]...

Runs inside the frozen environment `generate.py` builds from `requirements.txt`,
so `knowledgestore` and `graphify` here are the pinned releases; `generate.py`
writes the inputs and calls this. It follows `docs/creating-a-store.md` ("Build
the store" onwards) and the build skill's clustering procedure at the pinned
tag: each library stage runs as `python -m knowledgestore <stage>`, and graphify
runs from inside each clone, then merges, then the merged graph is re-clustered
through graphify's Python API (never `graphify cluster-only`).

The hand-written things it writes are what an agent authors in a real build:
the community labels file, and the summaries JSON. Each label and summary is
attached to the community holding a named anchor node, the way an author names a
community by what is in it. The summary texts come from `generate.py`, which
writes every other authored input (topic, deep-dive and answers Markdown) too.

Only the IO edges are stood in for, through the library's own seams:
`fetch_tickets.main(opener=, sleep=, today=)` answers each request with the
status and body `tracker_responses.json` gives it, and
`build_minimum_standard_settings.main(runner=, now=)` answers each `gh api` path
from `github_responses.json` and reads a fixed clock. A request either file does
not hold raises, so a stage cannot read an unexpected one as "nothing".

`--no-index` skips the `index` stage. `--skip <stage>` skips every step that
runs that library stage, on any release, the main pin included: an explicit
list, as the partial store asks for, never inferred. A skipped stage must be one
the release has, so a misspelt one refuses rather than skipping nothing, and its
layer (`LAYER_STAGES`) must then be absent. `--retire <name>` first syncs and
exports the history of a repository `config/repositories.txt` does not name, then
drops it from the list and deletes its clone, as an operator retires one; the
history directory stays, because `export-history` never prunes. `--local-only
<layer>` expects that layer on disk but kept off the store's git (the manifest's
`local_only`), as a `.gitignore` the caller wrote makes it.

A step that runs a stage the installed
release does not have - an older frozen release, as `generate.py --release`
builds - is skipped too: which stages a release has is read from its own
`knowledgestore.cli.STAGES`, never assumed. v0.18.3 has no `json-schema` and no
`minimum-standard*`; every other step here exists from v0.18.3 on. On the main
pin, `generate.VERSION`, nothing is skipped: a missing stage there refuses.

One release is given its clock another way: v0.19.0's `minimum-standard-settings`
has no `now=` seam, so for that release only the stage module's `datetime` is
rebound to a fixed clock for the one call (`REBOUND_CLOCK`).

Exits 0 only when every layer in `EXPECTED` (and `EXPECTED_EXTRA`) is present
in the manifest - `index` absent under `--no-index`, and a layer whose stage the
release lacks (`LAYER_STAGES`) not required - and every node in the committed
graph carries a community; otherwise names what is wrong and exits 1, so
`generate.py` commits nothing.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# The main store's pin. This file runs as a script from its own directory, so
# `generate` is its sibling module (standard library only).
from generate import VERSION as MAIN_VERSION

ORG = "example-org"
STORE_ID = "library-contract-store"

HERE = Path(__file__).resolve().parent
TRACKER_RESPONSES = HERE / "tracker_responses.json"
GITHUB_RESPONSES = HERE / "github_responses.json"

# Layers the manifest must mark present, in the `layers` table: every one
# v0.19.1 names, its 18 `LAYERS` and `deployments`. Kept in step with
# `LIBRARY_LAYERS` in tests/library_store/harness.py, which the generation and
# graph tests assert over the committed stores. `index` is absent, and must be, under
# `--no-index`.
EXPECTED = (
    "graph",
    "labels",
    "provenance",
    "repository_manifest",
    "context",
    "content_files",
    "history",
    "intent",
    "ticket_descriptions",
    "ticket_tracker",
    "summaries",
    "topics",
    "deep_dives",
    "semantic",
    "explorer",
    "relationships",
    "minimum_standard",
    "index",
    "deployments",
)
# The store's own layers, from `config/store-extra-layers.json`.
EXPECTED_EXTRA = ("answers", "ownership_notes")
INDEX = "index"
# The layers a release can only have when it has, and runs, the stage that
# writes them. `deployments` is a flag in the graph rather than a file: with its
# stage not run, nothing records `deployments.pairs`, so the manifest says `null`.
LAYER_STAGES = {
    "minimum_standard": "minimum-standard",
    "topics": "topics",
    "deep_dives": "deepdive",
    "deployments": "deployments",
}

# Hand-written community labels: the community holding each anchor node gets
# the label; every other community is "Other <n>". Node ids are graphify's
# scheme (path segments and symbol, lower-cased) behind `merge-graphs`' repository
# prefix, and the json-schema stage's `jsonschema:<file>#<pointer>`. The third
# field is the stage the anchor node needs, or None: a release without
# `json-schema` has no schema node to label.
ANCHORS = (
    ("alpha-lib::src_refund_refund", "Refunds", None),
    ("beta-app::src_pay_paymentservice", "Payments", None),
    ("alpha-lib::jsonschema:schemas/refund.schema.json#", "Refund schema", "json-schema"),
)

# Clones with no code for graphify to extract: deployment values only.
UNEXTRACTED = ("delta-deploy",)

GRAPH = Path("graphify-out/graph.json")
LABELS = Path("graphify-out/.graphify_labels.json")
MANIFEST = Path("knowledge/store-manifest.json")

# The tracker's answers, as `fetch-tickets` asks after `intent`: the project
# allowlisted, description and comments requested, and the day fixed so an open
# ticket's `checked` date is the same every run. The token is a placeholder.
# Four keys a request, sorted, so DEMO-1..DEMO-4 share one and DEMO-5..DEMO-7 the
# other, which the tracker refuses: the stage records a 401 or 403 against every
# key of the request it answered (`fetch_tickets.apply_page`).
TRACKER_SETTINGS = {
    "KSB_TRACKER_BASE_URL": "https://tracker.example.invalid",
    "KSB_TRACKER_TOKEN": "fixture-not-a-secret",
    "KSB_TRACKER_PROJECTS": "DEMO",
    "KSB_TRACKER_FETCH_DESCRIPTION": "1",
    "KSB_TRACKER_FETCH_COMMENTS": "1",
    "KSB_TRACKER_PAGE_SIZE": "4",
}
TRACKER_TODAY = "2026-10-20"

# The stage itself, called in a process of its own because `knowledgestore.config`
# reads the settings at import. The opener answers a URL with the status and body
# the file gives it, or raises; not an `OSError`, which the stage would record as a
# failed page and carry on.
FETCH_TICKETS = """\
import json, sys
from knowledgestore import fetch_tickets

responses = json.loads(open(sys.argv[1], encoding="utf-8").read())


def opener(url, headers):
    if url not in responses:
        raise LookupError("no canned tracker response for " + url)
    answer = responses[url]
    return fetch_tickets.Response(answer["status"], json.dumps(answer["body"]))


sys.exit(fetch_tickets.main([], opener=opener, sleep=lambda seconds: None, today=sys.argv[2]))
"""

# `minimum-standard-settings` with `gh api` answered from the file: 200 with a
# body, 204 with none, 404 as `gh` reports it (exit 1, GitHub's error JSON on
# stdout). Any other call raises.
#
# The clock: the stage stamps `read_at` from its `now=` seam, and the stamp is
# copied into every minimum-standard file and the manifest's digests, so the
# seam is given a fixed instant and every run writes the same bytes.
#
# v0.19.0, a frozen release, has no `now=` seam: it stamps with the module's
# `datetime.now()`. For that release only (`REBOUND_CLOCK`), and for this one
# call, the module's `datetime` is rebound to one whose `now()` is fixed, and
# restored afterwards. Any other release without the seam refuses, so the
# rebinding cannot spread to one nobody chose.
REBOUND_CLOCK = ("0.19.0",)
MINIMUM_STANDARD_SETTINGS = """\
import inspect, json, subprocess, sys
from datetime import datetime, timezone
from knowledgestore import __version__, build_minimum_standard_settings

READ_AT = datetime(2026, 10, 20, tzinfo=timezone.utc)
REBOUND_CLOCK = json.loads(sys.argv[2])


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return READ_AT.astimezone(tz) if tz is not None else READ_AT


responses = json.loads(open(sys.argv[1], encoding="utf-8").read())


def runner(arguments):
    if len(arguments) != 2 or arguments[0] != "api" or arguments[1] not in responses:
        raise LookupError("no canned gh response for " + repr(arguments))
    answer = responses[arguments[1]]
    if answer["status"] == 404:
        error = {"message": "Not Found", "status": "404"}
        raise subprocess.CalledProcessError(1, ["gh", *arguments], output=json.dumps(error))
    return json.dumps(answer["body"]) if answer["status"] == 200 else ""


main = build_minimum_standard_settings.main
seam = "now" in inspect.signature(main).parameters
if seam == (__version__ in REBOUND_CLOCK):
    raise SystemExit(f"{__version__}: now= seam {seam}, rebound clock releases {REBOUND_CLOCK}")
if seam:
    sys.exit(main(runner=runner, now=lambda: READ_AT))
original = build_minimum_standard_settings.datetime
build_minimum_standard_settings.datetime = FixedClock
try:
    status = main(runner=runner)
finally:
    build_minimum_standard_settings.datetime = original
sys.exit(status)
"""

# The agent-authored summaries `generate.py` writes, keyed by anchor node id;
# this step re-keys them by the community holding each anchor.
AUTHORED = "authored"
SUMMARIES_BY_ANCHOR = "summaries-by-anchor.json"
SUMMARIES_OUT = "summaries-out.json"

# The store's own repository, which `answers-layer` reads dates from: one commit,
# of the one answer that has no `date:`, at a fixed identity and date.
ANSWERS_DIR = "docs/answers"
COMMITTED_ANSWER = "docs/answers/who-approves-refunds.md"
ANSWER_COMMITTED_AT = "2026-10-19T09:00:00+00:00"
STORE_IDENTITY = {
    "GIT_AUTHOR_NAME": "Fixture Author",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture Author",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": ANSWER_COMMITTED_AT,
    "GIT_COMMITTER_DATE": ANSWER_COMMITTED_AT,
}


class Refused(Exception):
    """The pipeline cannot vouch for what it built."""


def _home(root: Path) -> dict[str, str]:
    """A home and XDG directories inside the build's temp dir, so nothing under
    the user's own (graphify reads `~/.claude` for its installed skill) reaches
    the build."""
    home = root.parent / "home"
    dirs = {
        "HOME": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_CACHE_HOME": home / ".cache",
        "XDG_DATA_HOME": home / ".local/share",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return {name: str(path) for name, path in dirs.items()}


def _environment(root: Path) -> dict[str, str]:
    """The stages' environment: the caller's, less every `KSB_*` setting it
    carries, plus this store's own. An operator's shell can export real
    settings - a tracker URL and token - and they would otherwise reach every
    stage: one guard run sent `fetch-tickets` at a real tracker, stopped only by
    the stub refusing the URL. fastembed's model cache goes under the isolated
    home too; its default is the system temp dir, shared across builds."""
    inherited = {name: value for name, value in os.environ.items() if not name.startswith("KSB_")}
    return {
        # How fastembed's model is fetched, never what it is: the same files land
        # either way, so these never reach the stored bytes. hf-xet has stalled a
        # download outright, and the default 10-second read timeout fails a slow
        # one. Defaults only: a value the caller exports wins.
        "HF_HUB_DISABLE_XET": "1",
        "HF_HUB_DOWNLOAD_TIMEOUT": "60",
        **inherited,
        **_home(root),
        "FASTEMBED_CACHE_PATH": str(root.parent / "home" / ".cache" / "fastembed"),
        "KSB_ROOT": str(root),
        "KSB_GITHUB_ORG": ORG,
        "KSB_STORE_ID": STORE_ID,
        "KSB_MIN_COMMUNITY_SIZE": "1",
        "KSB_DEPLOY_REPOS": "delta-deploy",
    }


def _run(
    root: Path, command: list[str], cwd: Path | None = None, extra: dict[str, str] | None = None
) -> None:
    print(f"$ {' '.join(command)}", flush=True)
    env = {**_environment(root), **(extra or {})}
    subprocess.run(command, cwd=cwd or root, env=env, check=True)


# The library stage each step runs, for a step not made by `stage`; a step in
# neither runs graphify or this file's own code, never a library stage.
NEEDS: dict[Callable[[Path], None], str] = {}


def needs(name: str) -> Callable[[Callable[[Path], None]], Callable[[Path], None]]:
    """Mark a step as running the library stage `name`."""

    def mark(step: Callable[[Path], None]) -> Callable[[Path], None]:
        NEEDS[step] = name
        return step

    return mark


def stage(*arguments: str) -> Callable[[Path], None]:
    """A library stage, as `knowledgestore <arguments>` at the store root."""

    def run(root: Path) -> None:
        _run(root, [sys.executable, "-m", "knowledgestore", *arguments])

    run.__name__ = arguments[0]
    NEEDS[run] = arguments[0]
    return run


def release_stages(root: Path) -> tuple[str, frozenset[str]]:
    """The installed release's version and the stages it has: its own
    `cli.STAGES`, read in a process with the stages' environment, since
    `knowledgestore.config` reads its settings at import."""
    script = (
        "import json; from knowledgestore import __version__; "
        "from knowledgestore.cli import STAGES; "
        "print(json.dumps([__version__, sorted(STAGES)]))"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=_environment(root),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    version, stages = json.loads(done.stdout)
    return version, frozenset(stages)


def _clones(root: Path) -> list[str]:
    lines = (root / "config/repositories.txt").read_text("utf-8").splitlines()
    return [line.split("|", 1)[0] for line in lines if line and not line.startswith("#")]


def extract(root: Path) -> None:
    """`graphify update .` from inside each clone, never from the store root.

    A clone holding no code gets no graph: graphify exits 1 with "No code files
    found", and the documented shell loop carries on, leaving `merge-inputs` to
    name the repository as declared but not extracted. That is expected of the
    clones in `UNEXTRACTED` and of no other; either surprise refuses."""
    for name in _clones(root):
        clone = root / "repositories" / name
        command = [sys.executable, "-m", "graphify", "update", "."]
        print(f"$ ({name}) {' '.join(command)}", flush=True)
        done = subprocess.run(command, cwd=clone, env=_environment(root))
        extracted = done.returncode == 0 and (clone / GRAPH).is_file()
        if extracted == (name in UNEXTRACTED):
            raise Refused(
                f"{name}: graphify exit {done.returncode}, graph "
                f"{'written' if (clone / GRAPH).is_file() else 'absent'}; "
                f"expected {'none' if name in UNEXTRACTED else 'a graph'}"
            )


def merge_graphs(root: Path) -> None:
    graphs = sorted(
        str(path.relative_to(root)) for path in root.glob("repositories/*/" + str(GRAPH))
    )
    _run(root, [sys.executable, "-m", "graphify", "merge-graphs", *graphs, "--out", str(GRAPH)])


def detect(root: Path) -> None:
    """The detect result, written as the docs write it: graphify's own scan at
    the store root, with `gitignore=False`."""
    (root / "graphify-out").mkdir(exist_ok=True)
    script = (
        "import json; from pathlib import Path; from graphify.detect import detect; "
        "print(json.dumps(detect(Path('.'), gitignore=False)))"
    )
    with (root / "graphify-out/.graphify_detect.json").open("w", encoding="utf-8") as out:
        subprocess.run(
            [sys.executable, "-c", script], cwd=root, env=_environment(root), stdout=out, check=True
        )


@needs("fetch-tickets")
def fetch_tickets(root: Path) -> None:
    """The real `fetch-tickets`, through its `opener=` seam."""
    script = [sys.executable, "-c", FETCH_TICKETS, str(TRACKER_RESPONSES), TRACKER_TODAY]
    _run(root, script, extra=TRACKER_SETTINGS)


@needs("minimum-standard-settings")
def minimum_standard_settings(root: Path) -> None:
    """The real `minimum-standard-settings`, through its `runner` and `now` seams
    (v0.19.0: its `runner` seam and the rebound clock)."""
    rebound = json.dumps(REBOUND_CLOCK)
    _run(root, [sys.executable, "-c", MINIMUM_STANDARD_SETTINGS, str(GITHUB_RESPONSES), rebound])


@needs("json-schema")
def json_schema(root: Path) -> None:
    unresolved = root.parent / "unresolved.json"
    _run(
        root,
        [sys.executable, "-m", "knowledgestore", "json-schema", "--unresolved", str(unresolved)],
    )


def cluster(root: Path) -> None:
    """Re-cluster the merged graph through graphify's API, as the build skill's
    "Clustering" section drives it, writing `community` and `community_name`
    onto every node. There is no previous clustering, so nothing to remap.
    The links are written back as read, as graphify's own re-cluster does, so
    a second edge between two nodes is not collapsed."""
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise Refused("PYTHONHASHSEED must be 0 before clustering (the build skill pins it)")
    # graphify is installed in the frozen environment only, never in this
    # repository's own, which is where the type checker runs.
    from graphify.cluster import cluster as partition  # pyright: ignore[reportMissingImports]
    from graphify.cluster import label_communities_by_hub  # pyright: ignore[reportMissingImports]
    from graphify.export import to_json  # pyright: ignore[reportMissingImports]
    from graphify.paths import load_node_link_graph  # pyright: ignore[reportMissingImports]

    path = root / GRAPH
    raw = json.loads(path.read_text("utf-8"))
    graph = load_node_link_graph(raw)
    communities = partition(graph)
    labels = label_communities_by_hub(graph, communities)
    written = to_json(
        graph,
        communities,
        str(path),
        community_labels=labels,
        built_at_commit=raw.get("built_at_commit"),
        original_links=raw.get("links") or raw.get("edges") or [],
    )
    if not written:
        raise Refused("graphify refused to write the clustered graph")


def compress(root: Path) -> None:
    """`gzip -9 -n`, as the docs recompress after clustering."""
    with (root / GRAPH).with_suffix(".json.gz").open("wb") as out:
        subprocess.run(["gzip", "-9", "-n", "-c", str(root / GRAPH)], stdout=out, check=True)


def _clustered(nodes: list[dict]) -> list[dict]:
    """`nodes`, once every one is known to carry an int community."""
    unclustered = [node["id"] for node in nodes if not isinstance(node.get("community"), int)]
    if not nodes or unclustered:
        raise Refused(f"{len(unclustered)} of {len(nodes)} nodes have no community")
    return nodes


def _community(nodes: list[dict], node_id: str) -> int:
    found = [node["community"] for node in nodes if node["id"] == node_id]
    if len(found) != 1:
        raise Refused(f"anchor {node_id!r} matched {len(found)} nodes")
    return found[0]


def labels(root: Path, stages: frozenset[str]) -> None:
    """The authored community labels, keyed by the community holding each anchor
    whose stage the release has. `main` passes the release's stages."""
    nodes = _clustered(json.loads((root / GRAPH).read_text("utf-8"))["nodes"])
    named: dict[int, str] = {}
    for node_id, text, needed in ANCHORS:
        if needed is not None and needed not in stages:
            continue
        community = _community(nodes, node_id)
        if community in named:
            raise Refused(f"anchors {named[community]!r} and {text!r} share a community")
        named[community] = text
    every = sorted({node["community"] for node in nodes})
    body = {str(c): named.get(c, f"Other {c}") for c in every}
    (root / LABELS).write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")


@needs("summaries")
def summaries_merge(root: Path) -> None:
    """The authored summaries, keyed by the community holding each anchor, merged
    by the library's `summaries merge` as an authoring agent's `out` file is."""
    authored = root.parent / AUTHORED
    by_anchor = json.loads((authored / SUMMARIES_BY_ANCHOR).read_text("utf-8"))
    nodes = _clustered(json.loads((root / GRAPH).read_text("utf-8"))["nodes"])
    keyed: dict[str, str] = {}
    for node_id, text in by_anchor.items():
        community = str(_community(nodes, node_id))
        if community in keyed:
            raise Refused(f"two authored summaries for the community holding {node_id!r}")
        keyed[community] = text
    out = authored / SUMMARIES_OUT
    out.write_text(json.dumps(keyed, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    _run(root, [sys.executable, "-m", "knowledgestore", "summaries", "merge", str(out)])


@needs("answers-layer")
def answers_layer(root: Path) -> None:
    """The store's own repository, then `answers-layer`, which dates each answer by
    its `date:` or else its last commit. Only the answer with no `date:` is
    committed, so the other is dated by its front matter and has no commit."""
    git = {**STORE_IDENTITY, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    _run(root, ["git", "init", "-q", "-b", "main"], extra=git)
    _run(root, ["git", "add", "--", COMMITTED_ANSWER], extra=git)
    _run(root, ["git", "commit", "-q", "-m", "Who approves refunds"], extra=git)
    _run(
        root,
        [
            sys.executable,
            "-m",
            "knowledgestore",
            "answers-layer",
            "--markdown",
            ANSWERS_DIR,
            "--out",
            "knowledge/answers.json",
        ],
    )


def verify(
    root: Path,
    stages: frozenset[str],
    index: bool = True,
    skipped: frozenset[str] = frozenset(),
    local_only: frozenset[str] = frozenset(),
) -> None:
    """Refuse a store missing an expected layer or a community on any node, or,
    built without its index, one that has an index. A layer whose stage the
    release lacks is not expected; one whose stage was skipped must not be
    present; one asked to be local only must be on disk and not present."""
    manifest = json.loads((root / MANIFEST).read_text("utf-8"))
    layers = manifest.get("layers", {})
    extra = manifest.get("extra_layers", {})
    run = stages - skipped
    unbuilt = [name for name in EXPECTED if LAYER_STAGES.get(name, "") in skipped]
    built = [name for name in unbuilt if layers.get(name, {}).get("present") is True]
    if built:
        raise Refused("layers present although their stage was skipped: " + ", ".join(built))
    wrong = [
        name
        for name in sorted(local_only)
        if layers.get(name, {}).get("present") is not False
        or layers.get(name, {}).get("local_only") is not True
    ]
    if wrong:
        raise Refused("layers not recorded local only: " + ", ".join(wrong))
    wanted = [
        name
        for name in EXPECTED
        if (index or name != INDEX)
        and (name not in LAYER_STAGES or LAYER_STAGES[name] in run)
        and name not in local_only
    ]
    missing = [name for name in wanted if not layers.get(name, {}).get("present")]
    missing += [name for name in EXPECTED_EXTRA if not extra.get(name, {}).get("present")]
    if missing:
        raise Refused("layers not present: " + ", ".join(missing))
    if not index and layers.get(INDEX, {}).get("present") is not False:
        raise Refused("built with --no-index, but the manifest does not record the index absent")
    with gzip.open(root / GRAPH.with_suffix(".json.gz"), "rt", encoding="utf-8") as stream:
        _clustered(json.load(stream)["nodes"])


# The one step `--no-index` skips, held by identity so no other step named
# alike can be skipped with it.
INDEX_STEP = stage("index")

# In order. Later layers go in before `manifest`.
# Every step takes the store root; `labels` alone also takes the release's stages.
STEPS: list[Callable[..., None]] = [
    stage("sync"),
    stage("export-history"),
    stage("context"),
    stage("intent"),
    # After `intent`, which discovers the tickets it asks about.
    fetch_tickets,
    extract,
    stage("merge-inputs", "--strict"),
    merge_graphs,
    detect,
    stage("content-set"),
    stage("gherkin"),
    # `packages` before `json-schema`, the build skill's order.
    stage("packages"),
    json_schema,
    stage("deployments"),
    stage("relationships"),
    # After `relationships` and before `index`, as the docs order them.
    minimum_standard_settings,
    stage("minimum-standard"),
    stage("source-urls"),
    cluster,
    # Prints a MISMATCH note against graph.json.gz: expected, because the
    # documented order re-gzips the clustered graph only after this records it.
    stage("record-clustering"),
    compress,
    labels,
    stage("summaries", "extract"),
    summaries_merge,
    stage("topics", "extract"),
    stage("topics", "merge"),
    stage("deepdive", "extract", "alpha-lib"),
    stage("deepdive", "merge"),
    answers_layer,
    stage("semantic"),
    stage("explorer"),
    INDEX_STEP,
    stage("manifest"),
]


def retire(root: Path, name: str) -> None:
    """Sync `name` and export its history with the library's own stages, then drop
    it from `config/repositories.txt` and delete its clone, as an operator retires
    a repository. The build's own `sync` then records provenance without it, and
    its history directory stays: `export-history` writes, never prunes."""
    listed = root / "config/repositories.txt"
    estate = listed.read_text("utf-8")
    if name in _clones(root):
        raise Refused(f"{name} is in config/repositories.txt, so it cannot be retired")
    listed.write_text(estate + f"{name}|https://github.com/{ORG}/{name}.git|main\n", "utf-8")
    try:
        stage("sync")(root)
        stage("export-history")(root)
    finally:
        listed.write_text(estate, "utf-8")
    shutil.rmtree(root / "repositories" / name)


@dataclass(frozen=True)
class Arguments:
    root: Path
    index: bool
    skipped: frozenset[str]
    retired: tuple[str, ...]
    local_only: frozenset[str]


USAGE = (
    "usage: pipeline.py <root> [--no-index] [--skip <stage>]... [--retire <name>]... "
    "[--local-only <layer>]..."
)


def arguments(argv: list[str]) -> Arguments | None:
    """The command line, or None when it is not `USAGE`'s shape."""
    if not argv or argv[0].startswith("--"):
        return None
    index, valued = True, {"--skip": [], "--retire": [], "--local-only": []}
    rest = argv[1:]
    while rest:
        flag, rest = rest[0], rest[1:]
        if flag == "--no-index" and index:
            index = False
        elif flag in valued and rest and not rest[0].startswith("--"):
            valued[flag].append(rest[0])
            rest = rest[1:]
        else:
            return None
    return Arguments(
        Path(argv[0]).resolve(),
        index,
        frozenset(valued["--skip"]),
        tuple(valued["--retire"]),
        frozenset(valued["--local-only"]),
    )


def main(argv: list[str]) -> int:
    parsed = arguments(argv)
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return 2
    root, index, skipped = parsed.root, parsed.index, parsed.skipped
    try:
        version, stages = release_stages(root)
        unknown = sorted(skipped - stages)
        if unknown:
            raise Refused(f"asked to skip {', '.join(unknown)}, which {version} does not have")
        for name in parsed.retired:
            retire(root, name)
        for step in STEPS:
            if step is INDEX_STEP and not index:
                print("$ (skipped: --no-index) knowledgestore index", flush=True)
                continue
            needed = NEEDS.get(step)
            if needed is not None and needed in skipped:
                print(f"$ (skipped: --skip {needed})", flush=True)
                continue
            if needed is not None and needed not in stages:
                # Skipping is for frozen older releases only: on the main pin, a
                # missing stage is a rename or removal to act on, not to skip.
                if version == MAIN_VERSION:
                    raise Refused(f"{version} is the main pin and has no stage {needed}")
                print(f"$ (skipped: this release has no stage {needed})", flush=True)
                continue
            if step is labels:
                labels(root, stages)
            else:
                step(root)
        verify(root, stages, index, skipped, parsed.local_only)
    except Refused as refused:
        print(f"pipeline refused: {refused}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as failed:
        print(f"pipeline stopped: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
