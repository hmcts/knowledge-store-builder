"""Package-level cross-repository edges - the npm layer.

The merged graph holds no cross-repository call edges, so "which applications
depend on the shared component library?" was an answer the store had to
decline. This stage adds the layer that answers it deterministically: one node
per shared package, edges from the files that import it, every edge citing the
importing file and every package node citing the `package.json` that declares
it. No installs, no feed access, no inference - if the evidence is not in a
committed file, the edge does not exist.

Scope is deliberate: npm packages only. The equivalent Maven layer needs its
own reconnaissance (artifact coordinates live in `pom.xml` hierarchies, and a
naive scan of this estate found none), and symbol-level identity (SCIP) stays
deferred until package-level answers prove insufficient - measured on a real
estate, cross-repository symbol name collisions were template scaffolding and
vendored copies, which are *independent implementations* and already answered
correctly.

Run after `gherkin` and before clustering, so package nodes join communities.
Re-running replaces the layer wholesale (idempotent by reconstruction).
"""

from __future__ import annotations

import json
import re

from . import config, io

FORMAT = "packages"
MANIFEST = "package.json"
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs")

# Terraform module reuse. An infrastructure estate's shared dependencies are
# declared here rather than in a language manifest, so an estate can be built
# almost entirely from shared modules and still report "0 shared packages".
#
# Unlike an npm package, the reference names the providing repository outright,
# so no manifest lookup is needed - the edge is repository to repository.
# Both forms are in use and neither dominates by design: on one estate the
# scp-style `git@` form outnumbered `git::https://` five to one, so handling only
# the documented form would have missed most of the reuse.
TERRAFORM_SUFFIX = ".tf"
_TF_SOURCE = re.compile(
    r"""source\s*=\s*"
        (?:git::)?
        # The scheme is optional in Terraform: its GitHub detector accepts a bare
        # `github.com/org/repo` and rewrites it to HTTPS itself. Requiring one made
        # 5 shared modules invisible on a 361-repository estate (#125). The opening
        # quote anchors this, so `notgithub.com/...` cannot match.
        (?:https://|git@|ssh://git@)?github\.com[/:]
        (?P<org>[\w.-]+)/(?P<repo>[\w.-]+?)
        (?:\.git)?(?:[?/][^"]*)?"
    """,
    re.VERBOSE,
)
MAX_EVIDENCE_FILES = 5  # edges per (consumer, package); totals go in metadata
_IMPORT = "from {q}{name}{q}", "require({q}{name}{q})", "from {q}{name}/"


def _named_packages(repo_dir) -> list[tuple[str, str]]:
    """(package name, repo-relative package.json path) declared by one clone."""
    found = []
    candidates = [repo_dir / MANIFEST]
    for sub in ("projects", "packages", "libs"):
        candidates.extend(sorted((repo_dir / sub).glob(f"*/{MANIFEST}")))
    for manifest in candidates:
        if not manifest.is_file():
            continue
        try:
            name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if name:
            found.append((str(name), str(manifest.relative_to(repo_dir))))
    return found


def _declared_dependencies(repo_dir) -> set[str]:
    manifest = repo_dir / MANIFEST
    if not manifest.is_file():
        return set()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return set()
    return set(data.get("dependencies", {})) | set(data.get("devDependencies", {}))


def _importing_files(repo_dir, package: str) -> list[str]:
    """Repo-relative source files that import the package, sorted."""
    needles = [pattern.format(q=quote, name=package) for pattern in _IMPORT for quote in ("'", '"')]
    hits = []
    for path in repo_dir.rglob("*"):
        if path.suffix not in SOURCE_SUFFIXES or "node_modules" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(needle in text for needle in needles):
            hits.append(str(path.relative_to(repo_dir)))
    return sorted(hits)


def _representatives(graph: dict) -> dict[tuple[str, str], str]:
    """(repo, source_file) -> one deterministic node id: the file's lowest id."""
    best: dict[tuple[str, str], str] = {}
    for node in graph["nodes"]:
        repo, src = node.get("repo"), node.get("source_file")
        if not (repo and src):
            continue
        key = (repo, src)
        if key not in best or node["id"] < best[key]:
            best[key] = node["id"]
    return best


def _anchor_under(graph_reps: dict, repo: str, prefix: str) -> str | None:
    """The provider-side anchor: lowest node id under the package's directory."""
    candidates = [
        node_id
        for (node_repo, src), node_id in graph_reps.items()
        if node_repo == repo and src.startswith(prefix)
    ]
    return min(candidates) if candidates else None


