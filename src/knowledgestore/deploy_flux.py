"""A Kustomize/Flux tree read as deployment declarations (#88).

The second layout the `deployments` stage reads. A base HelmRelease declares a
service's chart, its pinned version and its values; overlays patch those values
per environment and per stack; and a cluster's Flux Kustomizations say which of
those directories are reconciled where. Composed, that is the same
**(service, environment)** fact the stage already models, so this module produces
facts and `build_deployments` turns them into the nodes it already emits.

**What is matched, and on what.** Documents are classified by their own
`kind` and `apiVersion` group, never by the directory they sit in:

- `HelmRelease` in `helm.toolkit.fluxcd.io` - a service's declaration. The
  service is `metadata.name`. One carrying `spec.chart` is a base declaration;
  one without is a patch.
- `Kustomization` in `kustomize.toolkit.fluxcd.io` - a reconciliation. Its
  `spec.path` is a directory reconciled for one environment, named by the
  `clusters/<env>/` the file sits in or the `clusters/<env>/...` it reconciles.
  Flux defaults an absent `spec.path` to the source root, and so does this.
- `kustomization.yaml` (`kustomize.config.k8s.io`) - read only as an index: the
  resources and patch files it lists decide what a reconciled directory reaches.

**One fact is one service in one reconciled overlay path (#542).** The walk from
a reconciled directory follows indexes and nested Flux Kustomizations, and the
HelmReleases it reaches are that fact's layers, with the base. Overlays reached
by another environment, or by another stack or cluster of the same one, are never
merged in: a stack (`.../stacks/<id>`, or `clusters/<env>/<cluster>`) is a fact of
its own with a `stack` identifier. A fact whose layers still carry the overlays of
two environments or two stacks is refused and named in the report rather than
emitted - the guard is independent of the walk that composed it. An environment
never comes from an `apps/<name>` segment, which is a team or a stack; a
Kustomization found only there is reported.

A fixed list of directory globs was the alternative and is the mistake this
repository has been bitten by: it reads as coverage on the estate it was written
from and finds nothing on the next one, silently. A Kustomization file that names
no environment is reported rather than guessed at.

**Deliberately not matched:**

- Inline `patches:` blocks of a `kustomization.yaml`: they are rendering
  instructions, and only the files an index lists are followed.
- Every other Kubernetes kind. A `Deployment`, a `ConfigMap` or an
  `ExternalSecret` names no (service, environment) pair without inventing one.
- `spec.valuesFrom`, `spec.postBuild` and `spec.dependsOn`. Following the first
  two means rendering, which this library never does; the third is ordering
  rather than configuration.

**Nothing here renders and nothing resolves a secret.** Values arrive with both
interpolation dialects already stripped by `deploy_values.strip_template`, and
`deploy_values.withhold_secret_locations` - applied by `flatten` where these
facts become nodes - keeps a variable while withholding the store and entry it
comes from. These files carry a complete map of variable to store entry to
vault, which is exactly the map being withheld.
"""

from __future__ import annotations

import copy
import functools
import posixpath
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field

from . import config
from .deploy_values import PLACEHOLDER

HELM_RELEASE_KIND = "HelmRelease"
HELM_RELEASE_GROUP = "helm.toolkit.fluxcd.io"
KUSTOMIZATION_KIND = "Kustomization"
KUSTOMIZATION_GROUP = "kustomize.toolkit.fluxcd.io"

# The Flux source kinds that can provide a chart. A `sourceRef` of any other kind
# names something that is not a chart source, so emitting a chart dependency from
# it would be a guess presented as evidence.
CHART_SOURCE_KINDS = frozenset({"GitRepository", "HelmRepository", "OCIRepository", "Bucket"})


@functools.cache
def yaml_loader():
    """PyYAML's safe loader, reading a bare `=` as the string it is. Only called
    once `import yaml` has succeeded.

    PyYAML resolves a plain `=` to YAML 1.1's `value` type and the safe loader has
    no constructor for it, so a release whose values hold a policy rule
    (`op: =`) fails to load whole and its chart reference with it. Kubernetes reads
    the same scalar as `"="`. One loader for every reader of Flux YAML - the
    `deployments` graph stage and the relationships layer - so a release one of
    them reads is never a release the other cannot parse.
    """

    import yaml

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("tag:yaml.org,2002:value", yaml.SafeLoader.construct_yaml_str)
    return Loader


