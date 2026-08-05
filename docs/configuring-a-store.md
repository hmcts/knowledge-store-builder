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
| `KSB_TRACKER_BASE_URL` | none | Issue-tracker API root for the `fetch-tickets` stage, for example `https://tracker.example/jira`. Empty means the stage is not configured and does nothing |
| `KSB_TRACKER_TOKEN` | none | Personal access token, sent as `Authorization: Bearer`. Never written to an artefact, a summary line or an error message |
| `KSB_TRACKER_PROJECTS` | none | Comma-separated ticket prefixes this store may read, for example `AAA,BBB`. Empty means none, which is not the same as all |
| `KSB_TRACKER_DENY` | none | Prefixes that must never be requested, whatever the allowlist says. A deny entry wins, so an allowlist edit cannot re-enable a project somebody withdrew |
| `KSB_TRACKER_FETCH_DESCRIPTION` | `false` | Add each ticket's description to the request |
| `KSB_TRACKER_FETCH_COMMENTS` | `false` | Add each ticket's comments to the request |
| `KSB_TRACKER_PAGE_SIZE` | `100` | Tickets per search request |
| `KSB_TRACKER_DELAY_SECONDS` | `1` | Pause between requests |
| `KSB_AUTOMATION_IDENTITIES` | `jenkins,renovate,snyk,greenkeeper,devops-team,embedded_devops_sa` | Author or committer identities whose commit bodies are not treated as evidence. Matched as whole words against the name and the email's local part, so narrow it if a contributor shares a name with a build server — the run reports which identities it filtered. GitHub App accounts are always excluded via `[bot]` and need no entry; an empty value leaves only that rule |

The full set, including tuning thresholds, is defined in
[`src/knowledgestore/config.py`](../src/knowledgestore/config.py). A store can
keep its values in a file such as `config/pipeline.sh`; source it before each
build or refresh.

## Read ticket detail from the issue tracker

`fetch-tickets` asks the tracker what each discovered ticket is and commits the
answer to `knowledge/intent/ticket-tracker.json.gz`. It is **opt-in**: with no
base URL or token it names the missing settings, writes nothing, and the rest of
the pipeline runs unchanged. Historic tickets do not change, so a ticket that has
been fetched is never fetched again — a later build with no credentials reads the
committed cache rather than degrading.

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
| Fetched | the projected fields, redacted | No |
| Not in the response | `absent`, with the date | No |
| Access denied (401, 403) | `denied`, with the date | **Yes** |
| Transport error, 5xx, or a reply that is not a search response | not cached | Yes |

A token carries one person's permissions, so a denial records what this operator
could read, not what exists. Recorded as absence it would never be retried and
the gap would become permanent and invisible; every run reports how many tickets
are waiting on access it does not have.

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
| `intent` | `knowledge/intent/*.json.gz` | Link files to tickets and mine ticket descriptions |
| `ticket-titles` | `knowledge/intent/ticket-titles.json.gz` | Import real issue titles from CSV |
| `fetch-tickets` | `knowledge/intent/ticket-tracker.json.gz`, `knowledge/intent/tracker-undecided.json` | Ask the issue tracker about discovered tickets (opt-in; needs credentials) |
| `gherkin` | updated graph and labels | Add features, scenarios, ticket nodes and step-definition links |
| `packages` | updated graph | Add cross-repository package nodes and import edges (npm layer) |
| `summaries` | `knowledge/summaries/` | Extract, merge, verify and remap community prose |
| `semantic` | `knowledge/semantic/token-neighbours.json.gz` | Bridge vocabulary gaps at query time |
| `topics` | `docs/topics/`, `knowledge/topics/briefs.json` | Add pre-written answers to recurring questions |
| `deepdive` | `docs/deep-dives/`, `knowledge/deep-dives/` | Add a provenance-stamped repository dossier |
| `explorer` | `graphify-out/explorer.html` | Build the self-contained search and Q&A page |
| `status` | report only | Report provenance, coverage, citations, freshness and optional drift |
| `check-install-docs` | report only | Check the documented install commands against what the lock declares |
| `check-evidence` | report only | Fail if a committed ticket-descriptions artefact holds mined text matching a withholding rule |
