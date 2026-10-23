"""A store plugin must launch the knowledge-store server the way the store needs.

A plugin that names the wrong `--store-id`, an unpinned server or the wrong command
fails for every asker, and only when their assistant starts: the message names a
missing clone, not the wrong id. Every store adopting the server wrote the same
local gate, so it lives here.

The server declaration is read from `<plugin>/.mcp.json`, else from `mcpServers` in
`<plugin>/.claude-plugin/plugin.json`. It fails, naming every reason it finds, when:

  - no `knowledge-store` server is declared;
  - the command is not `uvx`;
  - `--from` is not `git+https://github.com/hmcts/knowledge-store-mcp@<exact tag>`,
    where a branch, `latest` or no ref are all refused;
  - `--store-id` is absent, or differs from `store.id` in
    `knowledge/store-manifest.json`; or
  - the plugin root is the repository root and a root `.mcp.json` exists, because
    that file also loads as project configuration for anyone working in the
    repository.

A pinned tag older than `DOCUMENTED_TAG` is a warning and not a failure: the
store has chosen an older server, which is a decision and not a defect.

`DOCUMENTED_TAG` is a constant because `docs/` is not in the wheel, so the stage
cannot read `docs/mcp-server.txt` from an installed library.
`tests/test_check_plugin.py` holds the constant to that file, so a bump in one
cannot be missed in the other.

Run: knowledgestore check-plugin [--plugin DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import config

SERVER = "knowledge-store"
MCP_JSON = ".mcp.json"
SOURCE = "git+https://github.com/hmcts/knowledge-store-mcp"
DOCUMENTED_TAG = "v0.2.1"
# The layers the server answers from; without either it falls back or does not answer.
READ_LAYERS = ("index", "relationships")

_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _tag_key(tag: str) -> tuple[int, int, int] | None:
    match = _TAG.match(tag)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def _option(args: list, flag: str) -> str | None:
    """The value after `flag` or in `flag=value`, else None. Last occurrence wins."""
    found = None
    for index, argument in enumerate(args):
        if argument == flag and index + 1 < len(args):
            found = str(args[index + 1])
        elif isinstance(argument, str) and argument.startswith(flag + "="):
            found = argument.split("=", 1)[1]
    return found


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def declared_servers(plugin: Path) -> tuple[dict, str] | str:
    """(servers, where it was read from), or the reason none could be read."""
    mcp = plugin / MCP_JSON
    manifest = plugin / ".claude-plugin" / "plugin.json"
    try:
        if mcp.is_file():
            data = _read_json(mcp)
            # A plugin `.mcp.json` may carry the servers at the top level or under
            # `mcpServers`; both load.
            servers = data.get("mcpServers", data) if isinstance(data, dict) else None
            return (
                (servers, ".mcp.json")
                if isinstance(servers, dict)
                else (f"{mcp} holds no server declarations")
            )
        if manifest.is_file():
            data = _read_json(manifest)
            servers = data.get("mcpServers") if isinstance(data, dict) else None
            if isinstance(servers, dict):
                return servers, ".claude-plugin/plugin.json"
            return f"{manifest} has no mcpServers"
    except json.JSONDecodeError as error:
        return f"cannot parse the plugin's server declaration: {error}"
    return f"no .mcp.json or .claude-plugin/plugin.json under {plugin}"


def manifest_store_id(path: Path) -> tuple[str | None, str | None]:
    """(id, None), or (None, why not)."""
    if not path.is_file():
        return None, f"{path} does not exist, so there is no store.id to compare --store-id with"
    try:
        data = _read_json(path)
    except json.JSONDecodeError as error:
        return None, f"{path} is not valid JSON: {error}"
    store = data.get("store") if isinstance(data, dict) else None
    store_id = store.get("id") if isinstance(store, dict) else None
    if not isinstance(store_id, str) or not store_id:
        return None, f"{path} states no store.id"
    return store_id, None


def _check_pin(args: list, failures: list[str], warnings: list[str]) -> str:
    """The pinned tag when `--from` is an exact tag of the server, else ''."""
    source = _option(args, "--from")
    prefix = SOURCE + "@"
    if source is None:
        failures.append("args carry no --from, so the server is not pinned to a tag")
        return ""
    if not (source.startswith(prefix) and _tag_key(source[len(prefix) :])):
        failures.append(
            f"--from is {source!r}, not {prefix}<exact tag> such as {prefix}{DOCUMENTED_TAG}: "
            "a branch, `latest` or no ref launches a server nobody chose"
        )
        return ""
    tag = source[len(prefix) :]
    if _tag_key(tag) < _tag_key(DOCUMENTED_TAG):  # type: ignore[operator]
        warnings.append(
            f"pinned {tag} is older than {DOCUMENTED_TAG}, the tag this library documents"
        )
    return tag


def _check_store_id(args: list, manifest: Path, failures: list[str]) -> str:
    """The declared `--store-id` when it equals the manifest's `store.id`, else ''."""
    declared = _option(args, "--store-id")
    wanted, why = manifest_store_id(manifest)
    if declared is None:
        failures.append("args carry no --store-id, so the server cannot find the store's clone")
    elif wanted is None:
        failures.append(f"--store-id is {declared!r} but {why}")
    elif declared != wanted:
        failures.append(f"--store-id is {declared!r} but store.id in {manifest.name} is {wanted!r}")
    else:
        return declared
    return ""


