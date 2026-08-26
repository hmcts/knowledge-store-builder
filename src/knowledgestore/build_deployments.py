"""Deployment evidence: what each service is configured as, in each environment.

A deployment repository holds the configuration that the rest of the estate is
deployed with, and nothing else in a store carries it. The unit here is
deliberately **(service, environment)** rather than service alone: a single node
per service would merge production and development configuration into one blob,
and an answer built on it would be confidently wrong.

Two layouts reach the same node. The **values** route reads
`KSB_DEPLOY_VALUES_GLOB`, one templated file per service and environment. The
**kustomize** route reads a Kustomize/Flux tree - a base HelmRelease patched by
environment and stack overlays, with a cluster's Flux Kustomizations saying which
of those directories are reconciled where (#88). Both emit onto the same
`deploy::env:<environment>` nodes, so one set of environments carries whichever
layout declared a service, and each fact records the route that produced it.

Modelled on `build_package_edges`: strip the layer this stage wrote before, walk
the clone, write nodes and edges back, report what joined and what did not.
"""

from __future__ import annotations

import json
import sys
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config, deploy_flux, deploy_values, graph_files, io

FORMAT = "deployments"
_VALUES_SUFFIX = re.compile(r"_values\.ya?ml(\.j2)?$")
_YAML_SUFFIXES = (".yaml", ".yml")

# Which layout a fact was read from, carried on every node. Without it "which
# services reach this environment by which route" is answerable only by guessing
# from the path a fact happens to cite.
ROUTE_VALUES = "values"
ROUTE_KUSTOMIZE = "kustomize"


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


