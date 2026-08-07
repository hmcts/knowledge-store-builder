"""Deployment evidence: what each service is configured as, in each environment.

A deployment repository holds the configuration that the rest of the estate is
deployed with, and nothing else in a store carries it. The unit here is
deliberately **(service, environment)** rather than service alone: a single node
per service would merge production and development configuration into one blob,
and an answer built on it would be confidently wrong.

Modelled on `build_package_edges`: strip the layer this stage wrote before, walk
the clone, write nodes and edges back, report what joined and what did not.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config, deploy_values, io

FORMAT = "deployments"
_VALUES_SUFFIX = re.compile(r"_values\.ya?ml(\.j2)?$")


def _glob_root(glob: str) -> str:
    """The fixed leading directory of the values glob, e.g. `ansible/group_vars`."""
    parts = []
    for part in Path(glob).parts:
        if any(ch in part for ch in "*?["):
            break
        parts.append(part)
    return "/".join(parts)


def environment_of(rel_path: str, glob_root: str) -> str:
    """The path segment between the glob root and the file names the environment.

    A file directly under the root belongs to no environment: it is the base
    layer every environment starts from, and is named so it can be quoted as
    such rather than mistaken for a real environment.
    """
    rel = rel_path[len(glob_root) :].strip("/") if rel_path.startswith(glob_root) else rel_path
    segments = rel.split("/")
    return config.DEPLOY_BASE_ENV if len(segments) == 1 else segments[0]


def service_of(filename: str) -> str:
    """`progression-service_values.yaml.j2` -> `progression-service`."""
    return _VALUES_SUFFIX.sub("", filename)


def _parse(text: str) -> dict[str, str] | None:
    try:
        import yaml
    except ImportError:  # pragma: no cover - exercised by the stage's preflight
        raise SystemExit(
            "The deployments stage parses YAML, which needs PyYAML.\n"
            "  pip install 'hmcts-knowledge-store-builder[deploy]'"
        ) from None
    try:
        loaded = yaml.safe_load(deploy_values.strip_template(text))
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, dict):
        return None
    return deploy_values.flatten(loaded, config.DEPLOY_MAX_KEYS, config.DEPLOY_VALUE_CHARS)


def discover(repo_dir: Path) -> tuple[dict[tuple[str, str], dict[str, str]], list[str]]:
    """(environment, service) -> flattened configuration, and what would not parse.

    The unparsed list is returned rather than logged away because a file this
    stage cannot read is a service whose configuration is simply missing from
    every answer, with nothing on the page to say so. Stripping the template can
    leave YAML that no longer parses - a `{% for %}` block around a list, most
    often - and silently dropping those would understate the estate by however
    many they are. Measured on one estate: 5 of 718.
    """
    root = _glob_root(config.DEPLOY_VALUES_GLOB)
    found: dict[tuple[str, str], dict[str, str]] = {}
    unparsed: list[str] = []
    for path in sorted(repo_dir.glob(config.DEPLOY_VALUES_GLOB)):
        rel = path.relative_to(repo_dir).as_posix()
        flat = _parse(path.read_text(encoding="utf-8", errors="replace"))
        if flat is None:
            unparsed.append(rel)
            continue
        found[(environment_of(rel, root), service_of(path.name))] = flat
    return found, unparsed


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


# Below this length a stem matches almost any repository name by accident, and the
# accident is invisible: a wrong join counts as a join, so it inflates the match
# rate rather than showing up as a gap. Measured on a realistic repository set, a
# bare substring rule sent `id-service` to `cpp-video` and `sc-service` to
# `cpp-context-scheduling`, both confidently and both wrong.
MIN_SUBSTRING_STEM = 4


def match_services(services: set[str], repos: set[str]) -> dict[str, str]:
    """service name -> the repository that holds it, where one clearly does.

    A deployed service is named for what it is (`progression-service`); the
    repository is named for where it sits (`cpp-context-progression`). The join is
    by name, so it has to be conservative in a specific way: a *missed* join shows
    up in the match-rate report and can be chased, whereas a *wrong* join is
    counted as a success and silently attaches production configuration to
    unrelated code.

    So a whole hyphen-delimited segment of the repository name must equal the
    stem. Only when no repository matches that way does a substring match apply,
    and then only for a stem long enough that the coincidence is implausible.
    Ambiguity resolves to the shortest then alphabetically first name so two runs
    agree; that is determinism, which is necessary but is not correctness, which
    is why the segment rule comes first.
    """
    matched: dict[str, str] = {}
    for service in sorted(services):
        stem = _norm(_VALUES_SUFFIX.sub("", service).removesuffix("-service"))
        if not stem:
            continue
        exact = [r for r in repos if stem in {_norm(part) for part in r.split("-")}]
        loose = [r for r in repos if stem in _norm(r)] if len(stem) >= MIN_SUBSTRING_STEM else []
        candidates = sorted(exact or loose, key=lambda r: (len(r), r))
        if candidates:
            matched[service] = candidates[0]
    return matched


def _strip_layer(graph: dict) -> None:
    """Idempotence by reconstruction: drop everything this stage added before."""
    ours = {n["id"] for n in graph["nodes"] if n.get("_origin") == FORMAT}
    graph["nodes"] = [n for n in graph["nodes"] if n.get("_origin") != FORMAT]
    graph["links"] = [
        e for e in graph["links"] if e["source"] not in ours and e["target"] not in ours
    ]


def _environment_node(environment: str, deploy_repo: str) -> dict:
    return {
        "id": f"deploy::env:{environment}",
        "label": environment,
        "norm_label": environment.lower(),
        "file_type": "concept",
        "source_file": None,
        "source_location": None,
        "repo": deploy_repo,
        "_origin": FORMAT,
        "local_id": f"env:{environment}",
        "metadata": {"kind": "environment"},
    }


def _deployment_node(
    environment: str, service: str, deploy_repo: str, rel: str, flat: dict[str, str]
) -> dict:
    return {
        "id": f"{deploy_repo}::deploy:{environment}:{service}",
        "label": f"{service} ({environment})",
        "norm_label": f"{service} ({environment})".lower(),
        "file_type": "concept",
        "source_file": rel,
        "source_location": None,
        "repo": deploy_repo,
        "_origin": FORMAT,
        "local_id": f"deploy:{environment}:{service}",
        "metadata": {
            "kind": "deployment",
            "service": service,
            "environment": environment,
            "config": flat,
        },
    }


def _edge(source: str, target: str, relation: str, rel: str | None) -> dict:
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


def _source_file(root: str, environment: str, service: str) -> str:
    """The values file a node cites, rebuilt from its (environment, service) key.

    The base layer sits directly under the glob root with no environment segment,
    so a path assembled as `root/<environment>/<service>` names a file that is not
    there. `status` counts an unopenable citation as dangling, and a reader sent to
    a path that does not exist stops trusting the citations that are good.

    The filename comes from the glob rather than a fixed suffix, so an estate that
    points `KSB_DEPLOY_VALUES_GLOB` at a different naming convention still cites
    the file it actually read.
    """
    name = Path(config.DEPLOY_VALUES_GLOB).name.replace("*", service, 1)
    segments = (root, "" if environment == config.DEPLOY_BASE_ENV else environment, name)
    return "/".join(segment for segment in segments if segment)


def _representatives(graph: dict) -> dict[str, str]:
    """repo -> one deterministic node id, the lowest in that repository.

    `min` over the ids, so the answer does not depend on node order. This stage's
    own nodes are excluded as well as stripped beforehand: without that, a second
    run could pick a deployment node as a repository's representative and chain
    deployment nodes to each other.
    """
    best: dict[str, str] = {}
    for node in graph["nodes"]:
        repo = node.get("repo")
        if not repo or node.get("_origin") == FORMAT:
            continue
        if repo not in best or node["id"] < best[repo]:
            best[repo] = node["id"]
    return best


def main() -> int:
    if not config.GRAPH_PATH.exists():
        print(f"Graph not found: {config.GRAPH_PATH} (gunzip -k graph.json.gz first)")
        return 1
    if not config.DEPLOY_REPOS:
        print(
            "Deployments: no repository named, nothing to do.\n"
            "  Set KSB_DEPLOY_REPOS to the clone holding this estate's deployment\n"
            "  configuration; most estates have none and this stage stays off."
        )
        return 0

    graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
    _strip_layer(graph)
    reps = _representatives(graph)
    known_repos = set(reps)

    environments: set[str] = set()
    added_nodes = added_edges = joined = 0
    unmatched: set[str] = set()
    unreadable: list[str] = []

    for deploy_repo in sorted(config.DEPLOY_REPOS):
        repo_dir = config.REPOSITORIES_DIR / deploy_repo
        if not repo_dir.is_dir():
            print(f"  {deploy_repo}: no clone under {config.REPOSITORIES_DIR} - skipped")
            continue
        found, unparsed = discover(repo_dir)
        unreadable.extend(unparsed)
        matched = match_services({service for _, service in found}, known_repos)
        unmatched |= {service for _, service in found} - set(matched)
        root = _glob_root(config.DEPLOY_VALUES_GLOB)
        for (environment, service), flat in sorted(found.items()):
            rel = _source_file(root, environment, service)
            node = _deployment_node(environment, service, deploy_repo, rel, flat)
            graph["nodes"].append(node)
            added_nodes += 1
            if environment not in environments:
                graph["nodes"].append(_environment_node(environment, deploy_repo))
                environments.add(environment)
                added_nodes += 1
            graph["links"].append(
                _edge(node["id"], f"deploy::env:{environment}", "deployed_in", rel)
            )
            added_edges += 1
            target = reps.get(matched.get(service, ""))
            if target:
                graph["links"].append(_edge(node["id"], target, "deploys", rel))
                added_edges += 1
                joined += 1

    serialised = json.dumps(graph, ensure_ascii=False)
    config.GRAPH_PATH.write_text(serialised, encoding="utf-8")  # NOSONAR(S2083)
    with io.gzip_text(config.GRAPH_PATH.with_suffix(".json.gz")) as out:
        out.write(serialised)

    total = added_nodes - len(environments)
    print(
        f"Deployments: {total} service/environment pairs across "
        f"{len(environments)} environments, {added_edges} edges added"
    )
    if total:
        rate = joined / total * 100
        print(f"  joined to a repository in the graph: {joined} of {total} ({rate:.0f}%)")
    else:
        print("  nothing found")
    if unmatched:
        shown = ", ".join(sorted(unmatched)[:10])
        print(f"  {len(unmatched)} service(s) matched no repository: {shown}")
        print("    a falling match rate means a rename broke the join - check before")
        print("    trusting a deployment answer")
    if unreadable:
        # Said out loud rather than counted silently: each of these is a service
        # whose configuration is absent from every answer, and nothing on the page
        # would show the gap.
        shown = ", ".join(unreadable[:5])
        print(f"  {len(unreadable)} values file(s) did not parse and carry no evidence: {shown}")
    print(f"Graph now: {len(graph['nodes'])} nodes, {len(graph['links'])} edges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
