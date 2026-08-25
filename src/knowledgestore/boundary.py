"""What the estate is, what it deliberately excludes, and what it never claims.

A store answers "there is no evidence of X". What it can honestly say is "there
is no evidence of X *in the repositories I hold*", and nothing in its output
distinguishes the two. That holds for every estate, single-host ones included,
and it has already produced a published finding that was drawn honestly from
what was indexed and was false: a payload schema was reported to have no
readable source because its references did not resolve, when they resolved
perfectly against a repository the estate simply did not hold.

**Absence of evidence in a store is a fact about the store's membership, not
about the estate.** This module is where an estate says so. None of it can be
derived - a ruling is a decision, and no amount of extraction produces one - so
it lives in a file an operator maintains by hand, `config/estate-boundary.txt`:

    searched <where>            a source that contributed beyond the configured
                                organisation, which the manifest already names
    unsearched <where>          one known to hold estate code and not read
    active <name>               a repository the estate rules live
    not-used <name>             one that exists and is not used
    decommissioned <name>       one that has been retired
    alias <other> <name>        `<other>` is the same repository as `<name>`
    snapshot <name> <date>      an off-host copy taken by hand on that date

Two of those earn their place by being counter-intuitive. **Aliases**: hosts use
different naming conventions, so the same repository arrives under two names and
is either counted twice or reported absent while it is present - a ruling
written under the off-host name is resolved here before anything reconciles it.
**Snapshots**: an off-host copy refreshed by hand is frozen at a date, and
without recording the date a store cannot tell fresh from frozen, so neither can
its reader.

The declaration deliberately does not claim completeness. An operator who has
enumerated every host may still be hunting deployed services with no locatable
repository anywhere, and a declaration implying "this is all of it" would be a
new false claim replacing the old silent one.

Discovery reading more than one host is the larger, later change (issue #92).
This half needs no multi-host support at all and removes the silent boundary on
its own.
"""

from __future__ import annotations

import datetime
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import config


# Ordered as a reader wants them, not alphabetically: what is live first.
RULINGS = ("active", "not-used", "decommissioned")

# The sentence the whole file exists to make unmissable. Rendered whether or not
# a declaration exists, because a store with no declaration is the case where a
# reader most needs telling.
NO_COMPLETENESS = (
    "**Completeness is not claimed.** A declared boundary says what this estate "
    "knows about, not what exists. A deployed service whose repository nobody has "
    "located is neither listed here nor ruled out by not being listed."
)


@dataclass(frozen=True)
class Boundary:
    """A parsed declaration. Every collection is ordered, so output is stable."""

    searched: tuple[str, ...] = ()
    unsearched: tuple[str, ...] = ()
    # Keyed by the repository's estate name, aliases already resolved.
    rulings: dict[str, str] = field(default_factory=dict)
    # Other name -> estate name.
    aliases: dict[str, str] = field(default_factory=dict)
    # Estate name -> ISO date the off-host copy was taken.
    snapshots: dict[str, str] = field(default_factory=dict)

    def names_for(self, name: str) -> list[str]:
        """Every other name this repository is known by, sorted."""
        return sorted(other for other, target in self.aliases.items() if target == name)


def _fields(kind: str, rest: str, count: int, path: Path, line_number: int) -> list[str]:
    """Split a rule's value, refusing a field count the kind cannot mean.

    Checked rather than tolerated because of the trap the filter file already
    carries: a rule takes the rest of its line, so `active orders-api # live`
    would otherwise declare a ruling for a repository called
    `orders-api # live`, match nothing, and say nothing. A repository name has
    no spaces in it, so here the mistake is detectable.
    """
    parts = rest.split()
    if len(parts) != count:
        raise ValueError(
            f"{path}:{line_number}: `{kind}` takes {count} value(s), got {len(parts)}: {rest!r}. "
            "Put a comment on its own line - a trailing one becomes part of the value."
        )
    return parts


def _iso_date(value: str, path: Path, line_number: int) -> str:
    """An unambiguous date, or an error naming the line.

    Unparseable is refused rather than kept as text: the point of recording when
    a manual copy was taken is that a reader can tell fresh from frozen, and a
    date nothing can compare reads exactly like no date at all.
    """
    try:
        datetime.date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(
            f"{path}:{line_number}: `{value}` is not a date - write it as YYYY-MM-DD. "
            "A date nothing can compare cannot tell a fresh copy from a frozen one."
        ) from error
    return value


