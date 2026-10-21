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
import importlib.util
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Set
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from xml.parsers import expat

from . import deploy_flux, io, secret_mask

if TYPE_CHECKING:
    from .source_urls import SourceUrls

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
    the chart. `org` is the organisation a GitHub source names, which the layer
    checks against the estate's.
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
    org: str = ""

    def __post_init__(self) -> None:
        # A URL in a declaration can carry `user:token@`. The fields are what the
        # layer writes (a target becomes a key of the unresolved report), so the
        # credential is dropped here, once, for every kind including a store's.
        for name in ("target", "chart", "ref", "text", "source"):
            object.__setattr__(self, name, strip_userinfo(getattr(self, name)))


def _no_skips(_clone: Path, _rel: str, _text: str) -> dict[str, int]:
    return {}


@dataclass(frozen=True)
class Kind:
    """A registered way of declaring a dependency between repositories.

    `references(clone, rel, text)` reads one file matching `globs` (`rel` is its
    path relative to `clone`, `text` its contents); a `ValueError` from it means the
    file is unreadable, and the layer counts it rather than stopping. `publishes(clone)`
    returns `(target key, declaring path)` for what the clone offers to others.
    `skips(clone, rel, text)` counts, by reason, declarations in the file that are
    deliberately not references (a local module path), so the layer can say how
    many it set aside.
    """

    name: str
    origin: str
    globs: tuple[str, ...]
    references: Callable[[Path, str, str], list[Reference]]
    publishes: Callable[[Path], list[tuple[str, str]]]
    skips: Callable[[Path, str, str], dict[str, int]] = _no_skips


_USERINFO = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<userinfo>[^/@\s\"']+)@")
_WEB_SCHEMES = ("http", "https")


def _drop_credential(match: re.Match) -> str:
    userinfo = match.group("userinfo")
    # `user:pass@` is a credential in any scheme. A bare `token@` is one on http(s),
    # where it is a token; on ssh it is the login name (`git@host`), which every
    # such URL carries and which is not a secret.
    if ":" in userinfo or match.group("scheme").split("+")[-1] in _WEB_SCHEMES:
        return f"{match.group('scheme')}://"
    return match.group(0)


