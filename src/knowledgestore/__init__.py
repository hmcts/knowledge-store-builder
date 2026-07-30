"""Build a committed, queryable knowledge store from a fleet of git repositories.

The pipeline turns source code, commit history and Gherkin specifications into
a graph plus a set of retrieval layers, all committed as static files so that
consumers can query them without a licence, a server or a build step.

Stages are plain modules with a `main()`; see `cli.py` for the run order, and
`config.py` for the settings that vary by estate.
"""

from importlib.metadata import PackageNotFoundError, version

from . import config, io  # noqa: F401  (re-exported for consumers)

__all__ = ["config", "io"]
try:
    __version__ = version("hmcts-knowledge-store-builder")
except PackageNotFoundError:  # running from a checkout that was never installed
    __version__ = "0.0.0+uninstalled"
