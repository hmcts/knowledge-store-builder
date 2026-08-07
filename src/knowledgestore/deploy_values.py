"""Templated YAML, reduced to configuration facts that can be quoted safely.

Deployment values files are Jinja-templated YAML: the structure and the keys are
literal, but many values are `{{ var }}` resolved at deploy time. This module
strips the template markers so the file parses, and flattens the result to dotted
keys.

It never renders. These files reference secrets by name, so evaluating a template
would pull credentials into a committed artefact; and the placeholder deliberately
does not carry the variable name, because a name is itself a hint about what is
held where.
"""

from __future__ import annotations

import re

PLACEHOLDER = "<set-at-deploy-time>"

_INTERPOLATION = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_CONTROL_LINE = re.compile(r"^[ \t]*\{%.*?%\}[ \t]*$\n?", re.MULTILINE | re.DOTALL)
_INLINE_CONTROL = re.compile(r"\{%.*?%\}", re.DOTALL)


def strip_template(text: str) -> str:
    """`text` with Jinja markers replaced, never evaluated."""
    without_control = _CONTROL_LINE.sub("", text)
    without_control = _INLINE_CONTROL.sub("", without_control)
    return _INTERPOLATION.sub(PLACEHOLDER, without_control)


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
    _walk(value, "", out, max_chars)
    return {key: out[key] for key in sorted(out)[:max_keys]}