def strip_userinfo(text: str) -> str:
    """`text` without the credential of any URL in it, keeping an ssh login name."""
    return _USERINFO.sub(_drop_credential, text)


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
    scan for. The layer reads with `read_text_or_raise` instead, so that it can say
    which files it could not read rather than reading them as empty."""
    try:
        return read_text_or_raise(path)
    except OSError:
        return ""


def read_text_or_raise(path: Path) -> str:
    """File contents, decoding errors replaced; an `OSError` propagates."""
    return path.read_text(encoding="utf-8", errors="replace")


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
_HEREDOC = re.compile(r"<<-?(?P<tag>[A-Za-z_]\w*)")
_PIN_PARAMS = ("ref", "depth")
_MODULE_BLOCK = re.compile(r'(?<![\w-])module\s+"[^"]*"\s*\{')


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


def _module_source_offsets(text: str) -> set[int]:
    """Offsets of the `source = "..."` attributes that sit directly inside a
    `module "<name>" { ... }` block.

    `source` is also a file argument (`aws_s3_object`), a provider address
    (`required_providers`) and a suffix of other attribute names, and none of those
    is a module dependency. A brace-depth scan, skipping strings and comments, so a
    commented-out source and a nested map's `source` are not read either.
    """
    found: set[int] = set()
    for block in _MODULE_BLOCK.finditer(text):
        i, depth = block.end(), 1
        while i < len(text) and depth:
            ch = text[i]
            if ch == '"':
                i = _string_end(text, i)
                continue
            heredoc = _HEREDOC.match(text, i)
            if heredoc:
                i = _heredoc_end(text, heredoc)
                continue
            if ch == "#" or text.startswith("//", i):
                i = text.find("\n", i)
                i = len(text) if i < 0 else i
                continue
            if text.startswith("/*", i):
                i = text.find("*/", i)
                i = len(text) if i < 0 else i + 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif depth == 1 and text.startswith("source", i) and not _word_before(text, i):
                match = _TF_ANY_SOURCE.match(text, i)
                if match:
                    found.add(match.start())
                    i = match.end()
                    continue
            i += 1
    return found


def _string_end(text: str, start: int) -> int:
    """The offset after the string opening at `start`. An interpolation (`${...}`,
    `%{...}`) can hold strings and braces of its own, so it is skipped as a unit."""
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == '"':
            return i + 1
        elif text.startswith(("$${", "%%{"), i):
            i += 3  # an escaped literal, not an interpolation
        elif text.startswith(("${", "%{"), i):
            i = _interpolation_end(text, i + 2)
        else:
            i += 1
    return i


def _interpolation_end(text: str, i: int) -> int:
    depth = 1
    while i < len(text) and depth:
        if text[i] == '"':
            i = _string_end(text, i)
            continue
        depth += (text[i] == "{") - (text[i] == "}")
        i += 1
    return i


def _heredoc_end(text: str, opening: re.Match) -> int:
    """The offset after the terminator line of a heredoc, or the end of the text.
    The body is free text: braces and quotes in it are not structure."""
    tag = opening.group("tag")
    offset = text.find("\n", opening.end())
    while offset >= 0:
        start = offset + 1
        offset = text.find("\n", start)
        line = text[start : len(text) if offset < 0 else offset]
        if line.strip() == tag:
            return len(text) if offset < 0 else offset
    return len(text)


def _word_before(text: str, i: int) -> bool:
    return i > 0 and (text[i - 1].isalnum() or text[i - 1] in "_-")


def _terraform_references(_clone: Path, rel: str, text: str) -> list[Reference]:
    """Every module source in the file: a GitHub one names its repository outright,
    any other is an address the estate may not hold.

    Read only from a module block's own `source` (`_module_source_offsets`), and
    never when built by interpolation, which no reader can resolve. A credential in
    the URL is dropped before the GitHub form is matched, so `https://user:token@`
    hides neither the repository nor anything else - the graph reads this function
    too, so the two cannot disagree about which repository a source names.
    """
    found = []
    in_module = _module_source_offsets(text)
    for match in _TF_ANY_SOURCE.finditer(text):
        address = match.group("address")
        if (
            match.start() not in in_module
            or not address
            or "${" in address
            or address.startswith(_LOCAL_SOURCE)
        ):
            continue
        line, linetext = _line_of(text, match.start())
        github = _TF_SOURCE.match(strip_userinfo(match.group(0)))
        # The organisation is carried, not checked: the layer decides whether it is
        # the estate's, because only the layer knows the estate.
        if github:
            found.append(
                Reference(
                    kind="terraform_module",
                    target=github.group("repo"),
                    chart="",
                    ref=_ref_of(github.group("tail")),
                    rel=rel,
                    line=line,
                    text=linetext,
                    resolved_repo=github.group("repo"),
                    org=github.group("org"),
                )
            )
            continue
        # A pin is not part of what is referenced, so two pins of one module are one
        # target. Any other query parameter selects something and stays.
        base, _, query = address.partition("?")
        kept = [p for p in query.split("&") if p and p.partition("=")[0] not in _PIN_PARAMS]
        found.append(
            Reference(
                kind="terraform_module",
                target=base + ("?" + "&".join(kept) if kept else ""),
                chart="",
                ref=_ref_of(address),
                rel=rel,
                line=line,
                text=linetext,
            )
        )
    return found


def _terraform_skips(_clone: Path, _rel: str, text: str) -> dict[str, int]:
    """Module sources inside the repository: a local path, or one built by
    interpolation (`${path.module}/...`), which no prefix can tell from local."""
    module_sources = _module_source_offsets(text)
    local = sum(
        1
        for match in _TF_ANY_SOURCE.finditer(text)
        if match.start() in module_sources
        and (match.group("address").startswith(_LOCAL_SOURCE) or "${" in match.group("address"))
    )
    return {"local": local} if local else {}


# --- Helm ---------------------------------------------------------------------


def _comment_free(line: str) -> str:
    """The line without a trailing YAML comment. `#` opens one only after
    whitespace and outside quotes, so a `#` inside a URL is kept."""
    quote = ""
    skip = False
    for i, ch in enumerate(line):
        if skip:
            skip = False
        elif quote:
            if ch == "\\" and quote == '"':
                skip = True
            elif ch == quote and quote == "'" and line[i + 1 : i + 2] == "'":
                skip = True  # `''` is an escaped quote inside a single-quoted scalar
            elif ch == quote:
                quote = ""
        elif ch in "\"'" and line[:i].rstrip()[-1:] in ("", ":", "-", ",", "[", "{"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _scalar(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        if text[0] == "'":
            return inner.replace("''", "'")
        return re.sub(r"\\([\"\\])", r"\1", inner)
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
    quoted values - and is held to PyYAML's output by a test. Quoted scalars
    unescape `\\"`, `\\\\` and `''` only; other escapes (`\\n`, `\\u...`) and multi-line
    scalars are not read; a chart that needs them is a chart this reader is wrong about, and
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


def _locate(lines: list[str], pattern: re.Pattern, at: int) -> tuple[str, int, int]:
    """`(line text, 1-based line number, next search start)` for the first line at or
    after `at` matching `pattern`.

    The next search starts after the hit, so two entries of one name (a chart
    dependency with an `alias`) get their own lines. A flow list can hold two on one
    line, so a miss is retried from the previous hit's line, and a second miss keeps
    the start rather than inventing a position.
    """
    for first in (at, max(at - 1, 0)):
        for index in range(first, len(lines)):
            if pattern.search(lines[index]):
                return lines[index].strip(), index + 1, index + 1
    index = min(at, len(lines) - 1)
    return (lines[index].strip(), index + 1, at) if lines else ("", at + 1, at)


def _chart_dependencies(rel: str, text: str) -> list[Reference]:
    _, deps = chart_yaml(text)
    lines = text.splitlines()
    found, at = [], 0
    for dep in deps:
        name = dep.get("name", "")
        if not name:
            continue
        pattern = re.compile(rf"\bname:\s*([\"']?){re.escape(name)}\1\s*(?:[,}}#]|$)")
        linetext, line, at = _locate(lines, pattern, at)
        found.append(
            Reference(
                kind="helm_chart",
                target=name,
                chart=name,
                ref=dep.get("version", ""),
                rel=rel,
                line=line,
                text=linetext,
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
        linetext, line, at = _locate(lines, pattern, at)
        found.append(
            Reference(
                kind="helm_chart",
                target=chart.name,
                chart=chart.name,
                ref=chart.version,
                rel=rel,
                line=line,
                text=linetext,
                source=chart.source,
            )
        )
    return found


def vendored(clone: Path, rel: str) -> bool:
    """Whether `rel` sits under a chart's own `charts/` directory.

    That directory holds the chart's dependencies, unpacked by `helm dependency
    build` or committed by hand. A `Chart.yaml` there is a copy of another
    repository's chart: read as this repository's, every consumer that vendors a
    dependency would publish it (a conflict with its owner) and declare its
    dependencies. A `charts/<name>/` directory with no `Chart.yaml` above it is a
    chart repository's own layout, and is not vendored.
    """
    parts = rel.split("/")
    return any(
        part == "charts" and clone.joinpath(*parts[:index], "Chart.yaml").is_file()
        for index, part in enumerate(parts[:-2])
    )


def _helm_references(clone: Path, rel: str, text: str) -> list[Reference]:
    if vendored(clone, rel):
        return []
    if rel.rsplit("/", 1)[-1] == "Chart.yaml":
        return _chart_dependencies(rel, text)
    return _helm_releases(rel, text)


def _helm_publishes(clone: Path) -> list[tuple[str, str]]:
    found = []
    for rel in files_of(clone, _CHART_FILES):
        if vendored(clone, rel):
            continue
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
            kind="maven_artifact",
            target=f"{group}:{artifact}",
            chart="",
            ref=version,
            rel=rel,
            line=line,
            text=lines[line - 1].strip(),
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

NPM_MANIFEST = "package.json"
NPM_SECTIONS = ("dependencies", "devDependencies", "peerDependencies")
# Where a clone publishes packages from: the root, and the workspace directories.
NPM_WORKSPACES = ("projects", "packages", "libs")


def package_json(text: str) -> dict:
    """A `package.json` as a mapping, `{}` when it is not a JSON object."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def npm_dependencies(manifest: dict, sections: tuple[str, ...] = NPM_SECTIONS) -> dict[str, dict]:
    """`{section: {package: version spec}}` for each of `sections` the manifest
    declares as an object, in the order asked for. The one reader of a manifest's
    dependencies: the graph's `imports_package` edges and the ingestion-gaps report
    ask for fewer sections than the layer, and filter here rather than re-read."""
    return {
        section: manifest[section]
        for section in sections
        if isinstance(manifest.get(section), dict)
    }


def _npm_references(_clone: Path, rel: str, text: str) -> list[Reference]:
    lines = text.splitlines()
    found = []
    for section, block in npm_dependencies(package_json(text)).items():
        _, _, at = _locate(lines, re.compile(rf'"{section}"\s*:'), 0)
        for name, spec in block.items():
            key = re.escape(json.dumps(name, ensure_ascii=False))
            linetext, line, at = _locate(lines, re.compile(rf"{key}\s*:"), at)
            found.append(
                Reference(
                    kind="npm_package",
                    target=name,
                    chart="",
                    ref=str(spec),
                    rel=rel,
                    line=line,
                    text=linetext,
                )
            )
    return found


def npm_publishes(clone: Path) -> list[tuple[str, str]]:
    """(package name, repo-relative `package.json` path) for each package the clone
    names, root first and then each workspace directory's packages in path order."""
    found = []
    candidates = [clone / NPM_MANIFEST]
    for sub in NPM_WORKSPACES:
        candidates.extend(sorted((clone / sub).glob(f"*/{NPM_MANIFEST}")))
    for manifest in candidates:
        if not manifest.is_file():
            continue
        try:
            name = package_json(manifest.read_text(encoding="utf-8")).get("name")
        except UnicodeDecodeError:
            continue
        if name:
            found.append((str(name), str(manifest.relative_to(clone))))
    return found


_CHART_FILES = Kind("", "", ("**/Chart.yaml",), lambda *_: [], lambda _: [])
_POM_FILES = Kind("", "", ("**/pom.xml",), lambda *_: [], lambda _: [])

BUILTIN: tuple[Kind, ...] = (
    Kind(
        "terraform_module",
        "builtin",
        ("**/*.tf",),
        _terraform_references,
        lambda _clone: [],
        _terraform_skips,
    ),
    Kind(
        "helm_chart",
        "builtin",
        ("**/Chart.yaml", "**/*.yaml", "**/*.yml"),
        _helm_references,
        _helm_publishes,
    ),
    Kind("maven_artifact", "builtin", ("**/pom.xml",), _maven_references, _maven_publishes),
    Kind("npm_package", "builtin", ("**/package.json",), _npm_references, npm_publishes),
)


# --- the layer ----------------------------------------------------------------
#
# Every kind's references, resolved to a repository in the estate or reported as
# outside it, aggregated per (consumer, provider, chart). `chart` is "" for every
# kind but `helm_chart`: one repository of generic charts would otherwise read as a
# dependency of nearly everything, with no way to ask which chart.

FORMAT = {"major": 1, "minor": 0}
FLUX_UNREAD = "not read: PyYAML is not installed"


class KindError(Exception):
    """A kind's parser raised something other than "this file is unreadable". The
    stage stops, naming the kind and the file, before anything is written."""


@dataclass
class Layer:
    """What `write` puts on disk, built entirely in memory first.

    `dependencies` is the `repository-dependencies.json` body, `by_repo` each
    `by-repo/<repo>.json` body, `kinds` the `kinds.json` body. `conflicts` is
    `{kind: {target: [repositories]}}`, the first of which was chosen; `skipped` is
    `{kind: {reason: count}}`; `unreadable` names each file a parser could not read,
    store-relative.
    """

    dependencies: dict
    by_repo: dict[str, dict]
    kinds: dict
    conflicts: dict
    skipped: dict
    unreadable: list[str]


def builtin(name: str) -> Kind:
    """The built-in kind called `name`."""
    return next(kind for kind in BUILTIN if kind.name == name)


def names_estate_repo(ref: Reference, estate: Set[str], org: str) -> bool:
    """Whether a reference that names its repository outright names one the estate
    holds: a clone of that name, and in the estate's organisation when one is set.
    The organisation compares ignoring case, as GitHub does. A same-named repository
    in another organisation is somebody else's."""
    return ref.resolved_repo in estate and (not org or ref.org.lower() == org.lower())


def resolve_chart(
    chart: str, source: str, estate: Set[str], pattern: str, defined: dict[str, str]
) -> str | None:
    """The repository providing `chart`, or None when the estate holds none.

    In order: the pattern's repository when the pattern is set and names a clone;
    the clone `defined` chose for the chart (a non-umbrella definition over an
    umbrella's, first by name); and, for a Flux `HelmRelease`, a `sourceRef` that
    names a clone - the graph's `uses_chart` rule, kept so the two agree.
    """
    if pattern:
        candidate = pattern.format(name=chart)
        if candidate in estate:
            return candidate
    if chart in defined:
        return defined[chart]
    if source and source in estate:
        return source
    return None


@dataclass
class _Edge:
    references: int = 0
    kinds: Counter = field(default_factory=Counter)
    refs: Counter = field(default_factory=Counter)
    examples: list = field(default_factory=list)


@dataclass
class _Unresolved:
    references: int = 0
    referenced_by: set = field(default_factory=set)


class _Resolver:
    """Decides, for one reference, whether it is an edge, a self-reference or a
    target outside the estate."""

    def __init__(
        self,
        estate: frozenset[str],
        org: str,
        pattern: str,
        umbrellas: Set[str],
        publishers: dict[str, dict[str, list[str]]],
    ) -> None:
        self.estate = estate
        self.org = org.lower()
        self.pattern = pattern
        self.publishers = publishers
        self.chosen: dict[str, dict[str, str]] = {}
        self.conflicts: dict[str, dict[str, list[str]]] = {}
        for kind, published in publishers.items():
            chosen = self.chosen.setdefault(kind, {})
            for target, repos in sorted(published.items()):
                # Only the chart resolver knows umbrellas: an umbrella bundles other
                # repositories' charts, and is the owner of none of them.
                own = [r for r in repos if r not in umbrellas] if kind == "helm_chart" else repos
                candidates = own or repos
                chosen[target] = candidates[0]
                if len(candidates) > 1:
                    # A key of a committed file, so masked like every other one.
                    self.conflicts.setdefault(kind, {})[secret_mask.mask(target)] = candidates

    def resolve(self, consumer: str, ref: Reference) -> tuple[str, str]:
        """`("edge", provider)`, `("self", "")` or `("unresolved", target key)`."""
        if ref.resolved_repo is not None:
            if not names_estate_repo(ref, self.estate, self.org):
                return "unresolved", f"{ref.org}/{ref.target}" if ref.org else ref.target
            provider = ref.resolved_repo
        elif ref.kind == "helm_chart":
            provider = resolve_chart(
                ref.target, ref.source, self.estate, self.pattern, self.chosen.get(ref.kind, {})
            )
        else:
            provider = self.chosen.get(ref.kind, {}).get(ref.target)
        if provider is None:
            return "unresolved", ref.target
        # `self` is where resolution landed, never what the consumer happens to
        # publish: an umbrella that also defines a chart another clone owns depends
        # on that clone, and a conflict another clone won is a dependency on it.
        if provider == consumer:
            return "self", ""
        return "edge", provider


def _publishers(clones: list[Path], kind: Kind) -> dict[str, list[str]]:
    found: dict[str, set[str]] = {}
    for clone in clones:
        try:
            published = kind.publishes(clone)
        except Exception as error:
            raise KindError(
                f"{kind.name}: publishes() raised on {clone.name}: {error!r}"
            ) from error
        for target, _rel in published:
            found.setdefault(target, set()).add(clone.name)
    return {target: sorted(repos) for target, repos in found.items()}


def _store_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _read_extra(extra_dir: Path, repo: str) -> dict:
    path = extra_dir / f"{repo}.json"
    if not path.is_file():
        return {}
    try:
        extra = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{path}: not readable as JSON: {error}") from error
    if not isinstance(extra, dict):
        raise ValueError(f"{path}: `extra` must be a JSON object")
    return extra


def _edge_body(edge: _Edge, consumer: str, chart: str, examples: int, urls) -> dict:
    shown = sorted(edge.examples)[: max(examples, 0)]
    return {
        "chart": chart,
        "references": edge.references,
        "kinds": dict(sorted(edge.kinds.items())),
        "refs_pinned": dict(sorted(edge.refs.items())),
        "examples": [
            {
                "url": urls.resolve(consumer, rel, f"L{line}").url if urls is not None else None,
                "line": secret_mask.mask(text),
            }
            for rel, line, text in shown
        ],
    }


def _scan_layer(clones, kinds, resolver, root):
    edges: dict[tuple[str, str, str], _Edge] = {}
    unresolved: dict[str, dict[str, _Unresolved]] = {kind.name: {} for kind in kinds}
    skipped: dict[str, Counter] = {kind.name: Counter() for kind in kinds}
    unreadable: list[str] = []
    for clone in clones:
        for kind in kinds:
            for rel in files_of(clone, kind):
                try:
                    text = read_text_or_raise(clone / rel)
                    found = kind.references(clone, rel, text)
                    skipped[kind.name].update(kind.skips(clone, rel, text))
                except (OSError, ValueError):
                    unreadable.append(_store_relative(root, clone / rel))
                    skipped[kind.name]["unreadable"] += 1
                    continue
                except Exception as error:
                    raise KindError(
                        f"{kind.name}: the parser raised on {clone.name}/{rel}: {error!r}"
                    ) from error
                for ref in found:
                    outcome, value = resolver.resolve(clone.name, ref)
                    if outcome == "self":
                        skipped[kind.name]["self"] += 1
                    elif outcome == "unresolved":
                        # A target is written as a key of a committed file, so it
                        # is masked like an example line.
                        entry = unresolved[kind.name].setdefault(
                            secret_mask.mask(value), _Unresolved()
                        )
                        entry.references += 1
                        entry.referenced_by.add(clone.name)
                    else:
                        edge = edges.setdefault((clone.name, value, ref.chart), _Edge())
                        edge.references += 1
                        edge.kinds[kind.name] += 1
                        # A pin is store text and a key of a committed file: an npm
                        # spec can be a URL carrying `access_token=`.
                        edge.refs[secret_mask.mask(ref.ref)] += 1
                        edge.examples.append((ref.rel, ref.line, ref.text))
    return edges, unresolved, skipped, sorted(unreadable)


def build(
    root: Path,
    clones: list[Path],
    kinds: tuple[Kind, ...],
    org: str,
    chart_pattern: str,
    umbrellas: set[str],
    examples: int,
    urls: SourceUrls | None,
    extra_dir: Path,
) -> Layer:
    """The layer for these clones, in memory. Nothing is written.

    `org` is the estate's GitHub organisation (`""` accepts any); `chart_pattern`
    names a chart's repository (`chart-{name}`, `""` to skip that step);
    `umbrellas` are repositories that bundle other repositories' charts; `examples`
    caps the example lines per edge and nothing else; `urls` links each example and
    names each repository's commit, or None for a store with no provenance;
    `extra_dir` holds the store's `<repo>.json` inputs for `extra`.
    Raises `KindError` when a parser fails, and `ValueError` for an unreadable
    `extra` input.
    """
    clones = sorted(clones, key=lambda clone: clone.name)
    names = [clone.name for clone in clones]
    publishers = {kind.name: _publishers(clones, kind) for kind in kinds}
    resolver = _Resolver(frozenset(names), org, chart_pattern, umbrellas, publishers)
    edges, unresolved, skipped, unreadable = _scan_layer(clones, kinds, resolver, root)

    flat, depends_on, depended_on_by = [], {}, {}
    for (consumer, provider, chart), edge in sorted(edges.items()):
        body = _edge_body(edge, consumer, chart, examples, urls)
        flat.append({"consumer": consumer, "provider": provider, **body})
        depends_on.setdefault(consumer, []).append({"repository": provider, **body})
        depended_on_by.setdefault(provider, []).append({"repository": consumer, **body})

    def by_other_end(found: list[dict]) -> list[dict]:
        return sorted(found, key=lambda edge: (edge["repository"], edge["chart"]))

    by_repo = {}
    for name in names:
        commit = urls.build_sha(name) if urls is not None else ""
        by_repo[name] = {
            "repository": name,
            "commit": commit or None,
            "base_url": urls.base(name) if urls is not None else None,
            "depends_on": by_other_end(depends_on.get(name, [])),
            "depended_on_by": by_other_end(depended_on_by.get(name, [])),
            "extra": _read_extra(extra_dir, name),
        }

    unresolved_body = {
        kind: {
            target: {"references": entry.references, "referenced_by": sorted(entry.referenced_by)}
            for target, entry in sorted(targets.items())
        }
        for kind, targets in unresolved.items()
    }
    flux_readable = importlib.util.find_spec("yaml") is not None
    kinds_body = {}
    for kind in kinds:
        carrying = [edge for edge in edges.values() if kind.name in edge.kinds]
        kinds_body[kind.name] = {
            "origin": kind.origin,
            "edges": len(carrying),
            "references": sum(edge.kinds[kind.name] for edge in carrying),
            "unresolved": len(unresolved[kind.name]),
            "skipped": dict(sorted(skipped[kind.name].items())),
            "notes": {"flux": FLUX_UNREAD}
            if kind.name == "helm_chart" and not flux_readable
            else {},
        }
    return Layer(
        dependencies={
            "format": dict(FORMAT),
            "edges": flat,
            "unresolved_targets_outside_estate": unresolved_body,
            "conflicts": resolver.conflicts,
        },
        by_repo=by_repo,
        kinds=kinds_body,
        conflicts=resolver.conflicts,
        skipped={kind: dict(sorted(counts.items())) for kind, counts in skipped.items()},
        unreadable=unreadable,
    )


def dumps(body) -> str:
    """The one serialisation every layer file is written with (CLAUDE.md 1.3)."""
    return json.dumps(body, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def write(layer: Layer, cfg) -> None:
    """Write the layer: every file to a temporary sibling first, then each renamed
    into place, then the `by-repo/` file of every repository no longer cloned removed.

    Nothing is renamed until every file has been written, so a failure part-way
    leaves the previous layer as it was. Only `*.json` files in `by-repo/` are
    candidates for removal: anything else a store keeps there is not this stage's.
    """
    files = {
        Path(cfg.REPOSITORY_DEPENDENCIES_PATH): layer.dependencies,
        Path(cfg.RELATIONSHIP_KINDS_PATH): layer.kinds,
    }
    by_repo_dir = Path(cfg.RELATIONSHIPS_BY_REPO_DIR)
    for repo, body in layer.by_repo.items():
        files[by_repo_dir / f"{repo}.json"] = body
    for path in files:
        io.checked_write_target(path)
    umask = os.umask(0)
    os.umask(umask)
    staged: list[tuple[str, Path]] = []
    try:
        for path, body in sorted(files.items()):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            staged.append((temporary, path))
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(dumps(body))
            # `mkstemp` creates 0600; the layer is committed and read by others.
            os.chmod(temporary, 0o666 & ~umask)
    except BaseException:
        for temporary, _ in staged:
            Path(temporary).unlink(missing_ok=True)
        raise
    for temporary, path in staged:
        os.replace(temporary, path)
    keep = {f"{repo}.json" for repo in layer.by_repo}
    for path in sorted(by_repo_dir.glob("*.json")):
        if path.name not in keep and path.is_file():
            path.unlink()
