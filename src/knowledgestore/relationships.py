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
to read a Flux `HelmRelease`, which is arbitrary YAML. Without it a release is not
skipped: `MissingDependency` stops the layer, because a reference it never read
would be absent from a layer that reads as complete.
"""

from __future__ import annotations

import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Set
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from xml.parsers import expat

from . import config, deploy_flux, io, secret_mask, source_urls

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


# What a pin is, per kind: `kinds.json` says so, because a chart version, a git ref
# and a version range are different quantities that share `refs_pinned`. A fixed
# phrase from the library for every kind, a store's included - never store text.
STORE_PINS = "as the store's parser writes them"


@dataclass(frozen=True)
class Kind:
    """A registered way of declaring a dependency between repositories.

    `references(clone, rel, text)` reads one file matching `globs` (`rel` is its
    path relative to `clone`, `text` its contents); a `ValueError` from it means the
    file is unreadable, and the layer counts it rather than stopping. `publishes(clone)`
    returns `(target key, declaring path)` for what the clone offers to others.
    `skips(clone, rel, text)` counts, by reason, declarations in the file that are
    deliberately not references (a local module path), so the layer can say how
    many it set aside. `pins` says what the kind's pins are (`STORE_PINS` for a
    store's kind).
    """

    name: str
    origin: str
    globs: tuple[str, ...]
    references: Callable[[Path, str, str], list[Reference]]
    publishes: Callable[[Path], list[tuple[str, str]]]
    skips: Callable[[Path, str, str], dict[str, int]] = _no_skips
    pins: str = STORE_PINS


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
# providing repository outright, so no manifest lookup is needed. Every form is in
# use and none dominates by design: on a real estate the scp-style `git@` form
# outnumbered the documented `git::https://` one several times over, so handling only
# the documented form would have missed most of the reuse.
# Matched against a source's address in two steps, the forge's prefix and then the
# repository, because one expression holding both is past what a reader can check.
# The scheme is optional in Terraform: its GitHub detector accepts a bare
# `github.com/org/repo` and rewrites it to HTTPS itself. Requiring one made shared
# modules invisible on a real estate (#125). The prefix is anchored at the start of
# the address, so `notgithub.com/...` cannot match. An ssh URL may carry any login
# name, and none once a `user:token@` credential has been stripped.
_TF_SCHEME = re.compile(r'(?:git::)?(?:https://|git@|ssh://(?:[^@/"]+@)?)?')
_TF_GITHUB = re.compile(
    r'github\.com[/:](?P<org>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?(?P<tail>[?/][^"]*)?\Z'
)
_TF_ANY_SOURCE = re.compile(r'source\s*=\s*"(?P<address>[^"]*)"')
_LOCAL_SOURCE = ("./", "../", "/")
_HEREDOC = re.compile(r"<<-?(?P<tag>[A-Za-z_]\w*)")
_PIN_PARAMS = ("ref", "depth")
_MODULE_BLOCK = re.compile(r'(?<![\w-])module\s+"[^"]*"\s*\{')


def _github_source(address: str) -> re.Match | None:
    """The organisation, repository and tail of a GitHub module source, or None."""
    prefix = _TF_SCHEME.match(address)
    return _TF_GITHUB.match(address, prefix.end() if prefix else 0)


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
        found.update(_block_source_offsets(text, block.end()))
    return found


def _block_source_offsets(text: str, start: int) -> list[int]:
    """The `source` attributes at the top level of the block whose body opens at
    `start`, read up to its closing brace."""
    found = []
    i, depth = start, 1
    while i < len(text) and depth:
        inert = _inert_end(text, i)
        if inert is not None:
            i = inert
            continue
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 1:
            match = _source_attribute(text, i)
            if match:
                found.append(match.start())
                i = match.end()
                continue
        i += 1
    return found


def _inert_end(text: str, i: int) -> int | None:
    """The offset after a string, heredoc or comment opening at `i`, or None when
    none opens there. Braces and `source` inside one are not structure."""
    if text[i] == '"':
        return _string_end(text, i)
    heredoc = _HEREDOC.match(text, i)
    if heredoc:
        return _heredoc_end(text, heredoc)
    if text[i] == "#" or text.startswith("//", i):
        end = text.find("\n", i)
        return len(text) if end < 0 else end
    if text.startswith("/*", i):
        end = text.find("*/", i)
        return len(text) if end < 0 else end + 2
    return None


def _source_attribute(text: str, i: int) -> re.Match | None:
    """A `source = "..."` attribute starting at `i`, not the tail of a longer name."""
    if text.startswith("source", i) and not _word_before(text, i):
        return _TF_ANY_SOURCE.match(text, i)
    return None


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
        github = _github_source(strip_userinfo(address))
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
            quote, skip = _in_quote(line, i, quote)
        elif ch in "\"'" and line[:i].rstrip()[-1:] in ("", ":", "-", ",", "[", "{"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _in_quote(line: str, i: int, quote: str) -> tuple[str, bool]:
    """The quote still open after `line[i]`, and whether the next character is
    escaped. Inside double quotes `\\` escapes; inside single quotes `''` is an
    escaped quote."""
    ch = line[i]
    if ch == "\\" and quote == '"':
        return quote, True
    if ch == quote and quote == "'" and line[i + 1 : i + 2] == "'":
        return quote, True
    if ch == quote:
        return "", False
    return quote, False


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
            if 0 <= dash_indent < indent:
                continue  # a dash nested inside an item's value
            dash_indent = indent
            body, key_indent, item = _open_item(content, indent)
            items.append(item)
            if body is None:
                continue
            content, indent = body, key_indent
        if items and indent == key_indent:
            _read_dependency_key(items[-1], content)
    return [_keep(item) for item in items if item]


def _open_item(content: str, indent: int) -> tuple[str | None, int, dict]:
    """A sequence item opening on `content`: the text after its dash, the indent its
    keys sit at, and the item so far. A flow mapping (`- {name: x}`) is the whole
    item, so its text comes back as None."""
    body = content[1:].lstrip()
    key_indent = indent + len(content) - len(body)
    if body.startswith("{"):
        flowed = _flow_complete(body, [])
        return None, key_indent, dict(flowed) if isinstance(flowed, dict) else {}
    return body, key_indent, {}


def _read_dependency_key(item: dict, content: str) -> None:
    """`name`, `version` or `repository` from a `key: value` line, into `item`."""
    if ":" not in content:
        return
    key, _, value = content.partition(":")
    if key.strip() in _DEP_KEYS and value.strip():
        item[key.strip()] = _scalar(value)


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


class MissingDependency(Exception):
    """A file needs an optional library that is not installed. Raised rather than
    read as empty: the layer stops, naming the library and the extra providing it."""

    def __init__(self, library: str, extra: str) -> None:
        super().__init__(f"needs {library}, which is not installed")
        self.library = library
        self.extra = extra


# A line declaring a document's kind a `HelmRelease`, the only thing that makes a
# file need PyYAML. The word anywhere is not enough: a pipeline that runs
# `flux reconcile helmrelease` or echoes the kind declares no release, and would
# otherwise refuse the default install's whole run.
_HELM_RELEASE_KIND = re.compile(r"""^\s*kind:\s*["']?HelmRelease["']?\s*$""", re.MULTILINE)


def _helm_releases(rel: str, text: str) -> list[Reference]:
    """Flux `HelmRelease`s in a YAML file, through the one reader of them.

    Raises `MissingDependency` without PyYAML, because there is nothing to read them
    with and the file is not therefore free of references; and `ValueError` for a
    file with a `kind: HelmRelease` line that will not parse (a Helm template full
    of `{{ }}`),
    so the layer counts it as unreadable rather than as holding no release."""
    if not _HELM_RELEASE_KIND.search(text):
        return []
    try:
        import yaml
    except ImportError:
        raise MissingDependency("PyYAML", "deploy") from None
    try:
        documents = list(yaml.load_all(text, Loader=deploy_flux.yaml_loader()))
    except yaml.YAMLError as error:
        raise ValueError(f"not YAML: {type(error).__name__}") from error
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
    properties = _pom_properties(project, scope)
    scope = {**properties, **scope}
    result = Pom(
        _resolve(group, scope),
        project.value("artifactId"),
        _resolve(version, scope),
        parent,
        properties,
    )
    result.dependencies.extend(_pom_dependencies(project, scope))
    return result


def _pom_properties(project: _Node, scope: dict) -> dict[str, str]:
    """`<properties>`, each resolved against the others and `scope`."""
    properties = {}
    props_node = project.child("properties")
    for node in props_node.children if props_node else []:
        properties[node.tag] = "".join(node.text).strip()
    for _ in range(8):
        properties = {k: _resolve(v, {**properties, **scope}) for k, v in properties.items()}
    return properties


def _pom_dependencies(project: _Node, scope: dict) -> list[tuple[str, str, str, int]]:
    """(group, artifact, version, line) of each dependency under `<project>` and its
    profiles, resolved against `scope`. One naming no group or artifact is not read."""
    found = []
    holders = [project]
    for profiles in project.kids("profiles"):
        holders.extend(profiles.kids("profile"))
    for holder in holders:
        for dependencies in holder.kids("dependencies"):
            for dependency in dependencies.kids("dependency"):
                artifact = dependency.child("artifactId")
                dep_group = _resolve(dependency.value("groupId"), scope)
                dep_artifact = _resolve(dependency.value("artifactId"), scope)
                if dep_group and dep_artifact and artifact:
                    dep_version = _resolve(dependency.value("version"), scope)
                    found.append((dep_group, dep_artifact, dep_version, artifact.line))
    return found


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
        pins="git refs",
    ),
    Kind(
        "helm_chart",
        "builtin",
        ("**/Chart.yaml", "**/*.yaml", "**/*.yml"),
        _helm_references,
        _helm_publishes,
        pins="chart versions",
    ),
    Kind(
        "maven_artifact",
        "builtin",
        ("**/pom.xml",),
        _maven_references,
        _maven_publishes,
        pins="version specs",
    ),
    Kind(
        "npm_package",
        "builtin",
        ("**/package.json",),
        _npm_references,
        npm_publishes,
        pins="version specs",
    ),
)