CLUSTERS_DIR = "clusters"
ENVIRONMENTS_DIR = "environments"
STACKS_DIR = "stacks"
APPS_DIR = "apps"

# The file names Kustomize reads as a directory's index.
_INDEX_NAMES = frozenset({"kustomization.yaml", "kustomization.yml", "Kustomization"})


@dataclass(frozen=True)
class Chart:
    """A chart reference: what is deployed, at which version, from where."""

    name: str = ""
    version: str = ""
    source: str = ""


@dataclass(frozen=True)
class Fact:
    """One service in one reconciled overlay path, composed from the layers that
    path includes and no others.

    `stack` is `""` unless the path is a stack overlay (`.../stacks/<id>`) or a
    cluster directory below `clusters/<env>/`; it is the difference between two
    facts of one service in one environment.
    """

    service: str
    environment: str
    layers: tuple[str, ...]
    values: dict
    chart: Chart
    stack: str = ""


@dataclass(frozen=True)
class Reading:
    """What one clone's Kustomize/Flux tree declared, and what could not be read.

    The lists are returned rather than logged away for the reason #88 was
    opened: a layout this reader cannot attribute is a service missing from every
    answer, and the run that omitted it looked clean.

    `documents` counts every document the parser returned, an empty one included,
    because it is the walk's own account of what it read - the count of *facts* is
    `facts`, and conflating the two is how a tree that declared nothing reads as
    one that declared something.

    `facts` is keyed `(environment, service, stack)`. `refused` is the
    `(fact label, reason)` of every fact the blending guard withheld (#542), and
    `sibling_environments` the `apps/<name>` directories a Kustomization sat in
    that would once have been read as an environment.
    """

    facts: dict[tuple[str, str, str], Fact] = field(default_factory=dict)
    documents: int = 0
    releases: int = 0
    kustomizations: int = 0
    unattached: tuple[str, ...] = ()
    unnamed_environments: tuple[str, ...] = ()
    unnamed_services: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = ()
    sibling_environments: tuple[str, ...] = ()
    unresolved_paths: tuple[str, ...] = ()


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _group(document: dict) -> str:
    """The API group of a Kubernetes document, `helm.toolkit.fluxcd.io/v2` -> group."""
    return _text(document.get("apiVersion")).split("/")[0]


def is_helm_release(document: object) -> bool:
    """A service's declaration, by its own kind and API group."""
    doc = _mapping(document)
    return doc.get("kind") == HELM_RELEASE_KIND and _group(doc) == HELM_RELEASE_GROUP


def is_kustomization(document: object) -> bool:
    """A Flux reconciliation, not a `kustomization.yaml` index - the groups differ."""
    doc = _mapping(document)
    return doc.get("kind") == KUSTOMIZATION_KIND and _group(doc) == KUSTOMIZATION_GROUP


def service_of(document: object) -> str:
    return _text(_mapping(_mapping(document).get("metadata")).get("name"))


def values_of(document: object) -> dict:
    return _mapping(_mapping(_mapping(document).get("spec")).get("values"))


def chart_of(document: object) -> Chart | None:
    """The chart a HelmRelease declares, or None when it declares none.

    None is the signal that the document is a patch rather than a base, so it is
    distinct from a chart block that names nothing this reader can use.
    """
    spec = _mapping(_mapping(_mapping(document).get("spec")).get("chart"))
    inner = _mapping(spec.get("spec"))
    if not inner:
        return None
    reference = _mapping(inner.get("sourceRef"))
    source = _text(reference.get("name")) if reference.get("kind") in CHART_SOURCE_KINDS else ""
    version = inner.get("version")
    return Chart(_text(inner.get("chart")), "" if version is None else str(version), source)


