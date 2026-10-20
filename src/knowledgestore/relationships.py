"""Which repositories reference which others - the declaration parsers.

An estate's repositories depend on each other through files they commit: a
Terraform module `source`, a Helm chart dependency or Flux `HelmRelease`, a
`pom.xml` dependency, a `package.json` dependency. Each of those is a *kind*, and
a kind is two functions over one clone: what it **references**, and what it
**publishes** (the coordinates other repositories can reference it by). Joining
the two is the layer's job; this module only reads declarations.

Nothing is inferred. A reference is a line in a committed file, carried with its
path and line number so an answer can cite it. A source that names no repository
(a registry address, a third-party coordinate) is still a reference - the store
does not hold that dependency, which is a finding - and a local path is none, being
inside the repository.

The library stays dependency-free: Maven is read with the stdlib's expat, `Chart.yaml`
with a small reader held to PyYAML's output by a test, and PyYAML is imported only
to read a Flux `HelmRelease`, which is arbitrary YAML.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from xml.parsers import expat

from . import deploy_flux

# Directories that hold copies of other repositories' files rather than this one's
# own. `.terraform` is Terraform's download cache: it holds the upstream modules,
# whose sources would read as this repository's dependencies. `node_modules` and
# `target` are the npm and Maven equivalents. Pruned during the walk, never entered,
# because `node_modules` alone can hold more files than the rest of a clone.
IGNORED_DIRS = frozenset({".git", ".terraform", "node_modules", "target"})


@dataclass(frozen=True)
class Reference:
    """One declaration that one repository makes about something it uses.

    `target` is a repository name, chart name, `group:artifact`, package name or
    registry address, by kind. `ref` is the version or pin as written, `""` when
    the declaration gives none (floating). `text` is the source line unmasked: it
    can carry a credential, and masking belongs where it is written out.
    `resolved_repo` is set only when the declaration names a repository outright,
    as a Terraform GitHub source does. `source` is a Flux `HelmRelease`'s
    `sourceRef` name, which the chart resolver falls back on when no clone defines
    the chart.
    """

    kind: str
    target: str
    chart: str
    ref: str
    rel: str
    line: int
    text: str
    resolved_repo: str | None = None
    source: str = ""


@dataclass(frozen=True)
class Kind:
    """A registered way of declaring a dependency between repositories.

    `references(clone, rel, text)` reads one file matching `globs` (`rel` is its
    path relative to `clone`, `text` its contents). `publishes(clone)` returns
    `(target key, declaring path)` for what the clone offers to others.
    """

    name: str
    origin: str
    globs: tuple[str, ...]
    references: Callable[[Path, str, str], list[Reference]]
    publishes: Callable[[Path], list[tuple[str, str]]]


def files_of(clone: Path, kind: Kind) -> list[str]:
    """Repo-relative paths in the clone that the kind's globs select, sorted.

    `fnmatch` has no `**`, so `**/x` also matches `x` at the root, which is what the
    glob means everywhere else.
    """
    found = []
    for directory, subdirs, names in os.walk(clone):
        subdirs[:] = [d for d in subdirs if d not in IGNORED_DIRS]
        for name in names:
            rel = (Path(directory) / name).relative_to(clone).as_posix()
            if any(_matches(rel, glob) for glob in kind.globs):
                found.append(rel)
    return sorted(found)


def _matches(rel: str, glob: str) -> bool:
    if fnmatch.fnmatchcase(rel, glob):
        return True
    return glob.startswith("**/") and fnmatch.fnmatchcase(rel, glob[3:])


def read_text(path: Path) -> str:
    """File contents, or "" when unreadable: one bad file is not worth abandoning a
    scan for."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def scan(clone: Path, kind: Kind) -> list[Reference]:
    """Every reference the kind finds in the clone, files in path order.

    A parser's own `ValueError` (a POM that will not parse) propagates: the caller
    decides whether that is an unreadable file to count or a failure to stop on.
    """
    found: list[Reference] = []
    for rel in files_of(clone, kind):
        found.extend(kind.references(clone, rel, read_text(clone / rel)))
    return found


def _line_of(text: str, offset: int) -> tuple[int, str]:
    """1-based line number, and that line stripped, for a character offset."""
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return text.count("\n", 0, offset) + 1, text[start : len(text) if end < 0 else end].strip()


