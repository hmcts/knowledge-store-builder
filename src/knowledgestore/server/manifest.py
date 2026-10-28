"""Read `knowledge/store-manifest.json` and apply the version rules.

A missing manifest or an unsupported major version refuses. A newer minor version
loads, and the layers it added are listed in `Manifest.unread`, because a reader
that does not know a layer must say it exists rather than say nothing.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .locate import MANIFEST, OutsideStore, contained

SUPPORTED_MAJORS = (1,)

# Layers this reader knows how to read, in the library's `store_manifest.LAYERS`
# order plus `deployments`. Any other name under `layers` came from a newer minor.
KNOWN_LAYERS = (
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


class Refused(Exception):
    """The store cannot be served. `found` says what was there, `supported` what is read."""

    def __init__(self, found: str, supported: tuple[int, ...]):
        self.found = found
        self.supported = supported
        super().__init__(
            f"cannot serve this store: found {found}; "
            f"supported format major versions: {', '.join(str(m) for m in supported)}"
        )


class NoManifest(Refused):
    """The root holds no manifest: a clone not yet pulled, or not a store. Kept apart
    from the other refusals because it can be put right by pulling, so a server that
    is already running reports it rather than exiting."""


def unrecognised(layer: str, found: str, supported: tuple[int, ...]) -> str:
    """Why a layer that carries its own format stamp is present but not read: `found`
    is this server's own description of the stamp, never text quoted from it."""
    majors = ", ".join(f"{major}.x" for major in supported)
    return (
        f"{layer} layer present but format unrecognised "
        f"(found {found}, supports {majors}); not read"
    )


@dataclass(frozen=True)
class Manifest:
    major: int
    minor: int
    store_id: str
    github_org: str
    library_version: str
    digest: str
    sources_as_of: str | None
    layers: dict[str, dict]
    extra_layers: dict[str, dict]
    unread: list[str] = field(default_factory=list)


def _format(document: dict) -> tuple[int, int]:
    """The manifest's `format.major` and `format.minor`, refused unless both are
    integers and the major is one this server reads."""
    format_ = document.get("format")
    major = format_.get("major") if isinstance(format_, dict) else None
    minor = format_.get("minor") if isinstance(format_, dict) else None
    if isinstance(major, bool) or not isinstance(major, int):
        raise Refused(
            f"a manifest with no integer format.major (format={format_!r})", SUPPORTED_MAJORS
        )
    if major not in SUPPORTED_MAJORS:
        raise Refused(f"format major version {major}", SUPPORTED_MAJORS)
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise Refused(
            f"a manifest with no integer format.minor (format={format_!r})", SUPPORTED_MAJORS
        )
    return major, minor


def load(root: Path) -> Manifest:
    try:
        path = contained(root, MANIFEST)
    except OutsideStore:
        # Where it resolves to is not said: stderr names no symlink target either.
        raise Refused(
            f"a manifest outside the store ({MANIFEST.as_posix()} resolves outside it)",
            SUPPORTED_MAJORS,
        ) from None
    if not path.is_file():
        raise NoManifest(f"no {MANIFEST.as_posix()} under {root}", SUPPORTED_MAJORS)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Refused(
            f"a manifest that cannot be read as JSON ({error})", SUPPORTED_MAJORS
        ) from error
    if not isinstance(document, dict):
        raise Refused("a manifest that is not a JSON object", SUPPORTED_MAJORS)

    major, minor = _format(document)

    store = document.get("store")
    store = store if isinstance(store, dict) else {}
    layers = document.get("layers")
    layers = layers if isinstance(layers, dict) else {}
    extra_layers = document.get("extra_layers")
    extra_layers = extra_layers if isinstance(extra_layers, dict) else {}
    store_id = store.get("id")
    if not isinstance(store_id, str) or not store_id:
        raise Refused("a manifest with no store.id", SUPPORTED_MAJORS)

    return Manifest(
        major=major,
        minor=minor,
        store_id=store_id,
        github_org=store.get("github_org") or "",
        library_version=document.get("library_version") or "",
        digest=document.get("digest") or "",
        sources_as_of=document.get("sources_as_of"),
        layers=layers,
        extra_layers=extra_layers,
        unread=sorted(
            name
            for name, entry in layers.items()
            if name not in KNOWN_LAYERS
            and not (isinstance(entry, dict) and entry.get("present") is False)
        ),
    )