def reconciled_path(document: object) -> str:
    """The repository-relative directory a Kustomization reconciles.

    `""` is the repository root, which is what Flux itself uses when `spec.path`
    is absent - so an omitted path widens the scope rather than narrowing it to
    nothing, and `.` means the same thing written out.

    Normalised by prefix rather than by stripping the characters `.` and `/` from
    both ends: that reads the same on every path these trees hold and differs on
    `../x`, where it would silently produce `x` and attribute another directory's
    patches to this environment.
    """
    raw = _text(_mapping(_mapping(document).get("spec")).get("path")).strip()
    return "" if raw in {".", "./"} else raw.removeprefix("./").strip("/")


def environment_of(rel: str) -> str:
    """The environment a Flux Kustomization file's own location declares, `""` when
    it declares none.

    Two places state one: `clusters/<env>/...`, and a directory below an
    `environments/` directory. A segment of `apps/<name>/...` is not one - those
    are teams and stacks, and taking them as environments is what filed every
    fact of one estate under a team's name (#542). A file that declares none is
    reported by the caller instead of being given an invented environment.
    """
    parts = rel.split("/")
    if len(parts) > 2 and parts[0] == CLUSTERS_DIR:
        return parts[1]
    if ENVIRONMENTS_DIR in parts[:-2]:
        return parts[parts.index(ENVIRONMENTS_DIR) + 1]
    return ""


def environment_of_path(path: str) -> str:
    """The environment a reconciled directory belongs to: `clusters/<env>[/...]`."""
    parts = path.split("/")
    return parts[1] if parts[0] == CLUSTERS_DIR and len(parts) > 1 else ""


def stack_of(path: str) -> str:
    """The stack a reconciled directory is, `""` when it is not one.

    `.../stacks/<id>[/...]` is a stack overlay, and `clusters/<env>/<cluster>[/...]`
    is a cluster of its environment. Two of them reconciled in one environment are
    two deployments, never one - which is the whole of #542.
    """
    parts = path.split("/")
    if STACKS_DIR in parts[:-1]:
        return parts[parts.index(STACKS_DIR) + 1]
    if parts[0] == CLUSTERS_DIR and len(parts) > 2:
        return parts[2]
    return ""


def overlay_markers(rel: str, environments: Collection[str]) -> set[str]:
    """The environment or stack names a layer's path carries, `aat` or `aat-01`.

    A path segment, or a file's stem, equal to a known environment or to that
    environment and a number. It is how a fact is checked against the overlays it
    claims to describe, independently of the walk that composed it.
    """
    found: set[str] = set()
    for segment in rel.split("/"):
        stem = segment.removesuffix(".yaml").removesuffix(".yml")
        for environment in environments:
            if stem == environment or re.fullmatch(re.escape(environment) + r"-[0-9]+", stem):
                found.add(stem)
    return found


def blending(environment: str, layers: Sequence[str], environments: Collection[str]) -> str:
    """Why a fact built from these layers would blend overlays, `""` when it would not.

    One fact describes one service in one reconciled overlay path. Its layers may
    carry its own environment's name and at most one stack of it; any other
    environment, or a second stack, is a configuration nobody deployed.
    """
    markers: set[str] = set()
    for rel in layers:
        markers |= overlay_markers(rel, environments)
    own = re.compile(re.escape(environment) + r"-[0-9]+")
    foreign = sorted(m for m in markers if m != environment and not own.fullmatch(m))
    if foreign:
        return f"layers carry overlays of other environments than {environment}: " + ", ".join(
            foreign
        )
    stacks = sorted(m for m in markers if m != environment)
    if len(stacks) > 1:
        return f"layers carry more than one stack overlay of {environment}: " + ", ".join(stacks)
    return ""


def _under(rel: str, directory: str) -> bool:
    return not directory or rel.startswith(f"{directory}/")


def _join(directory: str, reference: str) -> str | None:
    """`reference` resolved against `directory`, None when it leaves the repository
    or is not a path in it."""
    if "://" in reference or "\n" in reference:
        return None
    joined = posixpath.normpath(posixpath.join(directory, reference))
    if joined == "." or joined == "":
        return ""
    return None if joined.startswith(("..", "/")) else joined