# --- Terraform ----------------------------------------------------------------

# An infrastructure estate's shared dependencies are declared in module sources
# rather than a language manifest. Unlike an npm package, the reference names the
# providing repository outright, so no manifest lookup is needed. All five forms
# are in use and none dominates by design: on one estate the scp-style `git@` form
# outnumbered `git::https://` five to one, so handling only the documented form
# would have missed most of the reuse.
_TF_SOURCE = re.compile(
    r"""source\s*=\s*"
        (?:git::)?
        # The scheme is optional in Terraform: its GitHub detector accepts a bare
        # `github.com/org/repo` and rewrites it to HTTPS itself. Requiring one made
        # 5 shared modules invisible on a 361-repository estate (#125). The opening
        # quote anchors this, so `notgithub.com/...` cannot match.
        (?:https://|git@|ssh://git@)?github\.com[/:]
        (?P<org>[\w.-]+)/(?P<repo>[\w.-]+?)
        (?:\.git)?(?P<tail>[?/][^"]*)?"
    """,
    re.VERBOSE,
)
_TF_ANY_SOURCE = re.compile(r'source\s*=\s*"(?P<address>[^"]*)"')
_LOCAL_SOURCE = ("./", "../", "/")


def _ref_of(tail: str | None) -> str:
    """The `ref=` argument of a source's query string, `""` when there is none.

    Split by hand rather than with `parse_qs`, which turns `+` into a space and so
    changes a tag such as `v1+build`.
    """
    for part in (tail or "").partition("?")[2].split("&"):
        key, _, value = part.partition("=")
        if key == "ref":
            return value
    return ""


def terraform_sources(text: str) -> list[tuple[str, str, str, int, str]]:
    """`(org, repo, ref, line, linetext)` for each GitHub-hosted module source.

    Only GitHub sources: a registry or local path names no repository, so naming
    one would be a guess. `ref` is `""` when the source is unpinned.
    """
    found = []
    for match in _TF_SOURCE.finditer(text):
        line, linetext = _line_of(text, match.start())
        ref = _ref_of(match.group("tail"))
        found.append((match.group("org"), match.group("repo"), ref, line, linetext))
    return found


def _terraform_references(_clone: Path, rel: str, text: str) -> list[Reference]:
    found = []
    github_at = {match.start() for match in _TF_SOURCE.finditer(text)}
    # The organisation is not carried: the layer decides whether it is the estate's.
    for _org, repo, ref, line, linetext in terraform_sources(text):
        found.append(Reference("terraform_module", repo, "", ref, rel, line, linetext, repo))
    for match in _TF_ANY_SOURCE.finditer(text):
        address = match.group("address")
        if match.start() in github_at or not address or address.startswith(_LOCAL_SOURCE):
            continue
        line, linetext = _line_of(text, match.start())
        found.append(Reference("terraform_module", address, "", "", rel, line, linetext))
    return sorted(found, key=lambda ref: ref.line)


# --- Helm ---------------------------------------------------------------------


