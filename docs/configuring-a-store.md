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
| `KSB_SENSITIVE_PATTERNS` | email address (the only shipped rule; estates declare their own formats) | Extra rules for mined commit text that must not be stored, as a JSON object of rule name → regex merged over the defaults: `KSB_SENSITIVE_PATTERNS='{"listing-reference": "\\bREF/[0-9]{4}\\b"}'`. Anything matching a rule is replaced by a placeholder naming what was removed, and counted in the run report; a value left with only placeholders is not stored. Malformed JSON raises rather than emptying the rules — see [Redacting text that identifies a person or a record](how-it-works.md#redacting-text-that-identifies-a-person-or-a-record) |
| `KSB_AUTOMATION_IDENTITIES` | `jenkins,renovate,snyk,greenkeeper,devops-team,embedded_devops_sa` | Author or committer identities whose commit bodies are not treated as evidence. Matched as whole words against the name and the email's local part, so narrow it if a contributor shares a name with a build server — the run reports which identities it filtered. GitHub App accounts are always excluded via `[bot]` and need no entry; an empty value leaves only that rule |

The full set, including tuning thresholds, is defined in
[`src/knowledgestore/config.py`](../src/knowledgestore/config.py). A store can
keep its values in a file such as `config/pipeline.sh`; source it before each
build or refresh.

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
