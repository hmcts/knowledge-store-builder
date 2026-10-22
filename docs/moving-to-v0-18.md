# Moving to v0.18

From v0.18, every assistant answers a store's questions through the
knowledge-store MCP server, so Claude Code, GitHub Copilot and any other MCP
client give the same answer under the same rules. The `knowledge-store` skill no
longer reads a store's files to answer. A store whose plugin does not launch the
server gets no answers until its maintainer adds it.

## Choose your section

| You | Start with |
|---|---|
| Maintain a store and its plugin | [For store maintainers](#for-store-maintainers) |
| Ask questions of a store through Claude Code | [For askers](#for-askers) |
| Ask through GitHub Copilot or another MCP client | [For GitHub Copilot and other MCP clients](#for-github-copilot-and-other-mcp-clients) |

## For store maintainers

Do these in order, then tell your askers to follow [For askers](#for-askers).

1. **Name the library with the `ast` extra and no version, re-lock, and drop any
   separate `graphifyy` pin.** The extra installs graphify with the grammars the
   library needs, so a second pin can only disagree with it. In the store's
   `requirements.txt`:

   ```text
   hmcts-knowledge-store-builder[ast]
   ```

   The unversioned line means "the latest"; `requirements.lock` is the pin the
   build installs from. Add `deploy` to the extras (`[ast,deploy]`) when a clone
   holds a Flux `HelmRelease`, because `relationships` exits 1 without it. Then
   re-lock, commit the lock and install it:

   ```bash
   uv pip compile requirements.txt --generate-hashes \
     --extra-index-url https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/ \
     --emit-index-url --emit-build-options \
     --upgrade-package hmcts-knowledge-store-builder \
     --output-file requirements.lock
   pip install -r requirements.lock
   knowledgestore --version
   ```

   A release can change what a store commits, so upgrading is a deliberate
   re-lock and rebuild, never a side effect. A store that needs one release
   writes `==X.Y.Z` in `requirements.txt` instead. See
   [Update the library version](refreshing-a-store.md#update-the-library-version).

   A store moving from an older graphify re-extracts every repository and
   re-clusters, because Terraform node ids change. Plan it as
   [Moving to a newer graphify](refreshing-a-store.md#moving-to-a-newer-graphify)
   describes before you re-lock.

2. **If the store drives graphify itself**, rather than through `extract-ast`,
   run the grammar check first in its driver:

   ```bash
   knowledgestore check-grammars --clones
   ```

   It refuses when the interpreter that runs graphify lacks the Terraform or SQL
   grammar, which would otherwise read those files as nothing with no error. See
   [Check the grammars before any store-owned AST route](creating-a-store.md#check-the-grammars-before-any-store-owned-ast-route).

3. **Build what the server reads**, in this order, with `manifest` last.

   **Move your own per-repository fields first.** `relationships` rewrites every
   `knowledge/relationships/by-repo/<repo>.json` whole, from the clones alone,
   and removes the file of any repository no longer cloned. It never reads the
   file it replaces, so a field your store wrote there, such as deployed
   services, is lost without an error. Before the first run, move each
   repository's own fields into `knowledge/relationships/extra/<repo>.json`, a
   JSON object named after the clone directory. The stage copies that object,
   uninterpreted, into the `extra` field of the repository's `by-repo` file, and
   refuses when it is not a JSON object. See
   [The relationships layer](how-it-works.md#the-relationships-layer).

   **Say how the estate's charts are found.** Set `KSB_UMBRELLA_REPOS` to the
   repositories that republish other repositories' charts, comma-separated, and
   `KSB_CHART_REPO_PATTERN` when the estate names chart repositories by a
   convention such as `chart-{name}`. Without them, a chart defined in several
   repositories resolves less well. The order they take part in is under
   [The relationships layer](how-it-works.md#the-relationships-layer).

   ```bash
   export KSB_STORE_ID=<id>   # or give the clone an `origin` remote instead
   export KSB_UMBRELLA_REPOS=<wrapper-repo>       # if any repository republishes others' charts
   export KSB_CHART_REPO_PATTERN='chart-{name}'   # only if chart repositories follow a naming rule
   knowledgestore relationships
   knowledgestore source-urls
   knowledgestore index
   knowledgestore manifest
   ```

   `source-urls` rewrites the committed graph, `graphify-out/graph.json` and
   `graph.json.gz`, adding to every node it can link a `url` pinned to the
   commit the store was built from. The server returns that `url` as written
   and never builds one, so without it answers cite store-relative paths. It
   reads the uncompressed `graph.json` (`gunzip -k graphify-out/graph.json.gz`
   first if you only have the archive), `knowledge/provenance.json`, which
   `knowledgestore sync` writes, and `config/repositories.txt`. It refuses,
   writing nothing, when the provenance file is missing or records no
   repository, or when the repository list is missing or configures none of the
   graph's repositories.

   Until `relationships` has run, a relationships layer the store wrote itself
   carries no format stamp. The manifest marks it present but not recognised,
   and the server treats it as absent, so its dependency tools return nothing.

   `manifest` refuses, writing nothing, when it can name the store from neither
   `KSB_STORE_ID` nor the repository name of the clone's `origin` remote. It
   measures the files the other stages wrote, so run it after every stage that
   writes a layer.

4. **Commit the new artefacts and the lock, and ignore the plain index.** Add to
   `.gitignore`:

   ```gitignore
   knowledge/index/store.sqlite
   ```

   Then commit:

   ```bash
   git add .gitignore requirements.txt requirements.lock \
     knowledge/store-manifest.json knowledge/relationships/ \
     knowledge/index/store.sqlite.gz graphify-out/graph.json.gz
   ```

   GitHub refuses a file over 100 MB, which is why the compressed index is the
   one committed. See [Build the query index](creating-a-store.md#build-the-query-index)
   when the archive itself is too large.

5. **Add the server to the store plugin.** Create the plugin's `.mcp.json` as
   [Add the server to the store plugin](creating-a-store.md#add-the-server-to-the-store-plugin)
   shows. Its `--store-id` is the manifest's `store.id`, not the plugin name.

6. **Trim the store's own skill to routing and the estate's limits.** The server
   now carries the answering rules, so a store skill that restates how to read
   the graph, search the corpus or cite sources becomes a second set of rules
   that can disagree with it. Keep what only the store knows: which questions it
   covers, what the estate leaves out, and where to send everything else.

7. **Publish the plugin and tell askers to update.** Send them
   [For askers](#for-askers).

## For askers

Your assistant now answers through the knowledge-store server, which runs on
your machine with `uv`. Do these once per machine, then once per store.

1. **Install `uv`.** On macOS:

   ```bash
   brew install uv
   uv --version
   ```

   Other platforms: [Installing uv](https://docs.astral.sh/uv/getting-started/installation/).

2. **Update both plugins**, the `knowledge-store` plugin and the store's own,
   as [Update the plugin](asking-questions.md#update-the-plugin) describes. The
   store's plugin updates the same way, through its own marketplace. Then
   restart Claude Code, so it starts the server the store's plugin now
   declares.

3. **Pull the store clone**, so it holds the store's manifest:

   ```bash
   git -C <store-clone> pull
   ```

4. **Register a clone you made by hand.** The server finds a clone through
   `~/.config/knowledge-store/locations` (or `KNOWLEDGE_STORE_PATH`). The skill
   writes that file whenever it settles on a clone, so a clone it has used is
   already listed. For any other clone:

   ```bash
   cd <store-clone>
   mkdir -p ~/.config/knowledge-store
   grep -qxF "$(pwd)" ~/.config/knowledge-store/locations 2>/dev/null \
     || printf '%s\n' "$(pwd)" >> ~/.config/knowledge-store/locations
   ```

5. **Repair a clone with no graph.** A clone that has no `graphify-out/`
   directory needs the repair in
   [My clone has no graph](asking-questions.md#my-clone-has-no-graph); `git pull`
   does not fix it.

In Claude Code, `/mcp` lists `knowledge-store` and whether it started. When it
did not, `/knowledge-store:knowledge-store` names which of these steps is
missing.

## For GitHub Copilot and other MCP clients

There is nothing to migrate: v0.18 is the first release in which these clients
answer from a store. Set one up from the server's
[front doors](https://github.com/hmcts/knowledge-store-mcp#front-doors)
section.