def _comment_free(line: str) -> str:
    """The line without a trailing YAML comment. `#` opens one only after
    whitespace and outside quotes, so a `#` inside a URL is kept."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == "\\" and quote == '"':
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'" and line[:i].rstrip()[-1:] in ("", ":", "-", ",", "[", "{"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _scalar(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _flow(text: str, pos: int = 0):
    """A flow-style value (`[a, b]`, `{k: v}` or a scalar) from `pos`, with the
    position after it. `IndexError` means the text ended inside it."""
    pos = _skip(text, pos)
    if text[pos] == "[":
        items, pos = [], pos + 1
        while text[_skip(text, pos)] != "]":
            value, pos = _flow(text, pos)
            items.append(value)
            pos = _skip(text, pos)
            pos += text[pos] == ","
        return items, _skip(text, pos) + 1
    if text[pos] == "{":
        mapping, pos = {}, pos + 1
        while text[_skip(text, pos)] != "}":
            key, pos = _flow_scalar(text, pos, ":")
            value, pos = _flow(text, pos + 1)
            mapping[key] = value
            pos = _skip(text, pos)
            pos += text[pos] == ","
        return mapping, _skip(text, pos) + 1
    return _flow_scalar(text, pos, ",]}")


def _skip(text: str, pos: int) -> int:
    while text[pos] in " \t":
        pos += 1
    return pos


def _flow_scalar(text: str, pos: int, stops: str) -> tuple[str, int]:
    pos = _skip(text, pos)
    if text[pos] in "\"'":
        end = text.index(text[pos], pos + 1)
        return text[pos + 1 : end], _skip(text, end + 1)
    end = pos
    while end < len(text) and text[end] not in stops:
        end += 1
    if end >= len(text):
        raise IndexError("ended inside a flow value")
    return text[pos:end].strip(), end


def _flow_complete(first: str, rest: list[str]):
    """Parse a flow value that may continue over following lines."""
    text = first
    for more in [*rest, None]:
        try:
            return _flow(text + " ")[0]
        except (IndexError, ValueError):
            if more is None:
                return []
            text += " " + more.strip()
    return []


_DEP_KEYS = ("name", "version", "repository")


def _keep(mapping: dict) -> dict:
    return {key: str(mapping[key]) for key in _DEP_KEYS if key in mapping}


def _block_dependencies(block: list[tuple[int, str]]) -> list[dict]:
    """The items of a block sequence of mappings, reading `name`, `version` and
    `repository` at the item's own key indent (nested values are not these)."""
    items: list[dict] = []
    dash_indent = key_indent = -1
    for indent, content in block:
        if content == "-" or content.startswith("- "):
            if dash_indent < 0 or indent <= dash_indent:
                dash_indent = indent
                body = content[1:].lstrip()
                key_indent = indent + len(content) - len(body)
                items.append({})
                if body.startswith("{"):
                    flowed = _flow_complete(body, [])
                    items[-1].update(flowed if isinstance(flowed, dict) else {})
                    continue
                content, indent = body, key_indent
            else:
                continue
        if items and indent == key_indent and ":" in content:
            key, _, value = content.partition(":")
            if key.strip() in _DEP_KEYS and value.strip():
                items[-1][key.strip()] = _scalar(value)
    return [_keep(item) for item in items if item]


def chart_yaml(text: str) -> tuple[str, list[dict]]:
    """A chart's own `name` and its `dependencies` (`name`, `version`,
    `repository`), read without a YAML library.

    Covers what a `Chart.yaml` holds - block or flow `dependencies`, comments,
    quoted values - and is held to PyYAML's output by a test. Multi-line scalars
    are not read; a chart that needs them is a chart this reader is wrong about, and
    that test is what says so.
    """
    lines = [
        (len(raw) - len(raw.lstrip(" ")), content)
        for raw in text.splitlines()
        if (content := _comment_free(raw).strip())
    ]
    name, deps = "", []
    i = 0
    while i < len(lines):
        indent, content = lines[i]
        i += 1
        if indent or content.startswith("-"):
            continue
        key, _, value = content.partition(":")
        value = value.strip()
        if key == "name" and value:
            name = _scalar(value)
        elif key == "dependencies" and value.startswith("["):
            tail = [c for _, c in lines[i:]]
            flowed = _flow_complete(value, tail)
            deps = [_keep(d) for d in flowed if isinstance(d, dict)]
        elif key == "dependencies":
            block = []
            while i < len(lines) and (lines[i][0] or lines[i][1].startswith("-")):
                block.append(lines[i])
                i += 1
            deps = _block_dependencies(block)
    return name, deps


def _first_line(lines: list[str], pattern: re.Pattern, start: int) -> int:
    for index in range(start, len(lines)):
        if pattern.search(lines[index]):
            return index
    return start


def _chart_dependencies(rel: str, text: str) -> list[Reference]:
    _, deps = chart_yaml(text)
    lines = text.splitlines()
    found, at = [], 0
    for dep in deps:
        name = dep.get("name", "")
        if not name:
            continue
        pattern = re.compile(rf"\bname:\s*([\"']?){re.escape(name)}\1\s*(?:[,}}#]|$)")
        at = _first_line(lines, pattern, at)
        found.append(
            Reference(
                "helm_chart", name, name, dep.get("version", ""), rel, at + 1, lines[at].strip()
            )
        )
    return found