def _strip_layer(graph: dict) -> None:
    """Idempotence by reconstruction: drop everything this stage added before."""
    package_ids = {n["id"] for n in graph["nodes"] if n.get("_origin") == FORMAT}
    graph["nodes"] = [n for n in graph["nodes"] if n.get("_origin") != FORMAT]
    graph["links"] = [
        e
        for e in graph["links"]
        if e["source"] not in package_ids and e["target"] not in package_ids
    ]


def terraform_references(text: str) -> set[str]:
    """Repository names a Terraform file declares as module sources.

    Deliberately only GitHub-hosted sources. A registry or local path reference
    names no repository, so reporting one would be a guess.
    """
    return {m.group("repo") for m in _TF_SOURCE.finditer(text)}


def _module_node(repo_name: str, in_estate: bool) -> dict:
    """A referenced module, identified by the repository that provides it.

    `repo` is set only when that repository is actually in the estate. A module can
    be referenced from outside it - on one estate 3 of 33 were - and naming a
    repository the store has never synced in `repo` would add it to every per-repository
    aggregate: digests would count it, and `deepdive` would offer a dossier on a
    single synthetic node. The reference is kept because a dependency on something
    the estate does not hold is a finding in its own right; the claim to hold it is
    what is dropped.
    """
    return {
        "id": f"{repo_name}::module",
        "label": repo_name,
        "norm_label": repo_name.lower(),
        "file_type": "concept",
        "source_file": None,
        "source_location": None,
        "repo": repo_name if in_estate else "",
        "_origin": FORMAT,
        "local_id": "module",
        "metadata": {
            "kind": "terraform_module",
            "provider_repo": repo_name,
            "provider_in_estate": in_estate,
        },
    }


def _own_terraform_files(clone):
    """This repository's own .tf files.

    .terraform is Terraform's download cache: it holds copies of the upstream
    modules, whose sources would otherwise read as this repository's dependencies.
    Measured at zero files on the estate this was built against, so a guard rather
    than a fix - but a cache committed once would quietly invent reuse.
    """
    for path in clone.rglob(f"*{TERRAFORM_SUFFIX}"):
        if ".terraform" not in path.parts:
            yield path


def _read_text(path) -> str:
    """File contents, or "" if it cannot be read. One unreadable file is not worth
    abandoning an estate scan for."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _module_references(clones: list) -> dict[tuple[str, str], list[str]]:
    """(consumer repo, provider repo) -> the files declaring it, sorted."""
    found: dict[tuple[str, str], set[str]] = {}
    for clone in clones:
        for path in _own_terraform_files(clone):
            for provider in terraform_references(_read_text(path)):
                if provider == clone.name:
                    continue  # a repository referencing itself is not reuse
                found.setdefault((clone.name, provider), set()).add(str(path.relative_to(clone)))
    return {pair: sorted(files) for pair, files in found.items()}


def _add_module_layer(graph: dict, clones: list) -> tuple[int, int, int, int]:
    """Module nodes and consumer edges from Terraform sources.

    Returns (modules, consumer pairs, edges). The evidence cap is per
    (consumer, provider) pair, which is what MAX_EVIDENCE_FILES means everywhere
    else in this module. Capping per provider instead drops whole consuming
    repositories rather than surplus evidence for one: on a 361-repository estate
    that lost 290 of 388 relationships, and alphabetically, so the survivors
    looked like a complete answer.
    """
    # One pass over the representatives, not one per pair: a large graph has
    # hundreds of thousands of them and there are hundreds of pairs.
    anchors: dict[str, str] = {}
    for (node_repo, _), node_id in _representatives(graph).items():
        current = anchors.get(node_repo)
        if current is None or node_id < current:
            anchors[node_repo] = node_id
    estate = {clone.name for clone in clones}
    existing = {n["id"] for n in graph["nodes"]}
    references = _module_references(clones)

    edges = 0
    capped = 0
    for (consumer_repo, provider), files in sorted(references.items()):
        node_id = f"{provider}::module"
        if node_id not in existing:
            graph["nodes"].append(_module_node(provider, provider in estate))
            existing.add(node_id)
        anchor = anchors.get(consumer_repo)
        if not anchor:
            continue
        if len(files) > MAX_EVIDENCE_FILES:
            capped += len(files) - MAX_EVIDENCE_FILES
        for rel in files[:MAX_EVIDENCE_FILES]:
            graph["links"].append(_edge(anchor, node_id, "USES_MODULE", rel))
            edges += 1
    if capped:
        print(
            f"  ({capped} further declaring files not carried as edges, {MAX_EVIDENCE_FILES} per pair)"
        )
    providers = {pair[1] for pair in references}
    return len(providers), len(references), edges, len(providers - estate)


def _package_node(name: str, provider_repo: str, manifest_rel: str) -> dict:
    return {
        "id": f"{provider_repo}::pkg:{name}",
        "label": name,
        "norm_label": name.lower(),
        "file_type": "concept",
        "source_file": manifest_rel,
        "source_location": None,
        "repo": provider_repo,
        "_origin": FORMAT,
        "local_id": f"pkg:{name}",
        "metadata": {"kind": "package"},
    }


def _edge(source: str, target: str, relation: str, rel: str) -> dict:
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": "EXTRACTED",
        "confidence_score": 1.0,
        "source_file": rel,
        "source_location": None,
        "weight": 1.0,
    }


def _discover_providers(clones: list) -> dict[str, tuple[str, str]]:
    """package name -> (provider repo, manifest path); first repo wins a conflict."""
    providers: dict[str, tuple[str, str]] = {}
    for repo_dir in clones:
        for name, manifest_rel in _named_packages(repo_dir):
            if name not in providers or repo_dir.name < providers[name][0]:
                providers[name] = (repo_dir.name, manifest_rel)
    return providers


def _ensure_package_node(
    graph: dict, reps: dict, providers: dict, name: str, package_nodes: dict
) -> tuple[dict, int]:
    """The package's node, creating it with its provided_by edge on first sight."""
    node = package_nodes.get(name)
    if node is not None:
        return node, 0
    provider_repo, manifest_rel = providers[name]
    node = _package_node(name, provider_repo, manifest_rel)
    package_nodes[name] = node
    graph["nodes"].append(node)
    anchor = _anchor_under(reps, provider_repo, manifest_rel.rsplit(MANIFEST, 1)[0])
    if anchor:
        graph["links"].append(_edge(node["id"], anchor, "provided_by", manifest_rel))
        return node, 1
    return node, 0