def _references(index: dict) -> list[str]:
    """The files and directories a `kustomization.yaml` pulls in, in the order
    Kustomize applies them: resources, then patches."""
    found = [r for key in ("resources", "bases") for r in _sequence(index.get(key))]
    for patch in _sequence(index.get("patches")):
        found.append(_text(_mapping(patch).get("path")))
    found.extend(_sequence(index.get("patchesStrategicMerge")))
    return [ref for ref in found if isinstance(ref, str) and ref]


def _sequence(value: object) -> list:
    return value if isinstance(value, list) else []


def _overlay(base: dict, patch: dict) -> None:
    """`patch` over `base`, in place, mappings merged and everything else replaced.

    Not a Kustomize renderer: a list is replaced whole rather than merged by index
    or by a key, because a reader that guessed which it was would report a
    configuration nobody declared. Values are copied in, so composing one
    deployment cannot reach the document another deployment composes from.
    """
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _overlay(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def _composed(layers: Sequence[tuple[str, dict]]) -> tuple[dict, Chart]:
    """The layers' values merged in order, and the last chart any of them declared."""
    values: dict = {}
    chart = Chart()
    for _, document in layers:
        _overlay(values, values_of(document))
        declared = chart_of(document)
        if declared is not None:
            chart = declared
    return values, chart


def _bases(
    releases: Sequence[tuple[str, dict]], environments: Collection[str] = ()
) -> dict[str, tuple[str, dict]]:
    """service -> its base declaration, the lowest-sorting path when there are two.

    A declaration whose path carries an environment's name is an overlay that
    happens to declare its chart, not the base: it is chosen only when nothing else
    declares the service, because making it the base puts one environment's
    configuration under every other environment's fact.
    """
    bases: dict[str, tuple[str, dict]] = {}
    marked: set[str] = set()
    for rel, document in releases:
        service = service_of(document)
        if chart_of(document) is None:
            continue
        carries = bool(overlay_markers(rel, environments))
        if service in bases and (carries, rel) >= (service in marked, bases[service][0]):
            continue
        bases[service] = (rel, document)
        marked.discard(service)
        if carries:
            marked.add(service)
    return bases


def _ordered(
    base: tuple[str, dict] | None, layers: Sequence[tuple[str, dict]]
) -> list[tuple[str, dict]]:
    """The base first, then the patches, each file once.

    The base is included whether or not a cluster reconciles it directly: an
    overlay's own `kustomization.yaml` is what usually pulls it in, and a fact
    built from overrides alone would report a partial configuration as a whole one.
    """
    ordered = [base] if base else []
    seen = {rel for rel, _ in ordered}
    return ordered + [layer for layer in layers if layer[0] not in seen]


def _fact(
    service: str, environment: str, layers: Sequence[tuple[str, dict]], stack: str = ""
) -> Fact:
    values, chart = _composed(layers)
    return Fact(service, environment, tuple(rel for rel, _ in layers), values, chart, stack)


def label(service: str, environment: str, stack: str) -> str:
    """`service (environment)`, or `service (environment, stack)`."""
    return f"{service} ({environment}, {stack})" if stack else f"{service} ({environment})"


def _place_release(
    rel: str, document: object, releases: list[tuple[str, dict]], nameless: list[str]
) -> None:
    """A HelmRelease is a declaration when it names a service, and a report line
    when it does not.

    `metadata.name` is the service, and it is the only thing here that says which
    service a file is about: without it there is no service to compose, and
    inferring one from the path is the invention this reader refuses. So it is
    named in the report rather than dropped.
    """
    if service_of(document):
        releases.append((rel, _mapping(document)))
    else:
        nameless.append(rel)


class _Tree:
    """The parsed files of one clone, addressable by repository-relative path."""

    def __init__(self, entries: Sequence[tuple[str, Sequence[object]]]) -> None:
        self.files: dict[str, list[dict]] = {}
        self.indexes: dict[str, dict] = {}
        for rel, parsed in entries:
            documents = [document for document in parsed if isinstance(document, dict)]
            self.files[rel] = documents
            directory, _, name = rel.rpartition("/")
            if name in _INDEX_NAMES:
                for document in documents:
                    if document.get("kind") == KUSTOMIZATION_KIND and not is_kustomization(
                        document
                    ):
                        self.indexes[directory] = document

    def under(self, directory: str) -> list[str]:
        return sorted(rel for rel in self.files if _under(rel, directory))

    def holds(self, path: str) -> bool:
        return path in self.files or path in self.indexes or bool(self.under(path))


class _Walk:
    """What one environment's reconciliation reaches, from the directories it names.

    A directory with a `kustomization.yaml` contributes what that index lists; one
    without contributes every file below it, as Flux does. A Flux Kustomization met
    on the way is followed to its own `spec.path`, a templated segment read as the
    environment the walk belongs to and only when that directory exists. Nothing
    outside the walk is ever a layer, which is what keeps one deployment from
    borrowing another's overlays.
    """

    def __init__(
        self, tree: _Tree, environment: str, reached: set[str], unresolved: list[str]
    ) -> None:
        self.tree = tree
        self.environment = environment
        self.reached = reached
        self.unresolved = unresolved
        self.seen: set[str] = set()
        self.layers: list[tuple[str, dict]] = []

    def visit(self, target: str) -> None:
        if target in self.seen:
            return
        self.seen.add(target)
        if target in self.tree.files:
            self._read(target, self.tree.files[target])
        elif target in self.tree.indexes:
            for reference in _references(self.tree.indexes[target]):
                resolved = _join(target, reference)
                if resolved is not None:
                    self.visit(resolved)
        else:
            for rel in self.tree.under(target):
                self.visit(rel)

    def _read(self, rel: str, documents: Sequence[dict]) -> None:
        for document in documents:
            if is_helm_release(document) and service_of(document):
                self.layers.append((rel, document))
            elif is_kustomization(document):
                self.reached.add(rel)
                path = reconciled_path(document).replace(PLACEHOLDER, self.environment)
                # A nested Kustomization with no path reconciles the source root, which
                # is every environment's overlays at once: following it is the blend.
                if path and self.tree.holds(path):
                    self.visit(path)
                else:
                    self.unresolved.append(f"{rel}: {path}")

    def by_service(self) -> dict[str, list[tuple[str, dict]]]:
        grouped: dict[str, list[tuple[str, dict]]] = {}
        for layer in self.layers:
            grouped.setdefault(service_of(layer[1]), []).append(layer)
        return grouped


def _classify(
    entries: Sequence[tuple[str, Sequence[object]]],
) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]], Reading]:
    """Split the documents into declarations and reconciliations, counting as it goes.

    Each document is placed by its own kind, and what to do with a document of that
    kind is the placing helper's decision - which is also where the reasoning for it
    lives. A document of any other kind is counted and passed over.
    """
    releases: list[tuple[str, dict]] = []
    reconciliations: list[tuple[str, dict]] = []
    unnamed_services: list[str] = []
    documents = 0
    for rel, parsed in entries:
        for document in parsed:
            documents += 1
            if is_helm_release(document):
                _place_release(rel, document, releases, unnamed_services)
            elif is_kustomization(document):
                reconciliations.append((rel, _mapping(document)))
    counted = Reading(
        documents=documents,
        releases=len(releases),
        kustomizations=len(reconciliations),
        unnamed_services=tuple(sorted(set(unnamed_services))),
    )
    return releases, reconciliations, counted