def _resolve(
    written: dict[str, tuple[str, int]],
    aliases: dict[str, str],
    what: str,
    path: Path,
) -> dict[str, str]:
    """Re-key a rule set by estate name, refusing two answers for one repository.

    Resolving here is what makes an alias load-bearing rather than decorative: a
    ruling written under an off-host name lands on the repository the store
    actually holds, instead of being reported as a repository the store lacks -
    which is the false absence this whole module exists to remove.
    """
    resolved: dict[str, tuple[str, int]] = {}
    for name in sorted(written):
        value, line_number = written[name]
        target = aliases.get(name, name)
        held = resolved.get(target)
        if held is not None and held[0] != value:
            raise ValueError(
                f"{path}: `{target}` is declared {held[0]} at line {held[1]} and "
                f"{value} at line {line_number}. A repository has one {what}; "
                "if these are different repositories, remove the alias."
            )
        resolved[target] = (value, line_number)
    return {name: value for name, (value, _) in resolved.items()}


@dataclass
class _Draft:
    """Declarations as written, before aliases are resolved. Parsing only."""

    searched: set[str] = field(default_factory=set)
    unsearched: set[str] = field(default_factory=set)
    aliases: dict[str, str] = field(default_factory=dict)
    # Subject as written -> (value, line number). The line number is kept so a
    # conflict can name both places rather than only the winner.
    ruled: dict[str, tuple[str, int]] = field(default_factory=dict)
    taken: dict[str, tuple[str, int]] = field(default_factory=dict)


def _apply(draft: _Draft, kind: str, rest: str, path: Path, line_number: int) -> None:
    """Record one declaration, refusing a kind or a shape a reader could misread."""
    if kind == "searched":
        draft.searched.add(rest)
    elif kind == "unsearched":
        draft.unsearched.add(rest)
    elif kind in RULINGS:
        (name,) = _fields(kind, rest, 1, path, line_number)
        draft.ruled[name] = (kind, line_number)
    elif kind == "alias":
        other, name = _fields(kind, rest, 2, path, line_number)
        if other == name:
            raise ValueError(
                f"{path}:{line_number}: `alias {other} {name}` says a repository is "
                "itself. An alias names the estate repository an off-host name means."
            )
        draft.aliases[other] = name
    elif kind == "snapshot":
        name, when = _fields(kind, rest, 2, path, line_number)
        draft.taken[name] = (_iso_date(when, path, line_number), line_number)
    else:
        raise ValueError(
            f"{path}:{line_number}: unknown declaration `{kind}`. Known: "
            f"searched, unsearched, {', '.join(RULINGS)}, alias, snapshot."
        )


def _refuse_chains(aliases: dict[str, str], path: Path) -> None:
    """An alias whose target is itself an alias resolves differently by read order."""
    chained = sorted(set(aliases) & set(aliases.values()))
    if chained:
        raise ValueError(
            f"{path}: {', '.join(chained)} is both an alias and the repository an "
            "alias points at. Point every alias straight at the estate repository; a "
            "chain resolves differently depending on which end you read from."
        )


def _refuse_empty(declared: Boundary, path: Path) -> None:
    """A present file declaring nothing reads as a boundary and describes none."""
    if any(
        (
            declared.searched,
            declared.unsearched,
            declared.rulings,
            declared.aliases,
            declared.snapshots,
        )
    ):
        return
    raise ValueError(
        f"{path}: holds no declarations. An empty file reads as a declared boundary "
        "and declares nothing; delete it, or say what was searched."
    )


def parse(text: str, path: Path) -> Boundary:
    """Read a declaration, refusing anything a reader could misread."""
    draft = _Draft()
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, rest = line.partition(" ")
        rest = rest.strip()
        if not rest:
            raise ValueError(f"{path}:{line_number}: `{kind}` declares nothing: {raw!r}")
        _apply(draft, kind, rest, path, line_number)

    _refuse_chains(draft.aliases, path)
    declared = Boundary(
        searched=tuple(sorted(draft.searched)),
        unsearched=tuple(sorted(draft.unsearched)),
        rulings=_resolve(draft.ruled, draft.aliases, "ruling", path),
        aliases={other: draft.aliases[other] for other in sorted(draft.aliases)},
        snapshots=_resolve(draft.taken, draft.aliases, "snapshot date", path),
    )
    _refuse_empty(declared, path)
    return declared


def read(path: Path | None = None) -> Boundary | None:
    """The declaration, or None when the estate has not made one."""
    path = config.BOUNDARY_PATH if path is None else path
    if not path.is_file():
        return None
    return parse(path.read_text(encoding="utf-8"), path)


