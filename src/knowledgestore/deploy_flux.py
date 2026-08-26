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
- `Kustomization` in `kustomize.toolkit.fluxcd.io` - a reconciliation. It binds
  the directory in `spec.path` to the environment named by the file it is
  declared in, which is the only place these trees state that binding. Flux
  defaults an absent `spec.path` to the source root, and so does this.

A fixed list of directory globs was the alternative and is the mistake this
repository has been bitten by: it reads as coverage on the estate it was written
from and finds nothing on the next one, silently. The only path assumption left
is where an environment gets its *name* - the segment below the root of the tree
holding the cluster files, so `clusters/<env>/<cluster>/apps.yaml` names `<env>`.
A file too shallow to carry that segment is reported rather than guessed at.

**Deliberately not matched:**

- `kustomization.yaml` (`kustomize.config.k8s.io`). It indexes resources and
  patch files; the patches themselves carry `kind: HelmRelease`, so reading the
  index adds no fact, and its `patches:` blocks are rendering instructions.
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
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import config

HELM_RELEASE_KIND = "HelmRelease"
HELM_RELEASE_GROUP = "helm.toolkit.fluxcd.io"
KUSTOMIZATION_KIND = "Kustomization"
KUSTOMIZATION_GROUP = "kustomize.toolkit.fluxcd.io"

# The Flux source kinds that can provide a chart. A `sourceRef` of any other kind
# names something that is not a chart source, so emitting a chart dependency from
# it would be a guess presented as evidence.
CHART_SOURCE_KINDS = frozenset({"GitRepository", "HelmRepository", "OCIRepository", "Bucket"})

# `clusters/<env>/<cluster>/apps.yaml` and `clusters/<env>/apps.yaml` both name
# their environment at index 1. Anything shallower carries no such segment.
_ENVIRONMENT_SEGMENT = 1


@dataclass(frozen=True)
class Chart:
    """A chart reference: what is deployed, at which version, from where."""

    name: str = ""
    version: str = ""
    source: str = ""


@dataclass(frozen=True)
class Fact:
    """One (service, environment) pair, composed from the layers that declare it."""

    service: str
    environment: str
    layers: tuple[str, ...]
    values: dict
    chart: Chart


@dataclass(frozen=True)
class Reading:
    """What one clone's Kustomize/Flux tree declared, and what could not be read.

    The three lists are returned rather than logged away for the reason #88 was
    opened: a layout this reader cannot attribute is a service missing from every
    answer, and the run that omitted it looked clean.

    `documents` counts every document the parser returned, an empty one included,
    because it is the walk's own account of what it read - the count of *facts* is
    `facts`, and conflating the two is how a tree that declared nothing reads as
    one that declared something.
    """

    facts: dict[tuple[str, str], Fact] = field(default_factory=dict)
    documents: int = 0
    releases: int = 0
    kustomizations: int = 0
    unattached: tuple[str, ...] = ()
    unnamed_environments: tuple[str, ...] = ()
    unnamed_services: tuple[str, ...] = ()


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
    """The environment a cluster file belongs to, `""` when its path cannot say.

    `clusters/<env>/<cluster>/apps.yaml` -> `<env>`. The name has to come from
    somewhere, and these trees state it once: in the path of the cluster whose
    Kustomizations reconcile the overlays. A file with no such segment is
    reported by the caller instead of being given an invented environment.
    """
    parts = rel.split("/")
    return parts[_ENVIRONMENT_SEGMENT] if len(parts) > _ENVIRONMENT_SEGMENT + 1 else ""


def _under(rel: str, directory: str) -> bool:
    return not directory or rel.startswith(f"{directory}/")


