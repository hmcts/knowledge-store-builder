"""One way to open the library's stores for a contract test, in both read modes.

`store/` holds the library's query index; `no-index/` is the same inputs built
without the `index` stage, so the server reads the graph whole. A contract holds
in both, so each contract is written once, as a mixin, and `both_modes` makes it
two test classes:

    class GraphContract(Contract):
        def test_...(self): ...

    GraphWithIndex, GraphWithoutIndex = harness.both_modes(GraphContract)

`Contract` is `LibraryStoreCase` to the type checker and `object` at run time,
so the mixin is never collected and run on its own, with no store.

Git shas are the one input read rather than written as literals: the generator
records each clone's HEAD in `inputs.json`, from the clones it made.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from unittest import mock

from knowledgestore.server import store
from library_store.generate import INPUTS, NO_INDEX, OUT, PARTIAL

# The session's clock: 18 days and 3 hours after the newest input commit.
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
SHAS: dict[str, str] = json.loads(INPUTS.read_text("utf-8"))["shas"]

# Every layer v0.19.1's manifest names: its 18 `LAYERS` and `deployments`. The
# one test-side list: `pipeline.py` keeps its own `EXPECTED`, since it runs in
# the frozen environment and imports nothing from here.
LIBRARY_LAYERS = (
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


class LibraryStoreCase(unittest.TestCase):
    """A session over `STORE`, with its index cache in a fresh directory, so no
    test reads another's cache or the user's. `INDEXED` says whether `STORE` holds
    the query index."""

    STORE: Path
    INDEXED: bool

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = Path(tmp.name).resolve() / "cache"
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)})
        patcher.start()
        self.addCleanup(patcher.stop)
        opened: list[store.Store] = []

        def factory() -> store.Store:
            loaded = store.open_store(self.STORE, cache)
            opened.append(loaded)
            return loaded

        # Closes each store the session opened, and never opens one to close it.
        self.addCleanup(lambda: [loaded.close() for loaded in opened])
        self.session = store.Session(factory, lambda: NOW)

    @property
    def reader(self) -> str:
        """The layer every graph answer comes from in this mode."""
        return "index" if self.INDEXED else "graph"


def pages(call: Callable[..., dict[str, Any]], most: int = 20) -> list[dict[str, Any]]:
    """Every page of a listing at a cap of one byte, so each page holds one item:
    `call(cursor=..., cap_bytes=...)` from no cursor, following each `next_page`.
    Raises after `most` pages with a cursor still pending, so a cursor that never
    advances fails loudly rather than looping or truncating the listing."""
    found = [call(cursor=None, cap_bytes=1)]
    while found[-1]["next_page"] is not None:
        if len(found) >= most:
            raise AssertionError(f"still a next_page after {most} pages")
        found.append(call(cursor=found[-1]["next_page"], cap_bytes=1))
    return found


if TYPE_CHECKING:
    Contract = LibraryStoreCase
else:
    Contract = object


def _modes(mixin: type, stores: tuple[Path, Path], names: tuple[str, str]) -> tuple[type, type]:
    """`mixin`'s tests as two test classes, over the indexed store and the one
    built without its index, named `mixin`'s name plus each of `names`."""

    def form(store: Path, name: str, indexed: bool) -> type:
        return type(
            f"{mixin.__name__}{name}",
            (mixin, LibraryStoreCase),
            {"STORE": store, "INDEXED": indexed, "__module__": mixin.__module__},
        )

    return form(stores[0], names[0], True), form(stores[1], names[1], False)


def both_modes(mixin: type) -> tuple[type, type]:
    """`mixin`'s tests as two test classes: over `store/`, with its index, and over
    `no-index/`, where the graph answers."""
    return _modes(mixin, (OUT, NO_INDEX), ("WithIndex", "WithoutIndex"))


def partial_modes(mixin: type) -> tuple[type, type]:
    """`mixin`'s tests over the partial store (`generate.PARTIAL`), with its index
    and without: `…Indexed` and `…Unindexed`, so the coverage guard never takes
    them for a `WithIndex` class over `store/`."""
    return _modes(mixin, (PARTIAL.store, PARTIAL.no_index), ("Indexed", "Unindexed"))