# --- a store's own kinds --------------------------------------------------------
#
# `config/relationship-kinds.json` registers a kind whose parser lives in the store:
#
#     {"<kind>": {"module": "scripts/<file>.py", "references": "<function>",
#                 "publishes": "<function>", "globs": ["**/*.xml"]}}
#
# The file is the entry point. Store repositories are not Python packages (they ship
# `requirements.txt` and `scripts/`), so there is nothing to install an entry point
# from. A store's module is code the builder runs with its own authority: it is
# loaded only from inside the store root, which is the repository an operator is
# already building, and never from anywhere a config could point it.

KIND_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class UsageError(ValueError):
    """`config/relationship-kinds.json` is not a usable declaration. Raised before
    any store code runs, naming the kind and the field."""


def _declared(kind: str, field_name: str, problem: str) -> UsageError:
    return UsageError(f"relationship kind {kind!r}: `{field_name}` {problem}")


def _module_path(root: Path, kind: str, module: object) -> Path:
    """The file `module` names, which must be inside `root`.

    Checked lexically first - `realpath` collapses `..`, so resolving first would
    launder the path this rejects - and then on the real path, so a symlink inside
    the store cannot lead out of it.
    """
    if not isinstance(module, str) or not module:
        raise _declared(kind, "module", "must be a path inside the store, as a string")
    given = Path(module)
    if given.is_absolute() or ".." in given.parts:
        raise _declared(kind, "module", f"must be relative to the store, with no `..`: {module}")
    real_root = os.path.realpath(root)
    real = os.path.realpath(os.path.join(real_root, module))
    if os.path.commonpath([real_root, real]) != real_root:
        raise _declared(kind, "module", f"resolves outside the store: {module}")
    if not os.path.isfile(real):
        raise _declared(kind, "module", f"is not a file in the store: {module}")
    return Path(real)


