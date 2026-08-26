# Cheatsheet: the commands

Two separate installs live here. Pick your row.

| You want to | You need | Go to |
|---|---|---|
| Ask questions of a store someone else built | the plugin | [Use a store](#use-a-store) |
| Build or refresh a store | the plugin **and** the library | [Build a store](#build-a-store) |

For setup and explanation, see [Asking questions](docs/asking-questions.md),
[Creating a store](docs/creating-a-store.md) or
[Refreshing a store](docs/refreshing-a-store.md).

## Use a store

### Claude Code in a terminal

```
/plugin marketplace add hmcts/knowledge-store-builder
/plugin install knowledge-store@knowledge-store-builder
/reload-plugins
```

Then ask in plain words. No Python, no `pip`: the one tool the query skill needs
is the `graphify` CLI, and it installs that itself.

`/reload-plugins` is not optional — without it the plugin is installed but
inactive in the session you are in, which looks like a broken install.

### VS Code or JetBrains extension

The extension exposes only a subset of Claude Code's commands, so install through
its interface rather than the commands above:

- Type `/plugins` to open **Manage plugins**
- **Marketplaces** tab → add `hmcts/knowledge-store-builder`
- **Plugins** tab → install `knowledge-store`

What you configure either way is shared: plugins added in the extension are
available in the CLI, and the reverse.

### Claude desktop app

- **+** next to the prompt box → **Plugins** → **Add plugin**
- Choose `knowledge-store`. If it is not listed, add the marketplace
  `hmcts/knowledge-store-builder` first.
- Pick a scope: **user** (you, everywhere), **project** (everyone on this
  repository), **local** (you, here only).
- Later: **+** → **Plugins** → **Manage plugins** to enable, disable or remove.

### Cloud sessions and WSL

- **WSL:** plugins are not available. Work inside a clone of the store instead —
  a store ships its own skill, so it is picked up with nothing installed.
- **Cloud sessions:** the plugin browser does not exist, and plugins installed on
  desktop do not carry over. Declare it in the repository's
  `.claude/settings.json`, which installs at session start:

  ```json
  { "enabledPlugins": { "knowledge-store@knowledge-store-builder": true } }
  ```

  A cloud session runs in Anthropic's sandbox. If the store carries anything
  drawn from private repositories — commit messages, author names, file paths —
  that is a data-handling decision for whoever owns it, not a setup step.

### Without a Claude licence

Every store ships `explorer.html`. Clone the store, open the file in a browser.
Nothing to install, no network access.

### In a terminal, without Claude

```bash
pip install graphifyy                    # or: uv tool install graphifyy
gunzip -k graphify-out/graph.json.gz     # stores commit the graph compressed
graphify query "<your question>"
```

The `gunzip` comes first: the committed form is `graph.json.gz` and the CLI reads
`graph.json`.

## Build a store

You need the plugin (above) **and** these, because the build skill is
instructions for a pipeline that has to exist on the machine:

```bash
pip install --extra-index-url \
  https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/ \
  hmcts-knowledge-store-builder
pip install graphifyy      # graph extraction
gh auth status             # discovery needs an authenticated gh
knowledgestore             # lists every stage in run order
```

The feed reads anonymously — no credentials, no account. Credentials are only
needed to publish.

### A first store

```bash
mkdir my-estate-knowledge && cd my-estate-knowledge
mkdir config
printf 'prefix myteam-service-\nprefix myteam-ui-\n' > config/repository-filters.txt

export KSB_GITHUB_ORG=my-org
knowledgestore discover
knowledgestore sync
knowledgestore export-history
knowledgestore context
knowledgestore intent
```

A store is a **working directory**, not necessarily a git repository. Make it one
when you want to share the result.

### Then the graph

**Never run graphify at the store root** — `repositories/` is gitignored, so the
scan sees only the store's own config and docs and produces a near-empty graph.

```bash
export GRAPHIFY_MAX_GRAPH_BYTES=4GB   # a large graph needs the cap raised

while IFS='|' read -r repo _; do      # repositories.txt is pipe-delimited
  case "$repo" in ''|\#*) continue;; esac
  ( cd "repositories/$repo" && graphify update . --no-cluster )
done < config/repositories.txt

knowledgestore merge-inputs        # names what the glob below will read
graphify merge-graphs repositories/*/graphify-out/graph.json \
  --out graphify-out/graph.json

knowledgestore gherkin             # then cluster, then:
knowledgestore record-clustering   # which partitioner clustered: Leiden or Louvain
knowledgestore explorer
```

Cluster **after** `gherkin`. Follow `/knowledge-store:knowledge-store-build` for this step:
`graphify cluster-only` reports success without writing its result, which is
destructive if it goes unnoticed. Record the partitioner from the environment that
clustered: graphify uses Leiden where `graspologic` is installed and Louvain where
it is not, and that choice re-keys every community — so `status` needs the record
to tell an operator whose environment cannot reproduce the committed clustering.

### Checks worth running

```bash
knowledgestore status                        # coverage, citations, page freshness
knowledgestore status --drift                # how far the sources have moved
knowledgestore summaries verify --sample 200 # is the prose grounded in evidence?
knowledgestore check-install-docs            # can a reader run your install commands?
knowledgestore check-evidence                # does committed commit text identify a case?
```

`check-install-docs` and `check-evidence` exit non-zero, so they belong in CI.
`check-evidence` names the ticket, the field and the rule, never the value.
`status` never fails by design - drift is a condition to read, not a broken
build.

## When it does not work

| Symptom | Fix |
|---|---|
| Plugin installed, no skills in the session | `/reload-plugins` |
| Still no skills | `rm -rf ~/.claude/plugins/cache`, restart, reinstall |
| `marketplace add` says it already exists | `/plugin marketplace update knowledge-store-builder`, then reinstall |
| A skill change you merged is not there | `add` does not re-fetch; run `marketplace update` |
| `No matching distribution found` for the library | a lock compiled without `--emit-index-url` does not name the feed — pass `--extra-index-url`, or recompile (see [Refreshing a store](docs/refreshing-a-store.md#update-the-library-version)) |
| Terminal query fails on a missing file | you skipped the `gunzip` |
| Graph operations refuse outright | raise `GRAPHIFY_MAX_GRAPH_BYTES` |
| `remap` drops almost every summary | the graph is unclustered — check community coverage before remapping |

Confirm a plugin install: `claude plugin details knowledge-store` lists
**Skills (3)** by name, alongside the token cost it adds to each session. That
needs the standalone CLI on your `PATH` — an IDE extension does not put it there,
so in the extension use **Manage plugins** (`/plugins`) instead.
