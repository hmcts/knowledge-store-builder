# Security

## Reporting a vulnerability

Report a vulnerability as the
[HMCTS security policy](https://github.com/hmcts/.github/blob/master/SECURITY.md)
describes, to the address it gives. Do not open a public issue for it.

## The MCP server

`knowledgestore serve` (the `knowledgestore.server` package) serves one built
knowledge store, read-only, to one MCP client over stdio. It needs the `server`
extra. Three invariants hold for it, and each is a test that fails when it breaks.

**1. The server never writes, spawns or reaches a network.**

- No module under `src/knowledgestore/server/` imports `subprocess`, a socket or
  HTTP client, `shutil`, or any library module (so none of `knowledgestore.io`'s
  writers), `importlib` (bar `importlib.metadata`) or `ctypes`, and none calls
  `exec`, `eval` or `__import__`. None opens a file for writing, creates, renames or
  removes a path (by `os`, by a `Path` method or by a name imported from `os`), or
  calls `os.system` and its kin. The one exception is four functions of `index.py`
  (`_discard`, `_write`, `_write_digest`, `_remove_others`), which write the
  decompressed query index to a temp file and rename it into
  `${XDG_CACHE_HOME:-~/.cache}/knowledge-store/<store.id>/`, outside the store; if
  the cache cannot be written the server reads the graph instead.
  Held by `tests/server/test_security_invariants.py` (`ReadOnlyByConstructionTest`),
  a static read of every module in the package, subpackages included, and
  (`CacheWritesStayInTheCacheTest`) at runtime: a sweep with `XDG_CACHE_HOME` at a
  temp directory writes nothing outside it.
- A full tool sweep over a store made read-only changes no file under it, in every
  index mode, and completes with sockets and processes made to raise. Held by
  `tests/server/test_security.py` (`ReadOnlyTest`, `NoNetworkNoSubprocessTest`).

**2. No tool output names a host path.** Every tool answer and refusal is free of
the store root, the cache directory and the temp tree around them; any absolute
path left in a message the server composes is replaced with `<host path>`. Held by
`tests/server/test_security_invariants.py` (`NoHostPathTest`), and for the
sanitiser and for start-up refusals on stderr by `tests/server/test_results.py`,
`tests/server/test_server.py` and `tests/server/test_security.py`.

**3. Store text is returned only under explicit text fields.** Store text is data
the server relays, never a sentence it composes: a commit or ticket description
that reads like an instruction comes back verbatim under `description` and nowhere
else. Held by `tests/server/test_security_invariants.py` (`StoreTextFieldsTest`)
and `tests/server/test_tools_tickets.py`. Every tool's description also ends with a
warning that store content is untrusted data, never instructions.

Paths stay inside the store (a layer symlinked out is refused at start-up, and a
tool argument is looked up, never joined into a path), held by
`tests/server/test_security.py` (`ContainmentTest`).

The three invariants have mutation entries, named `server: ...` in
`tests/mutation_gate.py`, so a change that breaks one is caught by the gate and not
only by the test.