def _scopes(
    reconciliations: Sequence[tuple[str, dict]],
) -> dict[tuple[str, str], set[str]]:
    """(environment, stack) -> the directories reconciled for it.

    An environment is stated by where the Flux Kustomization lives or by the
    `clusters/<env>` it reconciles, never by an `apps/` segment. The path decides
    the stack, so two stacks of one environment are two scopes.
    """
    scopes: dict[tuple[str, str], set[str]] = {}
    for rel, document in reconciliations:
        raw = reconciled_path(document)
        environment = environment_of(rel) or environment_of_path(raw)
        if environment:
            path = raw.replace(PLACEHOLDER, environment)
            scopes.setdefault((environment, stack_of(path)), set()).add(path)
    return scopes


def _compose(
    scopes: dict[tuple[str, str], dict[str, list[tuple[str, dict]]]],
    bases: dict[str, tuple[str, dict]],
    environments: set[str],
) -> tuple[dict[tuple[str, str, str], Fact], list[tuple[str, str]]]:
    """One fact per (environment, stack, service), each from exactly one scope's
    layers plus its environment's stack-less ones, and the facts the guard refused.

    A service in several stacks of an environment gets a fact for each; a service
    only the environment itself reconciles gets one without a stack.
    """
    facts: dict[tuple[str, str, str], Fact] = {}
    refused: list[tuple[str, str]] = []

    def add(environment: str, stack: str, service: str, layers: list[tuple[str, dict]]) -> None:
        ordered = _ordered(bases.get(service), layers)
        reason = blending(environment, [rel for rel, _ in ordered], environments)
        if reason:
            refused.append((label(service, environment, stack), reason))
        else:
            facts[(environment, service, stack)] = _fact(service, environment, ordered, stack)

    by_environment: dict[str, dict[str, dict[str, list[tuple[str, dict]]]]] = {}
    for (environment, stack), services in scopes.items():
        by_environment.setdefault(environment, {})[stack] = services
    for environment, grouped in sorted(by_environment.items()):
        whole = grouped.get("", {})
        stacks = {stack: services for stack, services in grouped.items() if stack}
        for stack, services in sorted(stacks.items()):
            for service, layers in sorted(services.items()):
                add(environment, stack, service, whole.get(service, []) + layers)
        covered = {service for services in stacks.values() for service in services}
        for service in sorted(set(whole) - covered):
            add(environment, "", service, whole[service])
    return facts, refused


