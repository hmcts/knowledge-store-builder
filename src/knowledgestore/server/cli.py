"""Command line entry point: find the store, open it, serve it over stdio.

stdout is the protocol channel from the first byte, so nothing else is ever
written there: logging goes to stderr, and a store that cannot be served is
reported on stderr with exit status 2, before the server starts.

The exception is a store that is not there yet: no registered clone for the store
id, or a clone with no manifest. The server then starts, and every call refuses
with the cause and the fix (`unavailable`), looking again each time, so a client
that shows only "failed to start" for an exit still tells the asker what to do.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from . import __version__, index, locate
from .locate import STORE_ID, AmbiguousStoreId, NotFound
from .manifest import NoManifest, Refused
from .results import DEFAULT_MAX_RESPONSE_BYTES
from .server import INSTRUCTIONS, KnowledgeStoreServer
from .unavailable import explain

# The exit status of a store that cannot be served, as argparse uses for bad usage.
REFUSED = 2


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of bytes, 1 or more: {value!r}")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledgestore serve",
        description="A read-only MCP server that answers questions from a knowledge store.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--store", metavar="PATH", help="the store's root directory")
    parser.add_argument(
        "--store-id",
        metavar="ID",
        help="the store.id of a clone listed in ~/.config/knowledge-store/locations",
    )
    parser.add_argument(
        "--max-response-bytes",
        type=_positive,
        default=DEFAULT_MAX_RESPONSE_BYTES,
        metavar="N",
        help="the most bytes one answer may be; a longer list is paged "
        f"(default {DEFAULT_MAX_RESPONSE_BYTES})",
    )
    parser.add_argument(
        "--print-instructions",
        action="store_true",
        help="print the answering guidance the server sends to clients, and exit; "
        "for a client that drops it",
    )
    args = parser.parse_args(argv)
    if args.print_instructions:
        # Bytes, not text: the copy must be exactly what is sent, whatever the
        # platform does to line endings. No store is resolved or opened.
        sys.stdout.buffer.write(INSTRUCTIONS.encode("utf-8"))
        sys.stdout.flush()
        return 0
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    if args.store_id is not None and not STORE_ID.fullmatch(args.store_id):
        # No store has an id like this, so no clone registered later could answer to it.
        print(
            f"knowledgestore serve: --store-id {args.store_id!r} cannot be a store id",
            file=sys.stderr,
        )
        return REFUSED

    home = Path.home()
    cache = index.cache_home(os.environ, home)

    def resolve() -> locate.Resolved:
        return locate.resolve(args, os.environ, home)

    registered = locate.Registered(home)

    try:
        found = resolve()
        served = KnowledgeStoreServer(
            found.root,
            cache,
            max_response_bytes=args.max_response_bytes,
            source=found.source,
            registered=registered,
        )
    except (NotFound, NoManifest) as absent:
        # Not there yet: serve, and tell the asker on every call (see `unavailable`).
        print(
            f"knowledgestore serve: {absent}; serving a refusal until it is found", file=sys.stderr
        )
        served = KnowledgeStoreServer(
            None,
            cache,
            max_response_bytes=args.max_response_bytes,
            resolve=resolve,
            explain=lambda reason: explain(reason, args, os.environ),
            registered=registered,
        )
    except (AmbiguousStoreId, Refused) as refusal:
        print(f"knowledgestore serve: {refusal}", file=sys.stderr)
        return REFUSED
    try:
        asyncio.run(served.serve_stdio())
    finally:
        served.close()
    return 0
