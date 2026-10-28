"""A store that cannot be served yet, told to the asker rather than to a log.

A client that launches the server shows an exit as "failed to start" and nothing
more, so the asker never learns that the clone is unregistered or not pulled. A
store that is not there *yet* is therefore reported on every call, naming the
cause, where the server looked, and the fix, and is looked for again on the next
call. A store that is there but wrong (an unsupported format, a garbled id, two
clones claiming one id, a layer that escapes the store) is a misconfiguration no
pull will cure, and still stops the server at start-up with exit status 2.
"""

import argparse
from collections.abc import Mapping

from .locate import NotFound
from .manifest import NoManifest

# The cause, as a client can switch on it.
NO_REGISTERED_CLONE = "no_registered_clone"
NO_MANIFEST = "no_manifest"
OTHER = "other"

# Named with `~`, not as the absolute path: a message to a client names no host path.
LOCATIONS = "~/.config/knowledge-store/locations"

FIX = (
    "Register the clone (the knowledge-store skill records it on first clone, or add its "
    f"absolute path as a line in {LOCATIONS}), pull it so that "
    "knowledge/store-manifest.json exists, then reconnect this server. A later call looks "
    "again, so a clone registered now is found without a restart."
)


class StoreUnavailable(Exception):
    """No store is there to answer from yet. Carries the whole refusal."""

    def __init__(self, cause: str, message: str, checked: dict[str, str]):
        self.cause = cause
        self.message = message
        self.checked = checked
        super().__init__(message)

    def result(self) -> dict:
        return {
            "error": {
                "type": "store_unavailable",
                "message": self.message,
                "cause": self.cause,
                "checked": self.checked,
                "fix": FIX,
            }
        }


def explain(
    reason: NotFound | NoManifest, args: argparse.Namespace, env: Mapping[str, str]
) -> StoreUnavailable:
    """`reason`, which resolution or opening raised, as the asker's refusal."""
    store_id = getattr(args, "store_id", None)
    checked = {
        "locations_file": LOCATIONS if store_id else f"{LOCATIONS} (no --store-id given)",
        "KNOWLEDGE_STORE_PATH": "set" if env.get("KNOWLEDGE_STORE_PATH") else "not set",
    }
    if isinstance(reason, NoManifest):
        return StoreUnavailable(
            NO_MANIFEST,
            "the clone has no knowledge/store-manifest.json, so there is no store to answer from",
            checked,
        )
    if store_id:
        return StoreUnavailable(
            NO_REGISTERED_CLONE,
            f"no registered clone has store id {store_id!r}",
            checked,
        )
    return StoreUnavailable(
        OTHER, "no store is configured: no --store-id, --store or store path was given", checked
    )


# What a running server treats as "not there yet"; anything else it raises as itself.
NOT_YET = (NotFound, NoManifest)
