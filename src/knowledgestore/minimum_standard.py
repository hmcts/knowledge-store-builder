"""The minimum-standard layer's shared contract: format stamp, points and states.

The layer records, per repository, dated and cited evidence against the seven points
of the GOV.UK minimum standard for publicly accessible systems. The settings stage,
the evidence stage and the manifest all take these names from here, so a point
renamed or a state added is one edit rather than three that can disagree.
"""

from __future__ import annotations

# The layer's format version, stamped into `summary.json` and every `by-repo` file.
# Major for incompatible changes, minor for additions.
FORMAT: tuple[int, int] = (1, 0)
LAYER = "minimum_standard"

# In the order the standard states them.
POINTS: tuple[str, ...] = (
    "owner",
    "security_contact",
    "no_secrets",
    "secure_by_design",
    "hygiene",
    "patching",
    "unmaintained",
)

# `absent` means the good practice is not evidenced, never that a team does not do it;
# `not_determinable` means what the build can see cannot settle the point.
STATES = ("present", "absent", "not_determinable")


def format_stamp() -> dict:
    """The `format` object the layer's files carry at their top level."""
    return {"layer": LAYER, "major": FORMAT[0], "minor": FORMAT[1]}


def format_problem(body: object) -> str:
    """Why `body` is not a layer this release reads, or "" when its stamp is ours.

    A file with no stamp, another layer's stamp or another major is refused: a
    store's own script may write a differently shaped `knowledge/minimum-standard/`,
    and a reader must not take that for this stage's output.
    """
    stamp = body.get("format") if isinstance(body, dict) else None
    if not isinstance(stamp, dict) or stamp.get("layer") != LAYER:
        return f'it carries no `format` stamp for the "{LAYER}" layer'
    major = stamp.get("major")
    if isinstance(major, bool) or major != FORMAT[0]:
        return f"it is format major {major!r}; this release reads major {FORMAT[0]}"
    return ""


def point(
    key: str,
    state: str,
    evidence: list[dict] | None = None,
    reason: str | None = None,
) -> dict:
    """One point's record: its state, the evidence cited, and why when undetermined.

    Raises `ValueError` for a key or state outside the contract, and for
    `no_secrets` present: a pattern scan finding nothing cannot show that nothing
    is committed, so the layer never says so.
    """
    if key not in POINTS:
        raise ValueError(f"unknown point {key!r}; the points are {', '.join(POINTS)}")
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}; the states are {', '.join(STATES)}")
    if key == "no_secrets" and state == "present":
        raise ValueError("no_secrets is never present: a scan cannot show absence of secrets")
    record: dict = {"state": state, "evidence": list(evidence or [])}
    if reason is not None:
        record["reason"] = reason
    return record