def _load_module(kind: str, path: Path, given: str):
    """The file as a module under a name unique to its real path, so two stores in
    one process cannot share one. The store's directory is never put on `sys.path`:
    that would let any `import` in a store resolve to another file of the store's."""
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    name = f"knowledgestore_store_kind_{digest}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise _declared(kind, "module", f"cannot be loaded as Python: {given}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        sys.modules.pop(name, None)
        raise UsageError(
            f"relationship kind {kind!r}: `module` {given} failed to import "
            f"({type(error).__name__})"
        ) from error
    return module


def _function(kind: str, module, field_name: str, name: object) -> Callable:
    function = getattr(module, name, None) if isinstance(name, str) else None
    if not callable(function):
        raise _declared(kind, field_name, f"names no function in the module: {name!r}")
    return function


_KIND_NAME_FIELD = "kind name"


def _checked_declaration(name: str, entry: object, builtin_names: set[str]) -> tuple[str, ...]:
    """The globs of one declared kind, once its name, shape, globs and function
    names are usable. Raises `UsageError` naming the first that is not."""
    if not KIND_NAME.fullmatch(name):
        raise _declared(name, _KIND_NAME_FIELD, "must match ^[a-z][a-z0-9_]*$")
    if name in builtin_names:
        raise _declared(name, _KIND_NAME_FIELD, "shadows a built-in kind")
    if not isinstance(entry, dict):
        raise _declared(name, _KIND_NAME_FIELD, "must map to an object")
    globs = entry.get("globs")
    if not isinstance(globs, list) or not globs or not all(isinstance(g, str) and g for g in globs):
        raise _declared(name, "globs", "must be a non-empty list of glob strings")
    for field_name in ("references", "publishes"):
        if not isinstance(entry.get(field_name), str) or not entry[field_name]:
            raise _declared(name, field_name, "must name a function in the module")
    return tuple(globs)


