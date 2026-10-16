"""The environment every git call runs in: the caller's, less what binds a repository.

Inside a git hook, git exports `GIT_DIR`, `GIT_INDEX_FILE` and their kin, and they
outrank `git -C <path>`: `-C` changes the directory git starts from, and a
repository named by `GIT_DIR` is not looked for from a directory at all. So a stage
run from a store's pre-commit hook - `export-history`, `sync`, `drift`, `status`,
the clone check in `check-citations` - would read and write the store's own
repository while believing it was talking to a clone. A fixture commit landed on a
store's real branch that way.

The list is git's own, from `git rev-parse --local-env-vars`: the variables git
clears itself when it moves into another repository, such as a submodule. Three
of those carry configuration rather than a repository (`GIT_CONFIG`,
`GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`) and pass through, as does everything
about credentials and SSH: they say how to reach a repository, not which one.
Added to git's list are the variables that steer discovery and naming
(`GIT_CEILING_DIRECTORIES`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`, `GIT_NAMESPACE`),
because every call here names its repository explicitly and none should inherit a
caller's view of where one is.

`tests/test_git_env.py` holds `BINDING` to the git that runs the suite: a variable
that git starts listing and this module does not name fails it.
"""

from __future__ import annotations

import os

BINDING = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)

# In git's local list, and deliberately kept: configuration, not a repository.
KEPT = ("GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")


def git_env() -> dict[str, str]:
    """A copy of `os.environ` without the variables that bind git to a repository."""
    return {name: value for name, value in os.environ.items() if name not in BINDING}