def read(entries: Sequence[tuple[str, Sequence[object]]]) -> Reading:
    """The facts one clone's tree declares, from `(repo-relative path, documents)`.

    Parsing is the caller's, so this stays a function of the documents: the same
    reading is reproducible from a fixture without touching a filesystem, and
    there is one place that decides what a malformed file means.
    """
    ordered_entries = sorted(entries, key=lambda entry: entry[0])
    releases, reconciliations, counted = _classify(ordered_entries)
    tree = _Tree(ordered_entries)
    scopes = _scopes(reconciliations)
    environments = {environment for environment, _ in scopes}
    bases = _bases(releases, environments)
    reached: set[str] = set()
    unresolved: list[str] = []
    layered: set[str] = set()
    scoped: dict[tuple[str, str], dict[str, list[tuple[str, dict]]]] = {}
    for (environment, stack), paths in sorted(scopes.items()):
        walk = _Walk(tree, environment, reached, unresolved)
        for path in sorted(paths):
            walk.visit(path)
        scoped[(environment, stack)] = walk.by_service()
        layered |= {rel for rel, _ in walk.layers}
    facts, refused = _compose(scoped, bases, environments)
    for service, base in sorted(bases.items()):
        facts[(config.DEPLOY_BASE_ENV, service, "")] = _fact(
            service, config.DEPLOY_BASE_ENV, [base]
        )

    attached = {rel for rel, _ in bases.values()} | layered
    unattached = tuple(sorted({rel for rel, _ in releases} - attached))
    siblings: set[str] = set()
    unnamed: set[str] = set()
    for rel, document in reconciliations:
        if rel in reached or environment_of(rel) or environment_of_path(reconciled_path(document)):
            continue
        parts = rel.split("/")
        if parts[0] == APPS_DIR and len(parts) > 2:
            siblings.add(f"{APPS_DIR}/{parts[1]}")
        else:
            unnamed.add(rel)
    return Reading(
        facts=facts,
        documents=counted.documents,
        releases=counted.releases,
        kustomizations=counted.kustomizations,
        unattached=unattached,
        unnamed_environments=tuple(sorted(unnamed)),
        unnamed_services=counted.unnamed_services,
        refused=tuple(sorted(refused)),
        sibling_environments=tuple(sorted(siblings)),
        unresolved_paths=tuple(sorted(set(unresolved))),
    )