def load_store_kinds(root: Path, config_path: Path) -> tuple[Kind, ...]:
    """The kinds a store registers in `config_path`, ordered by name; none when the
    file does not exist. Raises `UsageError` for anything the file declares badly."""
    if not config_path.is_file():
        return ()
    try:
        declared = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise UsageError(f"{config_path}: not readable as JSON: {error}") from error
    if not isinstance(declared, dict):
        raise UsageError(f"{config_path}: must be a JSON object keyed by kind name")
    builtin_names = {kind.name for kind in BUILTIN}
    # Every declaration is checked before any module is imported: importing runs the
    # store's code, and a bad later declaration must not follow code that already ran.
    checked = []
    for name in sorted(declared):
        entry = declared[name]
        globs = _checked_declaration(name, entry, builtin_names)
        checked.append((name, entry, globs, _module_path(root, name, entry.get("module"))))
    kinds = []
    for name, entry, globs, path in checked:
        module = _load_module(name, path, entry["module"])
        kinds.append(
            Kind(
                name,
                "store",
                globs,
                _function(name, module, "references", entry["references"]),
                _function(name, module, "publishes", entry["publishes"]),
            )
        )
    return tuple(kinds)


# --- the layer ----------------------------------------------------------------
#
# Every kind's references, resolved to a repository in the estate or reported as
# outside it, aggregated per (consumer, provider, chart). `chart` is "" for every
# kind but `helm_chart`: one repository of generic charts would otherwise read as a
# dependency of nearly everything, with no way to ask which chart.

# The layer's format version, stamped into `repository-dependencies.json`. Major
# for incompatible changes, minor for additions. 1.1 added `unresolved_conflicts`.
FORMAT = (1, 1)
LAYER = "relationships"
PACKAGE = "hmcts-knowledge-store-builder"


def format_stamp() -> dict:
    """The `format` object `repository-dependencies.json` carries at its top level."""
    return {"layer": LAYER, "major": FORMAT[0], "minor": FORMAT[1]}


def format_problem(body: object) -> str:
    """Why `body` is not a layer this release reads, or "" when its stamp is ours.

    A file with no stamp, another layer's stamp or another major is refused: a
    store's own script writes a differently shaped `knowledge/relationships/`, and
    a reader must not take that for this stage's output.
    """
    stamp = body.get("format") if isinstance(body, dict) else None
    if not isinstance(stamp, dict) or stamp.get("layer") != LAYER:
        return f'it carries no `format` stamp for the "{LAYER}" layer'
    major = stamp.get("major")
    if isinstance(major, bool) or major != FORMAT[0]:
        return f"it is format major {major!r}; this release reads major {FORMAT[0]}"
    return ""


class KindError(Exception):
    """A kind's parser raised something other than "this file is unreadable". The
    stage stops, naming the kind and the file, before anything is written."""


@dataclass
class Layer:
    """What `write` puts on disk, built entirely in memory first.

    `dependencies` is the `repository-dependencies.json` body, `by_repo` each
    `by-repo/<repo>.json` body, `kinds` the `kinds.json` body. `conflicts` is
    `{kind: {target: [repositories]}}`, the first of which was chosen;
    `unresolved_conflicts` has the same shape, for a chart no rule chooses between
    its repositories, so none was chosen; `skipped` is
    `{kind: {reason: count}}`; `unreadable` names each file a parser could not read,
    store-relative.
    """

    dependencies: dict
    by_repo: dict[str, dict]
    kinds: dict
    conflicts: dict
    unresolved_conflicts: dict
    skipped: dict
    unreadable: list[str]


