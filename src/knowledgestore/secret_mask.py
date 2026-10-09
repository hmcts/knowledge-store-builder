"""Mask secret values out of a source file before a worker reads it.

A headless extraction worker reads the raw files of its chunk, and whatever it
reads goes into the model's context and the session transcript on disk. An
estate that commits a literal password, a storage key or a signed URL in its
source would hand every one of them to the worker. `mask` replaces each such
value with `[masked]` and keeps everything around it, so `password: hunter2`
becomes `password: [masked]`: the worker still sees that the service takes a
password, and never sees the password.

Two kinds of rule, applied in this order, each kind in rule-name order so two
runs on the same input give the same bytes:

1. **Value shapes**, wherever they appear: a private-key block, a SAS `sig=`, an
   `AccountKey=` in a connection string, an `Authorization` header, a URL's
   password, a JWT, and the token formats of several common services. They are
   settings (`config.SECRET_PATTERNS`), so an estate adds its own shapes with
   `KSB_SECRET_PATTERNS`. A rule with a capturing group masks its first group
   and keeps the rest of the match as context; a rule without one masks the
   whole match. Shapes run first so a count names the most specific rule.
2. **Key names**: an assignment - `key: value`, `key = value`, `"key": "value"`,
   `KEY=value`, `key => value` - whose key ends in a secret word (password,
   secret, token, api key, access key, private key, client secret, credentials,
   connection string, account key, sas, auth). The value goes, the key stays.

A value that only *points at* a secret is left alone, because which variable or
store a service reads is architecture worth extracting: `${VAR}`, `$(VAR)`,
`$VAR`, `{{ ... }}`, Key Vault and secret-store references, and keys such as
`secretKeyRef`. So are empty values, booleans, `null` and plain numbers. Code is
left readable: a type annotation (`password: str`), a call or subscript
(`os.environ[...]`), and an unquoted value that a `,`, `;` or closing bracket
ends mid-line - a parameter, an argument, an entry in a literal - are not values.

**What this does not do.** Masking is pattern-based. A secret in a shape no rule
recognises, under a key no rule names, is not masked: a literal password in a
comparison (`if pw == "..."`), a key in an XML element, the lines of a YAML block
scalar (`password: |`), an unquoted value in a one-line flow mapping
(`{password: x, user: y}`), which reads as code. It masks generously the other
way - an unquoted value under a secret-named key at the start of a line is masked
even when it is a variable name in code. It reduces what reaches a worker; it is
not a guarantee that nothing secret does.
"""

from __future__ import annotations

import re
from collections import Counter

from . import config, deploy_values

MASK = "[masked]"
KEY_NAME_RULE = "key-name"

# Compiled once per rule set and keyed on the rules themselves, as `sensitive`
# does: `configure()` runs after import, so a pattern captured at import would
# ignore it.
_COMPILED: dict[tuple[tuple[str, str], ...], tuple[tuple[str, re.Pattern[str]], ...]] = {}

# The words a key must end in. Matched against the key's words joined without
# separators, from a word boundary to the end of the key, so `DB_PASSWORD`,
# `dbPassword` and `db.password` read alike, `passwordSecretRef` (a reference)
# and `token_ttl` do not end in one, and `author` is not `auth`.
_SECRET_TERM = re.compile(
    r"passw(?:or)?d|pwd|secrets?|token|apikey|accesskey|privatekey|clientsecret"
    r"|credentials?|connectionstring|accountkey|sas|auth"
)
_KEY_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")

