"""Build a committed, queryable knowledge store from a fleet of git repositories.

The pipeline turns source code, commit history and Gherkin specifications into
a graph plus a set of retrieval layers, all committed as static files so that
consumers can query them without a licence, a server or a build step.

Stages are plain modules with a `main()`; see `cli.py` for the run order, and
`config.py` for the settings that vary by estate.
"""

from . import config, io  # noqa: F401  (re-exported for consumers)

__all__ = ["config", "io"]
# Kept in step with the released version. The publish workflow stamps the
# artefact version at build time, so this is the source-of-truth for a
# source or editable install.
__version__ = "0.2.0"
