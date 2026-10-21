"""Write `knowledge/store-manifest.json`: what this store contains.

Run it last. The manifest is measured from the files the other stages wrote, so
a manifest written earlier describes a store that no longer exists - and the
digest a reader compares to decide whether to refresh would be the old one.

Exit codes: 0 written; 1 a refusal (the store's id cannot be determined, or an
extra layer is declared wrongly), with nothing written; 2 the manifest could not
be written - the root is missing or `knowledge/` is not writable.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import config, io, store_manifest

# The mark for a layer whose presence nothing measured: neither built nor absent.
UNKNOWN = "?"


def _mark(present) -> str:
    if present is None:
        return UNKNOWN
    return "✓" if present else "✗"


def _local(layer: dict) -> str:
    """Said beside a layer git ignores: it exists here, and no clone receives it."""
    return " (local only: git ignores it)" if layer.get("local_only") else ""


def render(manifest: dict) -> str:
    """The manifest's text: sorted keys, indent 1, a trailing newline.

    A fixed form, so two runs over one store write the same bytes (CLAUDE.md 1.3)
    and a committed diff shows only what changed.
    """
    return json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def write_atomically(path: Path, text: str) -> None:
    """Write `text` to `path` through a temporary file and a rename.

    A reader opens this file first, and an interrupted in-place write would leave
    it a truncated manifest where a whole one stood.
    """
    io.checked_write_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")  # NOSONAR(S2083) - checked above
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def report(manifest: dict) -> None:
    for name, layer in manifest["layers"].items():
        print(f"  {_mark(layer['present'])} {name}{_local(layer)}")
    for name, layer in manifest["extra_layers"].items():
        print(f"  {_mark(layer['present'])} {name} (extra){_local(layer)}")
    format_ = manifest["format"]
    print(
        f"Store manifest: format {format_['major']}.{format_['minor']}, "
        f"id {manifest['store']['id']}, digest {manifest['digest']}"
    )


def main() -> int:
    root = config.ROOT
    path = config.STORE_MANIFEST_PATH
    if not root.is_dir():
        # Checked first: writing would otherwise create a store at a mistyped root.
        print(f"Cannot write {path}: {root} is not a directory.", file=sys.stderr)
        return 2
    try:
        manifest = store_manifest.compute(root)
    except store_manifest.UsageError as refusal:
        print(f"Store manifest not written: {refusal}", file=sys.stderr)
        return 1
    try:
        write_atomically(path, render(manifest))
    except OSError as failure:
        print(f"Cannot write {path}: {failure}", file=sys.stderr)
        return 2
    report(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