_KEY = r"(?P<key>[\w.-]+)[\"']?[ \t]*(?:=>|:=|[:=])(?!=)[ \t]*"
# Values read whole whatever follows them: already masked, an interpolation, or
# quoted.
_VALUE_FORMS = (
    r"\[masked\]|\$\([^)\n]*\)|\$\{[^}\n]*\}|\{\{[^\n]*?\}\}"
    r"|\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'"
)
# A key that starts its line is a config file's shape - `.env`, properties, YAML,
# INI - whose unquoted value runs to the end of the line, less a trailing comment
# and a trailing `;` or `,`. To the end, because `;` and `,` are legal inside
# such a value, and stopping at one would leave the rest of the secret behind.
# A quoted key is a JSON or dict-literal entry instead, where an unquoted value
# that a `,` ends is code, as it is inline.
_LINE_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?P<quote>[\"']?)"
    + _KEY
    + r"(?P<value>"
    + _VALUE_FORMS
    + r"|\S[^\n]*?)"
    r"(?=(?P<end>[ \t]+#|[ \t]*[,;]?[ \t]*$))",
    re.MULTILINE,
)
# A key anywhere else. Its unquoted value ends at a separator, and one that a
# `,`, `;` or closing bracket ends is code - a parameter, an argument, an entry
# in a literal - so it is left alone; `&` ends a query-string value.
_INLINE_ASSIGNMENT = re.compile(
    r"(?<![\w.-])" + _KEY + r"(?P<value>" + _VALUE_FORMS + r"|[^\s,;&'\"`)}\]][^\n,;&)}\]]*?)"
    r"(?=(?P<end>[ \t]+#|[ \t]*(?:[,;&)}\]]|$)))",
    re.MULTILINE,
)
_CODE_END = frozenset(",;)}]")
# A type annotation in front of a value: `password: str` is a declaration, and in
# `password: str = "..."` only the part after `=` is a value.
# Two patterns rather than one, each simple enough to read: the type, then what
# may follow it - an `=` before a default, or nothing at all.
_ANNOTATION_TYPE = re.compile(
    r"(?:str|bytes|int|bool|float|string|number|SecretStr|SecretBytes|Optional\[[^\]\n]*\])"
    r"(?:[ \t]*\|[ \t]*None)?"
)
_ANNOTATION_TAIL = re.compile(r"[ \t]*(?:=(?!=)[ \t]*|$)")


def _annotation_length(value: str) -> int:
    """How much of `value` is a type annotation and its `=`, or 0 when it is none."""
    annotation = _ANNOTATION_TYPE.match(value)
    if not annotation:
        return 0
    tail = _ANNOTATION_TAIL.match(value, annotation.end())
    return tail.end() if tail else 0


_ALNUM = re.compile(r"[A-Za-z0-9]")

# Values that are not secrets whatever key they sit under. `|` and `>` open a
# YAML block scalar, whose lines this rule cannot see.
_LITERAL_WORDS = frozenset({"true", "false", "null", "none", "nil", "yes", "no", "~"})
_NUMBER_OR_BLOCK = re.compile(r"-?\d+(?:\.\d+)?|[|>][-+0-9]*")
# Interpolation `deploy_values.strip_template` does not cover: bare `$VAR`,
# `$(command)`, and `%VAR%`.
_BARE_VARIABLE = re.compile(r"\$[A-Za-z_]\w*|\$\([^)\n]*\)|%[A-Za-z_]\w*%")
# A value naming the store a secret is read from, by the prefix that says so.
_STORE_REFERENCE = re.compile(
    r"(?i)@Microsoft\.KeyVault\(|ref\+[a-z0-9]+://|vault:|arn:aws:(?:secretsmanager|ssm):"
    r"|(?:sm|gcpsm|awssm|azurekv)://|secret(?:key)?ref\b"
)
# Code reading a value from somewhere else: a call or a subscript.
_CODE_EXPRESSION = re.compile(r"[A-Za-z_][\w.]*[ \t]*[(\[]")


def _names_a_secret(key: str) -> bool:
    words = [word.lower() for word in _KEY_WORD.findall(key)]
    while words and words[-1].isdigit():
        words.pop()
    return any(_SECRET_TERM.fullmatch("".join(words[i:])) for i in range(len(words)))


