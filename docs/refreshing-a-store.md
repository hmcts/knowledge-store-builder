# Refreshing and maintaining a knowledge store

Bring an existing knowledge store up to date without losing authored community
summaries. This guide also covers moving a store to another library release.
To build a store for the first time, see
[Creating a knowledge store](creating-a-store.md).

## Choose what to do

| You want to | Start with |
|---|---|
| Bring source repositories and generated artefacts up to date | [Refresh the store](#refresh-the-store) |
| Take repositories out of the estate | [Remove repositories](#remove-repositories) |
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
**Take it immediately before each re-cluster**, not once per session: a snapshot
of a clustering the summaries are no longer keyed to is not refused, it just
retains less, silently. Two re-clusters in one refresh need two snapshots.

Run `knowledgestore summaries adrift` **before** that snapshot, on the store as
committed. It answers the question a coverage count cannot: whether the committed
snapshot still describes the committed graph, and so whether each summary still
describes the community it names. Community ids are positional, so a store whose
prose has been silently re-pointed reports the same `status` coverage as one whose
has not.

Two things about when it means something:

- **It is vacuous immediately after `summaries snapshot`.** The snapshot is then
  taken from the graph it is compared against, so every summary matches by
  construction. Run it on a checkout, in CI, or at the start of a refresh — any
  point where the two committed artefacts might have gone out of step.
- **Exit 1 is drift; exit 2 means the check could not run**, and the two need
  opposite responses. An unreadable membership — a clustering step that printed
  success without persisting its result, say — makes every summary compare as
  adrift, and the answer to that is to fix the graph, never to re-author prose.

If the store reads its issue tracker, run `fetch-tickets` after `intent`, which
is the stage that discovers which tickets exist:

```bash
knowledgestore fetch-tickets
```

It asks the tracker only about tickets it has never had an answer for, so a
refresh costs requests for the tickets the refresh added. Read three numbers from
its report:

- **denied** — tickets waiting on access this token does not have. They are
  retried by every later run, so a colleague with broader permissions can close
  the gap without any change to the store.
- **undecided prefixes** — prefixes in neither `KSB_TRACKER_PROJECTS` nor
  `KSB_TRACKER_DENY`, listed in `knowledge/intent/tracker-undecided.json`.
  Nothing was requested for them. Decide, then re-run.
- **redacted** — identifiers withheld from fetched text, under the same rules as
  mined commit text. Carry it into your report: it is a finding about the tracker
  rather than a build statistic.

Failures are normal and not fatal: a 5xx, a dropped connection or a page the
tracker refused outright is not cached, so the next run retries it. If a whole
page keeps failing, lower `KSB_TRACKER_PAGE_SIZE` — a tracker that rejects a
search because one key in it is unreadable rejects the whole page.

A build with no tracker credentials skips the stage and reads the committed
cache, so `knowledge/intent/ticket-tracker.json.gz` has to be committed like any
other layer.

Review discovery and sync counts, then repeat
[Build the graph](creating-a-store.md#build-the-graph) against every configured
repository. Re-clustering can change community IDs even when the estate
contains the same repositories.

Only repositories whose sources moved need re-extracting; a repository's own
graph does not change because another left the estate. Record every clone's
`HEAD` before `sync`, compare afterwards, and re-extract the ones that differ.
Then reconcile the number of per-repository graphs against
`config/repositories.txt` before merging, because a loop that skipped
repositories still exits zero.

After clustering, record which partitioner produced the new IDs, then carry
summaries onto them by membership overlap:

```bash
knowledgestore record-clustering
knowledgestore summaries remap
knowledgestore summaries snapshot
knowledgestore summaries extract
```

The second `snapshot` re-keys the baseline to the clustering the remapped
summaries now sit on. Without it the committed snapshot describes the graph the
store no longer has, and the next refresh remaps from a baseline that is
consistently wrong — the one state `remap`'s own guards cannot see.

Read the retention reported by `remap` — and before you attribute a low figure to
the corpus, read what `status` says about the partitioner. A refresh run where
`graspologic` is installed or absent differently from the build that produced the
committed communities re-keys all of them, and the retention loss then has nothing
to do with the estate. Author and merge the uncovered summary
digests, then run `summaries verify`. Regenerate the semantic index when the
graph vocabulary or summaries changed materially, and refresh any affected
topic briefs and deep dives.

Recompress the graph, rebuild the page and check store health and source drift:

```bash
gzip -9 -n -c graphify-out/graph.json > graphify-out/graph.json.gz
knowledgestore explorer
knowledgestore check-evidence
knowledgestore status
knowledgestore status --drift
```

`intent` reports how many mined values it withheld as identifying a case or a
person, and `check-evidence` fails if any remain in the committed artefact —
including ones a store committed before the rule existed. Read that count as a
finding about the estate: the commit messages still carry the text, whatever the
store now publishes. See
[Redacting text that identifies a person or a record](how-it-works.md#redacting-text-that-identifies-a-person-or-a-record).

Commit the refreshed artefacts described in
[Publish the store](creating-a-store.md#publish-the-store). Report which stages
ran, what authored coverage remains, whether grounding checks passed and
whether the source-drift check is clean.

## Remove repositories

Removal is the more dangerous direction, because **nothing in the pipeline
prunes what you remove**. Change the filters, re-resolve, then delete the
leftovers by hand:

```bash
# config/repository-filters.txt: an `exclude <name>` line, or drop the rule
# that selected them. Exclusion always wins over any include.
knowledgestore discover                    # rewrites config/repositories.txt
rm -rf repositories/<repo>                 # else it re-enters the graph
rm -rf knowledge/git-history/<repo>        # else its tickets re-enter the index
```

Both deletions matter, for the same reason: those stages read the filesystem,
not the configuration.

| Left in place | What happens |
|---|---|
| `repositories/<repo>` | `merge-graphs` is given a shell glob of per-repository graphs, so a removed repository stays in the merged graph |
| `knowledge/git-history/<repo>` | `intent` globs `*/commits.ndjson`, so removed repositories keep contributing file-to-ticket links |

`sync --prune` prunes git refs, not repositories. `knowledge/provenance.json` is
the one thing that self-corrects, because `sync` rewrites it from the configured
set. Before merging, check for clones and history directories that are not in
`config/repositories.txt` at all — an orphan from an earlier estate is invisible
until it turns up in an answer.

Then continue as a normal refresh: snapshot, rebuild the graph, cluster, remap.
Removing repositories moves community IDs exactly as adding them does, and
`remap` will report the summaries whose members are gone — that count should
match the number of summaries describing the removed repositories, and is a
correct loss rather than a regression.

Authored prose does not fix itself. Topic briefs and deep dives still cite the
removed repositories and no gate catches it:

```bash
grep -rl '<repo>' docs/topics/ docs/deep-dives/
rm docs/deep-dives/<repo>.md && knowledgestore deepdive merge
```

Finally, check the estate's own regression tests: one that asserts an answer
naming a removed repository will fail, correctly, and needs replacing rather
than deleting if it was the only cover for that question shape.

## When clustering will not persist

`graphify cluster-only` can compute a clustering, decline to write it, and still
print that it updated the graph. Its writer refuses a net reduction in node
count, and the refusal is not fatal to the run.

**Verify the artefact, never the exit code.** After any clustering step:

```bash
python3 -c "
import json
n = json.load(open('graphify-out/graph.json'))['nodes']
have = sum(1 for x in n if x.get('community') is not None)
print(f'{len(n)} nodes, {have} clustered ({have/len(n)*100:.1f}%)')"
```

Anything below 100% means the clustering did not land. Check the file's
modification time too — a discarded write leaves the previous graph in place
while labels, signatures and `GRAPH_REPORT.md` are rewritten for the clustering
that was thrown away, so the outputs no longer agree with each other. Restore
those three from version control before trying again.

When the writer refuses, load the graph yourself and drive the public API. The
node count reaching the writer then matches the file on disk, so there is
nothing for the guard to fire on, and the output keeps graphify's own format
(`community`, `community_name`, `norm_label` — the last is graphify-internal,
which is why hand-writing the graph is not a substitute):

```python
from graphify.cluster import (cluster, community_member_sigs,
                              label_communities_by_hub, remap_communities_to_previous)
from graphify.export import to_json
from graphify.paths import load_node_link_graph

G = load_node_link_graph("graphify-out/graph.json")
communities = cluster(G)
communities = remap_communities_to_previous(communities, previous_node_community)
labels = label_communities_by_hub(G, communities)      # or reuse by signature
assert to_json(G, communities, "graphify-out/graph.json", community_labels=labels)
```

`remap_communities_to_previous` is the step that matters and the step
`cluster-only` does not take: it aligns new community IDs with the previous ones
by membership, which is what lets authored per-cluster prose survive.
`previous_node_community` is `{node_id: community_id}`, and
`knowledge/summaries/membership-snapshot.json.gz` already holds it the other way
round — invert it. Without this, clustering from scratch renames most
communities, and every summary keyed to a renamed ID is dropped by `remap` for
no reason connected to the change you made.

Do not pass `force=True` to work around the guard unless you can account for the
reduction. Tracked upstream as
[graphify#2436](https://github.com/Graphify-Labs/graphify/issues/2436).

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

### Deciding whether a local check can go

A release often absorbs something a store had built for itself, and the obvious
move is to delete the local copy. Do that only after comparing the **failure
mode**, not the feature. Two checks that detect the same condition and disagree
about whether it stops the build are not the same check.

| The library now… | Do this |
|---|---|
| detects it and **refuses** | delete the local guard |
| detects it and **reports** | keep it, and record the divergence |
| detects a **narrower** case | keep it, and note what is still uncovered |

Reporting is usually the right default for the library, because refusing would
break pipelines it does not own. That is exactly why it may be the wrong default
for one store: **a line on stderr in a run that exits 0 is indistinguishable from
no line at all**, so a check that reported where yours refused has quietly stopped
being a gate.

Where you keep a local check, write the divergence into the check itself rather
than into a note beside it. The person who deletes it later will be reading the
code, and "the library covers this now" is a reasonable-looking conclusion that
the code is the only place to contradict.

Two failures, both reported by store operators:

- **Swapping a refusing check for a reporting one.** It costs nothing on the day
  and everything on the day it matters.
- **Deleting a set of checks because the release notes mention the area.** One
  operator was told two local checks were now redundant; only one was, because
  the other detected a condition the library still does not detect at all. Verify
  each one against the library's actual behaviour, not against a summary of it -
  including a summary from whoever maintains the library.

## Troubleshooting

| Symptom | Action |
|---|---|
| `summaries remap` would discard most prose | Stop. Verify clustering coverage before remapping; a clustering command can report success without saving its result. |
| `summaries adrift` reports drift | The committed snapshot no longer describes the committed graph, so the prose is keyed to communities that moved. Re-take the snapshot from the graph the store ships, then remap or re-author what the report names. |
| `summaries adrift` exits 2 | The check could not run and the message names why — no membership read, the wrong snapshot, or no graph. Fix that and re-run; do not read it as drift. |
| `status` says the page is older than an embedded layer | Run `knowledgestore explorer` again and commit the rebuilt page. |