def builtin(name: str) -> Kind:
    """The built-in kind called `name`."""
    return next(kind for kind in BUILTIN if kind.name == name)


def estate_orgs(
    names: Iterable[str], clone_urls: Mapping[str, str], fallback_org: str
) -> dict[str, str]:
    """Clone name -> the GitHub organisation a source must name to mean that clone,
    lower-cased, `""` accepting any.

    Each clone's own organisation, read from its clone URL, so a store holding
    clones from several organisations resolves a source against the clone it names.
    A clone whose URL is not on github.com, or that `clone_urls` does not name,
    takes `fallback_org` (`KSB_GITHUB_ORG`).
    """
    orgs = {}
    for name in names:
        origin = source_urls.github_origin(strip_userinfo(clone_urls.get(name, "")))
        orgs[name] = (origin[0] if origin else fallback_org).lower()
    return orgs


def store_estate_orgs(names: Iterable[str]) -> dict[str, str]:
    """`estate_orgs` for these clones, from the store's `config/repositories.txt` and
    `KSB_GITHUB_ORG`. A malformed list raises `ValueError`, naming the line."""
    return estate_orgs(names, source_urls.configured_clone_urls(), config.GITHUB_ORG)


def names_estate_repo(ref: Reference, orgs: Mapping[str, str]) -> bool:
    """Whether a reference that names its repository outright names one the estate
    holds: a clone of that name, in the organisation `orgs` (`estate_orgs`) gives
    for it. The organisation compares ignoring case, as GitHub does. A same-named
    repository in another organisation is somebody else's."""
    repo = ref.resolved_repo
    if repo is None or repo not in orgs:
        return False
    org = orgs[repo]
    return not org or ref.org.lower() == org


def module_provider(ref: Reference, orgs: Mapping[str, str]) -> str | None:
    """The provider a graph or report names for a reference that names its
    repository outright, or None when it names none.

    The repository's name when the estate holds it (`names_estate_repo`), and when
    no clone of that name exists; `<org>/<repo>` when a clone of that name exists
    but the source is in another organisation - somebody else's repository, which
    must neither take the estate's name nor read as its consumer naming itself.
    """
    if ref.resolved_repo is None:
        return None
    if ref.resolved_repo in orgs and not names_estate_repo(ref, orgs):
        return f"{ref.org}/{ref.resolved_repo}"
    return ref.resolved_repo


def chart_pattern_problem(pattern: str) -> str:
    """Why `pattern` (`KSB_CHART_REPO_PATTERN`) cannot name a chart's repository, or
    `""` when it can. Every stage resolving charts refuses on this before reading
    anything, rather than ending in a `KeyError` at the first chart."""
    try:
        pattern.format(name="chart")
    except (KeyError, IndexError, ValueError) as error:
        return (
            f"KSB_CHART_REPO_PATTERN={pattern!r} is not a pattern with only {{name}} in it: "
            f"{error!r}"
        )
    return ""