def _is_reference(value: str) -> bool:
    """A value that says where a secret is held rather than holding it."""
    stripped = deploy_values.strip_template(value).replace(deploy_values.PLACEHOLDER, "")
    # Only punctuation left between the references, as in `${A}-${B}`.
    if not _ALNUM.search(_BARE_VARIABLE.sub("", stripped)):
        return True
    return bool(_STORE_REFERENCE.match(value))


def _not_a_secret(value: str, literals: bool) -> bool:
    """Empty, already masked, a reference - or, under a key name, a literal.

    `literals` only for the key-name rule: `token_ttl: 300` is a number under a
    secret-ish key, while a number a shape rule matched is what the shape is for.
    """
    value = value.strip()
    if not value or value == MASK or _is_reference(value):
        return True
    # A secret has letters or digits in it; `(` opening a multi-line value does not.
    if not _ALNUM.search(value):
        return True
    return literals and (value.lower() in _LITERAL_WORDS or bool(_NUMBER_OR_BLOCK.fullmatch(value)))


def _masked_value(value: str, literals: bool = False) -> str | None:
    """`value` masked, keeping its quotes, or None when it is not a secret."""
    quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
    inner = value[1:-1] if quoted else value
    if _not_a_secret(inner, literals):
        return None
    return f"{value[0]}{MASK}{value[0]}" if quoted else MASK


def rules() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """The value-shape rules in force right now, in rule-name order.

    Raises `re.error` for a rule that will not compile: an unusable rule must
    stop the run, not quietly mask nothing.
    """
    key = tuple(sorted(config.SECRET_PATTERNS.items()))
    if key not in _COMPILED:
        _COMPILED[key] = tuple((rule, re.compile(pattern)) for rule, pattern in key)
    return _COMPILED[key]


def _apply(text: str, rule: str, pattern: re.Pattern[str], counts: Counter[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        whole = match.group(0)
        group = 1 if pattern.groups and match.group(1) is not None else 0
        start, end = match.start(group) - match.start(), match.end(group) - match.start()
        masked = _masked_value(whole[start:end])
        if masked is None:
            return whole
        counts[rule] += 1
        return whole[:start] + masked + whole[end:]

    return pattern.sub(replace, text)


def _key_value(match: re.Match[str], counts: Counter[str]) -> str:
    """One assignment with its value masked, or unchanged when it holds no secret."""
    whole, value = match.group(0), match.group("value")
    if not _names_a_secret(match.group("key")):
        return whole
    ended_as_code = value[0] not in "\"'" and match.group("end").strip() in _CODE_END
    if ended_as_code and (match.re is _INLINE_ASSIGNMENT or match.group("quote")):
        return whole
    start = match.start("value") - match.start()
    head = _annotation_length(value)
    if head:
        start += head
        value = value[head:]
    if not value or (value[0] not in "\"'" and _CODE_EXPRESSION.match(value)):
        return whole
    masked = _masked_value(value, literals=True)
    if masked is None:
        return whole
    counts[KEY_NAME_RULE] += 1
    return whole[:start] + masked + whole[start + len(value) :]


def _apply_key_names(text: str, counts: Counter[str]) -> str:
    # Line-start keys first, so a config value is read to the end of its line
    # before the inline rule could stop it at a separator inside it.
    for pattern in (_LINE_ASSIGNMENT, _INLINE_ASSIGNMENT):
        text = pattern.sub(lambda match: _key_value(match, counts), text)
    return text


def mask(text: str, counts: Counter[str] | None = None) -> str:
    """`text` with every secret value these rules recognise replaced by `[masked]`.

    `counts` accumulates the values masked per rule name, for the run report.
    Masking masked text finds nothing more: `[masked]` is never read as a value.
    """
    tally: Counter[str] = Counter()
    for rule, pattern in rules():
        text = _apply(text, rule, pattern, tally)
    text = _apply_key_names(text, tally)
    if counts is not None:
        counts.update(tally)
    return text


__all__ = ["KEY_NAME_RULE", "MASK", "mask", "rules"]