def _helm_releases(rel: str, text: str) -> list[Reference]:
    """Flux `HelmRelease`s in a YAML file, through the one reader of them. Without
    PyYAML there is nothing to read them with, and a file that is not YAML (a Helm
    template full of `{{ }}`) is not a release."""
    if "HelmRelease" not in text:
        return []
    try:
        import yaml
    except ImportError:
        return []
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return []
    lines = text.splitlines()
    found, at = [], 0
    for document in documents:
        if not deploy_flux.is_helm_release(document):
            continue
        chart = deploy_flux.chart_of(document)
        if chart is None or not chart.name:
            continue
        pattern = re.compile(rf"\bchart:\s*([\"']?){re.escape(chart.name)}\1\s*(?:#|$)")
        at = _first_line(lines, pattern, at)
        found.append(
            Reference(
                "helm_chart",
                chart.name,
                chart.name,
                chart.version,
                rel,
                at + 1,
                lines[at].strip() if lines else "",
                None,
                chart.source,
            )
        )
    return found


def _helm_references(_clone: Path, rel: str, text: str) -> list[Reference]:
    if rel.rsplit("/", 1)[-1] == "Chart.yaml":
        return _chart_dependencies(rel, text)
    return _helm_releases(rel, text)


def _helm_publishes(clone: Path) -> list[tuple[str, str]]:
    found = []
    for rel in files_of(clone, _CHART_FILES):
        name, _ = chart_yaml(read_text(clone / rel))
        if name:
            found.append((name, rel))
    return found


# --- Maven --------------------------------------------------------------------


@dataclass
class Pom:
    """What one `pom.xml` declares. `group` and `version` fall back on the parent's
    when the POM omits them, as Maven does, so a child publishes a coordinate."""

    group: str = ""
    artifact: str = ""
    version: str = ""
    parent: tuple | None = None
    properties: dict = field(default_factory=dict)
    dependencies: list = field(default_factory=list)


class _Node:
    __slots__ = ("tag", "line", "text", "children")

    def __init__(self, tag: str, line: int):
        self.tag, self.line, self.text, self.children = tag, line, [], []

    def child(self, tag: str) -> _Node | None:
        return next((c for c in self.children if c.tag == tag), None)

    def kids(self, tag: str) -> list[_Node]:
        return [c for c in self.children if c.tag == tag]

    def value(self, tag: str) -> str:
        node = self.child(tag)
        return "".join(node.text).strip() if node else ""


def _parse_xml(text: str) -> _Node:
    """The document as a tree of local names with line numbers, which the stdlib
    tree builder does not keep. Namespaces are not processed: a tag's prefix is
    dropped, so `<project xmlns=...>` and a bare `<project>` read the same."""
    parser = expat.ParserCreate()
    root = _Node("", 0)
    stack = [root]

    def start(tag: str, _attrs) -> None:
        node = _Node(tag.rsplit(":", 1)[-1], parser.CurrentLineNumber)
        stack[-1].children.append(node)
        stack.append(node)

    parser.StartElementHandler = start
    parser.EndElementHandler = lambda _tag: stack.pop()
    parser.CharacterDataHandler = lambda data: stack[-1].text.append(data)
    try:
        parser.Parse(text.encode("utf-8"), True)
    except expat.ExpatError as error:
        raise ValueError(f"not well-formed XML: {error}") from error
    project = root.child("project")
    if project is None:
        raise ValueError("no <project> element")
    return project


_EXPRESSION = re.compile(r"\$\{([^}]+)\}")


def _resolve(value: str, scope: dict) -> str:
    """`${name}` replaced from `scope`, repeatedly (a property may name another),
    and left literal when unknown or circular - a bounded number of passes."""
    for _ in range(8):
        replaced = _EXPRESSION.sub(lambda m: scope.get(m.group(1), m.group(0)), value)
        if replaced == value:
            break
        value = replaced
    return value