def _manifest_data(path: Path) -> dict:
    """The parsed manifest, or {} when it cannot be read (the id check reports that)."""
    try:
        data = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _check_layers(manifest: Path, failures: list[str], warnings: list[str]) -> None:
    """The layers the server reads must be recorded as present, and recognised."""
    data = _manifest_data(manifest)
    if not data:
        return
    layers = data.get("layers")
    if not isinstance(layers, dict):
        layers = {}
    for name in READ_LAYERS:
        layer = layers.get(name)
        layer = layer if isinstance(layer, dict) else {}
        present = layer.get("present", False)
        if present is None:
            warnings.append(
                f"{manifest.name} does not record whether the {name} layer is present "
                "(present: null), so this cannot say the server can read it"
            )
        elif not present:
            failures.append(
                f"{manifest.name} records the {name} layer as missing, so the server "
                "answers from a fallback or not at all"
            )
        elif name == "relationships" and layer.get("recognised") is not True:
            failures.append(
                f"{manifest.name} records the relationships layer as not recognised: "
                "the server cannot read it"
            )


def _check_build_id(manifest: Path, failures: list[str]) -> None:
    """The id the next `manifest` run will write, when one is declared, must match.

    Read from `config.STORE_ID`, which is the id the build itself uses, rather than
    from the environment here: a CI step running this stage then sees whatever the
    build would see, including any source `config` learns to resolve it from.
    """
    building = config.STORE_ID.strip()
    if not building:
        return
    wanted, _why = manifest_store_id(manifest)
    if wanted is not None and building != wanted:
        failures.append(
            f"KSB_STORE_ID is {building!r} but store.id in {manifest.name} is {wanted!r}, so "
            "the next `knowledgestore manifest` writes an id the plugin does not ask for"
        )


def check(plugin: Path, root: Path, manifest: Path) -> tuple[list[str], list[str], str, str]:
    """(failures, warnings, tag, store id). Tag and id are empty when not established."""
    failures: list[str] = []
    warnings: list[str] = []
    tag = store_id = ""

    if plugin.resolve() == root.resolve() and (root / MCP_JSON).exists():
        failures.append(
            f"the plugin root is the repository root and {root / MCP_JSON} exists: it also "
            "loads as project configuration for anyone working in this repository. Move the "
            "mcpServers object into .claude-plugin/plugin.json and delete the file"
        )

    _check_layers(manifest, failures, warnings)
    _check_build_id(manifest, failures)

    found = declared_servers(plugin)
    if isinstance(found, str):
        failures.append(found)
        return failures, warnings, tag, store_id
    servers, origin = found
    server = servers.get(SERVER)
    if not isinstance(server, dict):
        failures.append(f"no `{SERVER}` server is declared in {origin}")
        return failures, warnings, tag, store_id

    if server.get("command") != "uvx":
        failures.append(f"the command is {server.get('command')!r}, not 'uvx'")
    args = server.get("args")
    args = args if isinstance(args, list) else []

    tag = _check_pin(args, failures, warnings)
    store_id = _check_store_id(args, manifest, failures)
    return failures, warnings, tag, store_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledgestore check-plugin",
        description="Check the store plugin launches the knowledge-store server correctly.",
    )
    parser.add_argument("--plugin", type=Path, help="the plugin directory; default: the store root")
    arguments = parser.parse_args(argv)
    root = config.ROOT
    plugin = arguments.plugin or root

    failures, warnings, tag, store_id = check(plugin, root, config.STORE_MANIFEST_PATH)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if failures:
        print(f"the plugin at {plugin} would not launch the server correctly:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"{plugin}: launches {SERVER} {tag} for store {store_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