def resolve_chart(
    chart: str, source: str, estate: Set[str], pattern: str, defined: dict[str, str]
) -> str | None:
    """The repository providing `chart`, or None when the estate holds none.

    In order: the pattern's repository when the pattern is set and names a clone;
    the clone `defined` chose for the chart (the one non-umbrella definition, else
    the one umbrella's; a chart with several is absent, never guessed); and, for a
    Flux `HelmRelease`, a `sourceRef` that names a clone - the graph's `uses_chart`
    rule, kept so the two agree.
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
    unpinned: int = 0
    examples: list = field(default_factory=list)


@dataclass
class _Unresolved:
    references: int = 0
    referenced_by: set = field(default_factory=set)


def _choose(
    kind: str, published: dict[str, list[str]], umbrellas: Set[str]
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, list[str]]]:
    """For one kind: target -> the clone chosen to provide it; the conflicts
    (masked target -> every candidate, the chosen one first); and the charts left
    unchosen (target, unmasked -> every candidate).

    A chart goes to the one clone defining it that is not an umbrella, else to the
    one umbrella defining it, and otherwise to nobody: a chart several clones
    define resolves by name order to whichever sorts first, which moves when a
    repository is renamed and silently captures the consumers of every chart a
    wrapper republishes (#475). Every other kind keeps the first by name.
    """
    chosen: dict[str, str] = {}
    conflicts: dict[str, list[str]] = {}
    unchosen: dict[str, list[str]] = {}
    for target, repos in sorted(published.items()):
        # Only the chart resolver knows umbrellas: an umbrella bundles other
        # repositories' charts, and is the owner of none of them.
        own = [r for r in repos if r not in umbrellas] if kind == "helm_chart" else repos
        candidates = own or repos
        if kind == "helm_chart" and len(candidates) > 1:
            unchosen[target] = candidates
            continue
        chosen[target] = candidates[0]
        if len(candidates) > 1:
            # A key of a committed file, so masked like every other one.
            conflicts[secret_mask.mask(target)] = candidates
    return chosen, conflicts, unchosen


class _Resolver:
    """Decides, for one reference, whether it is an edge, a self-reference or a
    target outside the estate."""

    def __init__(
        self,
        orgs: Mapping[str, str],
        pattern: str,
        umbrellas: Set[str],
        publishers: dict[str, dict[str, list[str]]],
    ) -> None:
        self.estate = frozenset(orgs)
        self.orgs = orgs
        self.pattern = pattern
        self.publishers = publishers
        self.chosen: dict[str, dict[str, str]] = {}
        self.conflicts: dict[str, dict[str, list[str]]] = {}
        self.unchosen: dict[str, dict[str, list[str]]] = {}
        for kind, published in publishers.items():
            self.chosen[kind], conflicts, unchosen = _choose(kind, published, umbrellas)
            if conflicts:
                self.conflicts[kind] = conflicts
            # A chart the naming pattern settles is no conflict: step 1 decides it
            # before any definition is consulted.
            unchosen = {
                target: candidates
                for target, candidates in unchosen.items()
                if not (pattern and pattern.format(name=target) in self.estate)
            }
            if unchosen:
                self.unchosen[kind] = unchosen

    @property
    def unresolved_conflicts(self) -> dict[str, dict[str, list[str]]]:
        """`{kind: {masked target: [candidates]}}` for every chart left unchosen."""
        return {
            kind: {secret_mask.mask(target): repos for target, repos in targets.items()}
            for kind, targets in self.unchosen.items()
        }

    def resolve(self, consumer: str, ref: Reference) -> tuple[str, str]:
        """`("edge", provider)`, `("self", "")`, `("ambiguous", "")` for a chart
        several clones define and no rule chooses between, or
        `("unresolved", target key)`."""
        if ref.resolved_repo is not None:
            if not names_estate_repo(ref, self.orgs):
                return "unresolved", f"{ref.org}/{ref.target}" if ref.org else ref.target
            provider = ref.resolved_repo
        elif ref.kind == "helm_chart":
            provider = resolve_chart(
                ref.target, ref.source, self.estate, self.pattern, self.chosen.get(ref.kind, {})
            )
        else:
            provider = self.chosen.get(ref.kind, {}).get(ref.target)
        if provider is None:
            if ref.target in self.unchosen.get(ref.kind, {}):
                # In the estate, so not a target outside it; counted, never dropped.
                return AMBIGUOUS, ""
            return "unresolved", ref.target
        # `self` is where resolution landed, never what the consumer happens to
        # publish: an umbrella that also defines a chart another clone owns depends
        # on that clone, and a conflict another clone won is a dependency on it.
        if provider == consumer:
            return "self", ""
        return "edge", provider


def chart_definitions(clones: list[Path], umbrellas: Set[str]) -> dict[str, str]:
    """chart name -> the clone defining it, chosen as the layer chooses
    (`_choose`). What `resolve_chart` takes as `defined`, for a caller resolving
    charts outside the layer - the graph's `uses_chart` edges."""
    chosen, _, _ = _choose("helm_chart", _publishers(clones, builtin("helm_chart")), umbrellas)
    return chosen


def _shape(value: object) -> str:
    """The type of a returned value - of each member, for a tuple or list - and
    nothing of its content."""
    if isinstance(value, (tuple, list)):
        return "(" + ", ".join(type(member).__name__ for member in value) + ")"
    return type(value).__name__


def _publishers(clones: list[Path], kind: Kind) -> dict[str, list[str]]:
    # A refusal names the kind, the clone and a type, never a value or an exception's
    # text: either is store text, which can carry a credential, and stderr is kept in
    # build logs.
    found: dict[str, set[str]] = {}
    for clone in clones:
        where = f"{kind.name}: publishes() on {clone.name}"
        try:
            published = list(kind.publishes(clone))
        except Exception as error:
            raise KindError(f"{where} raised {type(error).__name__}") from error
        for pair in published:
            if not (
                isinstance(pair, (tuple, list))
                and len(pair) == 2
                and all(isinstance(member, str) for member in pair)
            ):
                raise KindError(
                    f"{where} returned a {_shape(pair)}, not a (target, path) pair of strings"
                )
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
        "refs_unpinned": edge.unpinned,
        "examples": [
            {
                "url": urls.resolve(consumer, rel, f"L{line}").url if urls is not None else None,
                "line": secret_mask.mask(text),
            }
            for rel, line, text in shown
        ],
    }


