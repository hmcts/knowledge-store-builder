# Provenance, Status and Deep Dives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a knowledge store report its own staleness (provenance recording + a `status` stage), and add a `deepdive` stage that produces an evidence-grounded per-repository dossier — piloted on `cpp-context-progression`, the platform's most problematic service.

**Architecture:** Three additions to the existing stage pattern. (1) `sync` records each clone's HEAD into `knowledge/provenance.json`; the manifest and the explorer subtitle surface it. (2) A `status` stage reports four checks — source drift, layer coverage, dangling corpus citations, artefact freshness — and never fails the build (drift is normal; report, don't block). (3) `deepdive` follows the extract → agent-writes → merge shape proven by topic briefs: a deterministic evidence bundle per repo (scale, churn, instability, co-change coupling, hotspots, coupling surface, business features, timeline), a human/LLM-written dossier validated on merge, rendered and embedded in the explorer, served when a question names the repository.

**Tech Stack:** Python stdlib only (no new dependencies; `gh` CLI for the drift check, injectable and optional). Explorer changes in `src/knowledgestore/assets/app.js` (JSDoc-typed, `tsc --checkJs`-clean). Tests: `unittest` + the two Node harnesses.

## Global Constraints

- Library code lives in `src/knowledgestore/`; tests in `tests/`. Work on branch `feat/provenance-status-deepdive` off `main`.
- **No new runtime dependencies.** The core stays standard-library only. `gh` is invoked as a subprocess with an injectable runner, like `generate_repository_list.run_gh`.
- **Deterministic outputs.** No wall-clock timestamps written into artefacts. Provenance dates come from git commit data. Sort every collection before emitting; tiebreak by name.
- **Gates are blocking:** `ruff check`, `ruff format --check`, `pyright` (0 errors), `python3 -m unittest discover -s tests`, `node tests/explorer/engine-unit.mjs`, `python3 tests/explorer/fixture.py && node tests/explorer/page-regression.mjs`, and for app.js `tsc --checkJs --noEmit --target es2020 --lib es2020,dom`.
- Config values follow the existing pattern: module constant in `config.py`, `KSB_*` env override where user-facing, re-exported as a module-level name in the stage module so tests can monkeypatch.
- British English in all user-facing prose. No marketing language.
- Stage `main()` functions return an int exit code (0 success) and are registered in `cli.STAGES` with a lower-case one-line help string.
- Node kinds via `kinds.py` (`kinds.FEATURE`, `kinds.is_kind`) — never raw strings, old stores still carry `gherkin_feature`.
- The graph can be ~1.6 GB decompressed. Stages that load it must say so in their docstring; `status` must NOT load it (cheap by design) — the corpus citation check reads `graphify-out/graph-knowledge-corpus.json` (small, tracked).
- YAML/CI is untouched by this plan. No workflow changes needed.

## File Structure

```
src/knowledgestore/
  config.py               MODIFY  add PROVENANCE_PATH, deep-dive paths + thresholds
  provenance.py           CREATE  record/read per-repo HEAD info
  sync_repositories.py    MODIFY  record provenance after syncing
  build_knowledge_context.py MODIFY manifest gains a "Synced at" column from provenance
  build_explorer.py       MODIFY  subtitle gains "sources synced to <date>"; embed #dives block
  status.py               CREATE  the four checks + report printer
  build_deep_dives.py     CREATE  extract <repo> / merge, bundle building, validation
  cli.py                  MODIFY  register "status" and "deepdive"
  assets/app.js           MODIFY  DIVES block, matchDive(), serve dive when question names the repo
tests/
  test_provenance_and_status.py  CREATE
  test_build_deep_dives.py       CREATE
  test_build_explorer.py         MODIFY  smoke test covers #dives block + subtitle date
  explorer/fixture.py            MODIFY  add a dive to the synthetic estate
  explorer/engine-unit.mjs       MODIFY  dives stub + matchDive assertions
  explorer/page-regression.mjs   MODIFY  dive question shape + no-dive fallback
README.md                 MODIFY  stage table rows for status/deepdive
.claude/skills/knowledge-store-build/SKILL.md  MODIFY  deep-dive authoring loop + status
.claude/skills/knowledge-store/SKILL.md        MODIFY  read docs/deep-dives/ first; run status on recent-changes questions
CLAUDE.md                 MODIFY  note: status never blocks; deepdive loads the full graph
```

Data shapes introduced (canonical, used by every task below):

`knowledge/provenance.json`:
```json
{"repositories": {"<name>": {"sha": "<40-hex>", "branch": "main", "committed": "2026-07-30T09:14:02+01:00"}}}
```

`knowledge/deep-dives/<repo>-input.json` (the evidence bundle):
```json
{
  "repo": "cpp-context-progression",
  "provenance": {"sha": "…", "branch": "main", "committed": "…"},
  "scale": {"nodes": 0, "share": 0.0, "communities": 0,
            "top_communities": [{"id": 0, "label": "", "size": 0, "summary": null}]},
  "churn": {"files_with_history": 0,
            "top_files": [{"path": "", "tickets": 0, "first": "", "last": ""}]},
  "instability": {"tickets": 0, "revert_share": 0.0, "fix_share": 0.0,
                  "sample_reverts": [""], "sample_fixes": [""]},
  "timeline": {"2020": 0},
  "cochange": [{"a": "", "b": "", "n": 0}],
  "hotspots": [{"path": "", "tickets": 0, "degree": 0}],
  "coupling_surface": [{"label": "", "other_repos": [""]}],
  "features": [{"label": "", "tickets": [""]}],
  "summary_coverage": {"with": 0, "without": 0}
}
```

`knowledge/deep-dives/dives.json` (embedded in the page):
```json
{"<repo>": {"title": "Deep dive: <repo>", "html": "…", "source": "docs/deep-dives/<repo>.md", "sha": "<short-sha-or-empty>"}}
```

---

### Task 1: Provenance module, recorded by sync

**Files:**
- Create: `src/knowledgestore/provenance.py`
- Modify: `src/knowledgestore/config.py` (one path), `src/knowledgestore/sync_repositories.py`
- Test: `tests/test_provenance_and_status.py`

**Interfaces:**
- Consumes: `sync_repositories.run_git(arguments: list[str]) -> str`, `RepositoryConfig(name, clone_url, default_branch)`, `io.write_json`, `io.read_json_dict`.
- Produces: `config.PROVENANCE_PATH: Path`; `provenance.head_info(repo_dir: Path, branch: str, run=run_git) -> dict` returning `{"sha", "branch", "committed"}`; `provenance.write(entries: dict[str, dict]) -> None` (sorts keys, writes `PROVENANCE_PATH`); `provenance.read() -> dict[str, dict]` (returns `{}` when absent). `sync_repositories.main()` records provenance for every synced repo.

- [ ] **Step 1: Add the config path**

In `config.py`, after `SYNONYMS_PATH`:

```python
# What each repository's clone pointed at when the store was last built.
# Written by the sync stage; read by status, the manifest and the explorer.
PROVENANCE_PATH = ROOT / "knowledge" / "provenance.json"
```

And add the same line (key `PROVENANCE_PATH=root / "knowledge" / "provenance.json"`) to `_recompute_paths()`, and `"PROVENANCE_PATH"` to the `ROOTED` tuple in `tests/test_config_and_io.py`.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_provenance_and_status.py`:

```python
"""Provenance recording and the status stage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from knowledgestore import config, io, provenance


class HeadInfoTest(unittest.TestCase):
    def test_reads_sha_and_commit_date_from_git(self):
        calls = []

        def fake_git(arguments):
            calls.append(arguments)
            if "rev-parse" in arguments:
                return "a" * 40 + "\n"
            return "2026-07-30T09:14:02+01:00\n"

        info = provenance.head_info(Path("/tmp/x"), "main", run=fake_git)
        self.assertEqual(info, {
            "sha": "a" * 40, "branch": "main",
            "committed": "2026-07-30T09:14:02+01:00",
        })
        self.assertTrue(all("-C" in c for c in calls))


class WriteReadTest(unittest.TestCase):
    def test_round_trip_sorted_and_missing_file_is_empty(self):
        original = config.PROVENANCE_PATH
        self.addCleanup(config.configure, None, PROVENANCE_PATH=original)
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(PROVENANCE_PATH=Path(tmp) / "provenance.json")
            self.addCleanup(setattr, provenance, "PROVENANCE_PATH",
                            provenance.PROVENANCE_PATH)
            provenance.PROVENANCE_PATH = config.PROVENANCE_PATH
            self.assertEqual(provenance.read(), {})
            provenance.write({"zeta": {"sha": "z"}, "alpha": {"sha": "a"}})
            self.assertEqual(list(provenance.read()), ["alpha", "zeta"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run to verify failure**

Run: `python3 -m unittest tests.test_provenance_and_status -v` (from the repo root, after `pip install -e .`)
Expected: FAIL — `ImportError: cannot import name 'provenance'`.

- [ ] **Step 4: Implement `provenance.py`**

```python
"""What each repository's clone pointed at when the store was built.

