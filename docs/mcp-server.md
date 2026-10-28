# The knowledge-store MCP server

The server lets Claude, GitHub Copilot or another MCP client answer questions
about your team's code, with a source for every answer. It answers from a
knowledge store, a Git repository of facts about a team's repositories that this
library builds. It ships in this library as `knowledgestore serve`.

This page is for **store maintainers** wiring the server into a store's plugin,
and for **anyone asking from an assistant other than Claude Code**. Askers using
Claude Code need only [Ask with Claude Code](asking-questions.md#ask-with-claude-code).

- **Every answer cites its source:** a link to the file at a pinned commit, or a
  path in the store.
- **It says when the store cannot answer.** It reports what it searched, rather
  than guessing.
- **It only reads.** It reads a clone on your disk, never writes, and makes no
  network calls.

For example, ask "Which services depend on alpha-lib?". The server's `dependents`
tool returns this (trimmed):

```json
"items": [{"repository": "beta-app", "refs_pinned": {"^1.2.0": 1},
  "examples": [{"line": "\"@example/alpha-lib\": \"^1.2.0\",",
    "url": "https://github.com/example-org/beta-app/blob/085f0da738cebf28ac22b57c8c07530936769442/package.json#L4"}]}],
"total": 1
```

## Use it now

You need [`uv`](https://docs.astral.sh/uv/) and a clone of your team's knowledge
store. The server needs Python 3.13. `uv` downloads it unless managed Python
downloads are disabled (`UV_PYTHON_DOWNLOADS=never`), in which case 3.13 must
already be installed; see [Troubleshooting](#troubleshooting).

To add the server to Claude Code, run this in a terminal:

```bash
# from inside your clone of the store
mkdir -p ~/.config/knowledge-store
grep -qxF "$(pwd)" ~/.config/knowledge-store/locations 2>/dev/null \
  || printf '%s\n' "$(pwd)" >> ~/.config/knowledge-store/locations
claude mcp add knowledge-store -- uvx --from "hmcts-knowledge-store-builder[server] @ git+https://github.com/hmcts/knowledge-store-builder@v0.22.0" knowledgestore serve --store-id <id>
```

The first three lines register your clone's absolute path, which is how the
server finds it. `<id>` is `store.id` in the clone's
`knowledge/store-manifest.json`. If you cloned with the `knowledge-store` skill, it
has already registered the clone.

The tag, `v0.22.0` here, is a library release from
[the releases page](https://github.com/hmcts/knowledge-store-builder/releases).
Use `v0.22.0` or later: earlier tags do not contain `knowledgestore serve`.

The quotes matter. `uvx --from` takes a requirement, and the `[server]` extra in
it is what installs the MCP SDK; without the extra the server stops, saying it needs
the MCP SDK. Keep the quotes in a shell and keep the string whole in JSON.

Then start Claude Code and ask: "Which repositories depend on `<repo>`?"

**Read on:**

- [Front doors](#front-doors): Claude Desktop, GitHub Copilot and the Copilot coding agent
- [What it can answer](#what-it-can-answer) and [Troubleshooting](#troubleshooting)
- [How the model is told to answer](answering-guidance.md)
- [Deployable artefacts](architecture.md): what is released, and by whom
- [Microsoft 365 Copilot and the hosted agent](microsoft-365.md)

## Front doors

One server, one set of tools, wherever the question is asked. Each front door
below launches the same stdio server against a local clone of the store, so an
answer in Copilot and an answer in Claude Code come from the same thirteen
read-only tools.

Every block below runs the same command as [Use it now](#use-it-now): `uvx` at a
library release tag (`v0.22.0` or later), running `knowledgestore serve` with
`--store-id <id>`. Pin a tag; do not install from the default branch, because a
store should run a released server.

`--store-id` finds your clone through `~/.config/knowledge-store/locations`, which
holds one absolute clone path per line. The `knowledge-store` skill writes that
entry on first clone; anyone else adds the line by hand.

### Claude Code and Claude Desktop

The store's plugin declares the server, in `.mcp.json` at the plugin root:

```json
{
  "mcpServers": {
    "knowledge-store": {
      "command": "uvx",
      "args": ["--from", "hmcts-knowledge-store-builder[server] @ git+https://github.com/hmcts/knowledge-store-builder@v0.22.0",
               "knowledgestore", "serve", "--store-id", "<id>"]
    }
  }
}
```

A plugin whose root is the store repository's root declares the same object
inline instead; [Add the server to the store plugin](creating-a-store.md#add-the-server-to-the-store-plugin)
says why, and `knowledgestore check-plugin` verifies either form.

Claude Desktop takes the same `mcpServers` entry in `claude_desktop_config.json`
(in `~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on
Windows). If Desktop cannot start `uvx`, give its absolute path, as `which uvx`
prints it.

### GitHub Copilot in VS Code

Commit `.vscode/mcp.json` to the repository people work in. VS Code's own format
uses a top-level `servers` key and an explicit `type`:

```json
{
  "servers": {
    "knowledge-store": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "hmcts-knowledge-store-builder[server] @ git+https://github.com/hmcts/knowledge-store-builder@v0.22.0",
               "knowledgestore", "serve", "--store-id", "<id>"]
    }
  }
}
```

On a Copilot Business or Copilot Enterprise plan, an organisation or enterprise
owner must first enable the **MCP servers in Copilot** policy. It is disabled by
default.

### GitHub Copilot CLI

Add the server to your user configuration, `~/.copilot/mcp-config.json`:

```bash
copilot mcp add knowledge-store -- uvx --from "hmcts-knowledge-store-builder[server] @ git+https://github.com/hmcts/knowledge-store-builder@v0.22.0" knowledgestore serve --store-id <id>
```

To share it with everyone working in a repository, commit `.github/mcp.json`
instead. The CLI does not read `.vscode/mcp.json`, and it loads a repository's
configuration only once you have trusted the folder:

```json
{
  "mcpServers": {
    "knowledge-store": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "hmcts-knowledge-store-builder[server] @ git+https://github.com/hmcts/knowledge-store-builder@v0.22.0",
               "knowledgestore", "serve", "--store-id", "<id>"],
      "tools": ["*"]
    }
  }
}
```

### The Copilot coding agent

**Enable this front door only on repositories at least as private as the store.**
The agent writes pull requests, commits and comments in whichever repository it
works in, and an answer quotes what the tools return: commit messages, paths,
ticket ids. In a public repository, that is a route from an internal store into
public view. The read-only token limits what the agent can change, not where it
can repeat what it reads. A store whose content is already public has no such
restriction.

The coding agent works on a fresh runner, so setup steps clone the store and
install `uv` before the agent starts. Three pieces, all in the repository the
agent works in:

1. **A read-only token for the store**, as an **Agents** secret (Settings →
   Secrets and variables → Agents): a fine-grained token with *Contents: read*
   on the store repository and nothing else. Every Agents secret is also exposed
   to the agent as an environment variable, which is why the scope matters. Do
   not prefix its name with `COPILOT_MCP_`: those reach MCP servers only, and this
   one is for the setup steps.
2. **`.github/workflows/copilot-setup-steps.yml`**, on the default branch. The
   job must be named `copilot-setup-steps`:

   ```yaml
   name: Copilot Setup Steps
   on: workflow_dispatch
   jobs:
     copilot-setup-steps:
       runs-on: ubuntu-latest
       permissions:
         contents: read
       steps:
         - name: Clone the knowledge store
           env:
             GH_TOKEN: ${{ secrets.KNOWLEDGE_STORE_READ_TOKEN }}
           run: gh repo clone <org>/<store-repo> /home/runner/knowledge-store -- --depth 1
         - name: Install uv
           # Pin a release tag of astral-sh/setup-uv.
           uses: astral-sh/setup-uv@<setup-uv-tag>
         # Builds the server into uv's cache now, so a failure shows in this step's log.
         - name: Fetch the server
           run: uvx --from "hmcts-knowledge-store-builder[server] @ git+https://github.com/hmcts/knowledge-store-builder@v0.22.0" knowledgestore serve --version
   ```

3. **The MCP configuration**, in the repository's settings (Settings → Copilot →
   MCP servers). `KNOWLEDGE_STORE_PATH` names the clone, so no locations file is
   needed:

   ```json
   {
     "mcpServers": {
       "knowledge-store": {
         "type": "stdio",
         "command": "uvx",
         "args": ["--from", "hmcts-knowledge-store-builder[server] @ git+https://github.com/hmcts/knowledge-store-builder@v0.22.0",
                  "knowledgestore", "serve"],
         "env": { "KNOWLEDGE_STORE_PATH": "/home/runner/knowledge-store" },
         "tools": ["*"]
       }
     }
   }
   ```

   GitHub recommends listing tools individually, because the agent calls them
   without asking. `"*"` is equivalent here: every tool is read-only, and declares
   so in its annotations.

That path configures where the server reads. It never appears in an answer, which
cites a pinned URL or a store-relative path.

The path assumes GitHub's hosted Ubuntu runner, whose home directory is
`/home/runner`. The agent's firewall does not apply to MCP server processes or to
setup steps.

### A hosted agent

A hosted agent, built on the Claude Agent SDK and run on Amazon Bedrock, is being
designed separately. It will launch this same server as a stdio child process,
with the same tools and guidance. See
[Microsoft 365 Copilot and the hosted agent](microsoft-365.md).

### Microsoft 365 Copilot

There is no route from Microsoft 365 Copilot yet. It reaches MCP servers over
Streamable HTTP only, and this server speaks stdio only.
[Microsoft 365 Copilot and the hosted agent](microsoft-365.md) sets out the two
possible routes, the governance decisions, and what each store must agree before
a route is enabled for it.

## What it can answer

Thirteen read-only tools:

| Tool | Answers |
|---|---|
| `store_info` | The store's format, each repository's pinned commit, which layers are present, and how old the store is |
| `find_repo` | Whether a repository is in the estate, or near misses labelled as different repositories |
| `search` | Graph nodes whose label matches some words |
| `node` | One graph node by id |
| `neighbours` | Every node within a number of hops of a node |
| `community` | One community of the graph: its label, summary and members |
| `repo_answer` | A repository's whole relationships file |
| `dependents` | The repositories that depend on a repository |
| `dependencies` | The repositories a repository depends on |
| `tickets` | The tickets whose commits touched a repository or path, landed and proposed kept apart |
| `ticket` | One ticket: its branches, commit text, files touched, and the issue tracker's record |
| `answers` | Answers the store has already written, each dated |
| `minimum_standard` | Evidence against the GOV.UK minimum standard, per point, never as a verdict |

A plural parameter is a JSON array: call `minimum_standard` with
`repos: ["repo-a", "repo-b"]`, up to 100 names. A comma-separated string is still
accepted, for older callers.

## Troubleshooting

**Where the server looks.** The first of these that gives a store wins:

1. `--store <path>`
2. the `KNOWLEDGE_STORE_PATH` environment variable
3. `--store-id <id>`, matched against the clones listed in
   `~/.config/knowledge-store/locations`. `store_info` also reports `--store-id`
   after the server switches from the plugin root to a listed clone (see below)
4. the `CLAUDE_PLUGIN_ROOT` environment variable, when the plugin root holds a
   store (and the store asked for by `--store-id`, if given)

**Which copy the server answers from.** `store_info` names the rule that chose the
store as `store_source` (`--store`, `KNOWLEDGE_STORE_PATH`, `--store-id` or
`CLAUDE_PLUGIN_ROOT`). It names the copy itself under `served`: `store_id`,
`digest`, `sources_as_of`, and `plugin_root`, which is true when the plugin root
supplied the store. It never gives the copy's path. `digest` is the manifest's
top-level `digest`. A manifest without one is identified by the sha256 of its
bytes instead.

**The skill's clone and the server disagree.** A skill that checked its own clone
compares that clone's `knowledge/store-manifest.json` with `store_info`. If the
clone's top-level `digest` differs from `served.digest`, the server is answering
from another copy. Check the store ids first: a different `served.store_id` means
a different store. A store chosen through `CLAUDE_PLUGIN_ROOT` gives way to your
clone without a restart. Once the locations file lists a clone with the same
`store.id`, the next call answers from that clone. That answer's `notice` says
`reloaded: now serving the registered clone`, and `store_info` then shows
`store_source` `--store-id` and `plugin_root` false. The server never switches
back. A store chosen by `--store`, `KNOWLEDGE_STORE_PATH` or `--store-id` never
switches. While it serves the plugin root, the server reads the locations file
again only when that file, or the manifest of a clone it lists, has changed. So a
clone pulled after it was listed is found without another write to the file, and
without a reconnect. If more than one listed clone carries the store's id, the
server keeps serving the plugin copy, and the next answer's `notice` says
`reload deferred: more than one registered clone carries this store id`. It says
this once, and again only after the file or a listed clone changes. Remove the
entries you do not mean. If the digests still differ after that, the server is
serving a different clone from the one you checked. List the checked clone in the
locations file, or point `--store` at it, then reconnect the server. A page cursor
from before a switch is refused. Start the listing again without it.

**Every tool answers `store_unavailable`.** The server is running but has no store
to answer from. The result's `cause` says why, and its `fix` says what to do:

| `cause` | What it means | What to do |
|---|---|---|
| `no_registered_clone` | no clone in `~/.config/knowledge-store/locations` carries the `--store-id` | clone the store and add its absolute path as a line in that file (the `knowledge-store` skill does this on first clone), then reconnect the server |
| `no_manifest` | the clone is found but has no `knowledge/store-manifest.json` | pull the clone, or clone it again if the pull is not enough, then reconnect the server |
| `other` | no `--store-id`, `--store` or `KNOWLEDGE_STORE_PATH` was given | add `--store-id <id>` to the server's arguments, or set `KNOWLEDGE_STORE_PATH` |

The server looks again on every call, so a clone registered or pulled after the
client started is picked up on the next question. Reconnecting is only needed if
it is not.

**The tools never appear, and no error is shown.** `uvx` could not find Python 3.13
or later and could not download one: uv has `UV_PYTHON_DOWNLOADS=never` or
`UV_PYTHON_PREFERENCE=only-system` set (the `python-downloads` and
`python-preference` settings in `uv.toml` or `pyproject.toml`, and the
`--no-python-downloads` flag, have the same effect), or the machine cannot reach
python-build-standalone. Claude Code shows only that the server's tools are
missing. Install Python 3.13 or later, or allow uv's managed Pythons (unset those
variables, and allow the network route), then reconnect the server.

**The server stops with `knowledgestore serve needs the MCP SDK`.** The launch
left out the `[server]` extra, so the MCP SDK is not installed. Use the `--from`
value exactly as [Use it now](#use-it-now) gives it.

**The server fails to start.** Only a wrong configuration stops it, and its reason
is on stderr (exit status 2): a `--store-id` that is not a valid id, two clones in
the locations file with the same `store.id`, a manifest in a format major version
this server does not read, or a layer that points outside the store. Fix the
configuration; waiting or pulling will not help.

**`store_info` lists a layer under `digest_mismatch` or `digest_missing`.** The
store's manifest describes files the clone no longer holds. A layer under
`digest_mismatch` has a file whose content differs from the digest the manifest
records: the file was rewritten or restored after the manifest was written. A
layer under `digest_missing` has a file the manifest declares that is not on disk,
or not readable as a file. Either way the server still answers, but the store's
freshness and identity come from a manifest that is wrong about those layers.
Whoever maintains the store re-runs `knowledgestore manifest`, or rebuilds the
layer, and commits the result; then pull the clone. The server checks on the first
`store_info` after it loads or reloads the store. `digest_unchecked` names the
directory layers, which the server does not check: their digest depends on which
files a git clone receives.