def _check_references(kind: Kind, clone: Path, rel: str, found: object) -> None:
    """Stop, naming the kind and the file, on what a parser must not return.

    A reference under another kind's name resolves against that kind's publishers
    and so resolves nothing, silently; a `chart` is the key of an edge and is only
    meaningful for `helm_chart`; and a parser that returns something other than a
    list of `Reference` has broken the contract the layer is built on.
    """
    where = f"{kind.name}: the parser returned a bad result for {clone.name}/{rel}"
    if not isinstance(found, (list, tuple)) or not all(isinstance(r, Reference) for r in found):
        raise KindError(f"{where}: it must be a list of Reference")
    for ref in found:
        if ref.kind != kind.name:
            raise KindError(f"{where}: a reference is not of kind {kind.name}")
        if ref.chart and kind.name != "helm_chart":
            raise KindError(f"{where}: a non-empty `chart` is only for helm_chart")


UNREADABLE = "unreadable"
# The skip reason for a reference to a chart several clones define with no rule to
# choose between them (`unresolved_conflicts`).
AMBIGUOUS = "ambiguous"


class ConservationError(KindError):
    """A kind's references read and references placed do not balance: one was
    dropped (not an edge, not an unresolved target, not a counted skip) or counted
    twice. The stage stops before writing a layer that would read as complete."""


def unaccounted(
    read: Mapping[str, int],
    edges: Mapping[str, int],
    unresolved: Mapping[str, int],
    skipped: Mapping[str, Mapping[str, int]],
) -> list[str]:
    """One line per kind whose references read differ from those placed, `[]` when
    every kind balances (CLAUDE.md 2.5).

    `read` is what the parsers returned plus the declarations they set aside
    (`Kind.skips`); placed is the references in edges, plus those of unresolved
    targets, plus every skip but `unreadable`, which counts files, not references.
    """
    found = []
    for kind in sorted(read):
        in_edges, outside = edges.get(kind, 0), unresolved.get(kind, 0)
        set_aside = sum(n for reason, n in skipped.get(kind, {}).items() if reason != UNREADABLE)
        placed = in_edges + outside + set_aside
        if placed != read[kind]:
            found.append(
                f"{kind}: {read[kind]} references read, {placed} placed "
                f"({in_edges} in edges, {outside} unresolved, {set_aside} skipped)"
            )
    return found


def _check_conservation(read, edges, unresolved, skipped) -> None:
    # Covered by the mutation entries "relationships: a self reference is placed
    # nowhere" (a dropped reference must stop the build) and "relationships: the
    # conservation check counts unreadable files as placed references".
    problems = unaccounted(
        read,
        Counter(kind for edge in edges.values() for kind in edge.kinds.elements()),
        {
            kind: sum(entry.references for entry in targets.values())
            for kind, targets in unresolved.items()
        },
        skipped,
    )
    if problems:
        raise ConservationError(
            "references read and references placed do not balance - " + "; ".join(problems)
        )


def _scan_layer(clones, kinds, resolver, root):
    edges: dict[tuple[str, str, str], _Edge] = {}
    unresolved: dict[str, dict[str, _Unresolved]] = {kind.name: {} for kind in kinds}
    skipped: dict[str, Counter] = {kind.name: Counter() for kind in kinds}
    read: Counter = Counter({kind.name: 0 for kind in kinds})
    unreadable: list[str] = []
    for clone in clones:
        for kind in kinds:
            for rel in files_of(clone, kind):
                parsed = _parse_file(kind, clone, rel, root)
                if parsed is None:
                    unreadable.append(_store_relative(root, clone / rel))
                    skipped[kind.name][UNREADABLE] += 1
                    continue
                found, set_aside = parsed
                skipped[kind.name].update(set_aside)
                _check_references(kind, clone, rel, found)
                read[kind.name] += len(found) + sum(set_aside.values())
                for ref in found:
                    _place(ref, kind.name, clone.name, resolver, edges, unresolved, skipped)
    _check_conservation(read, edges, unresolved, skipped)
    return edges, unresolved, skipped, sorted(unreadable)