def pom(text: str) -> Pom:
    """Coordinates, parent, `<properties>` and dependencies of one POM.

    Read: `<parent>`, and `<dependencies>/<dependency>` under `<project>` and under
    `<profiles>/<profile>`. Not read: `<dependencyManagement>`, which declares a
    version without using the artifact, and `<build>/<plugins>`, which is deferred.
    `${project.groupId}`, `${project.version}`, `${project.parent.version}` and
    `<properties>` resolve within this POM; an unknown expression stays literal so
    it reads as pinned-by-property rather than vanishing. Raises `ValueError` for
    a file that is not a POM.
    """
    project = _parse_xml(text)
    parent_node = project.child("parent")
    parent = (
        (
            parent_node.value("groupId"),
            parent_node.value("artifactId"),
            parent_node.value("version"),
        )
        if parent_node
        else None
    )
    group = project.value("groupId") or (parent[0] if parent else "")
    version = project.value("version") or (parent[2] if parent else "")
    scope = {"project.groupId": group, "project.version": version}
    if parent:
        scope["project.parent.groupId"] = parent[0]
        scope["project.parent.version"] = parent[2]
    properties = {}
    props_node = project.child("properties")
    for node in props_node.children if props_node else []:
        properties[node.tag] = "".join(node.text).strip()
    for _ in range(8):
        properties = {k: _resolve(v, {**properties, **scope}) for k, v in properties.items()}
    scope = {**properties, **scope}
    result = Pom(
        _resolve(group, scope),
        project.value("artifactId"),
        _resolve(version, scope),
        parent,
        properties,
    )
    holders = [project]
    for profiles in project.kids("profiles"):
        holders.extend(profiles.kids("profile"))
    for holder in holders:
        for dependencies in holder.kids("dependencies"):
            for dependency in dependencies.kids("dependency"):
                artifact = dependency.child("artifactId")
                dep_group = _resolve(dependency.value("groupId"), scope)
                dep_artifact = _resolve(dependency.value("artifactId"), scope)
                if not (dep_group and dep_artifact and artifact):
                    continue
                dep_version = _resolve(dependency.value("version"), scope)
                result.dependencies.append((dep_group, dep_artifact, dep_version, artifact.line))
    return result


def _maven_references(_clone: Path, rel: str, text: str) -> list[Reference]:
    lines = text.splitlines()
    return [
        Reference(
            "maven_artifact", f"{group}:{artifact}", "", version, rel, line, lines[line - 1].strip()
        )
        for group, artifact, version, line in pom(text).dependencies
    ]


def _maven_publishes(clone: Path) -> list[tuple[str, str]]:
    found = []
    for rel in files_of(clone, _POM_FILES):
        try:
            declared = pom(read_text(clone / rel))
        except ValueError:
            continue  # reported as unreadable by the reference pass over the same file
        if declared.group and declared.artifact:
            found.append((f"{declared.group}:{declared.artifact}", rel))
    return found


# --- npm ----------------------------------------------------------------------

NPM_SECTIONS = ("dependencies", "devDependencies", "peerDependencies")


def _npm_references(_clone: Path, rel: str, text: str) -> list[Reference]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    lines = text.splitlines()
    found = []
    for section in NPM_SECTIONS:
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        at = _first_line(lines, re.compile(rf'"{section}"\s*:'), 0)
        for name, spec in block.items():
            at = _first_line(lines, re.compile(rf"{re.escape(json.dumps(name))}\s*:"), at)
            found.append(
                Reference("npm_package", name, "", str(spec), rel, at + 1, lines[at].strip())
            )
    return found


def _npm_publishes(clone: Path) -> list[tuple[str, str]]:
    # The same locations the `packages` stage reads: the root and the workspace
    # directories. Imported here because that module imports this one.
    from .build_package_edges import _named_packages

    return _named_packages(clone)


_CHART_FILES = Kind("", "", ("**/Chart.yaml",), lambda *_: [], lambda _: [])
_POM_FILES = Kind("", "", ("**/pom.xml",), lambda *_: [], lambda _: [])

BUILTIN: tuple[Kind, ...] = (
    Kind("terraform_module", "builtin", ("**/*.tf",), _terraform_references, lambda _clone: []),
    Kind(
        "helm_chart",
        "builtin",
        ("**/Chart.yaml", "**/*.yaml", "**/*.yml"),
        _helm_references,
        _helm_publishes,
    ),
    Kind("maven_artifact", "builtin", ("**/pom.xml",), _maven_references, _maven_publishes),
    Kind("npm_package", "builtin", ("**/package.json",), _npm_references, _npm_publishes),
)