def _add_package_layer(graph: dict, clones: list, providers: dict) -> tuple[int, int, int]:
    """Mutate the graph in place; return (packages, consumer pairs, edges added)."""
    reps = _representatives(graph)
    package_nodes: dict[str, dict] = {}
    edges = 0
    consumer_pairs = 0
    for repo_dir in clones:
        consumer = repo_dir.name
        shared = {
            name
            for name in _declared_dependencies(repo_dir)
            if name in providers and providers[name][0] != consumer
        }
        for name in sorted(shared):
            node, added = _ensure_package_node(graph, reps, providers, name, package_nodes)
            edges += added
            importing = _importing_files(repo_dir, name)
            node["metadata"].setdefault("consumers", []).append(
                {"repo": consumer, "import_sites": len(importing)}
            )
            consumer_pairs += 1
            for rel in importing[:MAX_EVIDENCE_FILES]:
                source = reps.get((consumer, rel))
                if source:
                    graph["links"].append(_edge(source, node["id"], "imports_package", rel))
                    edges += 1
    return len(package_nodes), consumer_pairs, edges


def main() -> int:
    if not config.GRAPH_PATH.exists():
        print(f"Graph not found: {config.GRAPH_PATH} (gunzip -k graph.json.gz first)")
        return 1
    if not config.REPOSITORIES_DIR.is_dir():
        print(f"No clones under {config.REPOSITORIES_DIR} - run `knowledgestore sync` first")
        return 1

    clones = sorted(d for d in config.REPOSITORIES_DIR.iterdir() if (d / ".git").is_dir())
    providers = _discover_providers(clones)

    graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
    _strip_layer(graph)
    package_count, consumer_pairs, edges = _add_package_layer(graph, clones, providers)
    module_count, module_pairs, module_edges, module_external = _add_module_layer(graph, clones)

    serialised = json.dumps(graph, ensure_ascii=False)
    config.GRAPH_PATH.write_text(serialised, encoding="utf-8")  # NOSONAR(S2083)
    with io.gzip_text(config.GRAPH_PATH.with_suffix(".json.gz")) as out:
        out.write(serialised)

    print(
        f"Packages: {package_count} shared across repositories "
        f"({len(providers)} named in total), {consumer_pairs} consumer relationships, "
        f"{edges} edges added"
    )
    print(
        f"Terraform modules: {module_count} referenced across repositories, "
        f"{module_pairs} consumer relationships, {module_edges} edges added"
    )
    if module_external:
        print(
            f"  ({module_external} of them are not repositories this estate holds - "
            "recorded as references, not as estate members)"
        )
    print(f"Graph now: {len(graph['nodes'])} nodes, {len(graph['links'])} edges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