Written by the sync stage, one entry per repository. This is the input every
staleness check needs: without a recorded SHA and commit date, "has the source
moved on?" cannot be answered. Dates are git commit dates, never wall-clock,
so two builds of the same clones produce identical output.
"""

from __future__ import annotations

from pathlib import Path

from . import config, io
from .sync_repositories import run_git

PROVENANCE_PATH = config.PROVENANCE_PATH


def head_info(repo_dir: Path, branch: str, run=run_git) -> dict:
    """The clone's current commit: sha, configured branch, commit date."""
    sha = run(["-C", str(repo_dir), "rev-parse", "HEAD"]).strip()
    committed = run(["-C", str(repo_dir), "log", "-1", "--format=%cI"]).strip()
    return {"sha": sha, "branch": branch, "committed": committed}


def write(entries: dict[str, dict]) -> None:
    io.write_json(
        PROVENANCE_PATH,
        {"repositories": dict(sorted(entries.items()))},
        indent=1,
    )


def read() -> dict[str, dict]:
    """Recorded provenance by repository name, or {} when never recorded."""
    data = io.read_json_dict(PROVENANCE_PATH)
    repos = data.get("repositories", {})
    return repos if isinstance(repos, dict) else {}
```

- [ ] **Step 5: Wire it into `sync_repositories.main()`**

Replace the existing loop body (currently `for repo in read_repository_config(CONFIG): print(...); count = sync_repository(...); print(...)`) with:

```python
    from . import provenance

    entries: dict[str, dict] = {}
    for repo in read_repository_config(CONFIG):
        print(f"\nSynchronising {repo.name}")
        count = sync_repository(repo, REPOSITORIES)
        print(f"{repo.name}: {count} commits available")
        entries[repo.name] = provenance.head_info(
            REPOSITORIES / repo.name, repo.default_branch
        )
    provenance.write(entries)
    print(f"\nProvenance recorded for {len(entries)} repositories "
          f"-> {provenance.PROVENANCE_PATH}")
    return 0
```

(Put the `from . import provenance` at module top with the other relative imports, not inside `main` — shown here only for locality. Import at top: `from . import config, provenance` alongside the existing `from . import config`; note `provenance` imports `run_git` from this module, so import it lazily inside `main()` to avoid the cycle: `from . import provenance` as the first line of `main()`.)

- [ ] **Step 6: Run the tests**

Run: `python3 -m unittest tests.test_provenance_and_status tests.test_repo_list_and_sync tests.test_config_and_io -v`
Expected: PASS (the sync tests inject `run=` and never execute git).

- [ ] **Step 7: Gates and commit**

```bash
python3 -m ruff format src tests && python3 -m ruff check src tests && python3 -m pyright
git add -A && git commit -m "feat: record per-repository provenance at sync time"
```

---

### Task 2: Surface provenance in the manifest and the explorer subtitle

**Files:**
- Modify: `src/knowledgestore/build_knowledge_context.py:29-53` (manifest table), `src/knowledgestore/build_explorer.py` (subtitle)
- Test: `tests/test_build_knowledge_context.py` (extend), `tests/test_build_explorer.py` (extend smoke test)

**Interfaces:**
- Consumes: `provenance.read() -> dict[str, dict]` (Task 1).
- Produces: manifest rows gain a `Synced at` column (`YYYY-MM-DD` + short SHA, or `—` when unrecorded). `build_explorer.PROVENANCE_PATH` module constant; the page subtitle contains `sources synced to <YYYY-MM-DD>` when provenance exists (the latest `committed` date across repos).

- [ ] **Step 1: Failing test for the manifest column**

Append to `tests/test_build_knowledge_context.py` (it already builds a manifest from a temp `HISTORY_DIR`; follow its existing setup pattern for monkeypatching module paths):

```python
    def test_manifest_includes_provenance_when_recorded(self):
        # arrange a history dir with one repo and a provenance file
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "history" / "repo-a"
            repo.mkdir(parents=True)
            (repo / "commits.ndjson").write_text(
                '{"repository_url": "git@example.com:o/repo-a.git"}\n',
                encoding="utf-8")
            self.addCleanup(setattr, context, "HISTORY_DIR", context.HISTORY_DIR)
            self.addCleanup(setattr, context, "MANIFEST_PATH", context.MANIFEST_PATH)
            self.addCleanup(setattr, context, "CONTEXT_PATH", context.CONTEXT_PATH)
            context.HISTORY_DIR = root / "history"
            context.MANIFEST_PATH = root / "manifest.md"
            context.CONTEXT_PATH = root / "context.md"
            from knowledgestore import provenance
            self.addCleanup(setattr, provenance, "PROVENANCE_PATH",
                            provenance.PROVENANCE_PATH)
            provenance.PROVENANCE_PATH = root / "provenance.json"
            provenance.write({"repo-a": {
                "sha": "abcdef0123456789" + "0" * 24, "branch": "main",
                "committed": "2026-07-30T09:14:02+01:00"}})
            context.main()
            manifest = context.MANIFEST_PATH.read_text(encoding="utf-8")
        self.assertIn("Synced at", manifest)
        self.assertIn("2026-07-30 (`abcdef01`)", manifest)
```

(`context` is this test file's existing import alias for the module.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_build_knowledge_context -v`
Expected: FAIL — `'Synced at' not found`.

- [ ] **Step 3: Implement the manifest column**

In `build_knowledge_context.py`, add `from . import provenance` won't work at module import (cycle-free here, provenance imports sync not context — safe at top). Modify `build_manifest`:

```python
def synced_cell(name: str, recorded: dict[str, dict]) -> str:
    entry = recorded.get(name)
    if not entry or not entry.get("sha"):
        return "—"
    return f"{str(entry.get('committed', ''))[:10]} (`{entry['sha'][:8]}`)"
```

Change the header row to
`"| Repository | Commits | Synced at | History | Current source |"` /
`"|---|---:|---|---|---|"`, load `recorded = provenance.read()` once before the
loop, and add `f"| {synced_cell(repository_name, recorded)} "` in the row
f-string between the commit count and the history link.

- [ ] **Step 4: Failing test for the subtitle**

In `tests/test_build_explorer.py::BuildPageSmokeTest.test_main_produces_page_with_all_blocks`, after the existing path monkeypatches add:

```python
            explorer.PROVENANCE_PATH = root / "provenance.json"
            _json.dump({"repositories": {"r": {
                "sha": "a" * 40, "branch": "main",
                "committed": "2026-07-30T09:14:02+01:00"}}},
                open(explorer.PROVENANCE_PATH, "w"))
```

and after the block assertions:

```python
        self.assertIn("sources synced to 2026-07-30", html)
```

- [ ] **Step 5: Implement the subtitle**

In `build_explorer.py`: add `PROVENANCE_PATH = config.PROVENANCE_PATH` beside the other module path constants. In `main()` where `sub` is built:

```python
    recorded = io.read_json_dict(PROVENANCE_PATH).get("repositories", {})
    synced = max((str(e.get("committed", "")) for e in recorded.values()),
                 default="")
    if synced:
        sub += f" &middot; sources synced to {synced[:10]}"
```

- [ ] **Step 6: Run tests, gates, commit**

Run: `python3 -m unittest discover -s tests -q` then the format/lint/pyright gates.
Expected: all green (fixture/page-regression unaffected: no provenance file in the fixture store → no date, and the regression asserts nothing about it).

```bash
git add -A && git commit -m "feat: surface provenance in the manifest and the page subtitle"
```

---

### Task 3: The status stage — coverage, citations, freshness (no network)

**Files:**
- Create: `src/knowledgestore/status.py`
- Modify: `src/knowledgestore/cli.py` (register), `src/knowledgestore/config.py` (nothing new needed)
- Test: `tests/test_provenance_and_status.py` (extend)