def _overlay(base: dict, patch: dict) -> None:
    """`patch` over `base`, in place, mappings merged and everything else replaced.

    Not a Kustomize renderer: a list is replaced whole rather than merged by index
    or by a key, because a reader that guessed which it was would report a
    configuration nobody declared. Values are copied in, so composing one
    environment cannot reach the document another environment composes from.
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


def _bases(releases: Sequence[tuple[str, dict]]) -> dict[str, tuple[str, dict]]:
    """service -> its base declaration, the lowest-sorting path when there are two."""
    bases: dict[str, tuple[str, dict]] = {}
    for rel, document in releases:
        service = service_of(document)
        if chart_of(document) is None:
            continue
        if service not in bases or rel < bases[service][0]:
            bases[service] = (rel, document)
    return bases


def _layers_for(
    releases: Sequence[tuple[str, dict]], directories: Sequence[str]
) -> dict[str, list[tuple[str, dict]]]:
    """service -> the declarations one environment reconciles, in path order."""
    layers: dict[str, list[tuple[str, dict]]] = {}
    for rel, document in releases:
        if any(_under(rel, directory) for directory in directories):
            layers.setdefault(service_of(document), []).append((rel, document))
    return layers


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


def _fact(service: str, environment: str, layers: Sequence[tuple[str, dict]]) -> Fact:
    values, chart = _composed(layers)
    return Fact(service, environment, tuple(rel for rel, _ in layers), values, chart)


def _place_release(
    rel: str, document: object, releases: list[tuple[str, dict]], nameless: list[str]
) -> None:
    """A HelmRelease is a declaration when it names a service, and a report line
    when it does not.

    `metadata.name` is the service, and it is the only thing here that says which
    service a file is about: without it there is no (service, environment) pair to
    compose, and inferring one from the path is the invention this reader refuses.
    So it is named in the report rather than dropped.
    """
    if service_of(document):
        releases.append((rel, _mapping(document)))
    else:
        nameless.append(rel)


def _place_reconciliation(
    rel: str, document: object, reconciled: dict[str, set[str]], unattributed: list[str]
) -> None:
    """A Kustomization binds the directory it reconciles to the environment its own
    file names, and is a report line when that file names none.

    The environment comes from the path of the cluster whose Kustomizations
    reconcile the overlays, so a file too shallow to carry that segment leaves
    whatever it reconciles unattributed - reported, never given an invented
    environment.
    """
    environment = environment_of(rel)
    if environment:
        reconciled.setdefault(environment, set()).add(reconciled_path(document))
    else:
        unattributed.append(rel)


def _classify(
    entries: Sequence[tuple[str, Sequence[object]]],
) -> tuple[list[tuple[str, dict]], dict[str, set[str]], Reading]:
    """Split the documents into declarations and reconciliations, counting as it goes.

    Each document is placed by its own kind, and what to do with a document of that
    kind is the placing helper's decision - which is also where the reasoning for it
    lives. A document of any other kind is counted and passed over.
    """
    releases: list[tuple[str, dict]] = []
    reconciled: dict[str, set[str]] = {}
    unnamed_environments: list[str] = []
    unnamed_services: list[str] = []
    documents = 0
    kustomizations = 0
    for rel, parsed in sorted(entries, key=lambda entry: entry[0]):
        for document in parsed:
            documents += 1
            if is_helm_release(document):
                _place_release(rel, document, releases, unnamed_services)
            elif is_kustomization(document):
                kustomizations += 1
                _place_reconciliation(rel, document, reconciled, unnamed_environments)
    counted = Reading(
        documents=documents,
        releases=len(releases),
        kustomizations=kustomizations,
        unnamed_environments=tuple(sorted(set(unnamed_environments))),
        unnamed_services=tuple(sorted(set(unnamed_services))),
    )
    return releases, reconciled, counted


def read(entries: Sequence[tuple[str, Sequence[object]]]) -> Reading:
    """The facts one clone's tree declares, from `(repo-relative path, documents)`.

    Parsing is the caller's, so this stays a function of the documents: the same
    reading is reproducible from a fixture without touching a filesystem, and
    there is one place that decides what a malformed file means.
    """
    releases, reconciled, counted = _classify(entries)
    bases = _bases(releases)
    facts: dict[tuple[str, str], Fact] = {}
    for environment in sorted(reconciled):
        directories = sorted(reconciled[environment])
        for service, layers in sorted(_layers_for(releases, directories).items()):
            ordered = _ordered(bases.get(service), sorted(layers, key=lambda layer: layer[0]))
            facts[(environment, service)] = _fact(service, environment, ordered)
    for service, base in sorted(bases.items()):
        facts[(config.DEPLOY_BASE_ENV, service)] = _fact(service, config.DEPLOY_BASE_ENV, [base])

    every_directory = sorted({d for dirs in reconciled.values() for d in dirs})
    attached = {rel for rel, _ in bases.values()}
    attached |= {rel for rel, _ in releases if any(_under(rel, d) for d in every_directory)}
    unattached = tuple(sorted({rel for rel, _ in releases} - attached))
    return Reading(
        facts=facts,
        documents=counted.documents,
        releases=counted.releases,
        kustomizations=counted.kustomizations,
        unattached=unattached,
        unnamed_environments=counted.unnamed_environments,
        unnamed_services=counted.unnamed_services,
    )
