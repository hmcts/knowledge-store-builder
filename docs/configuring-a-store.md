# Configuring a knowledge store

Change pipeline behaviour when the defaults do not fit the estate. For the
build sequence, see [Creating a knowledge store](creating-a-store.md); for
source updates, see [Refreshing a knowledge store](refreshing-a-store.md).

## Configure the pipeline

Most settings have defaults. `KSB_GITHUB_ORG` is required for discovery.

| Variable | Default | Purpose |
|---|---|---|
| `KSB_ROOT` | current directory | Store root; `knowledgestore --root <path>` sets the same value for one command |
| `KSB_GITHUB_ORG` | none | GitHub organisation used by discovery |
| `KSB_TICKET_PATTERN` | uppercase project key and number | Ticket IDs recognised in commit messages |
| `KSB_TICKET_BROWSE_URL` | none | URL prefix used to turn ticket IDs into links |
| `KSB_EXPLORER_TITLE` | `Estate Explorer` | Browser-page title |
| `KSB_BRIEF_REQUEST_URL` | none | Destination for “request a topic brief”; unset hides the link |
| `KSB_E2E_REPOS` | none | Repositories whose test code should be indexed as business documentation |
| `KSB_FEATURES_DIR` | `features/` | Feature-directory segment used to group Gherkin features |
| `KSB_SENSITIVE_PATTERNS` | email address (the only shipped rule; estates declare their own formats) | Extra rules for mined commit text that must not be stored, as a JSON object of rule name → regex merged over the defaults: `KSB_SENSITIVE_PATTERNS='{"record-reference": "\\bREC/[0-9]{4}\\b"}'`. Anything matching a rule is replaced by a placeholder naming what was removed, and counted in the run report; a value left with only placeholders is not stored. Malformed JSON raises rather than emptying the rules — see [Redacting text that identifies a person or a record](how-it-works.md#redacting-text-that-identifies-a-person-or-a-record) |
| `KSB_SECRET_PATTERNS` | the shipped secret shapes (private keys, SAS signatures, connection-string keys, bearer tokens, URL passwords and common token formats) | Extra value shapes masked out of the copies a headless extraction worker reads, as a JSON object of rule name → regex merged over the defaults: `KSB_SECRET_PATTERNS='{"estate-pin": "PIN-([0-9]{4})"}'`. A rule with capturing groups masks the first that matched, one without masks its whole match, and a group named `guarded` is masked only when its value looks like a credential. Masking is pattern-based, so a secret in a shape no rule names is not masked — see [the headless route](../skills/knowledge-store-build/SKILL.md#the-headless-route-workers-extract) |
| `KSB_TRACKER_TRANSPORT` | `rest` | How `fetch-tickets` reaches the tracker. `rest` is Jira Data Center or Server over HTTP, using the two settings below. `twg` is Jira Cloud through the Atlassian TWG CLI, which signs in by itself, so it needs neither — see [Read Jira Cloud through the TWG CLI](#read-jira-cloud-through-the-twg-cli). Any other value stops the stage before it requests anything |
| `KSB_TRACKER_BASE_URL` | none | Issue-tracker API root for the `rest` transport, for example `https://tracker.example/jira`: Bearer authentication and `GET /rest/api/2/search`, which Jira Cloud has removed. Empty means the `rest` transport is not configured and the stage does nothing |
| `KSB_TRACKER_TOKEN` | none | Data Center personal access token, sent as `Authorization: Bearer`. Never written to an artefact, a summary line or an error message |
| `KSB_TRACKER_PROJECTS` | none | Comma-separated ticket prefixes this store may read, for example `AAA,BBB`. Empty means none, which is not the same as all |
| `KSB_TRACKER_DENY` | none | Prefixes that must never be requested, whatever the allowlist says. A deny entry wins, so an allowlist edit cannot re-enable a project somebody withdrew |
| `KSB_TRACKER_FETCH_DESCRIPTION` | `false` | Add each ticket's description to the request |
| `KSB_TRACKER_FETCH_COMMENTS` | `false` | Add each ticket's comments to the request |
| `KSB_TRACKER_PAGE_SIZE` | `100` | Tickets per search request |
| `KSB_TRACKER_DELAY_SECONDS` | `1` | Pause between requests |
| `KSB_TICKET_REFRESH_DAYS` | `7` | Days before an unresolved ticket's record is requested again. Resolved tickets are never requested again. `0` turns refresh off — see [Refreshing open tickets](#refreshing-open-tickets) |
| `KSB_TICKET_REFRESH_MAX` | `200` | Most open tickets one run requests again, oldest first. The rest wait for later runs |
| `KSB_AUTOMATION_IDENTITIES` | `jenkins,renovate,snyk,greenkeeper,devops-team,embedded_devops_sa` | Author or committer identities whose commit bodies are not treated as evidence. Matched as whole words against the name and the email's local part, so narrow it if a contributor shares a name with a build server — the run reports which identities it filtered. GitHub App accounts are always excluded via `[bot]` and need no entry; an empty value leaves only that rule |

The full set, including tuning thresholds, is defined in
[`src/knowledgestore/config.py`](../src/knowledgestore/config.py). A store can
keep its values in a file such as `config/pipeline.sh`; source it before each
build or refresh.

## Read ticket detail from the issue tracker

`fetch-tickets` asks the tracker what each discovered ticket is and commits the
answer to `knowledge/intent/ticket-tracker.json.gz`. It is **opt-in**: with no
base URL or token it names the missing settings, writes nothing, and the rest of
the pipeline runs unchanged. A resolved ticket that has been fetched is never
fetched again, so a later build with no credentials reads the committed cache
rather than degrading. An unresolved ticket is still changing: its record carries
the date it was read, and a run requests it again once that date is
`KSB_TICKET_REFRESH_DAYS` old, within the bounds described in
[Refreshing open tickets](#refreshing-open-tickets).

By default the stage speaks Jira Data Center or Server: a personal access token
sent as a Bearer credential, and `GET /rest/api/2/search`. Against Jira Cloud that
search answers 404 or 410, and the run says so by name and points at the `twg`
transport rather than printing the status alone.

Requests are batched into one search per `KSB_TRACKER_PAGE_SIZE` keys, with a
single request in flight, `Retry-After` honoured, and a pause between pages. The
field projection is requested from the server, not trimmed locally: description
and comments are added to the request only when their settings are on, so a
response that never carried narrative text is a guarantee rather than a claim.
Everything fetched goes through the same withholding rules as mined commit text
(`KSB_SENSITIVE_PATTERNS`), and the run reports how many identifiers went.

**A prefix is in one of three states, not two.** In `KSB_TRACKER_PROJECTS` it is
fetched; in `KSB_TRACKER_DENY` it is never requested; in neither it is
*undecided* — not requested, and written to
`knowledge/intent/tracker-undecided.json` with its ticket count for a person to
decide about. Nothing is ever silently skipped or silently fetched, because
failing closed on unknown prefixes can discard most of an estate's tickets while
reporting success. Running the stage with an empty allowlist is the usual way to
produce that list in the first place.

Each ticket ends up in one of four states, and the difference between the last
two matters:

| Outcome | Cached as | Asked about again |
|---|---|---|
| Fetched, resolved | the projected fields, redacted | No |
| Fetched, unresolved | the projected fields, redacted, with the date | Once the date is `KSB_TICKET_REFRESH_DAYS` old |
| Not in the response | `absent`, with the date | No |
| Access denied (401, 403) | `denied`, with the date | **Yes** |
| Transport error, 5xx, or a reply that is not a search response | not cached | Yes |

A token carries one person's permissions, so a denial records what this operator
could read, not what exists. Recorded as absence it would never be retried and
the gap would become permanent and invisible; every run reports how many tickets
are waiting on access it does not have.

### Refreshing open tickets

A ticket's status and text change until it is resolved, so the stage asks about
an open ticket again once its record is old enough. Two settings bound what that
costs:

| Setting | What it buys | What it costs |
|---|---|---|
| `KSB_TICKET_REFRESH_DAYS` (default `7`) | How stale an open ticket's record can get | A shorter interval makes more records due each run. `0` turns refresh off: open tickets keep the record from their first fetch, which is how the stage behaved before refresh existed |
| `KSB_TICKET_REFRESH_MAX` (default `200`) | How many due records one run brings up to date | At most `KSB_TICKET_REFRESH_MAX ÷ KSB_TRACKER_PAGE_SIZE` extra requests per run, rounded up — two at the defaults — each followed by the `KSB_TRACKER_DELAY_SECONDS` pause |

Refreshes go through the same batched search as new tickets, in pages of their
own after them, so they add whole requests rather than one per ticket. When more
records are due than the cap allows, the oldest go first and the rest stay due:
the first run after upgrading from a release without refresh finds every open
record due, and that backlog drains over several runs rather than landing on one.
Only tickets the store still cites are refreshed; a cached ticket that no commit
mentions any more, or whose prefix is now in `KSB_TRACKER_DENY`, is kept as it
is and never requested.

Each run prints `refreshed N of M due, with K left for later runs, in R
requests`, and records `tickets.refreshed`, `tickets.refresh_due` and
`tickets.refresh_requests` in `knowledge/telemetry.json`, so the next run says
whether the cost moved.

A refresh never downgrades a record. If the tracker leaves a fetched ticket out
of its reply, or denies the page, the earlier record is kept and the run reports
it as kept: a search omits tickets the token cannot browse, so an operator with
narrower access would otherwise turn known text into a permanent `absent`.
A ticket resolved by either a resolution name or a resolution date counts as
resolved.

### Read Jira Cloud through the TWG CLI

Jira Cloud has removed the search endpoint the default transport uses. With the
Atlassian TWG CLI installed and signed in, set the transport and run the stage as
usual:

```bash
twg jira workitem get ABC-1 -o json      # confirms twg is on PATH and signed in
export KSB_TRACKER_TRANSPORT=twg
export KSB_TRACKER_PROJECTS=ABC
knowledgestore fetch-tickets
```

If the first command does not print the ticket as JSON, the stage cannot read
anything either; it records every page as failed, caches nothing, and says to
check `twg`. No base URL or token is needed, and none passes through the library.

Only the transport changes. The allowlist, deny list and undecided report, the
field projection, redaction, the four outcomes, refresh and its bounds, and the
cache writer are the same code on both routes. What the `twg` route adds:

- **One `bulk-get` per page.** Each page of keys becomes one `twg jira workitem
  bulk-get` asking for exactly the fields the projection asked for, so
  descriptions and comments are requested only when their settings are on.
- **Bodies flattened to text.** Cloud descriptions and comments are structured
  documents. Each block — a paragraph, a heading, a list item, a table cell —
  becomes one line.
- **No names.** A mention is stored as `[~accountid:<id>]`, never as the display
  name the document carries beside it, and a comment's author is dropped before
  the stage sees the comment. An inline node the transport does not know renders
  as nothing rather than being trusted to hold only text.
- **The exit code is not the answer.** `twg` exits non-zero whenever a key in the
  page was not returned, and still prints the full result; the stage reads that
  output either way. Output it cannot read, or that leaves out a key it asked
  about, is no answer: nothing is cached and the next run asks again.
- **Moved tickets are followed once.** `bulk-get` reports a ticket moved to
  another project as not returned. Each such key gets one `twg jira workitem
  get`, which follows the move, and the ticket is filed under the key the
  commits cite — but only when its new project is in `KSB_TRACKER_PROJECTS` and
  not in `KSB_TRACKER_DENY`. A ticket that moved to an undecided project stays
  absent.

One limit to know: `bulk-get` returns a page of each ticket's most recent
comments rather than the whole thread, so a ticket with a long thread is stored
without its earliest comments on this route.

## Use BDD specifications

The `gherkin` stage reads `.feature` files wherever they occur and links their
features and scenarios to matching step definitions:

| Language | Default search | Recognised declaration |
|---|---|---|
| Java | `src/test/java/**/*.java` | `@Given("...")` and the other Cucumber annotations |
| Python | `**/*.py` | `@given("...")` from behave or pytest-bdd |
| TypeScript | `**/*.ts` | `Given("...", ...)` from cucumber-js |

Cucumber expressions, typed behave parameters, regular-expression groups and
quoted values are normalised before matching. Override
`config.STEP_DEFINITION_LANGUAGES` through the Python configuration API when an
estate uses another language or layout.

## Stage reference

| Stage | Main output | Purpose |
|---|---|---|
| `discover` | `config/repositories.txt` | Resolve the estate from reviewed filters |
| `sync` | `repositories/`, `knowledge/provenance.json` | Clone or update sources and record their commits |
| `export-history` | `knowledge/git-history/` | Create per-commit NDJSON and Markdown datasets |
| `context` | `knowledge_context.md`, `knowledge/repository-manifest.md` | Record how to interpret the estate and what was read |
| `intent` | `knowledge/intent/*.json.gz`, `knowledge/telemetry.json` | Link files to tickets and mine ticket descriptions |
| `ticket-titles` | `knowledge/intent/ticket-titles.json.gz` | Import real issue titles from CSV |
| `fetch-tickets` | `knowledge/intent/ticket-tracker.json.gz`, `knowledge/intent/tracker-undecided.json` | Ask the issue tracker about discovered tickets (opt-in; needs credentials) |
| `size-cuts` | report, `knowledge/telemetry.json` | Size each candidate content cut in `config/content-cuts.txt` by the nodes and edges it would keep |
| `merge-inputs` | report only | Name the graphs `merge-graphs` would read and reconcile them against the declaration and provenance |
| `gherkin` | updated graph and labels | Add features, scenarios, ticket nodes and step-definition links |
| `packages` | updated graph | Add cross-repository package nodes and import edges (npm layer) |
| `summaries` | `knowledge/summaries/` | Extract, merge, verify and remap community prose |
| `semantic` | `knowledge/semantic/token-neighbours.json.gz` | Bridge vocabulary gaps at query time |
| `topics` | `docs/topics/`, `knowledge/topics/briefs.json` | Add pre-written answers to recurring questions |
| `deepdive` | `docs/deep-dives/`, `knowledge/deep-dives/` | Add a provenance-stamped repository dossier |
| `explorer` | `graphify-out/explorer.html`, `knowledge/telemetry.json` | Build the self-contained search and Q&A page |
| `status` | report only | Report provenance, coverage, citations, freshness, recorded telemetry and optional drift |
| `check-install-docs` | report only | Check the documented install commands against what the lock declares |
| `check-evidence` | report only | Fail if a committed ticket-descriptions artefact holds mined text matching a withholding rule |
| `check-citations` | report only | Fail if a published answer cites code the graph does not hold; name the nearest real identifier and list database columns apart |
