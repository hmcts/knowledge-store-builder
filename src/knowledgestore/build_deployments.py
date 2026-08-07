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

import re
from pathlib import Path

from . import config, deploy_values

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


def discover(repo_dir: Path) -> dict[tuple[str, str], dict[str, str]]:
    """(environment, service) -> flattened configuration, for one deployment clone."""
    root = _glob_root(config.DEPLOY_VALUES_GLOB)
    found: dict[tuple[str, str], dict[str, str]] = {}
    for path in sorted(repo_dir.glob(config.DEPLOY_VALUES_GLOB)):
        rel = path.relative_to(repo_dir).as_posix()
        flat = _parse(path.read_text(encoding="utf-8", errors="replace"))
        if flat is None:
            continue
        found[(environment_of(rel, root), service_of(path.name))] = flat
    return found


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def match_services(services: set[str], repos: set[str]) -> dict[str, str]:
    """service name -> the repository that holds it, where one clearly does.

    A deployed service is named for what it is (`progression-service`); the
    repository is named for where it sits (`cpp-context-progression`). Matching
    on the normalised stem finds the join without a hand-maintained table, and
    an ambiguous match resolves to the shortest then alphabetically first name
    so two runs agree.
    """
    matched: dict[str, str] = {}
    for service in sorted(services):
        stem = _norm(_VALUES_SUFFIX.sub("", service).removesuffix("-service"))
        if not stem:
            continue
        candidates = sorted(
            (r for r in repos if stem and stem in _norm(r)), key=lambda r: (len(r), r)
        )
        if candidates:
            matched[service] = candidates[0]
    return matched