def plural(count: int, noun: str, many: str | None = None) -> str:
    """`1 repository` / `2 repositories`. Reported counts get read aloud."""
    return f"{count} {noun if count == 1 else (many or noun + 's')}"


def manifest_section(declared: Boundary | None) -> list[str]:
    """The declared boundary as markdown, for the committed repository manifest.

    Sorted throughout and derived only from the declaration file, so two builds
    of the same store produce the same bytes. The configured organisation is
    deliberately not restated here: the paragraph this section follows already
    names it, and adding it back produced it twice for any estate that also
    declared it by hand.
    """
    if declared is None:
        return [
            "### No boundary is declared",
            "",
            "This estate has not written `config/estate-boundary.txt`, so nothing here "
            "records which other hosts exist, which repositories the estate has ruled "
            "out, or which names are aliases of one another. Until it does, treat every "
            "absence as unexplained rather than as a decision.",
            "",
            NO_COMPLETENESS,
            "",
        ]

    lines = ["### Declared boundary", ""]
    if declared.searched:
        lines += [f"Also consulted: {', '.join(declared.searched)}.", ""]
    if declared.unsearched:
        lines += [
            "**Known and not consulted:** "
            + ", ".join(declared.unsearched)
            + ". Code held there is absent from this store without appearing as a gap.",
            "",
        ]
    if declared.rulings:
        lines += [
            "| Repository | Ruling | Also known as | Off-host copy taken |",
            "|---|---|---|---|",
        ]
        for name in sorted(declared.rulings):
            others = ", ".join(f"`{other}`" for other in declared.names_for(name)) or "-"
            when = declared.snapshots.get(name)
            lines.append(
                f"| `{name}` | {declared.rulings[name]} | {others} "
                f"| {when + ' (refreshed by hand)' if when else '-'} |"
            )
        lines.append("")
    unruled = sorted(set(declared.aliases.values()) - set(declared.rulings))
    if unruled:
        lines += [
            "Also known by another name: "
            + "; ".join(
                f"`{name}` is also " + ", ".join(f"`{o}`" for o in declared.names_for(name))
                for name in unruled
            )
            + ".",
            "",
        ]
    return lines + [NO_COMPLETENESS, ""]


def summary_line(declared: Boundary) -> str:
    """One line for `status`: what the declaration covers, without the detail.

    A part that would read `0` is left out rather than printed, so the line says
    what was declared instead of listing what was not. Parsing refuses a file
    with nothing in it, so the line can never be empty.
    """
    counts = Counter(declared.rulings.values())
    parts = []
    if declared.searched:
        parts.append(plural(len(declared.searched), "source") + " declared searched")
    if declared.unsearched:
        parts.append(plural(len(declared.unsearched), "source") + " declared not searched")
    if declared.rulings:
        ruled = ", ".join(f"{counts[r]} {r}" for r in RULINGS if counts[r])
        parts.append(
            plural(len(declared.rulings), "repository", "repositories") + f" ruled ({ruled})"
        )
    if declared.aliases:
        parts.append(plural(len(declared.aliases), "alias", "aliases"))
    if declared.snapshots:
        parts.append(
            plural(len(declared.snapshots), "hand-taken copy", "hand-taken copies")
            + f" (oldest {min(declared.snapshots.values())})"
        )
    return "Estate boundary: " + ", ".join(parts) + "; completeness not claimed."


def reconciliation(declared: Boundary, held: set[str]) -> dict[str, list[str]]:
    """Where the declaration and the repositories the store holds disagree.

    A declaration nothing checks is a second thing that can be quietly wrong, so
    it is reconciled against provenance - what actually reached disk - rather
    than taken on trust. Each key is a different wrong answer waiting to happen:
    a repository ruled live and absent answers as though it did not exist, and
    one ruled dead and present is cited as current.
    """
    return {
        "active_absent": sorted(
            name
            for name, ruling in declared.rulings.items()
            if ruling == "active" and name not in held
        ),
        "ruled_out_held": sorted(
            name for name, ruling in declared.rulings.items() if ruling != "active" and name in held
        ),
        # Only targets nothing else already names: one that is ruled and absent is
        # reported above, and saying it twice trains a reader to skip the line.
        "alias_absent": sorted(
            target
            for target in set(declared.aliases.values())
            if target not in held and target not in declared.rulings
        ),
    }
