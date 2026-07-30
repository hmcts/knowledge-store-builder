"""Shared pipeline IO helpers - JSON and gzip-JSON reading/writing.

Consolidates the read/write patterns previously re-implemented per stage
(measured in docs/audit-extraction-readiness.md). Named "pipeline_io" (not
"io") so it can never shadow the stdlib io module when scripts run with
sys.path[0] pointing at scripts/.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path


def read_json(path: Path, default=None):
    """Parse a JSON file; return `default` if it does not exist."""
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8"
    )


def read_gzip_json(path: Path, default=None):
    """Parse a gzip-compressed JSON file; return `default` if absent."""
    if not path.exists():
        return default
    with gzip.open(path, "rt", encoding="utf-8") as source:
        return json.load(source)


def write_gzip_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as out:
        json.dump(data, out, ensure_ascii=False)


def load_graph(path: Path) -> dict:
    """The estate graph (node-link JSON). Raises if absent - callers treat a
    missing graph as a hard error with their own message."""
    return json.loads(path.read_text(encoding="utf-8"))


def load_labels(path: Path) -> dict:
    """Community labels, or {} when not yet generated."""
    return read_json(path, default={})
