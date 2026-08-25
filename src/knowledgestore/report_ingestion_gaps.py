"""What this estate already depends on and does not hold - a report, not an action.

Deciding what an estate should ingest next was a judgement call with no
measurement behind it. Asked "should we broaden the repository prefixes?", the
intuitive move is to widen the naming rules; on the estate where that was
measured it would have added mostly reusable infrastructure wrappers and empty
repositories, and contradicted an exclusion the estate had already recorded
deliberately. A large cost for very little knowledge.

Dependency evidence asks a different question: not *what shares our naming* but
*what do we already depend on that we do not hold*. This stage reads the build
files in the repositories the store holds, collects the artefact coordinates
they **consume**, subtracts the coordinates the estate **builds**, and ranks
what is left. On the one estate the method was tried by hand it found a
repository holding a shared schema model, heavily referenced by artefacts
nothing in the estate built; adding it resolved every unresolved reference in a
payload contract the store had already published a finding about, and that
finding - honestly drawn from what was indexed, and false - was rewritten.

    knowledgestore gaps

Four things shape the design, each learned the same way.

**Classification comes before ranking.** Most reference weight turns out to be
framework plumbing, and references to test utilities say the estate writes
tests, not how its business works. So rows are split `domain` / `framework` by
`classify`, and domain rows are reported first whatever their weight. Without
the split the ranking points confidently at the wrong thing.

**Coordinates are never resolved to repositories.** Internal artefacts are
published to a binary repository, so an `artifactId` need appear in no source
file on the forge at all, and name matching against a large organisation returns
confident nonsense from unrelated programmes. The authoritative mapping is the
published POM's `<scm>` URL in the artefact repository, which is not something
this stage can read. It reports coordinates as written and stops there. Nothing
here touches the network.

**The scope is stated, and test scopes are counted separately.** A blended
figure hides which question it answered: `test` scope and `devDependencies`
answer "the estate writes tests against this", compile scope answers "the
estate's product needs this". They are reported as two columns, never summed.

**Unbuilt does not mean addable.** Most unbuilt coordinates are not worth
adding: some are framework, some are built on a host nobody can read, and on
the estate this was measured against roughly a hundred were unbuilt and one was
worth adding. A stage that proposed additions would have been wrong almost
every time, so this one ranks and explains. It reads no graph, writes no
artefact, and never returns non-zero on a finding; the decision stays with the
operator.

The declared boundary (`config/estate-boundary.txt`) is what tells an absence
apart from a decision, which is why this report reads it: a repository the
estate has ruled `not-used` is not a gap, one ruled `active` and not held is the
strongest kind of candidate, and a module consumed under an off-host `alias` of
a repository the store does hold is no gap at all.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import boundary, build_package_edges, config

# How many leading dot-segments make a namespace. Two is too wide - `uk.gov`
# spans unrelated organisations and group ids of that shape are common - and
# four splits one estate's namespace into several.
NAMESPACE_DEPTH = 3

# A namespace has to be one the estate demonstrably publishes under rather than
# one a single vendored fork happens to carry, so it needs more than one built
# artefact under it before consuming from it counts as consuming internally.
MIN_ARTEFACTS_FOR_NAMESPACE = 2

# Directories that hold copies rather than declarations: build output carries
# generated poms, `node_modules` carries every dependency's own manifest, and
# `.terraform` carries the upstream modules themselves. Reading any of them
# reports another project's dependencies as this estate's.
SKIP_DIRS = frozenset(
    {".git", ".gradle", ".terraform", "node_modules", "target", "build", "out", "dist"}
)

GRADLE_BUILD_FILES = ("build.gradle", "build.gradle.kts")
GRADLE_SETTINGS_FILES = ("settings.gradle", "settings.gradle.kts")

# Domain rows first. The order is the finding, not a presentation choice.
KIND_ORDER = {"domain": 0, "framework": 1}

DEFAULT_LIMIT = 20

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_POM_BLOCKS = ("dependency", "plugin", "parent", "exclusion", "extension")
# Gradle's short-form dependency notation. The map form (`group:`, `name:`) and
# version-catalog accessors (`libs.foo`) name no coordinate that can be read
# here, so they are skipped rather than guessed at.
_GRADLE_DEP = re.compile(r"""["'](?P<group>[\w.\-]+):(?P<artefact>[\w.\-]+)(?::[^"']*)?["']""")
_GRADLE_GROUP = re.compile(r"""\bgroup\s*=?\s*["']([\w.\-]+)["']""")
_GRADLE_ROOT_NAME = re.compile(r"""rootProject\.name\s*=\s*["']([\w.\-]+)["']""")
_GRADLE_INCLUDE = re.compile(r"""include\s*\(?\s*["']:?([\w.\-]+)["']""")
_PROPERTIES_GROUP = re.compile(r"""^\s*group\s*=\s*["']?([\w.\-]+)["']?\s*$""", re.MULTILINE)

# What the declaration already says about a repository this estate consumes and
# does not hold. The ruling is the difference between a gap and a decision.
RULING_NOTES = {
    "active": "declared active and not held - the boundary already calls this a gap",
    "not-used": "declared not-used - a decision, not a gap",
    "decommissioned": "declared decommissioned - a decision, not a gap",
    "": "no ruling - the estate has not said whether this belongs",
}


@dataclass(frozen=True, order=True)
class Coordinate:
    """An artefact as its consumer wrote it. Never resolved to a repository."""

    group: str
    artefact: str

    def __str__(self) -> str:
        return f"{self.group}:{self.artefact}" if self.group else self.artefact


@dataclass
class Evidence:
    """Everything read off disk, before any subtraction."""

    built: set[Coordinate] = field(default_factory=set)
    # Where the built side came from. A Gradle coordinate is a convention rather
    # than a declaration, so a reader has to be able to see how much of the
    # subtraction rests on one.
    built_from: Counter = field(default_factory=Counter)
    # (coordinate, scope) -> the (repository, file) pairs declaring it. A set,
    # so one file declaring the same dependency twice weighs one, and the
    # consuming repositories are countable without a second pass.
    declared: dict[tuple[Coordinate, str], set[tuple[str, str]]] = field(default_factory=dict)
    scanned: Counter = field(default_factory=Counter)
    # Declarations whose coordinate is a build property (`${project.groupId}`).
    # Counted, not resolved: resolving properties means implementing Maven.
    unresolved: int = 0
    # npm dependencies with no scope. An unscoped name carries no namespace, so
    # nothing here can tell an internal package from a public one.
    unscoped: set[str] = field(default_factory=set)
    held: set[str] = field(default_factory=set)
    # Terraform module provider repository -> the repositories consuming it.
    modules: dict[str, set[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Row:
    """One namespace's unbuilt artefacts, with the weight behind them."""

    group: str
    artefacts: tuple[str, ...]
    main: int
    test: int
    repos: int
    kind: str


def _walk(clone: Path) -> Iterator[Path]:
    """Files in one clone, pruning the directories that hold copies.

    Pruned rather than filtered afterwards: `node_modules` on a large front-end
    estate holds more manifests than the estate does, and walking it to throw
    the results away costs minutes per repository.
    """
    for entry in sorted(clone.iterdir()):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.name not in SKIP_DIRS:
                yield from _walk(entry)
        else:
            yield entry


def _read(path: Path) -> str:
    """File contents, or "" when it cannot be read. One unreadable build file is
    not worth abandoning an estate scan for."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tag(text: str, name: str) -> str:
    """The first value of an XML element, or ""."""
    match = re.search(rf"<{name}>\s*([^<]+?)\s*</{name}>", text)
    return match.group(1) if match else ""


def _without_blocks(text: str) -> str:
    """A pom with everything that is not the project's own identity removed.

    A child module inherits its group from `<parent>`, and a `<dependency>`
    carries a groupId of its own, so reading the first `<groupId>` in the raw
    text answers a different question - usually the parent's, sometimes a
    dependency's.
    """
    for name in _POM_BLOCKS:
        text = re.sub(rf"<{name}>.*?</{name}>", "", text, flags=re.DOTALL)
    return text


def pom_coordinates(text: str) -> tuple[Coordinate | None, list[tuple[Coordinate, str]], int]:
    """(what this pom builds, what it consumes, declarations left unresolved).

    A deliberate regex reader rather than an XML parser: a pom arrives from a
    cloned repository, which this library treats as untrusted content, and
    `xml.etree` expands internal entities. Three tag values out of a
    machine-written file do not need a parser that can be made to allocate.
    """
    text = _COMMENT.sub("", text)
    own = _without_blocks(text)
    parent = re.search(r"<parent>(.*?)</parent>", text, re.DOTALL)
    group = _tag(own, "groupId") or (_tag(parent.group(1), "groupId") if parent else "")
    artefact = _tag(own, "artifactId")
    built = (
        Coordinate(group, artefact)
        if group and artefact and "${" not in group and "${" not in artefact
        else None
    )

    consumed: list[tuple[Coordinate, str]] = []
    unresolved = 0
    for block in re.finditer(r"<dependency>(.*?)</dependency>", text, re.DOTALL):
        declaration = block.group(1)
        group_id, artefact_id = _tag(declaration, "groupId"), _tag(declaration, "artifactId")
        if not group_id or not artefact_id:
            continue
        if "${" in group_id or "${" in artefact_id:
            unresolved += 1
            continue
        scope = "test" if _tag(declaration, "scope") == "test" else "main"
        consumed.append((Coordinate(group_id, artefact_id), scope))
    return built, consumed, unresolved


def gradle_dependencies(text: str) -> list[tuple[Coordinate, str]]:
    """Short-form Gradle dependencies, with the scope taken from the line.

    The scope comes from the text before the coordinate on its own line rather
    than from the token nearest it, because `testImplementation platform("g:a:1")`
    puts `platform` next to the coordinate and the test configuration further
    left. Reading the nearer token attributes test dependencies to main.
    """
    found: list[tuple[Coordinate, str]] = []
    for line in text.splitlines():
        if line.strip().startswith(("//", "*", "/*", "#")):
            continue
        for match in _GRADLE_DEP.finditer(line):
            scope = "test" if "test" in line[: match.start()].lower() else "main"
            found.append((Coordinate(match.group("group"), match.group("artefact")), scope))
    return found


def _gradle_group(clone: Path) -> str:
    """The group a Gradle repository publishes under, from wherever it is set."""
    for name in ("gradle.properties", *GRADLE_BUILD_FILES):
        path = clone / name
        text = _read(path) if path.is_file() else ""
        match = _PROPERTIES_GROUP.search(text) if name.endswith(".properties") else None
        match = match or _GRADLE_GROUP.search(text)
        if match:
            return match.group(1)
    return ""


def gradle_identity(clone: Path) -> set[Coordinate]:
    """What a Gradle repository publishes, by convention.

    Weaker evidence than a pom, knowingly: Gradle's publication name is the
    project name unless a build script overrides it, so this is what the estate
    almost certainly publishes rather than what it declares it does. The report
    says how many built coordinates came from here for exactly that reason - a
    coordinate missing from the built side becomes a candidate for ingestion,
    and a weak built side manufactures candidates.
    """
    group = _gradle_group(clone)
    if not group:
        return set()
    artefacts: set[str] = set()
    for name in GRADLE_SETTINGS_FILES:
        path = clone / name
        text = _read(path) if path.is_file() else ""
        root = _GRADLE_ROOT_NAME.search(text)
        if root:
            artefacts.add(root.group(1))
        artefacts.update(match.group(1) for match in _GRADLE_INCLUDE.finditer(text))
    return {Coordinate(group, artefact) for artefact in artefacts or {clone.name}}


def npm_coordinate(name: str) -> Coordinate:
    """`@scope/pkg` -> (`@scope`, `pkg`); an unscoped name has no namespace."""
    if name.startswith("@") and "/" in name:
        scope, _, rest = name.partition("/")
        return Coordinate(scope, rest)
    return Coordinate("", name)


def _record(evidence: Evidence, coordinate: Coordinate, scope: str, repo: str, rel: str) -> None:
    evidence.declared.setdefault((coordinate, scope), set()).add((repo, rel))


def _read_pom(evidence: Evidence, repo: str, rel: str, text: str) -> None:
    built, consumed, unresolved = pom_coordinates(text)
    if built:
        evidence.built.add(built)
        evidence.built_from["pom.xml"] += 1
    for coordinate, scope in consumed:
        _record(evidence, coordinate, scope, repo, rel)
    evidence.unresolved += unresolved


def _read_package_json(evidence: Evidence, repo: str, rel: str, text: str) -> None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(data, dict):
        return
    name = data.get("name")
    if isinstance(name, str) and name:
        evidence.built.add(npm_coordinate(name))
        evidence.built_from["package.json"] += 1
    for key, scope in (("dependencies", "main"), ("devDependencies", "test")):
        declared = data.get(key)
        if not isinstance(declared, dict):
            continue
        for dependency in sorted(declared):
            coordinate = npm_coordinate(dependency)
            if coordinate.group:
                _record(evidence, coordinate, scope, repo, rel)
            else:
                evidence.unscoped.add(dependency)


def _read_terraform(evidence: Evidence, repo: str, text: str) -> None:
    """Module sources, through the parser that already reads them (#122).

    Reused rather than rewritten: an estate can be built almost entirely from
    shared modules, both source forms are in use, and a divergent copy of that
    regex would report different reuse from the one already in the graph.
    """
    for provider in build_package_edges.terraform_references(text):
        if provider != repo:
            evidence.modules.setdefault(provider, set()).add(repo)


def read_clone(evidence: Evidence, clone: Path) -> None:
    """Fold one repository's build files into the evidence."""
    repo = clone.name
    evidence.held.add(repo)
    published = gradle_identity(clone)
    evidence.built |= published
    evidence.built_from["gradle convention"] += len(published)
    for path in _walk(clone):
        rel = str(path.relative_to(clone))
        if path.name == "pom.xml":
            evidence.scanned["pom.xml"] += 1
            _read_pom(evidence, repo, rel, _read(path))
        elif path.name in GRADLE_BUILD_FILES:
            evidence.scanned["build.gradle"] += 1
            for coordinate, scope in gradle_dependencies(_read(path)):
                _record(evidence, coordinate, scope, repo, rel)
        elif path.name == "package.json":
            evidence.scanned["package.json"] += 1
            _read_package_json(evidence, repo, rel, _read(path))
        elif path.suffix == build_package_edges.TERRAFORM_SUFFIX:
            evidence.scanned[".tf"] += 1
            _read_terraform(evidence, repo, _read(path))


def read_estate(clones: list[Path]) -> Evidence:
    """Every clone's build evidence, in one deterministic pass."""
    evidence = Evidence()
    for clone in sorted(clones):
        read_clone(evidence, clone)
    return evidence


def namespace_of(group: str) -> str:
    return ".".join(group.split(".")[:NAMESPACE_DEPTH])


def internal_namespaces(built: set[Coordinate]) -> tuple[str, ...]:
    """The namespaces this estate publishes under, derived from what it builds.

    Nothing declares which coordinates are internal, and asking an operator to
    declare it is asking the question this stage exists to answer. An empty
    group is excluded deliberately: an estate publishing one unscoped npm
    package would otherwise make the empty namespace internal, and every public
    dependency it has would be reported as something to ingest.
    """
    counts = Counter(namespace_of(coordinate.group) for coordinate in built if coordinate.group)
    return tuple(
        sorted(name for name, here in counts.items() if here >= MIN_ARTEFACTS_FOR_NAMESPACE)
    )


def is_internal(group: str, namespaces: tuple[str, ...]) -> bool:
    """Segment-aware, so `com.example.platformer` is not inside `com.example.platform`."""
    return any(group == name or group.startswith(name + ".") for name in namespaces)


def _tokens(name: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", name.lower()) if token}


def classify(group: str, artefacts: tuple[str, ...]) -> str:
    """`framework` or `domain`, by whole-token match against the marker list.

    Tokens rather than substrings: `attestation-service` and `latest-contract`
    both contain `test`, and classifying either as framework buries a domain row
    under the plumbing this split exists to demote.

    A tie counts as framework. The error that costs an operator a finding is a
    framework row promoted above a domain one, not the reverse, and on the
    estate this was measured against two thirds of all reference weight was
    plumbing.
    """
    markers = set(config.FRAMEWORK_MARKERS)
    if _tokens(group) & markers:
        return "framework"
    marked = sum(1 for artefact in artefacts if _tokens(artefact) & markers)
    return "framework" if artefacts and marked * 2 >= len(artefacts) else "domain"


def _weights(evidence: Evidence, coordinates: set[Coordinate]) -> tuple[int, int, int]:
    """(main-scope declaring files, test-scope declaring files, consuming repositories)."""
    main = test = 0
    repos: set[str] = set()
    for (coordinate, scope), sites in evidence.declared.items():
        if coordinate not in coordinates:
            continue
        if scope == "test":
            test += len(sites)
        else:
            main += len(sites)
        repos |= {repo for repo, _ in sites}
    return main, test, len(repos)


def unbuilt(evidence: Evidence, namespaces: tuple[str, ...]) -> tuple[list[Row], int]:
    """Ranked rows for internal coordinates the estate does not build, and how
    many internal coordinates it consumes in total.

    Ranked by classification first, then weight, then name. The name tiebreak is
    not decoration: two namespaces of equal weight would otherwise come out in
    whatever order the process's hash seed produced, and two runs of the same
    store would disagree.
    """
    consumed = {
        coordinate
        for coordinate, _ in evidence.declared
        if is_internal(coordinate.group, namespaces)
    }
    # Grouped by walking the declarations in the order they were read - clones
    # sorted, files sorted - rather than by iterating the set difference above.
    # A set's iteration order is the process's hash seed, so grouping from one
    # would put a hash-ordered list into a sort and leave the name tiebreak as
    # the only thing standing between two builds and a spurious diff. Reading
    # the dict instead means the tiebreak is the *single* place order is
    # decided, which is also what makes removing it observable.
    by_group: dict[str, set[Coordinate]] = {}
    for coordinate, _ in evidence.declared:
        if coordinate in consumed and coordinate not in evidence.built:
            by_group.setdefault(coordinate.group, set()).add(coordinate)

    rows = []
    for group, coordinates in by_group.items():
        artefacts = tuple(sorted(coordinate.artefact for coordinate in coordinates))
        main, test, repos = _weights(evidence, coordinates)
        rows.append(Row(group, artefacts, main, test, repos, classify(group, artefacts)))
    rows.sort(key=lambda row: (KIND_ORDER[row.kind], -row.main, -row.test, -row.repos, row.group))
    return rows, len(consumed)


def module_gaps(evidence: Evidence, declared: boundary.Boundary | None) -> list[tuple[str, str]]:
    """(repository, what the declaration says) for modules consumed and not held.

    The alias resolution is load-bearing rather than tidy. A module source
    written against an off-host name for a repository the store *does* hold
    would otherwise be reported as something to ingest - a false gap invented
    inside the report whose whole subject is false absence.
    """
    aliases = declared.aliases if declared else {}
    rulings = declared.rulings if declared else {}
    notes: dict[str, str] = {}
    for provider in sorted(evidence.modules):
        name = aliases.get(provider, provider)
        if name in evidence.held:
            continue
        notes[name] = RULING_NOTES[rulings.get(name, "")]
    return sorted(notes.items())


def _scope_line(evidence: Evidence) -> str:
    """What was read, so a reader can see the funnel rather than trust the total."""
    if not evidence.scanned:
        return (
            "No build file was read at all: no pom.xml, build.gradle, package.json or "
            ".tf under the repositories this store holds. This report can say nothing "
            "about what the estate consumes, which is not the same as it consuming nothing."
        )
    counted = ", ".join(f"{count} {kind}" for kind, count in sorted(evidence.scanned.items()))
    return f"Read {counted} across {boundary.plural(len(evidence.held), 'repository', 'repositories')}."


def _built_line(evidence: Evidence) -> str:
    """How the built side was established, and how much of it is convention.

    A coordinate absent from the built side becomes a candidate, so a weak built
    side manufactures candidates. Naming the Gradle share is what lets a reader
    discount a row instead of chasing it.
    """
    sources = ", ".join(
        f"{count} from {kind}" for kind, count in sorted(evidence.built_from.items()) if count
    )
    line = f"Built here: {len(evidence.built)} coordinates ({sources or 'none found'})."
    if evidence.built_from.get("gradle convention"):
        line += (
            " A Gradle coordinate is the project's conventional publication name, not a "
            "declaration, so the subtraction is weaker wherever the estate uses Gradle."
        )
    return line


def _membership_lines(declared: boundary.Boundary | None) -> list[str]:
    """Why an unbuilt coordinate is not the same thing as a missing repository."""
    lines = [
        "Derived only from the repositories this store holds. A coordinate unbuilt here "
        "may be built somewhere nobody has read."
    ]
    if declared is None:
        lines.append(
            "No boundary is declared (`config/estate-boundary.txt`), so nothing records "
            "whether a repository is outside this estate by decision or merely unlocated."
        )
    elif declared.unsearched:
        lines.append(
            "The declaration names "
            + boundary.plural(len(declared.unsearched), "source")
            + " nobody read ("
            + ", ".join(declared.unsearched)
            + "), so an artefact built there is unbuilt here by construction."
        )
    return lines


def _row_lines(rows: list[Row], limit: int) -> list[str]:
    """The ranking, domain first, with each namespace's artefacts under it."""
    shown = rows if limit <= 0 else rows[:limit]
    lines = ["", "  main  test  repos  class      namespace / artefacts not built here"]
    indent = " " * 34
    for row in shown:
        lines.append(f"  {row.main:>4}  {row.test:>4}  {row.repos:>5}  {row.kind:<9}  {row.group}")
        lines.extend(f"{indent}{artefact}" for artefact in row.artefacts)
    if len(rows) > len(shown):
        lines.append(f"  ... and {len(rows) - len(shown)} further namespaces (--limit 0 for all)")
    return lines


FOOTER = (
    "Coordinates are reported as written and never resolved to a repository: an internal "
    "artefact is published to a binary repository, so its artifactId need appear in no "
    "source file on the forge, and name matching against a large organisation returns "
    "confident nonsense from unrelated programmes. Resolve one from the published POM's "
    "<scm> URL in your artefact repository, then decide. Most unbuilt coordinates are not "
    "worth adding."
)


def report(evidence: Evidence, declared: boundary.Boundary | None, limit: int) -> list[str]:
    """The whole report as lines. Deterministic: every collection is sorted."""
    namespaces = internal_namespaces(evidence.built)
    rows, consumed = unbuilt(evidence, namespaces)
    lines = ["Ingestion candidates from dependency evidence.", ""]
    lines += _membership_lines(declared)
    lines += ["", _scope_line(evidence), _built_line(evidence)]
    if namespaces:
        lines.append(
            "Internal namespaces (the estate builds "
            f"{MIN_ARTEFACTS_FOR_NAMESPACE}+ artefacts under each): " + ", ".join(namespaces)
        )
    else:
        lines.append(
            "No internal namespace could be derived: nothing the estate builds shares a "
            f"namespace with {MIN_ARTEFACTS_FOR_NAMESPACE} or more of its own artefacts, so "
            "no consumed coordinate can be called internal and none is ranked below."
        )
    lines.append(
        f"Consumed internally: {consumed} coordinates, "
        f"{len(rows)} namespaces holding artefacts this estate does not build."
    )
    if evidence.unresolved:
        lines.append(
            "Not counted: "
            + boundary.plural(evidence.unresolved, "declaration")
            + " naming a build property rather than a coordinate; resolving one means "
            "implementing Maven."
        )
    if evidence.unscoped:
        lines.append(
            "Not classified: "
            + boundary.plural(len(evidence.unscoped), "npm dependency", "npm dependencies")
            + " carrying no scope. An unscoped name has no namespace, so nothing here "
            "can tell an internal package from a public one."
        )
    if rows:
        lines += _row_lines(rows, limit)

    gaps = module_gaps(evidence, declared)
    if gaps:
        lines += ["", f"Terraform modules consumed and not held: {len(gaps)}"]
        lines += [f"  {name:<32}{note}" for name, note in gaps]
    lines += ["", FOOTER]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledgestore gaps",
        description="Report what this estate depends on and does not hold, ranked.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"namespaces to show (default {DEFAULT_LIMIT}; 0 for all)",
    )
    arguments = parser.parse_args(argv)

    if not config.REPOSITORIES_DIR.is_dir():
        print(f"No clones under {config.REPOSITORIES_DIR} - run `knowledgestore sync` first")
        return 1
    clones = sorted(d for d in config.REPOSITORIES_DIR.iterdir() if (d / ".git").is_dir())
    if not clones:
        print(f"No clones under {config.REPOSITORIES_DIR} - run `knowledgestore sync` first")
        return 1

    print("\n".join(report(read_estate(clones), boundary.read(), arguments.limit)))
    # Findings are the normal state of an estate, so this stage reports and
    # stops. A non-zero exit would make "you depend on something you do not
    # hold" a build failure, when it is a decision for an operator.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
