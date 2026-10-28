# Repository manifest

This manifest describes the source repositories represented by the combined Graphify knowledge graph.

| Repository | Commits | Synced at | History | Current source |
|---|---:|---|---|---|
| `alpha-lib` | 4 | 2026-10-04 (`ebfe7220`) | [History](git-history/alpha-lib/index.md) | `https://github.com/example-org/alpha-lib.git` |
| `beta-app` | 3 | 2026-10-06 (`085f0da7`) | [History](git-history/beta-app/index.md) | `https://github.com/example-org/beta-app.git` |
| `delta-deploy` | 2 | 2026-10-08 (`3e462391`) | [History](git-history/delta-deploy/index.md) | `https://github.com/example-org/delta-deploy.git` |
| `eta-portal` | 1 | 2026-10-05 (`002b1cff`) | [History](git-history/eta-portal/index.md) | `https://github.com/example-org/eta-portal.git` |
| `zeta-archive` | 1 | — | [History](git-history/zeta-archive/index.md) | `https://github.com/example-org/zeta-archive.git` |

The repositories themselves are cloned into `repositories/` during graph generation and are not committed to this parent repository.

## What this manifest does not cover

These 5 repositories were discovered from the `example-org` organisation on a single code host. Repositories outside it were never searched, so they are absent from this store without appearing as gaps.

**Absence of evidence here is a fact about this store's membership, not about the estate.** When this store reports that there is no evidence of something, read it as no evidence *in the repositories listed above* - and check whether the thing you are looking for lives somewhere that was never in scope.

### No boundary is declared

This estate has not written `config/estate-boundary.txt`, so nothing here records which other hosts exist, which repositories the estate has ruled out, or which names are aliases of one another. Until it does, treat every absence as unexplained rather than as a decision.

**Completeness is not claimed.** A declared boundary says what this estate knows about, not what exists. A deployed service whose repository nobody has located is neither listed here nor ruled out by not being listed.