def _place(ref, kind_name, consumer, resolver, edges, unresolved, skipped) -> None:
    """One reference, as a `self` skip, an unresolved target or part of an edge."""
    outcome, value = resolver.resolve(consumer, ref)
    if outcome == "self":
        skipped[kind_name]["self"] += 1
    elif outcome == AMBIGUOUS:
        skipped[kind_name][AMBIGUOUS] += 1
    elif outcome == "unresolved":
        _add_unresolved(unresolved[kind_name], value, consumer)
    else:
        edge = edges.setdefault((consumer, value, secret_mask.mask(ref.chart)), _Edge())
        _add_to_edge(edge, kind_name, ref)


def _parse_file(kind: Kind, clone: Path, rel: str, root: Path):
    """The kind's references in one file and what it set aside, or None when the
    file cannot be read. A missing library or any other failure of the parser stops
    the layer as a `KindError` naming the kind and the file."""
    try:
        text = read_text_or_raise(clone / rel)
        return kind.references(clone, rel, text), kind.skips(clone, rel, text)
    except MissingDependency as missing:
        raise KindError(
            f"{kind.name}: {_store_relative(root, clone / rel)} {missing} - "
            f"pip install '{PACKAGE}[{missing.extra}]'"
        ) from missing
    except (OSError, ValueError):
        return None
    except Exception as error:
        raise KindError(
            f"{kind.name}: the parser raised {type(error).__name__} on {clone.name}/{rel}"
        ) from error


def _add_unresolved(targets: dict[str, _Unresolved], value: str, consumer: str) -> None:
    # A target is written as a key of a committed file, so it is masked like an
    # example line.
    entry = targets.setdefault(secret_mask.mask(value), _Unresolved())
    entry.references += 1
    entry.referenced_by.add(consumer)


def _add_to_edge(edge: _Edge, kind_name: str, ref: Reference) -> None:
    edge.references += 1
    edge.kinds[kind_name] += 1
    # A pin is store text and a key of a committed file: an npm spec can be a URL
    # carrying `access_token=`. A reference that gives none is counted beside the
    # pins, not under a "" key.
    if ref.ref:
        edge.refs[secret_mask.mask(ref.ref)] += 1
    else:
        edge.unpinned += 1
    edge.examples.append((ref.rel, ref.line, ref.text))


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

    `org` is the estate's GitHub organisation (`""` accepts any), for a clone whose
    own is not known from `urls` (`estate_orgs`); `chart_pattern`
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
    clone_urls = {name: urls.clone_url(name) for name in names} if urls is not None else {}
    resolver = _Resolver(estate_orgs(names, clone_urls, org), chart_pattern, umbrellas, publishers)
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
    kinds_body = {}
    for kind in kinds:
        carrying = [edge for edge in edges.values() if kind.name in edge.kinds]
        kinds_body[kind.name] = {
            "origin": kind.origin,
            "pins": kind.pins,
            "edges": len(carrying),
            "references": sum(edge.kinds[kind.name] for edge in carrying),
            "unresolved": len(unresolved[kind.name]),
            "skipped": dict(sorted(skipped[kind.name].items())),
        }
    return Layer(
        dependencies={
            "format": format_stamp(),
            "edges": flat,
            "unresolved_targets_outside_estate": unresolved_body,
            "conflicts": resolver.conflicts,
            "unresolved_conflicts": resolver.unresolved_conflicts,
        },
        by_repo=by_repo,
        kinds=kinds_body,
        conflicts=resolver.conflicts,
        unresolved_conflicts=resolver.unresolved_conflicts,
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
    # NOSONAR(S2612) - reads the process umask, which Python can only do by
    # setting one; the next line puts it back before any file is created.
    umask = os.umask(0)  # NOSONAR(S2612)
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
            # NOSONAR(S2612) - deliberate: `0o666 & ~umask` is the mode a plain
            # `open(path, "w")` gives the same file, so the layer is no more open
            # than any other file this library writes.
            os.chmod(temporary, 0o666 & ~umask)  # NOSONAR(S2612)
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