**Interfaces:**
- Consumes: `config` paths; `io.read_json_dict`, `io.read_gzip_json_dict`; `provenance.read()`; `build_topic_briefs.read_topics` (for configured-topic count).
- Produces: pure check functions, each returning a dict and never raising on missing inputs:
  - `layer_coverage() -> dict` → `{"summaries_written", "summaries_expected", "briefs_written", "topics_configured"}`
  - `corpus_citations(root: Path) -> dict` → `{"checked", "dangling": [paths]}`
  - `artefact_freshness(run=run_git) -> dict` → `{"explorer_committed", "layers_committed", "explorer_stale": bool}` using `git log -1 --format=%cI -- <path>`; all-empty dict `{}` when not a git repo.
  - `main() -> int` printing a report; **always returns 0** (drift and gaps are normal; report, don't block).

- [ ] **Step 1: Failing tests**

Append to `tests/test_provenance_and_status.py`:

```python
class LayerCoverageTest(unittest.TestCase):
    def test_counts_summaries_briefs_and_topics(self):
        from knowledgestore import status
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for attr, value in {
                "SUMMARIES_PATH": root / "communities.json",
                "SUMMARIES_INPUT_PATH": root / "communities-input.json",
                "TOPICS_BRIEFS_PATH": root / "briefs.json",
                "TOPICS_CONFIG_PATH": root / "topics.txt",
            }.items():
                self.addCleanup(setattr, status, attr, getattr(status, attr))
                setattr(status, attr, value)
            io.write_json(root / "communities.json", {"1": "x", "2": "y"})
            io.write_json(root / "communities-input.json",
                          [{"id": 1}, {"id": 2}, {"id": 3}])
            io.write_json(root / "briefs.json", {"welsh": {}})
            (root / "topics.txt").write_text(
                "welsh | Welsh | welsh\naddr | Addresses | address\n",
                encoding="utf-8")
            got = status.layer_coverage()
        self.assertEqual(got, {"summaries_written": 2, "summaries_expected": 3,
                               "briefs_written": 1, "topics_configured": 2})


class CorpusCitationsTest(unittest.TestCase):
    def test_reports_nodes_citing_missing_files(self):
        from knowledgestore import status
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "present.md").write_text("x", encoding="utf-8")
            corpus = root / "graphify-out"
            corpus.mkdir()
            io.write_json(corpus / "graph-knowledge-corpus.json", {"nodes": [
                {"id": "a", "source_file": "present.md"},
                {"id": "b", "source_file": "gone/away.sh"},
                {"id": "c"},
            ]})
            got = status.corpus_citations(root)
        self.assertEqual(got, {"checked": 2, "dangling": ["gone/away.sh"]})


class FreshnessTest(unittest.TestCase):
    def test_flags_explorer_older_than_layers(self):
        from knowledgestore import status

        def fake_git(arguments):
            path = arguments[-1]
            return ("2026-07-01T00:00:00+00:00\n" if "explorer" in path
                    else "2026-07-20T00:00:00+00:00\n")

        got = status.artefact_freshness(run=fake_git)
        self.assertTrue(got["explorer_stale"])

    def test_empty_outside_a_git_repository(self):
        from knowledgestore import status

        def failing_git(arguments):
            raise RuntimeError("not a git repository")

        self.assertEqual(status.artefact_freshness(run=failing_git), {})
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_provenance_and_status -v`
Expected: FAIL — `ImportError: cannot import name 'status'`.

- [ ] **Step 3: Implement `status.py`**

```python
"""Report how stale the store's layers are. Never fails: drift is normal.

Cheap by design — this stage must not load the graph. The corpus citation
check reads the small tracked extract, not graph.json.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import config, io, provenance
from .build_topic_briefs import read_topics

SUMMARIES_PATH = config.SUMMARIES_PATH
SUMMARIES_INPUT_PATH = config.SUMMARIES_INPUT_PATH
TOPICS_BRIEFS_PATH = config.TOPICS_BRIEFS_PATH
TOPICS_CONFIG_PATH = config.TOPICS_CONFIG_PATH
EXPLORER_PATH = config.EXPLORER_PATH
ROOT = config.ROOT

# committed layers the page embeds; if any is newer than the page, rebuild
EMBEDDED_LAYERS = ("knowledge/summaries/communities.json",
                   "knowledge/topics/briefs.json",
                   "knowledge/semantic/token-neighbours.json.gz",
                   "knowledge/intent/ticket-descriptions.json.gz")


def run_git(arguments: list[str]) -> str:
    completed = subprocess.run(["git", *arguments], check=True, text=True,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    return completed.stdout


def layer_coverage() -> dict:
    summaries = io.read_json_dict(SUMMARIES_PATH)
    digests = io.read_json(SUMMARIES_INPUT_PATH, default=[])
    briefs = io.read_json_dict(TOPICS_BRIEFS_PATH)
    try:
        topics = read_topics(TOPICS_CONFIG_PATH)
    except (OSError, ValueError):
        topics = []
    return {
        "summaries_written": len(summaries),
        "summaries_expected": len(digests) if isinstance(digests, list) else 0,
        "briefs_written": len(briefs),
        "topics_configured": len(topics),
    }


def corpus_citations(root: Path) -> dict:
    corpus = io.read_json_dict(root / "graphify-out" / "graph-knowledge-corpus.json")
    nodes = [n for n in corpus.get("nodes", []) if n.get("source_file")]
    dangling = sorted(n["source_file"] for n in nodes
                      if not (root / n["source_file"]).exists())
    return {"checked": len(nodes), "dangling": dangling}


def _committed_at(path: str, run) -> str:
    try:
        return run(["log", "-1", "--format=%cI", "--", path]).strip()
    except Exception:
        return ""


def artefact_freshness(run=run_git) -> dict:
    explorer = _committed_at(str(EXPLORER_PATH.relative_to(ROOT))
                             if EXPLORER_PATH.is_relative_to(ROOT)
                             else str(EXPLORER_PATH), run)
    if not explorer:
        return {}
    layers = [d for layer in EMBEDDED_LAYERS if (d := _committed_at(layer, run))]
    newest_layer = max(layers, default="")
    return {
        "explorer_committed": explorer,
        "layers_committed": newest_layer,
        "explorer_stale": bool(newest_layer) and newest_layer > explorer,
    }


def main() -> int:
    recorded = provenance.read()
    print(f"Provenance: {len(recorded)} repositories recorded"
          if recorded else
          "Provenance: none recorded - run `knowledgestore sync` to record it")

    cov = layer_coverage()
    print(f"Summaries: {cov['summaries_written']}/{cov['summaries_expected']} "
          f"significant communities have prose")
    print(f"Topic briefs: {cov['briefs_written']} written, "
          f"{cov['topics_configured']} topics configured")

    cites = corpus_citations(ROOT)
    if cites["dangling"]:
        print(f"Dangling corpus citations ({len(cites['dangling'])}):")
        for path in cites["dangling"][:10]:
            print(f"  - {path}")
    else:
        print(f"Corpus citations: {cites['checked']} checked, none dangling")

    fresh = artefact_freshness()
    if fresh.get("explorer_stale"):
        print("Explorer page is OLDER than a layer it embeds - "
              "run `knowledgestore explorer` and commit the rebuilt page")
    elif fresh:
        print("Explorer page is newer than every embedded layer")

    return 0
```

- [ ] **Step 4: Register in the CLI**

In `cli.py` `STAGES`, after `"explorer"`:

```python
    "status": ("status",
               "report provenance, layer coverage, dangling citations and page freshness"),
```

- [ ] **Step 5: Run tests, gates, commit**

Run: `python3 -m unittest discover -s tests -q` + gates. The existing CLI test
`test_stages_are_listed_in_pipeline_order` in `tests/test_repo_list_and_sync.py`
pins the stage list — update its expected list to end
`..., "topics", "explorer", "status"`.

```bash
git add -A && git commit -m "feat: status stage - coverage, citations, freshness; never blocks"
```

---

### Task 4: Status drift check (gh, optional and injectable)

**Files:**
- Modify: `src/knowledgestore/status.py`
- Test: `tests/test_provenance_and_status.py` (extend)

**Interfaces:**
- Consumes: `provenance.read()`; `config.GITHUB_ORG`; a `runner(args: list[str]) -> str` shaped like `generate_repository_list.run_gh`.
- Produces: `source_drift(runner) -> list[dict]` — for each recorded repo, `{"repo", "behind": int}` where `behind` is commits on the recorded branch since the recorded commit date; entries with `behind == 0` omitted; empty list (and a printed note) when `gh` is unavailable or unauthenticated. `main()` gains `--drift` flag: drift is opt-in because it makes one API call per repository.

- [ ] **Step 1: Failing test**

```python
class DriftTest(unittest.TestCase):
    def test_reports_repos_with_commits_since_recorded_date(self):
        from knowledgestore import provenance, status
        with tempfile.TemporaryDirectory() as tmp:
            self.addCleanup(setattr, provenance, "PROVENANCE_PATH",
                            provenance.PROVENANCE_PATH)
            provenance.PROVENANCE_PATH = Path(tmp) / "provenance.json"
            provenance.write({
                "moved": {"sha": "a" * 40, "branch": "main",
                          "committed": "2026-07-01T00:00:00+00:00"},
                "still": {"sha": "b" * 40, "branch": "main",
                          "committed": "2026-07-01T00:00:00+00:00"},
            })

            def fake_gh(arguments):
                return "9\n" if "moved" in arguments[1] else "0\n"

            got = status.source_drift(runner=fake_gh)
        self.assertEqual(got, [{"repo": "moved", "behind": 9}])
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_provenance_and_status.DriftTest -v`
Expected: FAIL — `AttributeError: ... no attribute 'source_drift'`.

- [ ] **Step 3: Implement**

Add to `status.py` (plus `import argparse`, `import shutil` at top and `GITHUB_ORG = config.GITHUB_ORG` beside the constants):

```python
def run_gh(arguments: list[str]) -> str:
    completed = subprocess.run(["gh", *arguments], check=True, text=True,
                               stdout=subprocess.PIPE)
    return completed.stdout


def source_drift(runner=run_gh) -> list[dict]:
    """Repositories with commits on their branch since the recorded date.

    One API call per repository - the caller opts in. Counts cap at 100
    (one page); "100" therefore means "at least 100".
    """
    drifted = []
    for name, entry in provenance.read().items():
        since = entry.get("committed", "")
        branch = entry.get("branch", "")
        if not since:
            continue
        raw = runner([
            "api",
            f"/repos/{GITHUB_ORG}/{name}/commits"
            f"?sha={branch}&since={since}&per_page=100",
            "--jq", "length",
        ])
        behind = int(raw.strip() or 0)
        if behind:
            drifted.append({"repo": name, "behind": behind})
    return sorted(drifted, key=lambda d: (-d["behind"], d["repo"]))
```

And extend `main()`:

```python
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drift", action="store_true",
                        help="also check GitHub for commits since the build "
                             "(one API call per repository)")
    arguments = parser.parse_args()
    ...existing report...

    if arguments.drift:
        if not shutil.which("gh"):
            print("Drift: gh CLI not available - skipped")
        elif not provenance.read():
            print("Drift: no provenance recorded - skipped")
        else:
            drifted = source_drift()
            if drifted:
                print(f"Source drift ({len(drifted)} repositories moved on):")
                for d in drifted[:15]:
                    print(f"  {d['repo']}: {d['behind']}+ commits since the build")
            else:
                print("Source drift: none - every repository is at the build state")
    return 0
```

- [ ] **Step 4: Run tests, gates, commit**

Run: `python3 -m unittest discover -s tests -q` + gates.

```bash
git add -A && git commit -m "feat: opt-in source drift check via gh"
```

---

### Task 5: Deep-dive bundle — scale, churn, instability, timeline

**Files:**
- Create: `src/knowledgestore/build_deep_dives.py`
- Modify: `src/knowledgestore/config.py` (paths + thresholds), `src/knowledgestore/cli.py`
- Test: `tests/test_build_deep_dives.py`

**Interfaces:**
- Consumes: `io.load_graph`, `io.read_gzip_json_dict`, `io.read_json_dict`, `io.write_json`; `provenance.read()`; graph node shape (`id`, `label`, `repo`, `source_file`, `community`, `metadata`); intent shape `{repo: {path: {"tickets": {t: n}, "first", "last"}}}`; descriptions shape `{t: {"d": [...], "first", ...}}`; summaries `{cid: text}`; labels `{cid: label}`.
- Produces: `config` gains `DEEPDIVES_INPUT_DIR`, `DEEPDIVES_DOCS_DIR`, `DEEPDIVES_PATH`, `DIVE_TOP_FILES = 15`, `DIVE_MIN_COCHANGE = 10`, `DIVE_COCHANGE_MAX_FILES_PER_TICKET = 40`, `REVERT_PATTERN`, `FIX_PATTERN`. Module functions:
  - `scale_section(graph: dict, repo: str, labels: dict, summaries: dict) -> dict`
  - `churn_section(files: dict) -> dict` (`files` = `intent[repo]`)
  - `instability_section(tickets: set[str], descriptions: dict) -> dict`
  - `timeline_section(tickets: set[str], descriptions: dict) -> dict[str, int]`
  - `repo_tickets(files: dict) -> set[str]`
  - `extract(repo: str) -> int` writing `knowledge/deep-dives/<repo>-input.json` (loads the graph — say so in the docstring), exit 1 with a clear message when the repo has no nodes.
  - `main() -> int` dispatching `extract <repo>` / `merge` from `sys.argv`, printing usage otherwise.

- [ ] **Step 1: Config additions**

```python
# Deep dives: evidence-grounded dossiers on individual repositories.
DEEPDIVES_INPUT_DIR = ROOT / "knowledge" / "deep-dives"
DEEPDIVES_DOCS_DIR = ROOT / "docs" / "deep-dives"
DEEPDIVES_PATH = ROOT / "knowledge" / "deep-dives" / "dives.json"
# Bundle thresholds (env-overridable where an estate may reasonably differ).
DIVE_TOP_FILES = _env_int("KSB_DIVE_TOP_FILES", 15)
DIVE_MIN_COCHANGE = _env_int("KSB_DIVE_MIN_COCHANGE", 10)
# Tickets touching more files than this are sweeping changes (renames,
# reformat commits) and are excluded from co-change pairing.
DIVE_COCHANGE_MAX_FILES_PER_TICKET = _env_int(
    "KSB_DIVE_COCHANGE_MAX_FILES_PER_TICKET", 40)
# Instability wording in commit-mined ticket descriptions.
REVERT_PATTERN = re.compile(r"\brevert", re.IGNORECASE)
FIX_PATTERN = re.compile(r"\b(fix|defect|bug|hotfix)", re.IGNORECASE)
```

Add the three paths to `_recompute_paths()` and to the `ROOTED` tuple in `tests/test_config_and_io.py`.

- [ ] **Step 2: Failing tests**

Create `tests/test_build_deep_dives.py`:

```python
"""Deep-dive evidence bundles and dossier merging."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from knowledgestore import build_deep_dives as dives


def node(nid, repo, label, community, source_file=None, kind=None):
    return {"id": nid, "repo": repo, "label": label, "community": community,
            "source_file": source_file,
            "metadata": {"kind": kind} if kind else {}}


GRAPH = {"nodes": [
    node("t::a", "target", "CaseAggregate", 1, "src/CaseAggregate.java"),
    node("t::b", "target", "HearingAggregate", 1, "src/HearingAggregate.java"),
    node("t::c", "target", "progression.case.json", 2, "raml/progression.case.json"),
    node("o::c", "other", "progression.case.json", 7, "schema/progression.case.json"),
    node("o::x", "other", "Unrelated", 8, "src/Unrelated.java"),
    node("e::f", "e2e", "Progress a case", 9, "features/progress.feature",
         kind="gherkin_feature"),
], "links": [
    {"source": "t::a", "target": "t::b"},
    {"source": "t::a", "target": "t::c"},
]}
GRAPH["nodes"][5]["metadata"]["tickets"] = ["DD-1"]

LABELS = {"1": "Case handling", "2": "Case schema"}
SUMMARIES = {"1": "The case handling cluster."}
INTENT_FILES = {
    "src/CaseAggregate.java": {"tickets": {"DD-1": 3, "DD-2": 1, "DD-3": 1},
                               "first": "2020-01-01", "last": "2026-07-01"},
    "src/HearingAggregate.java": {"tickets": {"DD-1": 1, "DD-2": 2},
                                  "first": "2021-01-01", "last": "2026-06-01"},
    "pom.xml": {"tickets": {"DD-3": 1}, "first": "2020-01-01",
                "last": "2020-02-01"},
}
DESCRIPTIONS = {
    "DD-1": {"d": ["Fix defect in case progression"], "first": "2020-03-04"},
    "DD-2": {"d": ["Revert hearing change"], "first": "2021-05-06"},
    "DD-3": {"d": ["Add feature toggles"], "first": "2020-07-08"},
}


class ScaleTest(unittest.TestCase):
    def test_counts_nodes_communities_and_summarised_top(self):
        got = dives.scale_section(GRAPH, "target", LABELS, SUMMARIES)
        self.assertEqual(got["nodes"], 3)
        self.assertAlmostEqual(got["share"], 3 / 6)
        self.assertEqual(got["communities"], 2)
        top = got["top_communities"][0]
        self.assertEqual((top["id"], top["label"], top["size"]),
                         (1, "Case handling", 2))
        self.assertEqual(top["summary"], "The case handling cluster.")
        self.assertIsNone(got["top_communities"][1]["summary"])


class ChurnTest(unittest.TestCase):
    def test_orders_files_by_distinct_tickets(self):
        got = dives.churn_section(INTENT_FILES)
        self.assertEqual(got["files_with_history"], 3)
        self.assertEqual(got["top_files"][0]["path"], "src/CaseAggregate.java")
        self.assertEqual(got["top_files"][0]["tickets"], 3)


class InstabilityTest(unittest.TestCase):
    def test_measures_revert_and_fix_shares_with_samples(self):
        tickets = {"DD-1", "DD-2", "DD-3"}
        got = dives.instability_section(tickets, DESCRIPTIONS)
        self.assertEqual(got["tickets"], 3)
        self.assertAlmostEqual(got["revert_share"], 1 / 3)
        self.assertAlmostEqual(got["fix_share"], 1 / 3)
        self.assertEqual(got["sample_reverts"], ["DD-2: Revert hearing change"])

    def test_timeline_buckets_by_first_seen_year(self):
        got = dives.timeline_section({"DD-1", "DD-2", "DD-3"}, DESCRIPTIONS)
        self.assertEqual(got, {"2020": 2, "2021": 1})


class RepoTicketsTest(unittest.TestCase):
    def test_union_of_file_tickets(self):
        self.assertEqual(dives.repo_tickets(INTENT_FILES),
                         {"DD-1", "DD-2", "DD-3"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run to verify failure**

Run: `python3 -m unittest tests.test_build_deep_dives -v`
Expected: FAIL — `ModuleNotFoundError: knowledgestore.build_deep_dives`.

- [ ] **Step 4: Implement the sections**

Create `src/knowledgestore/build_deep_dives.py`:

```python
"""Deep dives - an evidence-grounded dossier on one repository.

Same shape as topic briefs: a deterministic `extract` gathers evidence, a
person or agent writes the dossier from it, and `merge` validates and renders
before anything enters the store.

    knowledgestore deepdive extract <repo>   # NOTE: loads the full graph
    # write docs/deep-dives/<repo>.md from the bundle, then:
    knowledgestore deepdive merge

Everything in the bundle is derived from committed layers - the graph, the
intent index, ticket descriptions, community summaries - so a dossier's every
claim is checkable against the store itself.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from itertools import combinations

from . import config, io, kinds, provenance
from .build_topic_briefs import markdown_to_html

GRAPH_PATH = config.GRAPH_PATH
LABELS_PATH = config.LABELS_PATH
INTENT_PATH = config.INTENT_INDEX_PATH
DESCRIPTIONS_PATH = config.TICKET_DESCRIPTIONS_PATH
SUMMARIES_PATH = config.SUMMARIES_PATH
INPUT_DIR = config.DEEPDIVES_INPUT_DIR
DOCS_DIR = config.DEEPDIVES_DOCS_DIR
DIVES_PATH = config.DEEPDIVES_PATH
TOP_FILES = config.DIVE_TOP_FILES
MIN_COCHANGE = config.DIVE_MIN_COCHANGE
COCHANGE_MAX_FILES = config.DIVE_COCHANGE_MAX_FILES_PER_TICKET
REVERT = config.REVERT_PATTERN
FIX = config.FIX_PATTERN

MIN_DIVE_LENGTH = 800


def scale_section(graph: dict, repo: str, labels: dict, summaries: dict) -> dict:
    mine = [n for n in graph["nodes"] if n.get("repo") == repo]
    communities = Counter(n.get("community") for n in mine
                          if n.get("community") is not None)
    top = [
        {"id": cid, "label": labels.get(str(cid), f"Community {cid}"),
         "size": size, "summary": summaries.get(str(cid))}
        for cid, size in sorted(communities.items(),
                                key=lambda kv: (-kv[1], kv[0]))[:10]
    ]
    return {"nodes": len(mine),
            "share": len(mine) / max(len(graph["nodes"]), 1),
            "communities": len(communities),
            "top_communities": top}


def churn_section(files: dict) -> dict:
    ranked = sorted(files.items(),
                    key=lambda kv: (-len(kv[1].get("tickets", {})), kv[0]))
    return {"files_with_history": len(files),
            "top_files": [
                {"path": path, "tickets": len(info.get("tickets", {})),
                 "first": info.get("first", ""), "last": info.get("last", "")}
                for path, info in ranked[:TOP_FILES]]}


def repo_tickets(files: dict) -> set[str]:
    return {t for info in files.values() for t in info.get("tickets", {})}


def _described(tickets: set[str], descriptions: dict) -> dict[str, str]:
    return {t: " ".join(descriptions[t].get("d", []))
            for t in sorted(tickets) if t in descriptions}


def instability_section(tickets: set[str], descriptions: dict) -> dict:
    texts = _described(tickets, descriptions)
    reverts = [t for t, text in texts.items() if REVERT.search(text)]
    fixes = [t for t, text in texts.items() if FIX.search(text)]
    total = max(len(texts), 1)
    sample = lambda ids: [f"{t}: {texts[t][:120]}" for t in ids[:5]]  # noqa: E731
    return {"tickets": len(texts),
            "revert_share": len(reverts) / total,
            "fix_share": len(fixes) / total,
            "sample_reverts": sample(reverts),
            "sample_fixes": sample(fixes)}


def timeline_section(tickets: set[str], descriptions: dict) -> dict:
    years = Counter(str(descriptions[t].get("first", ""))[:4]
                    for t in tickets if t in descriptions
                    and descriptions[t].get("first"))
    return dict(sorted(years.items()))
```

(`merge`, co-change and the rest arrive in Tasks 6–7; keep the module importable and the tests above green.)

- [ ] **Step 5: Run the tests**

Run: `python3 -m unittest tests.test_build_deep_dives -v`
Expected: PASS.

- [ ] **Step 6: Gates and commit**

```bash
python3 -m ruff format src tests && python3 -m ruff check src tests && python3 -m pyright
git add -A && git commit -m "feat: deep-dive bundle sections - scale, churn, instability, timeline"
```

---

### Task 6: Deep-dive bundle — co-change, hotspots, coupling surface, features; extract CLI

**Files:**
- Modify: `src/knowledgestore/build_deep_dives.py`, `src/knowledgestore/cli.py`
- Test: `tests/test_build_deep_dives.py` (extend)

**Interfaces:**
- Consumes: fixtures and functions from Task 5.
- Produces:
  - `cochange_section(files: dict) -> list[dict]` — pairs `{"a", "b", "n"}` with `n >= MIN_COCHANGE`, sweeping tickets excluded, **test-pairs excluded** (a pair where one path's stem is the other's stem + `Test`, ignoring directories), sorted by `-n` then `(a, b)`, capped at 25.
  - `hotspot_section(files: dict, graph: dict, repo: str) -> list[dict]` — files in the top-`TOP_FILES` by churn AND with summed node degree in the top quartile of the repo's per-file degrees; `{"path", "tickets", "degree"}`.
  - `coupling_surface(graph: dict, repo: str) -> list[dict]` — labels ending `.json` on this repo's nodes that also appear as labels in other repos: `{"label", "other_repos": sorted[...]}`, sorted by `-len(other_repos)` then label, capped at 20.
  - `feature_section(graph: dict, tickets: set[str]) -> list[dict]` — feature-kind nodes (via `kinds.is_kind(n, kinds.FEATURE)`) whose `metadata.tickets` intersect the repo's tickets: `{"label", "tickets": sorted shared}` capped at 15.
  - `summary_coverage(graph: dict, repo: str, summaries: dict) -> dict`.
  - `extract(repo: str) -> int` assembling the full bundle (schema in the header) and writing `INPUT_DIR / f"{repo}-input.json"`; returns 1 with a message naming the repo when the graph has no nodes for it.
  - `cli.STAGES` gains `"deepdive": ("build_deep_dives", "extract a repository evidence bundle, or merge written deep dives")` after `"topics"` (update the CLI order test again).

- [ ] **Step 1: Failing tests**

```python
class CochangeTest(unittest.TestCase):
    def _files(self, pairs_count):
        # DD-n tickets each touching both files -> co-change support
        tickets = {f"DD-{i}": 1 for i in range(pairs_count)}
        return {
            "src/A.java": {"tickets": dict(tickets)},
            "src/B.java": {"tickets": dict(tickets)},
            "src/ATest.java": {"tickets": dict(tickets)},
        }

    def test_pairs_meet_threshold_and_test_pairs_are_excluded(self):
        self.addCleanup(setattr, dives, "MIN_COCHANGE", dives.MIN_COCHANGE)
        dives.MIN_COCHANGE = 10
        got = dives.cochange_section(self._files(12))
        self.assertIn({"a": "src/A.java", "b": "src/B.java", "n": 12}, got)
        self.assertFalse(any("ATest" in p["a"] or "ATest" in p["b"]
                             for p in got if "A.java" in (p["a"], p["b"])))

    def test_sweeping_tickets_are_ignored(self):
        files = {f"f{i}.java": {"tickets": {"BIG-1": 1}} for i in range(60)}
        self.assertEqual(dives.cochange_section(files), [])


class CouplingSurfaceTest(unittest.TestCase):
    def test_shared_schema_labels_name_the_other_repos(self):
        got = dives.coupling_surface(GRAPH, "target")
        self.assertEqual(got, [{"label": "progression.case.json",
                                "other_repos": ["other"]}])


class FeatureSectionTest(unittest.TestCase):
    def test_features_sharing_tickets_are_linked(self):
        got = dives.feature_section(GRAPH, {"DD-1", "DD-9"})
        self.assertEqual(got, [{"label": "Progress a case",
                                "tickets": ["DD-1"]}])


class HotspotTest(unittest.TestCase):
    def test_high_churn_high_degree_files_flagged(self):
        got = dives.hotspot_section(INTENT_FILES, GRAPH, "target")
        paths = [h["path"] for h in got]
        self.assertIn("src/CaseAggregate.java", paths)   # churn 3, degree 2
        self.assertNotIn("pom.xml", paths)               # churn 1, no nodes


class ExtractTest(unittest.TestCase):
    def test_extract_writes_a_complete_bundle(self):
        import gzip, json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for attr, rel in {
                "GRAPH_PATH": "graph.json", "LABELS_PATH": "labels.json",
                "INTENT_PATH": "intent.json.gz",
                "DESCRIPTIONS_PATH": "desc.json.gz",
                "SUMMARIES_PATH": "summaries.json",
                "INPUT_DIR": "deep-dives",
            }.items():
                self.addCleanup(setattr, dives, attr, getattr(dives, attr))
                setattr(dives, attr, root / rel)
            from knowledgestore import provenance
            self.addCleanup(setattr, provenance, "PROVENANCE_PATH",
                            provenance.PROVENANCE_PATH)
            provenance.PROVENANCE_PATH = root / "provenance.json"
            (root / "graph.json").write_text(json.dumps(GRAPH))
            (root / "labels.json").write_text(json.dumps(LABELS))
            (root / "summaries.json").write_text(json.dumps(SUMMARIES))
            with gzip.open(root / "intent.json.gz", "wt") as f:
                json.dump({"target": INTENT_FILES}, f)
            with gzip.open(root / "desc.json.gz", "wt") as f:
                json.dump(DESCRIPTIONS, f)
            self.assertEqual(dives.extract("target"), 0)
            bundle = json.loads(
                (root / "deep-dives" / "target-input.json").read_text())
        for key in ("repo", "provenance", "scale", "churn", "instability",
                    "timeline", "cochange", "hotspots", "coupling_surface",
                    "features", "summary_coverage"):
            self.assertIn(key, bundle)
        self.assertEqual(bundle["repo"], "target")

    def test_extract_unknown_repo_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            import json
            root = Path(tmp)
            self.addCleanup(setattr, dives, "GRAPH_PATH", dives.GRAPH_PATH)
            dives.GRAPH_PATH = root / "graph.json"
            (root / "graph.json").write_text(json.dumps({"nodes": [], "links": []}))
            self.assertEqual(dives.extract("nope"), 1)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_build_deep_dives -v`
Expected: new tests FAIL — missing attributes.

- [ ] **Step 3: Implement**

Append to `build_deep_dives.py`:

```python
def _is_test_pair(a: str, b: str) -> bool:
    sa = a.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    sb = b.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return sa + "Test" == sb or sb + "Test" == sa


def cochange_section(files: dict) -> list[dict]:
    by_ticket: dict[str, list[str]] = defaultdict(list)
    for path, info in files.items():
        for t in info.get("tickets", {}):
            by_ticket[t].append(path)
    pairs: Counter = Counter()
    for paths in by_ticket.values():
        if 2 <= len(paths) <= COCHANGE_MAX_FILES:
            for a, b in combinations(sorted(paths), 2):
                pairs[(a, b)] += 1
    kept = [{"a": a, "b": b, "n": n} for (a, b), n in pairs.items()
            if n >= MIN_COCHANGE and not _is_test_pair(a, b)]
    return sorted(kept, key=lambda p: (-p["n"], p["a"], p["b"]))[:25]


def hotspot_section(files: dict, graph: dict, repo: str) -> list[dict]:
    degree: Counter = Counter()
    ids = {}
    for n in graph["nodes"]:
        if n.get("repo") == repo and n.get("source_file"):
            ids[n["id"]] = n["source_file"]
    for e in graph["links"]:
        for end in (e["source"], e["target"]):
            if end in ids:
                degree[ids[end]] += 1
    if not degree:
        return []
    quartile = sorted(degree.values())[int(len(degree) * 0.75)]
    churn_top = {f["path"]: f["tickets"]
                 for f in churn_section(files)["top_files"]}
    hot = [{"path": p, "tickets": t, "degree": degree[p]}
           for p, t in churn_top.items() if degree.get(p, 0) >= quartile]
    return sorted(hot, key=lambda h: (-h["tickets"], -h["degree"], h["path"]))


def coupling_surface(graph: dict, repo: str) -> list[dict]:
    mine = {n["label"] for n in graph["nodes"]
            if n.get("repo") == repo
            and str(n.get("label", "")).endswith(".json")}
    elsewhere: dict[str, set] = defaultdict(set)
    for n in graph["nodes"]:
        if n.get("label") in mine and n.get("repo") not in (repo, "", None):
            elsewhere[n["label"]].add(n["repo"])
    surface = [{"label": label, "other_repos": sorted(repos)}
               for label, repos in elsewhere.items()]
    return sorted(surface,
                  key=lambda s: (-len(s["other_repos"]), s["label"]))[:20]


def feature_section(graph: dict, tickets: set[str]) -> list[dict]:
    linked = []
    for n in graph["nodes"]:
        if not kinds.is_kind(n, kinds.FEATURE):
            continue
        shared = sorted(set((n.get("metadata") or {}).get("tickets", []))
                        & tickets)
        if shared:
            linked.append({"label": n.get("label", ""), "tickets": shared})
    return sorted(linked, key=lambda f: (-len(f["tickets"]), f["label"]))[:15]


def summary_coverage(graph: dict, repo: str, summaries: dict) -> dict:
    comms = {n.get("community") for n in graph["nodes"]
             if n.get("repo") == repo and n.get("community") is not None}
    covered = sum(1 for c in comms if str(c) in summaries)
    return {"with": covered, "without": len(comms) - covered}


def extract(repo: str) -> int:
    graph = io.load_graph(GRAPH_PATH)          # NOTE: the full graph
    if not any(n.get("repo") == repo for n in graph["nodes"]):
        print(f"No nodes for repository '{repo}' - is it in the estate, "
              f"and is the graph decompressed?", file=sys.stderr)
        return 1
    labels = io.read_json_dict(LABELS_PATH)
    summaries = io.read_json_dict(SUMMARIES_PATH)
    intent = io.read_gzip_json_dict(INTENT_PATH)
    descriptions = io.read_gzip_json_dict(DESCRIPTIONS_PATH)
    files = intent.get(repo, {})
    tickets = repo_tickets(files)
    bundle = {
        "repo": repo,
        "provenance": provenance.read().get(repo),
        "scale": scale_section(graph, repo, labels, summaries),
        "churn": churn_section(files),
        "instability": instability_section(tickets, descriptions),
        "timeline": timeline_section(tickets, descriptions),
        "cochange": cochange_section(files),
        "hotspots": hotspot_section(files, graph, repo),
        "coupling_surface": coupling_surface(graph, repo),
        "features": feature_section(graph, tickets),
        "summary_coverage": summary_coverage(graph, repo, summaries),
    }
    io.write_json(INPUT_DIR / f"{repo}-input.json", bundle, indent=1)
    print(f"{repo}: bundle -> {INPUT_DIR / (repo + '-input.json')}")
    return 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "extract":
        return extract(sys.argv[2])
    if len(sys.argv) >= 2 and sys.argv[1] == "merge":
        return merge()          # Task 7
    print(__doc__)
    return 1
```

- [ ] **Step 4: Register the CLI stage** (after `"topics"`; update the CLI order test to `..., "topics", "deepdive", "explorer", "status"` — final order decision: `deepdive` before `explorer` because merge output is embedded by the explorer).

- [ ] **Step 5: Run tests, gates, commit**

```bash
python3 -m unittest discover -s tests -q
git add -A && git commit -m "feat: deep-dive extract - co-change, hotspots, coupling surface, CLI"
```

---

### Task 7: Deep-dive merge + explorer embedding + harness coverage

**Files:**
- Modify: `src/knowledgestore/build_deep_dives.py` (merge), `src/knowledgestore/build_explorer.py` (#dives block), `src/knowledgestore/assets/app.js` (DIVES + matchDive), `tests/explorer/fixture.py`, `tests/explorer/engine-unit.mjs`, `tests/explorer/page-regression.mjs`
- Test: `tests/test_build_deep_dives.py` (merge), `tests/test_build_explorer.py` (block)

**Interfaces:**
- Consumes: `markdown_to_html` (already imported in Task 5); bundle files in `INPUT_DIR`; dossiers in `DOCS_DIR / f"{repo}.md"`.
- Produces:
  - `merge() -> int` — for every `*-input.json` in `INPUT_DIR` (excluding `dives.json`): require `docs/deep-dives/<repo>.md`, length ≥ `MIN_DIVE_LENGTH`, and — when the bundle recorded provenance — the dossier must contain the short SHA `sha[:8]` (the staleness stamp; a dossier that does not say what it measured is not mergeable). Renders to `DIVES_PATH` as `{repo: {"title", "html", "source", "sha"}}`. Exit 1 listing problems, like topic-brief merge.
  - Explorer: `DIVES_PATH = config.DEEPDIVES_PATH` constant; template gains `<script id="dives" type="application/json">__DIVES__</script>` after `#topics`, `__DIVES__` substituted like `__TOPICS__`; smoke-test block list gains `"dives"`.
  - app.js: `const DIVES = JSON.parse(getEl('dives').textContent || '{}')` beside `TOPICS`; `matchDive(lq)` returning the dive whose **repo name appears as a substring** of the lowercase question (longest name wins on ties), else `null`; in `runAsk`, when no topic matched but a dive matched, prepend `diveHtml(dive)` (same `.brief` styling, footer says `Deep dive (docs/deep-dives/<repo>.md) - evidence measured at build <shortsha>; live evidence follows below.`) and set `meta` to `'deep dive: ' + repo + (existing ? ' | ' + existing : '')`; the request-a-brief link is suppressed when a dive is shown. Export `matchDive` in `__explorerApi`.

- [ ] **Step 1: Failing merge test**

```python
class MergeTest(unittest.TestCase):
    def test_merge_validates_length_and_provenance_stamp(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for attr, rel in {"INPUT_DIR": "in", "DOCS_DIR": "docs",
                              "DIVES_PATH": "in/dives.json"}.items():
                self.addCleanup(setattr, dives, attr, getattr(dives, attr))
                setattr(dives, attr, root / rel)
            (root / "in").mkdir(); (root / "docs").mkdir()
            (root / "in" / "good-input.json").write_text(json.dumps(
                {"repo": "good", "provenance": {"sha": "abcd1234" + "0" * 32}}))
            (root / "in" / "bad-input.json").write_text(json.dumps(
                {"repo": "bad", "provenance": {"sha": "feed5678" + "0" * 32}}))
            (root / "docs" / "good.md").write_text(
                "# Deep dive: good\n\nMeasured at `abcd1234`.\n\n"
                + "Evidence paragraph. " * 60, encoding="utf-8")
            (root / "docs" / "bad.md").write_text(
                "# Deep dive: bad\n\n" + "No stamp here. " * 60,
                encoding="utf-8")
            code = dives.merge()
            written = json.loads((root / "in" / "dives.json").read_text())
        self.assertEqual(code, 1)                      # bad was rejected
        self.assertEqual(list(written), ["good"])
        self.assertEqual(written["good"]["sha"], "abcd1234")
        self.assertIn("<h2>", written["good"]["html"])
```

- [ ] **Step 2: Run to verify failure** — `AttributeError: 'merge'`.

- [ ] **Step 3: Implement `merge`**

```python
def merge() -> int:
    dives_out: dict[str, dict] = {}
    problems: list[str] = []
    bundles = sorted(INPUT_DIR.glob("*-input.json"))
    if not bundles:
        print(f"No bundles in {INPUT_DIR} - run `knowledgestore deepdive "
              f"extract <repo>` first", file=sys.stderr)
        return 1
    for bundle_path in bundles:
        bundle = io.read_json_dict(bundle_path)
        repo = str(bundle.get("repo", ""))
        doc = DOCS_DIR / f"{repo}.md"
        sha = str((bundle.get("provenance") or {}).get("sha", ""))[:8]
        if not doc.exists():
            problems.append(f"{repo}: missing {doc}")
            continue
        markdown = doc.read_text(encoding="utf-8")
        if len(markdown) < MIN_DIVE_LENGTH:
            problems.append(f"{repo}: dossier shorter than {MIN_DIVE_LENGTH}")
            continue
        if sha and sha not in markdown:
            problems.append(
                f"{repo}: dossier does not state the build it measured "
                f"(expected the short sha `{sha}`)")
            continue
        dives_out[repo] = {
            "title": f"Deep dive: {repo}",
            "html": markdown_to_html(markdown),
            "source": f"docs/deep-dives/{repo}.md",
            "sha": sha,
        }
    io.write_json(DIVES_PATH, dict(sorted(dives_out.items())), indent=1)
    for problem in problems:
        print(f"skipped - {problem}")
    print(f"{len(dives_out)} deep dives -> {DIVES_PATH}")
    return 1 if problems else 0
```

- [ ] **Step 4: Explorer block (python side)**

In `build_explorer.py`: `DIVES_PATH = config.DEEPDIVES_PATH` beside the constants; in the template after the `#topics` script line add
`<script id="dives" type="application/json">__DIVES__</script>`; in `main()` `divesdata = io.read_json_dict(DIVES_PATH)` and chain
`.replace("__DIVES__", json.dumps(divesdata, ensure_ascii=False).replace("</", "<\\/"))`.
Extend the smoke test's block tuple with `"dives"` and monkeypatch `explorer.DIVES_PATH` to a missing file in its temp root.

- [ ] **Step 5: app.js**

After the `TOPICS` constant:

```javascript
/** Deep dives: build-time dossiers on individual repositories, keyed by
 * repository name, served when a question names the repository.
 * @type {Record<string, {title: string, html: string, source: string, sha: string}>} */
const DIVES = JSON.parse(getEl('dives').textContent || '{}');
```

Beside `matchTopic`:

```javascript
/** The dive whose repository name the question mentions; longest name wins.
 * @param {string} lq lowercase question
 * @returns {{repo: string, title: string, html: string, source: string, sha: string}|null} */
function matchDive(lq) {
  let best = null;
  for (const [repo, dive] of Object.entries(DIVES)) {
    if (lq.includes(repo) && (!best || repo.length > best.repo.length)) {
      best = { repo, ...dive };
    }
  }
  return best;
}

/** @param {{repo: string, html: string, source: string, sha: string}} dive */
const diveHtml = (dive) =>
  '<div class="brief">' + dive.html +
  '<div class="b-src">Deep dive (' + esc(dive.source) + ')'
  + (dive.sha ? ' - evidence measured at build ' + esc(dive.sha) : '')
  + '; live evidence follows below.</div></div>';
```

In `runAsk`, replace the tail (`if (topic) {...} else { out.innerHTML += requestBriefHtml(raw); }`) with:

```javascript
  const dive = topic ? null : matchDive(raw.toLowerCase());
  if (topic) {
    out.innerHTML = topicBriefHtml(topic) + out.innerHTML;
    meta.textContent = 'topic brief: ' + topic.title
      + (meta.textContent ? ' | ' + meta.textContent : '');
  } else if (dive) {
    out.innerHTML = diveHtml(dive) + out.innerHTML;
    meta.textContent = 'deep dive: ' + dive.repo
      + (meta.textContent ? ' | ' + meta.textContent : '');
  } else {
    out.innerHTML += requestBriefHtml(raw);
  }
```

Add `matchDive` to the `__explorerApi` export list. Run `tsc --checkJs` locally.

- [ ] **Step 6: Harnesses**

`tests/explorer/engine-unit.mjs`: add beside `TOPICS`
`const DIVES = { 'demo-core': { title: 'Deep dive: demo-core', keywordsUnused: 0, html: '<h2>demo-core</h2>', source: 'docs/deep-dives/demo-core.md', sha: 'abcd1234' } };`
(drop the stray key — exactly `{title, html, source, sha}`), stub `dives: { textContent: JSON.stringify(DIVES) }`, and assert:

```javascript
assert('matchDive hits when the question names the repository',
  context.matchDive('what is wrong with demo-core right now?').repo === 'demo-core');
assert('matchDive returns null otherwise',
  context.matchDive('how are payments taken?') === null);
```

`tests/explorer/fixture.py`: write a dive for `demo-core` — bundle-free path: build `dives.json` directly is cheating the pipeline; instead write `docs/deep-dives/demo-core.md` (with stamp `abcd1234`), a minimal `knowledge/deep-dives/demo-core-input.json` (`{"repo": "demo-core", "provenance": {"sha": "abcd1234" + "0"*32}}`), repoint `build_deep_dives` paths in `_repoint`-style, call `build_deep_dives.merge()`, and repoint `build_explorer.DIVES_PATH` before `build_explorer.main()`.

`tests/explorer/page-regression.mjs`: add `'dives'` to the required block list, plus:

```javascript
// deep dives: naming the repository serves the dossier, with its build stamp
check('what is going on with demo-core?', 'deep dive: demo-core',
  ['Deep dive', 'demo-core', 'evidence measured at build abcd1234']);
// a topic match takes precedence over the request-a-brief link, unchanged
check('how are payments taken?', 'open question',
  ['No pre-written brief covers this question']);
```

- [ ] **Step 7: Run everything, commit**

```bash
python3 -m unittest discover -s tests -q
node tests/explorer/engine-unit.mjs
python3 tests/explorer/fixture.py && node tests/explorer/page-regression.mjs
npx --no-install tsc --checkJs --noEmit --target es2020 --lib es2020,dom src/knowledgestore/assets/app.js
npx --no-install eslint src/knowledgestore/assets/app.js tests/explorer/*.mjs
python3 -m ruff format src tests && python3 -m ruff check src tests && python3 -m pyright
git add -A && git commit -m "feat: deep-dive merge, explorer embedding, dive question shape"
```

---

### Task 8: Documentation — README, skills, CLAUDE.md

**Files:**
- Modify: `README.md`, `.claude/skills/knowledge-store-build/SKILL.md`, `.claude/skills/knowledge-store/SKILL.md`, `CLAUDE.md`

**Interfaces:** none — prose only, but exact copy below so no drafting is needed.

- [ ] **Step 1: README stage table** — add rows (keeping table style):

```markdown
| `deepdive` | `knowledge/deep-dives/`, `docs/deep-dives/` | Evidence-grounded dossier on one repository: churn, instability, co-change coupling, hotspots |
| `status` | (report only) | Provenance, layer coverage, dangling citations, page freshness; `--drift` checks GitHub for commits since the build |
```

And under "The two-part stages", append:

```markdown
`deepdive` is the third two-part stage, scoped to a single repository:

```bash
knowledgestore deepdive extract cpp-context-progression   # loads the full graph
# write docs/deep-dives/cpp-context-progression.md from the bundle, then:
knowledgestore deepdive merge
knowledgestore explorer
```

A dossier must state which build it measured (the bundle's short commit SHA);
`merge` rejects one that does not, because churn and instability figures go
stale with every commit and a dossier that does not say when it was true is
misleading rather than useful.
```

- [ ] **Step 2: `knowledge-store-build` skill** — add a section after the topic-briefs one:

```markdown
## Writing a deep dive

A deep dive is a dossier on one repository — usually the one everybody already
suspects is the problem. The bundle gives you the evidence to confirm or
refute that suspicion; your job is the narrative.

```bash
knowledgestore deepdive extract <repo>    # loads the full graph; be patient
```

Write `docs/deep-dives/<repo>.md` from the bundle. Structure that works:

1. `# Deep dive: <repo>`, then a **headline verdict** paragraph, then a line
   stating what was measured: "Evidence measured at build `<short-sha>`,
   `<n>` tickets, sources synced `<date>`." The merge step **rejects a
   dossier that omits the short SHA.**
2. `## Scale and shape` — nodes, share of the estate, community spread.
3. `## What changes, and why` — churn leaders and the instability numbers
   (revert share, fix share) with sample tickets quoted.
4. `## Hidden coupling` — the co-change pairs, especially cross-concern ones
   (domain files coupled to build files); hotspots (high churn AND high
   degree) are the refactoring targets worth naming.
5. `## Coupling surface` — schema/event names other repositories also carry.
6. `## What this is NOT` — claims the evidence cannot support. Note that the
   graph holds no cross-repository call edges, so blast radius must come from
   the coupling surface, never asserted from graph edges.
7. `**Sources:**` — the bundle path and the graph-build caveat.

Only the constrained markdown subset renders (headings, bold, inline code,
flat bullets, pipe tables). Base every number on the bundle — never re-derive
figures by hand, and never soften them either.

Then:

```bash
knowledgestore deepdive merge
knowledgestore explorer
```

## Checking a store's health

`knowledgestore status` reports provenance, summary/brief coverage, dangling
corpus citations and whether the page is older than a layer it embeds. Add
`--drift` to ask GitHub how far each repository has moved since the build
(one API call per repository). It never fails the build: drift is normal,
and the response to it is a refresh, not a red cross.
```

- [ ] **Step 3: `knowledge-store` skill** — in "Read the written answers first", add
`- docs/deep-dives/ — evidence-grounded dossiers on individual repositories; if the question is about one repository's health, answer from its dive and cite it.`
And in the recent-changes section, replace the vague staleness advice with:

```markdown
When the question concerns recent changes, run `knowledgestore status --drift`
(if the pipeline is installed) and report concretely — "the store predates
9 commits to cpp-context-progression" — rather than a vague staleness caveat.
```

- [ ] **Step 4: `CLAUDE.md`** — under "Graph handling", append:

```markdown
- **`deepdive extract` loads the full graph** (can be ~1.6 GB decompressed);
  `status` deliberately never does. Keep it that way.
- **`status` never returns non-zero.** Drift and coverage gaps are normal
  operating conditions; the stage reports, humans decide.
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "docs: deep-dive and status stages in README, skills, CLAUDE.md"
```

Then push the branch and open the PR (base `main`) titled
"Provenance, status and deep dives" summarising: provenance recorded at sync;
status stage (report-only); deepdive extract/merge with explorer embedding;
the merge-time staleness stamp; pilot to follow in the store repository.

---

### Task 9: Pilot — cpp-context-progression (in `cp-knowledge-store`, after the library PR merges and a release is cut)

This task runs in the **store repository**, not the library. It is build-time
agent work plus verification, not TDD.

**Files (in `hmcts/cp-knowledge-store`):**
- Modify: `requirements.txt` + `requirements.lock` (bump to the release carrying deepdive; regenerate the lock with `uv pip compile requirements.txt --generate-hashes -o requirements.lock` — needs feed credentials)
- Create: `docs/deep-dives/cpp-context-progression.md`, `knowledge/deep-dives/` outputs
- Modify: `tests/explorer/estate-regression.mjs` (one new shape), `graphify-out/explorer.html` (rebuilt)

- [ ] **Step 1:** Branch `feat/deepdive-progression` off `main`; bump + re-lock; `pip install -r requirements.lock`.
- [ ] **Step 2:** `source config/pipeline.sh`; `gunzip -k graphify-out/graph.json.gz` if needed; `knowledgestore sync && knowledgestore export-history && knowledgestore intent` **only if refreshing first** — otherwise run against the committed state and note in the dossier that provenance is absent for pre-provenance builds (the stamp then uses the graph-build commit from `GRAPH_REPORT.md`).
- [ ] **Step 3:** `knowledgestore deepdive extract cpp-context-progression`.
- [ ] **Step 4:** Write the dossier from the bundle following the build skill's structure. Known evidence to expect (from the scoping analysis; verify against the actual bundle, do not copy blindly): ~72.5k nodes ≈ 9.7% of the estate, 1,420 communities, 815 tickets, revert share ≈ 6%, fix share ≈ 31%, `pom.xml` churned by 397 tickets, `pom.xml ↔ CaseAggregate.java` co-change ≈ 56. **Audit every citation against the bundle before merging** (the topic-brief citation audit pattern).
- [ ] **Step 5:** `knowledgestore deepdive merge && knowledgestore explorer`.
- [ ] **Step 6:** Extend `estate-regression.mjs`:

```javascript
// the pilot deep dive: naming the repository serves the dossier
check('what is wrong with cpp-context-progression?', 'deep dive: cpp-context-progression',
  ['Deep dive', 'cpp-context-progression']);
```

- [ ] **Step 7:** Run the estate regression; commit `docs/deep-dives/`, `knowledge/deep-dives/`, the rebuilt page, the lock; open the PR with the headline numbers in the description. **Check `main` afterwards for stranded commits** (see CLAUDE.md).

---

## Self-Review (completed)

- **Spec coverage:** provenance recording (T1), surfacing (T2), status checks incl. report-don't-block decision (T3–4), drift opt-in with per-call cost stated (T4), deepdive bundle with all eight evidence sections (T5–6), agent-authoring loop with validation incl. the staleness stamp (T7), explorer serving + precedence rules + harness coverage (T7), docs and skills (T8), pilot (T9). The "no cross-repo edges" honesty constraint lives in the build skill's dossier structure (T8) and the pilot instructions (T9).
- **Placeholder scan:** none — every step carries code or exact copy. Task 7's fixture step describes edits against an existing pattern by name; the pattern (`_repoint`) exists in the file being edited.
- **Type consistency:** `provenance.read()` returns `dict[str, dict]` and is consumed that way in T2/T3/T4/T6; bundle keys in T6's `extract` match the canonical schema in the header and the merge/regression expectations in T7; `matchDive` return shape matches `diveHtml`'s parameter; stage list order (`…, topics, deepdive, explorer, status`) is asserted once in the CLI test and stated identically in T3/T6.
