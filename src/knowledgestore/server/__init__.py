"""A read-only MCP server that answers questions from a knowledge store."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("hmcts-knowledge-store-builder")
except PackageNotFoundError:  # running from a checkout that was never installed
    __version__ = "0.0.0+uninstalled"

__all__ = ["__version__"]
