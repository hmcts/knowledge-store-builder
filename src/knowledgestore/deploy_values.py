"""Templated YAML, reduced to configuration facts that can be quoted safely.

Deployment values files are Jinja-templated YAML: the structure and the keys are
literal, but many values are `{{ var }}` resolved at deploy time. This module
strips the template markers so the file parses, and flattens the result to dotted
keys.

It never renders. These files reference secrets by name, so evaluating a template
would pull credentials into a committed artefact; and the placeholder deliberately
does not carry the variable name, because a name is itself a hint about what is
held where.

Two interpolation dialects get the same treatment, `{{ }}` and `${ }`, because a
name withheld from one format and published from the other is withheld from
neither.

The same principle settles secret references, and it is settled here rather than
in a parser so that every deployment format inherits one answer (#88). A mapping
that names both a store and an entry inside it is a map of where a credential
lives, so `withhold_secret_locations` keeps the variable - that a service takes
this value from a store is an architectural fact worth recording - and replaces
the store and the entry with the placeholder. Keys are kept, locations are not.
"""

from __future__ import annotations

import re

PLACEHOLDER = "<set-at-deploy-time>"

_INTERPOLATION = re.compile(r"\{\{.*?\}\}", re.DOTALL)
# The `${ }` dialect, `re.DOTALL` for the same reason the Jinja patterns use it: a
# reference broken over lines must not leave its name on the second one.
#
# `$${ }` is an escaped literal in some tools - they emit the text `${ }` and leave
# interpolation to whatever consumes the rendered file. Deferred is not cancelled,
# so the name still says where something is held: the leading `$` is part of the
# match and the whole run is withheld, leaving no stray dollar behind.
#
# Bare `$VAR` is deliberately out of scope. It has no terminator, and `$` followed
# by word characters occurs in shell fragments, patterns and prices, so matching it
# would rewrite configuration values that are not interpolation at all. The layouts
# this module reads write their references braced.
_DOLLAR_INTERPOLATION = re.compile(r"\$?\$\{.*?\}", re.DOTALL)
_CONTROL_LINE = re.compile(r"^[ \t]*\{%.*?%\}[ \t]*$\n?", re.MULTILINE | re.DOTALL)
_INLINE_CONTROL = re.compile(r"\{%.*?%\}", re.DOTALL)


def strip_template(text: str) -> str:
    """`text` with both dialects' markers replaced, never evaluated."""
    without_control = _CONTROL_LINE.sub("", text)
    without_control = _INLINE_CONTROL.sub("", without_control)
    stripped = _INTERPOLATION.sub(PLACEHOLDER, without_control)
    return _DOLLAR_INTERPOLATION.sub(PLACEHOLDER, stripped)


# Shape, not vendor. Keying on a vendor's name would be a list that ages into
# silence: the format it did not know about publishes everything, and nothing
# reports that. A secret reference is a mapping in which one key names the store
# and a *different* key names the entry inside it.
#
# Deliberately not matched: a mapping that names only an entry (`path`, `key` and
# `property` are ordinary configuration words on their own, and redacting them
# would take real facts with them), a mapping that names only a store (a store
# with no entry beside it is not a map of where a credential lives), and one key
# that fills both roles by itself, such as `keyVault`.
_STORE_ROLE = re.compile(r"store|vault")
_ENTRY_ROLE = re.compile(r"key|path|entry|property")


def _roles(key: str) -> tuple[bool, bool]:
    """(names a store, names an entry) for one mapping key, ignoring case and
    separators so `secret_store`, `secretStore` and `SecretStore` read alike."""
    normalised = re.sub(r"[^a-z0-9]", "", key.lower())
    return bool(_STORE_ROLE.search(normalised)), bool(_ENTRY_ROLE.search(normalised))


def _is_secret_reference(mapping: dict) -> bool:
    roles = {str(key): _roles(str(key)) for key in mapping}
    stores = {key for key, (store, _) in roles.items() if store}
    entries = {key for key, (_, entry) in roles.items() if entry}
    return bool(stores) and bool(entries) and len(stores | entries) > 1


def withhold_secret_locations(value: object) -> object:
    """`value` with every secret reference's store and entry replaced.

    Recursive and structure-preserving, so a format that keeps the nesting
    inherits the policy as readily as one that flattens. A role key's whole
    value is replaced, mapping or scalar: a store is often named one level down
    (`secretStoreRef.name`), and walking into it would publish the location
    under a key that reads as harmless structure.
    """
    if isinstance(value, dict):
        reference = _is_secret_reference(value)
        withheld: dict[object, object] = {}
        for key, item in value.items():
            store, entry = _roles(str(key))
            withheld[key] = (
                PLACEHOLDER if reference and (store or entry) else withhold_secret_locations(item)
            )
        return withheld
    if isinstance(value, list):
        return [withhold_secret_locations(item) for item in value]
    return value


def _walk(value: object, prefix: str, out: dict[str, str], max_chars: int) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _walk(item, f"{prefix}.{key}" if prefix else str(key), out, max_chars)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, f"{prefix}.{index}", out, max_chars)
        return
    if value is None:
        out[prefix] = ""
        return
    out[prefix] = str(value)[:max_chars]


def flatten(value: object, max_keys: int, max_chars: int) -> dict[str, str]:
    """Dotted keys to scalar values, capped in key count and value length.

    Sorted before capping so two runs on the same input keep the same keys - an
    unstable cap would churn the committed graph for no reason.
    """
    out: dict[str, str] = {}
    _walk(withhold_secret_locations(value), "", out, max_chars)
    return {key: out[key] for key in sorted(out)[:max_keys]}
