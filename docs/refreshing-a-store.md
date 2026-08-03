# Refreshing and maintaining a knowledge store

Bring an existing knowledge store up to date without losing authored community
summaries. This guide also covers moving a store to another library release.
To build a store for the first time, see
[Creating a knowledge store](creating-a-store.md).

## Choose what to do

| You want to | Start with |
|---|---|
| Bring source repositories and generated artefacts up to date | [Refresh the store](#refresh-the-store) |
| Change the library release used by the store | [Update the library version](#update-the-library-version) |

## Refresh the store

Open Claude Code in the store directory and run
`/knowledge-store:knowledge-store-build`. Snapshot existing community
membership before re-clustering, then refresh the deterministic layers:

```bash
source .venv/bin/activate
knowledgestore summaries snapshot
knowledgestore discover
knowledgestore sync
knowledgestore export-history
knowledgestore context
knowledgestore intent
```

Skip `summaries snapshot` only when the store has no summaries to preserve.
Review discovery and sync counts, then repeat
[Build the graph](creating-a-store.md#build-the-graph) against every configured
repository. Re-clustering can change community IDs even when the estate
contains the same repositories.

After clustering, carry summaries onto new IDs by membership overlap:

```bash
knowledgestore summaries remap
knowledgestore summaries extract
```

Read the retention reported by `remap`. Author and merge the uncovered summary
digests, then run `summaries verify`. Regenerate the semantic index when the
graph vocabulary or summaries changed materially, and refresh any affected
topic briefs and deep dives.

Recompress the graph, rebuild the page and check store health and source drift:

```bash
gzip -9 -n -c graphify-out/graph.json > graphify-out/graph.json.gz
knowledgestore explorer
knowledgestore status
knowledgestore status --drift
```

Commit the refreshed artefacts described in
[Publish the store](creating-a-store.md#publish-the-store). Report which stages
ran, what authored coverage remains, whether grounding checks passed and
whether the source-drift check is clean.

## Update the library version

Activate the store's environment:

```bash
source .venv/bin/activate
```

Do this only when deliberately moving a store to another library release. Find
the version on the
[knowledge-store-builder releases page](https://github.com/hmcts/knowledge-store-builder/releases),
then change `X.Y.Z` in the store's pinned requirements file — `requirements.txt`
in a store that follows the reference layout. Keep the exact `==` pin, so that
rebuilding the store resolves the same versions it was built with.

```text
--extra-index-url https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/
--only-binary :all:
hmcts-knowledge-store-builder==X.Y.Z
```

Install `uv` if it is not already available. On macOS:

```bash
brew install uv
```

Recompile and install the lock file:

```bash
uv pip compile requirements.txt --generate-hashes \
  --emit-index-url --emit-build-options \
  --output-file requirements.lock
pip install -r requirements.lock
```

The emit flags write the package feed and the binary-only setting into the lock,
so installing from it needs no extra arguments.

**A lock compiled without them does not name the feed**, and installing from it
fails with `No matching distribution found` until `--extra-index-url` is passed
on the command line. Existing stores can be in that state; recompiling with the
flags above is what removes the need for it.

Either way, `knowledgestore check-install-docs` keeps the lock and the store's
own documentation in step: while the lock names no index, every documented
command that installs from it has to pass one. It exits non-zero, so put it in
CI - it was written after a store's README spent weeks telling readers to run a
command that could not work.

The library and plugin update separately: this changes the pinned library
release, not the installed plugin. See
[Update the plugin](asking-questions.md#update-the-plugin) when you need newer
skills.

## Troubleshooting

| Symptom | Action |
|---|---|
| `summaries remap` would discard most prose | Stop. Verify clustering coverage before remapping; a clustering command can report success without saving its result. |
| `status` says the page is older than an embedded layer | Run `knowledgestore explorer` again and commit the rebuilt page. |