def _yaml():
    """PyYAML, or the install line. One copy, because both routes parse YAML."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - exercised by the stage's preflight
        raise SystemExit(
            "The deployments stage parses YAML, which needs PyYAML.\n"
            "  pip install 'hmcts-knowledge-store-builder[deploy]'"
        ) from None
    return yaml


def _parse(text: str) -> dict[str, str] | None:
    yaml = _yaml()
    try:
        loaded = yaml.safe_load(deploy_values.strip_template(text))
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, dict):
        return None
    return deploy_values.flatten(loaded, config.DEPLOY_MAX_KEYS, config.DEPLOY_VALUE_CHARS)


def _documents(text: str) -> list[object] | None:
    """Every YAML document in one file, or None when the file will not parse.

    `safe_load_all`, not `safe_load`: a cluster's Kustomizations are conventionally
    one file of `---`-separated documents, and reading only the first would take
    the environment binding of every document after it with no sign that anything
    was missed.
    """
    yaml = _yaml()
    try:
        return list(yaml.safe_load_all(deploy_values.strip_template(text)))
    except yaml.YAMLError:
        return None


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


def _readable_yaml(repo_dir: Path):
    """Every YAML file in the clone, hidden directories excluded.

    `.git` holds packed objects rather than configuration, and `.github` holds
    workflow YAML that declares no deployment - reading either would inflate the
    document count the report is judged on without adding a fact.
    """
    for path in sorted(repo_dir.rglob("*")):
        if path.suffix not in _YAML_SUFFIXES or not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(repo_dir).parts):
            continue
        yield path


def discover_flux(repo_dir: Path) -> tuple[deploy_flux.Reading, list[str], int]:
    """The Kustomize/Flux layout in one clone: what it declares, and what would not read.

    Returns the reading, the files that would not parse, and how many YAML files
    were opened. The file count is what makes "nothing found" a statement rather
    than a silence: a run that read 400 files and found no HelmRelease has looked,
    and one that read none has not.
    """
    entries: list[tuple[str, list[object]]] = []
    unparsed: list[str] = []
    files = 0
    for path in _readable_yaml(repo_dir):
        files += 1
        rel = path.relative_to(repo_dir).as_posix()
        documents = _documents(path.read_text(encoding="utf-8", errors="replace"))
        if documents is None:
            unparsed.append(rel)
            continue
        entries.append((rel, documents))
    return deploy_flux.read(entries), unparsed, files


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


# Below this length a stem matches almost any repository name by accident, and the
# accident is invisible: a wrong join counts as a join, so it inflates the match
# rate rather than showing up as a gap. Measured on a realistic repository set, a
# bare substring rule sent `id-service` to a video repository (`id` sits inside
# `video`) and `sc-service` to a scheduling one (`scheduling` starts with `sc`),
# both confidently and both wrong.
MIN_SUBSTRING_STEM = 4

# The leading-run rule keeps a floor, but not for the reason above. Whole segments
# already remove the accident that constant was measured against - a name sitting
# inside another name, `id` within `video` - because a run is either a whole name
# or nothing, so the coincidence has no way to occur and the floor cannot be what
# prevents it.
#
# What remains is a failure segment boundaries cannot see, and it is the reason the
# segment rule above needs no floor while this one does. There, the *whole* service
# stem is what matched, so the entire name took part. Here a leading run matched,
# which leaves the segments that say which component this is unexamined - and at one
# to three characters the claimant is shared vocabulary an estate uses as a
# convention prefix (`id-`, `ui-`, `api-`), so a repository with such a name would
# collect the whole convention. Every one of those joins is silent, because a wrong
# join is counted as a join.
#
# The threshold is the same constant deliberately, so the library holds one answer
# to "too short to be evidence" rather than two that can drift apart with nothing in
# the report to say which rule made a join.
MIN_PREFIX_REPO = MIN_SUBSTRING_STEM

# The third use of the same threshold, this one on a word two names have in
# common. Named separately only so each use site says which quantity it bounds.
_MIN_WORD = MIN_SUBSTRING_STEM


def _bare(service: str) -> str:
    """The service name with its values-file suffix and a trailing `-service` gone.

    One definition, because the matcher and the reachability report both need it
    and two copies would let them disagree about what a service is called.
    """
    return _VALUES_SUFFIX.sub("", service).removesuffix("-service")


def _leading_runs(name: str) -> set[str]:
    """The normalised leading runs of a hyphenated name, the whole name excluded.

    `alpha-agent-worker` offers `alpha` and `alphaagent`. The whole name is left
    out because a component is what this rule is looking for: the service adds at
    least one segment to the repository's name, and the segments it adds are what
    say which component it is. A name matching in full is the segment or substring
    rule's business and is already theirs.
    """
    segments = [_norm(part) for part in name.split("-")]
    return {"".join(segments[:count]) for count in range(1, len(segments))}


def _prefix_claims(name: str, repos: set[str]) -> list[str]:
    """Repositories whose whole name is a leading run of the service's segments.

    One repository often ships several deployed components, and neither rule
    above can see any of them: a repository named `alpha` is not a segment match
    for `alpha-agent`, because `alphaagent` is no segment of `alpha`, and not a
    substring match either, because a stem longer than the name cannot sit inside
    it. So `alpha-agent`, `alpha-backend` and `alpha-frontend` all went unjoined
    while the code they deploy sat in the graph.

    Whole segments rather than a character prefix, which is what stops a namesake
    claiming a service it merely starts. `alpha` starts `alphabet`, and a
    character prefix therefore attached `alphabet-agent` to `alpha` whenever the
    repository named `alphabet` was absent from the graph: a deployment
    relationship that was never declared anywhere, mechanically valid, entirely
    wrong, and invisible, since a wrong join is counted as a join and raises the
    match rate. A missed join is a gap a reader can see, so where the two trade
    off this stage takes the gap.

    Directional, because the runs come from the service: a repository's own name
    is never split into runs, so a service cannot claim a repository whose name
    extends its own.

    Longest run first, so where both `alpha` and `alpha-agent` are runs of
    `alpha-agent-worker` the more specific one claims it. Two names can only tie
    by normalising to the same string (`alpha-core` and `alphacore`), and that tie
    breaks on the name, because `repos` is a set: without it two runs of one build
    attach the same configuration to different nodes, and hash randomisation keeps
    that invisible until someone diffs two graphs.

    **Measured on the estate that reported the gap: no effect.** Not one service
    there is newly joined by this rule, or by the character prefix it was first
    written as, because repository names in that estate carry an organisational
    prefix that service stems never carry - so no repository name is a run of any
    stem, and none will be after the next refresh either. The gap is real and the
    rule cannot fabricate, which is why it ships; what it is not is the reason that
    estate's services go unjoined. `unreachable_services` is.
    """
    runs = _leading_runs(name)
    claiming = [r for r in repos if len(_norm(r)) >= MIN_PREFIX_REPO and _norm(r) in runs]
    return sorted(claiming, key=lambda r: (-len(_norm(r)), r))


def match_services(services: set[str], repos: set[str]) -> dict[str, str]:
    """service name -> the repository that holds it, where one clearly does.

    A deployed service is named for what it is (`progression-service`); the
    repository is named for where it sits (`context-progression`). The join is
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

    Only when neither has anything to say does the leading-run rule in
    `_prefix_claims` apply, which is what joins the several components one
    repository ships. Running it last is what makes it additive: every join the
    two older rules made, they still make, and the same match cannot be moved
    from one repository to another by adding this rule.
    """
    matched: dict[str, str] = {}
    for service in sorted(services):
        bare = _bare(service)
        stem = _norm(bare)
        if not stem:
            continue
        exact = [r for r in repos if stem in {_norm(part) for part in r.split("-")}]
        loose = [r for r in repos if stem in _norm(r)] if len(stem) >= MIN_SUBSTRING_STEM else []
        candidates = sorted(exact or loose, key=lambda r: (len(r), r)) or _prefix_claims(
            bare, repos
        )
        if candidates:
            matched[service] = candidates[0]
    return matched


def _words(name: str) -> set[str]:
    """The name's whole hyphen-delimited words, normalised, long enough to identify.

    Floored on the same constant the matching rules use, so the library holds one
    answer to "too short to be evidence". Sharing `api` or `ops` with a repository
    is shared vocabulary rather than a lead, and counting it would hide a scope
    fact behind a name coincidence - the direction that costs an operator time,
    because it reads as matcher work that can be done.
    """
    return {word for word in (_norm(part) for part in name.split("-")) if len(word) >= _MIN_WORD}


def unreachable_services(services: set[str], repos: set[str]) -> set[str]:
    """Of these unmatched services, the ones no name rule could ever have joined.

    A service that matched nothing is two different facts, and the report stated
    only one of them. Either a repository is in the graph and the join missed it -
    matcher work, chaseable - or the estate holds no repository for that service at
    all, because a deployment repository configures third-party components and
    report jobs whose code is somewhere else entirely. Reported identically, the
    second sends an operator to tune a matcher against services no matcher can
    reach, and it makes the match rate read as a matcher missing half its joins
    when the denominator holds what it cannot touch. Measured on one estate, the
    unmatched services were of the second kind.

    Deliberately looser than every matching rule: one whole word in common is far
    less than any rule needs, so a service counted as reachable is one where some
    repository is at least worth looking at, and a service with nothing in common
    is beyond any rule that works on names. Being looser is what makes the answer
    conservative in the right direction - it under-reports the scope fact rather
    than inventing it.
    """
    known: set[str] = set()
    for repo in repos:
        known |= _words(repo)
    return {service for service in services if not _words(_bare(service)) & known}


def _strip_layer(graph: dict) -> None:
    """Idempotence by reconstruction: drop everything this stage added before."""
    ours = {n["id"] for n in graph["nodes"] if n.get("_origin") == FORMAT}
    graph["nodes"] = [n for n in graph["nodes"] if n.get("_origin") != FORMAT]
    graph["links"] = [
        e for e in graph["links"] if e["source"] not in ours and e["target"] not in ours
    ]


class Communities:
    """One community per environment, allocated above whatever clustering produced.

    Deployment nodes arrive after the graph has been clustered, so nothing else
    would place them: they would carry no community, sit outside the "what these
    areas do" layer, and render with a blank area on the page. `gherkin` has the
    same problem and answers it the same way, by minting ids above the maximum
    and labelling them.

    Environment is the right grain. A community per service would be hundreds of
    areas holding one node each, which says nothing; a single community for
    everything deployed would put production and development in one area, which
    is the confusion this whole stage exists to avoid.
    """

    def __init__(self, graph: dict, labels: dict[str, str]) -> None:
        self.labels = labels
        self.max_community = max((n.get("community") or 0 for n in graph["nodes"]), default=0)
        self.by_environment: dict[str, int] = {}

    def for_environment(self, environment: str) -> int:
        if environment not in self.by_environment:
            self.max_community += 1
            self.by_environment[environment] = self.max_community
            self.labels[str(self.max_community)] = f"Deployments: {environment}"
        return self.by_environment[environment]


def _environment_node(environment: str, deploy_repo: str, community: int, name: str) -> dict:
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
        "community": community,
        "community_name": name,
        "metadata": {"kind": "environment"},
    }


@dataclass(frozen=True)
class _Fact:
    """One (service, environment) pair ready to become a node, whichever route read it.

    Both routes reduce to this, which is what keeps the node shape single: the
    difference between them is the route name, the file cited, and whatever extra
    metadata the layout can state (a Kustomize/Flux fact names its layers and its
    chart; a values fact has neither).
    """

    environment: str
    service: str
    rel: str
    route: str
    config: dict[str, str]
    extra: dict[str, object] = field(default_factory=dict)
    chart_source: str = ""


def _deployment_node(fact: _Fact, deploy_repo: str, community: int, name: str) -> dict:
    return {
        "id": f"{deploy_repo}::deploy:{fact.environment}:{fact.service}",
        "label": f"{fact.service} ({fact.environment})",
        "norm_label": f"{fact.service} ({fact.environment})".lower(),
        "file_type": "concept",
        "source_file": fact.rel,
        "source_location": None,
        "repo": deploy_repo,
        "_origin": FORMAT,
        "local_id": f"deploy:{fact.environment}:{fact.service}",
        "community": community,
        "community_name": name,
        "metadata": {
            "kind": "deployment",
            "service": fact.service,
            "environment": fact.environment,
            "route": fact.route,
            "config": fact.config,
            **fact.extra,
        },
    }


def _chart_node(source: str, in_estate: bool) -> dict:
    """A chart source, identified by the repository providing the chart.

    The shape `build_package_edges` gives a Terraform module (#122): the provider
    names the node, and `repo` is set only when the estate actually holds that
    repository, because naming an unsynced one puts it into every per-repository
    aggregate. The id namespace is `::chart` rather than `::module` deliberately -
    both stages strip their own layer by `_origin`, so a shared id would have each
    stage delete the other's edges, and the loss would surface a stage later in a
    count nobody is comparing.
    """
    return {
        "id": f"{source}::chart",
        "label": source,
        "norm_label": source.lower(),
        "file_type": "concept",
        "source_file": None,
        "source_location": None,
        "repo": source if in_estate else "",
        "_origin": FORMAT,
        "local_id": "chart",
        "metadata": {
            "kind": "chart_source",
            "provider_repo": source,
            "provider_in_estate": in_estate,
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


class _FluxTally:
    """What the Kustomize/Flux route read, and everything it could not attribute.

    Counted separately from the values route rather than added to it: a total over
    two layouts cannot say which one contributed nothing, and #88 exists because
    a layout contributing nothing was indistinguishable from a clean run.
    """

    def __init__(self) -> None:
        self.files = 0
        self.documents = 0
        self.releases = 0
        self.kustomizations = 0
        self.facts = 0
        self.unreadable: list[str] = []
        self.unattached: list[str] = []
        self.unnamed_environments: list[str] = []
        self.unnamed_services: list[str] = []
        self.chart_sources: dict[str, bool] = {}
        self.chart_edges = 0
        self.both_routes: list[str] = []


class _Tally:
    """What one run accumulated across every deployment repository."""

    def __init__(self) -> None:
        self.environments: set[str] = set()
        self.pairs = 0
        self.edges = 0
        self.joined = 0
        self.unmatched: set[str] = set()
        self.out_of_reach: set[str] = set()
        self.unreadable: list[str] = []
        self.flux = _FluxTally()


def _values_facts(found: dict[tuple[str, str], dict[str, str]]) -> dict[tuple[str, str], _Fact]:
    """The layout this stage already read, as facts."""
    root = _glob_root(config.DEPLOY_VALUES_GLOB)
    return {
        (environment, service): _Fact(
            environment,
            service,
            _source_file(root, environment, service),
            ROUTE_VALUES,
            flat,
        )
        for (environment, service), flat in sorted(found.items())
    }


def _flux_facts(reading: deploy_flux.Reading) -> dict[tuple[str, str], _Fact]:
    """A Kustomize/Flux reading as facts, its values flattened by the same rules.

    `deploy_values.flatten` is what applies the secret-location policy, so the
    second layout inherits it rather than restating it - the whole point of
    settling that policy in the library (#88).
    """
    facts: dict[tuple[str, str], _Fact] = {}
    for key, fact in sorted(reading.facts.items()):
        extra: dict[str, object] = {"layers": list(fact.layers)}
        if fact.chart.name or fact.chart.version or fact.chart.source:
            extra["chart"] = fact.chart.name
            extra["chart_version"] = fact.chart.version
            extra["chart_source"] = fact.chart.source
        facts[key] = _Fact(
            fact.environment,
            fact.service,
            # The first layer, which is the base declaration wherever there is one.
            # No single file holds a composed fact, so the citation has to choose:
            # the declaration is the file a reader should open first, and it does
            # not move when a stack overlay is added, whereas "the layer with the
            # final word" would cite an override that sets one key and read as the
            # place this service is defined. Every layer is listed in `layers`.
            fact.layers[0],
            ROUTE_KUSTOMIZE,
            deploy_values.flatten(fact.values, config.DEPLOY_MAX_KEYS, config.DEPLOY_VALUE_CHARS),
            extra,
            fact.chart.source,
        )
    return facts


def _merged_facts(
    found: dict[tuple[str, str], dict[str, str]],
    reading: deploy_flux.Reading,
    tally: _FluxTally,
) -> dict[tuple[str, str], _Fact]:
    """Both routes' facts, keyed as before, with a clash resolved to the values route.

    One clone holding both layouts would otherwise produce two nodes with one id.
    The existing route wins because its output is already committed in consumer
    repositories, and the clash is named rather than resolved quietly - a service
    declared twice in one repository is a migration half-done, which is a finding.
    """
    facts = _values_facts(found)
    for key, fact in _flux_facts(reading).items():
        if key in facts:
            tally.both_routes.append(f"{fact.service} ({fact.environment})")
            continue
        facts[key] = fact
    return facts


def _chart_edges(
    graph: dict, fact: _Fact, node_id: str, reps: dict[str, str], tally: _Tally
) -> None:
    """The chart source a fact declares, as a node and a dependency edge."""
    source = fact.chart_source
    if not source:
        return
    if source not in tally.flux.chart_sources:
        in_estate = source in reps
        tally.flux.chart_sources[source] = in_estate
        graph["nodes"].append(_chart_node(source, in_estate))
    # Cited on the fact's own file, which is the declaration the chart is part of.
    graph["links"].append(_edge(node_id, f"{source}::chart", "uses_chart", fact.rel))
    tally.flux.chart_edges += 1
    tally.edges += 1


def _record_flux(
    reading: deploy_flux.Reading, unparsed: list[str], files: int, tally: _FluxTally
) -> None:
    tally.files += files
    tally.documents += reading.documents
    tally.releases += reading.releases
    tally.kustomizations += reading.kustomizations
    tally.facts += len(reading.facts)
    tally.unreadable.extend(unparsed)
    tally.unattached.extend(reading.unattached)
    tally.unnamed_environments.extend(reading.unnamed_environments)
    tally.unnamed_services.extend(reading.unnamed_services)


def _add_repo_layer(
    graph: dict,
    deploy_repo: str,
    reps: dict[str, str],
    tally: _Tally,
    communities: Communities,
) -> None:
    """Write one deployment clone's nodes and edges into the graph."""
    repo_dir = config.REPOSITORIES_DIR / deploy_repo
    found, unparsed = discover(repo_dir)
    tally.unreadable.extend(unparsed)
    reading, flux_unparsed, files = discover_flux(repo_dir)
    _record_flux(reading, flux_unparsed, files, tally.flux)

    facts = _merged_facts(found, reading, tally.flux)
    # Read off the facts rather than destructured out of their keys: the key's
    # second element and the fact's own `service` are the same string by
    # construction, and the matcher sorts what it is given, so neither the set nor
    # anything downstream of it depends on iteration order.
    services = {fact.service for fact in facts.values()}
    matched = match_services(services, set(reps))
    unmatched = services - set(matched)
    tally.unmatched |= unmatched
    tally.out_of_reach |= unreachable_services(unmatched, set(reps))

    for _, fact in sorted(facts.items()):
        environment = fact.environment
        cid = communities.for_environment(environment)
        name = communities.labels[str(cid)]
        node = _deployment_node(fact, deploy_repo, cid, name)
        graph["nodes"].append(node)
        tally.pairs += 1
        if environment not in tally.environments:
            graph["nodes"].append(_environment_node(environment, deploy_repo, cid, name))
            tally.environments.add(environment)
        graph["links"].append(
            _edge(node["id"], f"deploy::env:{environment}", "deployed_in", fact.rel)
        )
        tally.edges += 1
        target = reps.get(matched.get(fact.service, ""))
        if target:
            graph["links"].append(_edge(node["id"], target, "deploys", fact.rel))
            tally.edges += 1
            tally.joined += 1
        _chart_edges(graph, fact, node["id"], reps, tally)


def _named(paths: list[str], limit: int = 5) -> str:
    """The first few of a list of files, named rather than counted."""
    return ", ".join(sorted(set(paths))[:limit])


def _flux_report(flux: _FluxTally) -> None:
    """What the Kustomize/Flux route read, and everything it could not attribute.

    Printed on every configured run, including the run that found nothing: the
    defect #88 reports is a repository contributing no nodes and no message, so
    "nothing here" has to be a sentence the stage says out loud.
    """
    if flux.releases or flux.kustomizations:
        # Files opened and documents parsed are two quantities, and the difference
        # between them is the unparsable files named below. One figure covering both
        # would report a file that yielded nothing as a file that was read.
        print(
            f"  Kustomize/Flux: {flux.files} YAML file(s) opened, {flux.documents} "
            f"document(s) parsed - {flux.releases} HelmRelease, "
            f"{flux.kustomizations} Kustomization, {flux.facts} service/environment fact(s)"
        )
    else:
        print(
            f"  Kustomize/Flux: no HelmRelease or Kustomization document in "
            f"{flux.files} YAML file(s) - this layout contributed nothing"
        )
    if flux.chart_sources:
        outside = sum(1 for in_estate in flux.chart_sources.values() if not in_estate)
        print(
            f"    chart sources: {len(flux.chart_sources)} referenced, "
            f"{flux.chart_edges} edge(s) added, {outside} not held by this estate"
        )
    if flux.unreadable:
        print(
            f"    {len(set(flux.unreadable))} YAML file(s) in the Kustomize/Flux walk did not "
            f"parse and carry no evidence: {_named(flux.unreadable)}"
        )
    if flux.unattached:
        print(
            f"    {len(set(flux.unattached))} HelmRelease file(s) carry no environment because "
            f"no Kustomization reconciles them: {_named(flux.unattached)}"
        )
    if flux.unnamed_environments:
        print(
            f"    {len(set(flux.unnamed_environments))} Kustomization file(s) name no "
            f"environment segment, so what they reconcile is unattributed: "
            f"{_named(flux.unnamed_environments)}"
        )
    if flux.unnamed_services:
        print(
            f"    {len(set(flux.unnamed_services))} HelmRelease document(s) name no service: "
            f"{_named(flux.unnamed_services)}"
        )
    if flux.both_routes:
        print(
            f"    {len(set(flux.both_routes))} fact(s) are declared by both layouts; the "
            f"values-layout fact is kept: {_named(flux.both_routes)}"
        )


def _report(tally: _Tally, graph: dict) -> None:
    """Everything the run needs to say, including what it could not do."""
    print(
        f"Deployments: {tally.pairs} service/environment pairs across "
        f"{len(tally.environments)} environments, {tally.edges} edges added"
    )
    if tally.pairs:
        rate = tally.joined / tally.pairs * 100
        print(
            f"  joined to a repository in the graph: {tally.joined} of {tally.pairs} ({rate:.0f}%)"
        )
    else:
        print("  nothing found")
    if tally.unmatched:
        shown = ", ".join(sorted(tally.unmatched)[:10])
        print(f"  {len(tally.unmatched)} service(s) matched no repository: {shown}")
        print("    a falling match rate means a rename broke the join - check before")
        print("    trusting a deployment answer")
    if tally.out_of_reach:
        # Said separately because it is a different instruction. These services
        # have no repository in the graph sharing even one word with them, so no
        # name rule can join them and matcher work on them is time spent for
        # nothing: the deployment repository configures more than this store holds.
        shown = ", ".join(sorted(tally.out_of_reach)[:10])
        print(
            f"  {len(tally.out_of_reach)} of those share no name with any repository "
            f"in the graph: {shown}"
        )
        print("    no name rule can reach these - they are a scope fact, not a matcher")
        print("    gap, and what closes them is a repository rather than a better rule")
    if tally.unreadable:
        # Said out loud rather than counted silently: each of these is a service
        # whose configuration is absent from every answer, and nothing on the page
        # would show the gap.
        shown = ", ".join(tally.unreadable[:5])
        print(
            f"  {len(tally.unreadable)} values file(s) did not parse and carry no evidence: {shown}"
        )
    _flux_report(tally.flux)
    print(f"Graph now: {len(graph['nodes'])} nodes, {len(graph['links'])} edges")


def main() -> int:
    if not config.GRAPH_PATH.exists():
        print(f"Graph not found: {config.GRAPH_PATH} (gunzip -k graph.json.gz first)")
        return 1
    refusal = graph_files.stale_refusal(config.GRAPH_PATH)
    if refusal:
        print(refusal, file=sys.stderr)
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

    # Community labels live beside the graph, so the page can name an area. Read
    # what clustering wrote, add one entry per environment, write it back - the
    # same contract `gherkin` follows for its business-feature communities.
    labels: dict[str, str] = (
        json.loads(config.LABELS_PATH.read_text(encoding="utf-8"))
        if config.LABELS_PATH.exists()
        else {}
    )
    communities = Communities(graph, labels)

    tally = _Tally()
    for deploy_repo in sorted(config.DEPLOY_REPOS):
        if not (config.REPOSITORIES_DIR / deploy_repo).is_dir():
            print(f"  {deploy_repo}: no clone under {config.REPOSITORIES_DIR} - skipped")
            continue
        _add_repo_layer(graph, deploy_repo, reps, tally, communities)

    serialised = json.dumps(graph, ensure_ascii=False)
    config.GRAPH_PATH.write_text(serialised, encoding="utf-8")  # NOSONAR(S2083)
    with io.gzip_text(config.GRAPH_PATH.with_suffix(".json.gz")) as out:
        out.write(serialised)
    config.LABELS_PATH.write_text(  # NOSONAR(S2083)
        json.dumps(labels, ensure_ascii=False), encoding="utf-8"
    )

    _report(tally, graph)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
