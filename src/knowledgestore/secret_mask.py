"""Mask secret values out of a source file before a worker reads it.

A headless extraction worker reads the raw files of its chunk, and whatever it
reads goes into the model's context and the session transcript on disk. An
estate that commits a literal password, a storage key or a signed URL in its
source would hand every one of them to the worker. `mask` replaces each such
value with `[masked]` and keeps everything around it, so `password: hunter2`
becomes `password: [masked]`: the worker still sees that the service takes a
password, and never sees the password. Nothing else changes - not a line ending,
not an indent - so a CRLF file stays CRLF.

Three kinds of rule, applied in this order, each in a fixed order so two runs on
the same input give the same bytes:

1. **Value shapes**, wherever they appear: a private-key block, a SAS `sig=`, an
   `AccountKey=` or a `Password=` in a connection string, an `Authorization`
   header, a URL's password, a JWT, and the token formats of several common
   services. They are settings (`config.SECRET_PATTERNS`), so an estate adds its
   own shapes with `KSB_SECRET_PATTERNS`. A rule with a capturing group masks
   its first group and keeps the rest of the match as context; a rule without
   one masks the whole match. Shapes run first so a count names the most
   specific rule, and a value a shape has already masked part of is left as the
   shape left it. A value over several lines - a private-key block - is masked
   line by line, so every line after it keeps its number. One shape is built
   in rather than a setting, because it reads a name before it decides:
   `url-query-secret`, the value of a URL query or fragment parameter named
   exactly for a secret (`?token=`, `#access_token=`, `&amp;api_key=`, and
   `code` or `key` only when the value looks like a credential), wherever the
   URL sits.
2. **Key names**: an assignment - `key: value`, `key = value`, `"key": "value"`,
   `KEY=value`, `key => value` - whose key ends in a secret word (password,
   pass, passphrase, secret, token, api key, access key, private key, client
   secret, credentials, account key, sas, auth), or in one fused onto another
   word (`DEFAULTPASSWORD`, `apitoken`). The value goes, the key stays. A key
   naming `key` or `webhook`, or a secret word with another word after it
   (`secret_key_base`, `API_KEY_LIVE`), gives up its value only when the value
   looks like a credential: one token of sixteen or more key characters, not a
   URL, a path or a dotted name, and hex, a UUID, base64, or letters and digits
   mixed as no name made of words is. So `JWT_KEY: <base64>` goes and
   `key: password`, `keyFile: /etc/x.pem` and `KEY_VAULT_NAME: my-vault` stay.
   A last word saying the key configures a secret - `secretName`, `secret_ref`,
   `passwordPolicy`, `token_ttl`, `*_enabled` - names none. Only the value
   changes: a literal with code after it (`'...');`) loses the literal, and in
   a call (`base64encode("...")`) each literal goes and the call stays.
3. **Key names in other syntax**, counted under the same rule: an XML attribute
   or element named for a secret (`password="..."`, `<password>...</password>`);
   the `value` beside a `name` or `key` naming one - a Kubernetes `env:` item,
   a .NET `<add key=... value=...>`, a Helm `set` block; a Terraform variable's
   `default` when the variable is named for one; the lines of a YAML block
   scalar (`password: |`); an unquoted value in a one-line YAML flow mapping
   (`{password: x, user: y}`); and every value under a Kubernetes Secret's
   `data:` or `stringData:`, whatever its key, because the manifest's kind says
   all of it is secret.

A value that only *points at* a secret is left alone, because which variable or
store a service reads is architecture worth extracting: `${VAR}`, `$(VAR)`,
`$VAR`, `{{ ... }}`, `#{Var}`, Key Vault and secret-store references, and keys
such as `secretKeyRef` and a chart's `existingSecret`. So are empty values,
booleans, `null` and plain numbers. Code is left readable: a type annotation
(`password: str`), a call or subscript (`os.environ[...]`), an unquoted value
that a `,`, `;` or closing bracket ends mid-line - a parameter, an argument, an
entry in a literal - and a name assigned to a member (`self.token = token`) or
ended by a `,` or `;` (`this.password = password;`) are not values. Nor, in a
call, is a literal naming what is looked up (`os.environ["DB_PASSWORD"]`,
`config.get("db.password")`), a raw string or an f-string.

A value that says where something is, rather than being a secret, is left alone
under any key: a URL with no password in it (`auth: http://auth-service:4502`;
a secret-named parameter in its query still goes) and a Terraform reference
(`var.x`, `local.x`, `module.a.b`, `data.a.b.c`, `each.value`,
`random_password.db.result`). A connection string is masked where it holds a
credential - its `Password=`, `AccountKey=`, URL password or `sig=` - by the
value shapes, not whole, so the worker still sees the server and database.

It masks generously the other way: any other unquoted value under a
secret-named key at the start of a line is masked even when it is a variable
name in code.

**What this does not mask.** Masking is pattern-based. It reduces what reaches a
worker; it is not a guarantee that nothing secret does. Each of these still
reaches the worker, and each is pinned by a test that fails once it is masked,
so fixing one means changing this list and its copy in the build skill:

- a literal compared or passed in code: `if pw == "example-secret":`
- a value under a key that names no secret word, or in prose:
  `signing-cert: example-secret`
- a value that does not look like a credential, under a key whose secret word
  is `key`, `webhook`, or followed by another word: `signing-key: example-secret`
- a secret in a URL's path, under any key: `auth: https://hooks.example/example-secret`
- a literal in a call that reads like a name: `get("DB_PASSWORD", "example-secret")`
- a value whose name is in another column: a SQL `INSERT` naming
  `client_secret` in its column list
- the lines a value continues onto: the next line of a properties value ending
  in a backslash
- a value in an XML CDATA section:
  `<password><![CDATA[example-secret]]></password>`
"""

from __future__ import annotations

import bisect
import re
from collections import Counter
from typing import NamedTuple

from . import config, deploy_values

MASK = "[masked]"
KEY_NAME_RULE = "key-name"
URL_QUERY_RULE = "url-query-secret"

# Compiled once per rule set and keyed on the rules themselves, as `sensitive`
# does: `configure()` runs after import, so a pattern captured at import would
# ignore it.
_COMPILED: dict[tuple[tuple[str, str], ...], tuple[tuple[str, re.Pattern[str]], ...]] = {}

# The secret words. Matched against runs of the key's words joined without
# separators, so `DB_PASSWORD`, `dbPassword` and `db.password` read alike and
# `author` is not `auth`. A key ending in one masks any value it holds; a key
# holding one before another word - `secret_key_base` - masks only a value that
# looks like a credential.
_SECRET_TERM = re.compile(
    r"passw(?:or)?d|pwd|pass(?:phrase)?|secrets?|token|apikey|accesskey|privatekey|clientsecret"
    r"|credentials?|accountkey|sas|auth"
)
# Words that name a credential only sometimes - `key: password` names an entry,
# `keyFile` a file - so wherever they sit, only a credential-looking value goes.
_CREDENTIAL_TERM = re.compile(r"key|webhook")
# A secret word fused onto the end of a word: `DEFAULTPASSWORD`, `apitoken`,
# `masterkey`. Not `pass`, `pwd`, `auth` or `sas`, which end other words -
# `bypass`, `oldpwd`, `oauth` - and not the words that merely end in `key`.
_FUSED_SECRET = re.compile(r"(?:passw(?:or)?d|passphrase|secrets?|token|credentials?)$")
_FUSED_CREDENTIAL = re.compile(r"(?:key|webhook)$")
_NOT_FUSED = frozenset({"monkey", "donkey", "turkey", "hockey", "jockey", "whiskey", "lackey"})
# A last word saying the key names, points at or configures a secret rather than
# holding one: `secretName`, `secret_ref`, `passwordPolicy`, `token_ttl`.
_METADATA_WORDS = frozenset(
    {
        "name",
        "names",
        "ref",
        "refs",
        "reference",
        "policy",
        "ttl",
        "enabled",
        "disabled",
        "file",
        "type",
        "version",
    }
)
_KEY_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
# What a key's name says about its value. `_ANY`: a key ending in a secret word,
# whose value goes whatever it is. `_GUARDED`: `key`, `webhook`, or a secret word
# with another word after it, whose value goes only when it looks like a
# credential. `_MANIFEST`: every value of a Kubernetes Secret's data.
_ANY = "any"
_GUARDED = "guarded"
_MANIFEST = "manifest"

# A quoted value, which no line break can end inside.
_QUOTED = r"\"(?:[^\"\\\r\n]|\\.)*\"|'(?:[^'\\\r\n]|\\.)*'"
_KEY = r"(?P<key>[\w.-]+)[\"']?[ \t]*(?:=>|:=|[:=])(?!=)[ \t]*"
# Values read whole whatever follows them: already masked, an interpolation, or
# quoted.
_VALUE_FORMS = r"\[masked\]|\$\([^)\r\n]*\)|\$\{[^}\r\n]*\}|\{\{[^\r\n]*?\}\}|" + _QUOTED
# A key that starts its line is a config file's shape - `.env`, properties, YAML,
# INI - whose unquoted value runs to the end of the line, less a trailing comment,
# a trailing `;` or `,`, and a shell line continuation. To the end, because `;` and `,` are legal inside
# such a value, and stopping at one would leave the rest of the secret behind.
# A quoted key is a JSON or dict-literal entry instead, where an unquoted value
# that a `,` ends is code, as it is inline.
_LINE_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?P<quote>[\"']?)"
    + _KEY
    + r"(?P<value>"
    + _VALUE_FORMS
    + r"|\S[^\r\n]*?)"
    r"(?=(?P<end>[ \t]+[#\\]|[ \t]*[,;]?[ \t\r]*$))",
    re.MULTILINE,
)
# A key anywhere else. Its unquoted value ends at a separator, and one that a
# `,`, `;` or closing bracket ends is code - a parameter, an argument, an entry
# in a literal - so it is left alone; `&` ends a query-string value.
_INLINE_ASSIGNMENT = re.compile(
    r"(?<![\w.-])" + _KEY + r"(?P<value>" + _VALUE_FORMS + r"|[^\s,;&'\"`)}\]][^\r\n,;&)}\]]*?)"
    r"(?=(?P<end>[ \t]+[#\\]|[ \t\r]*(?:[,;&)}\]]|$)))",
    re.MULTILINE,
)
_CODE_END = frozenset(",;)}]")
# Code at the start of a line: a name assigned to a member, or a name a `,` or `;`
# ends - `this.password = password;`, `self.token = token`, `password: password,`.
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
_MEMBER_KEY = re.compile(r"(?:this|self)\.")
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
# `$(command)`, `%VAR%`, and a `#{Var}` deployment-tool substitution token.
_BARE_VARIABLE = re.compile(r"\$[A-Za-z_]\w*|\$\([^)\r\n]*\)|%[A-Za-z_]\w*%|#\{[^}\r\n]*\}")
# A value naming the store a secret is read from, by the prefix that says so.
_STORE_REFERENCE = re.compile(
    r"(?i)@Microsoft\.KeyVault\(|ref\+[a-z0-9]+://|vault:|arn:aws:(?:secretsmanager|ssm):"
    r"|(?:sm|gcpsm|awssm|azurekv)://|secret(?:key)?ref\b"
)
# Code reading a value from somewhere else: a call or a subscript.
_CODE_EXPRESSION = re.compile(r"[A-Za-z_][\w.]*[ \t]*[(\[]")


# A word that turns a secret word into the name of where it is kept: a chart's
# `existingSecret` names the Secret object, not its contents.
_REFERENCE_WORD = "existing"


def _key_words(key: str) -> list[str]:
    words = [word.lower() for word in _KEY_WORD.findall(key)]
    while words and words[-1].isdigit():
        words.pop()
    return words


def _spelt_ends(words: list[str], term: re.Pattern[str]) -> list[int]:
    """Where each run of whole words spelling `term` ends, `existing` ones aside."""
    return [
        end
        for start in range(len(words))
        for end in range(start + 1, len(words) + 1)
        if term.fullmatch("".join(words[start:end]))
        and words[start - 1 : start] != [_REFERENCE_WORD]
    ]


def _fused_ends(words: list[str], ending: re.Pattern[str]) -> list[int]:
    """Where each word that ends in `ending` after some other letters ends."""
    found = ((index, ending.search(word)) for index, word in enumerate(words))
    return [
        index + 1
        for index, match in found
        if match and match.start() > 0 and words[index] not in _NOT_FUSED
    ]


def _secret_kind(key: str) -> str | None:
    """`_ANY` or `_GUARDED` for a key named for a secret, else None."""
    words = _key_words(key)
    if not words or words[-1] in _METADATA_WORDS:
        return None
    strong = _spelt_ends(words, _SECRET_TERM) + _fused_ends(words, _FUSED_SECRET)
    if len(words) in strong:
        return _ANY
    weak = _spelt_ends(words, _CREDENTIAL_TERM) + _fused_ends(words, _FUSED_CREDENTIAL)
    return _GUARDED if strong or weak else None


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
    if not value or MASK in value or _is_reference(value):
        return True
    # A secret has letters or digits in it; `(` opening a multi-line value does not.
    if not _ALNUM.search(value):
        return True
    return literals and (value.lower() in _LITERAL_WORDS or bool(_NUMBER_OR_BLOCK.fullmatch(value)))


# A URL: its scheme, then the authority a password would sit in.
_URL_SCHEME = re.compile(r"[A-Za-z][\w+.-]*(?::[A-Za-z][\w+.-]*)*://")
_URL_AUTHORITY = re.compile(r"[^\s/?#]*")
# A Terraform reference, its head then any attributes and indexes after it.
_HCL_HEAD = re.compile(
    r"(?:var|local|each|count)\.[\w-]+|(?:module|data)\.[\w-]+\.[\w-]+"
    r"|[a-z][a-z0-9]*_[a-z0-9_]+\.[\w-]+\.[\w-]+"
)
_HCL_TAIL = re.compile(r"(?:\.[\w-]+|\[[^\]\s]*\])*")
_PATH = re.compile(r"[~.]{0,2}/|[A-Za-z]:[\\/]")
_DOTTED_NAME = re.compile(r"[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)+")
_WHITESPACE = re.compile(r"\s")
_HEX = re.compile(r"[0-9a-fA-F]{16,}")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
# The characters of a generated key or token: base64, hex, URL-safe and separators.
_TOKEN_CHARACTERS = re.compile(r"[\w+/=.~:-]+")
_LETTER = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"\d")
# A name made of words: `user-profile-cache-v2`, `createdAtTimestamp`, `DB_HOST`.
# Three shapes, each its own pattern: a camel or capitalised word, an acronym, a number.
_NAME_SEGMENTS = (
    re.compile(r"[A-Za-z][a-z]*(?:[A-Z][a-z]+)*\d*"),
    re.compile(r"[A-Z]+\d*"),
    re.compile(r"\d+"),
)
_SEGMENT_SEPARATOR = re.compile(r"[-_]")
_CREDENTIAL_LENGTH = 16


def _is_plain_url(value: str) -> bool:
    """A URL with no password in it: `scheme://host[:port][/path][?query]`."""
    scheme = _URL_SCHEME.match(value)
    if scheme is None or _WHITESPACE.search(value):
        return False
    authority = _URL_AUTHORITY.match(value, scheme.end())
    return authority is not None and "@" not in authority.group(0)


def _is_hcl_reference(value: str) -> bool:
    head = _HCL_HEAD.match(value)
    return head is not None and _HCL_TAIL.fullmatch(value, head.end()) is not None


def _points_elsewhere(value: str) -> bool:
    """A plain URL or a Terraform reference: where something is, not a secret."""
    return _is_plain_url(value) or _is_hcl_reference(value)


def _is_name(value: str) -> bool:
    return all(_is_name_segment(part) for part in _SEGMENT_SEPARATOR.split(value))


def _is_name_segment(part: str) -> bool:
    return any(shape.fullmatch(part) for shape in _NAME_SEGMENTS)


def _has_secret_shape(value: str) -> bool:
    """Hex, a UUID, or base64 that is not just a word: mixed case and padded or `+`."""
    if _HEX.fullmatch(value) or _UUID.fullmatch(value):
        return True
    mixed_case = value.lower() != value and value.upper() != value
    padded = "+" in value or value.endswith("=")
    return mixed_case and padded and _BASE64.fullmatch(value) is not None


def _is_not_a_token(value: str) -> bool:
    """Too short, holding a character no generated key does, or saying where
    something is - a URL, a path, a dotted name: not a credential."""
    return (
        len(value) < _CREDENTIAL_LENGTH
        or _TOKEN_CHARACTERS.fullmatch(value) is None
        or _URL_SCHEME.match(value) is not None
        or _PATH.match(value) is not None
        or _DOTTED_NAME.fullmatch(value) is not None
    )


def _looks_like_credential(value: str) -> bool:
    """One token of sixteen key characters or more, not a URL, a path or a dotted
    name, and either a hex, UUID or base64 shape, or letters and digits mixed
    in a way no name made of words is."""
    if _is_not_a_token(value):
        return False
    if _has_secret_shape(value):
        return True
    mixed = _LETTER.search(value) is not None and _DIGIT.search(value) is not None
    return mixed and not _is_name(value)


def _admits(kind: str, value: str) -> bool:
    """Whether a value is secret under a key of this kind, before the checks every
    key-name value gets."""
    if kind == _GUARDED:
        return _looks_like_credential(value)
    return kind == _MANIFEST or not _points_elsewhere(value)


def _masked_value(value: str, literals: bool = False) -> str | None:
    """`value` masked, keeping its quotes, or None when it is not a secret."""
    quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
    inner = value[1:-1] if quoted else value
    if _not_a_secret(inner, literals):
        return None
    return f"{value[0]}{MASK}{value[0]}" if quoted else MASK


def _masked_under(kind: str, value: str) -> str | None:
    """`value` masked, keeping its quotes, or None when a key of `kind` holding it
    holds no secret."""
    if not _admits(kind, _unquote(value).strip()):
        return None
    return _masked_value(value, literals=True)


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
        return whole[:start] + _line_by_line(whole[start:end], masked) + whole[end:]

    return pattern.sub(replace, text)


def _line_by_line(span: str, masked: str) -> str:
    """`masked`, or for a span over several lines each of its lines masked, so the
    lines after it keep their numbers and every line ending stays."""
    if "\n" not in span:
        return masked
    return "\n".join(_masked_line(line) for line in span.split("\n"))


def _masked_line(line: str) -> str:
    body = line.strip(" \t\r")
    if not body:
        return line
    lead = len(line) - len(line.lstrip(" \t"))
    return line[:lead] + MASK + line[len(line.rstrip(" \t\r")) :]


# A URL query or fragment parameter: its separator, name and value. Found in two
# steps - any parameter here, then its name read against the secret names - so
# each pattern stays small and linear.
_QUERY_PARAMETER = re.compile(r"(?:[?&#]|&amp;)(?P<name>[A-Za-z_]+)=(?P<value>[^&#\s\"'<>)]+)")
_QUERY_TOKEN_NAME = re.compile(r"(?i)(?:(?:access|refresh|id)_)?token")
_QUERY_SECRET_NAME = re.compile(r"(?i)api_?key|(?:client_)?secret|passw(?:or)?d|pwd|auth|signature")
# Names that are as often an ordinary parameter - `?code=200`, `?key=name`.
_QUERY_GUARDED_NAME = re.compile(r"(?i)code|key")


def _is_query_secret(name: str, value: str) -> bool:
    named = _QUERY_TOKEN_NAME.fullmatch(name) or _QUERY_SECRET_NAME.fullmatch(name)
    guarded = _QUERY_GUARDED_NAME.fullmatch(name) is not None
    return _looks_like_credential(value) if guarded else named is not None


def _apply_query_secrets(text: str, counts: Counter[str]) -> str:
    """Each secret-named URL parameter's value masked; the URL around it kept."""

    def replace(match: re.Match[str]) -> str:
        whole = match.group(0)
        masked = None
        if _is_query_secret(match.group("name"), match.group("value")):
            masked = _masked_value(match.group("value"))
        if masked is None:
            return whole
        counts[URL_QUERY_RULE] += 1
        return whole[: match.start("value") - match.start()] + masked

    return _QUERY_PARAMETER.sub(replace, text)


def _reads_as_code(match: re.Match[str], value: str) -> bool:
    """An unquoted value the shape around it says is code rather than a setting."""
    if value[0] in "\"'":
        return False
    ended_as_code = match.group("end").strip() in _CODE_END
    if ended_as_code and (match.re is _INLINE_ASSIGNMENT or match.group("quote")):
        return True
    named = _IDENTIFIER.fullmatch(value) is not None
    return named and (ended_as_code or _MEMBER_KEY.match(match.group("key")) is not None)


# A literal in code that names what is looked up rather than holding a value:
# `os.environ["DB_PASSWORD"]`, `config.get("db.password")`, `get_secret("app-db")`.
_LOOKUP_NAME = re.compile(r"[A-Z][A-Z0-9_]*|[a-z]+(?:[-_][a-z]+)*")
# A string prefix in front of a literal's quote. A raw string is a pattern or a
# path, and an f-string interpolates: neither is a value written out.
_STRING_PREFIX = re.compile(r"(?<!\w)[A-Za-z]{1,2}$")


def _names_a_lookup(code: str, literal: re.Match[str]) -> bool:
    """A literal in code that is a name looked up, a pattern or a template."""
    inner = _unquote(literal.group(0))
    if _LOOKUP_NAME.fullmatch(inner) or _DOTTED_NAME.fullmatch(inner):
        return True
    prefix = _STRING_PREFIX.search(code, 0, literal.start())
    return prefix is not None and bool(set(prefix.group(0).lower()) & {"r", "f"})


def _masked_literals(kind: str, code: str, counts: Counter[str]) -> str | None:
    """`code` with each quoted literal in it masked, or None when none is secret.
    The call, its references and its punctuation are kept."""
    edits = []
    for literal in _QUOTED_VALUE.finditer(code):
        if _names_a_lookup(code, literal):
            continue
        masked = _masked_under(kind, literal.group(0))
        if masked is not None:
            edits.append((literal.start(), literal.end(), masked))
    counts[KEY_NAME_RULE] += len(edits)
    return _splice(code, edits) if edits else None


def _masked_query(value: str, counts: Counter[str]) -> str | None:
    """A plain URL, quoted or not, with each secret-named parameter of its query
    masked, or None when it has none."""
    url = _unquote(value)
    query = url.find("?")
    if query < 0:
        return None
    original = url[query:]
    tail = _INLINE_ASSIGNMENT.sub(lambda match: _key_value(match, counts), original)
    if tail == original:
        return None
    quote = value[: (len(value) - len(url)) // 2]
    return quote + url[:query] + tail + quote


def _edited_value(kind: str, value: str, counts: Counter[str]) -> str | None:
    """The value of an assignment under a key of `kind` with its secret masked, or
    None when it holds none."""
    if value[0] not in "\"'" and _CODE_EXPRESSION.match(value):
        return _masked_literals(kind, value, counts)
    if kind == _ANY and _is_plain_url(_unquote(value)):
        return _masked_query(value, counts)
    # A literal with code after it, as in `'...');`: only the literal is the value.
    literal = _QUOTED_VALUE.match(value)
    rest = value[literal.end() :] if literal else ""
    masked = _masked_under(kind, value[: len(value) - len(rest)])
    if masked is None:
        return None
    counts[KEY_NAME_RULE] += 1
    return masked + rest


def _key_value(match: re.Match[str], counts: Counter[str]) -> str:
    """One assignment with its value masked, or unchanged when it holds no secret."""
    whole, value = match.group(0), match.group("value")
    kind = _secret_kind(match.group("key"))
    if kind is None or _reads_as_code(match, value):
        return whole
    start = match.start("value") - match.start()
    head = _annotation_length(value)
    if head:
        start += head
        value = value[head:]
    edited = _edited_value(kind, value, counts) if value else None
    if edited is None:
        return whole
    return whole[:start] + edited + whole[start + len(value) :]


def _apply_key_names(text: str, counts: Counter[str]) -> str:
    # Line-start keys first, so a config value is read to the end of its line
    # before the inline rule could stop it at a separator inside it.
    for pattern in (_LINE_ASSIGNMENT, _INLINE_ASSIGNMENT):
        text = pattern.sub(lambda match: _key_value(match, counts), text)
    return text


# --- Shapes the assignment rules cannot see ----------------------------------
# Each of these holds a secret under a key that names nothing, or a value that is
# not on its key's line. All count under the key-name rule: each is a key naming
# a secret, written in another syntax. Every pattern here is small on purpose;
# the functions put them together.

_NAME_KEYS = ("name", "key")
_VALUE_KEY = "value"


class _Field(NamedTuple):
    """One `key: value` or `key = value` read out of a larger text."""

    key: str
    quoted_key: bool
    separator: str
    value: str | None  # None for a key with nothing after it on its line
    start: int  # where the value starts and ends in the text it was read from
    end: int


def _splice(text: str, edits: list[tuple[int, int, str]]) -> str:
    """`text` with each `(start, end)` span replaced, the spans not overlapping."""
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _unquote(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value


def _pair_kind(fields: dict[str, str]) -> str | None:
    """How secret the `value` beside a `name`/`key` field is, by what that field
    names, or None when it names no secret."""
    kinds = (_secret_kind(_unquote(fields.get(key, ""))) for key in _NAME_KEYS)
    return next((kind for kind in kinds if kind), None)


def _values_by_key(fields: list[_Field]) -> dict[str, str]:
    return {field.key.lower(): field.value or "" for field in fields}


# An XML start tag with attributes, and one attribute in it. Attribute values are
# always quoted, so the quotes say where the value ends.
_XML_TAG = re.compile(r"<[A-Za-z_][\w.:-]*[ \t\r\n][^<>]*>")
_XML_ATTRIBUTE = re.compile(
    r"(?<![\w.:-])(?P<key>[\w.:-]+)[ \t\r\n]*=[ \t\r\n]*(?P<value>\"[^\"<>]*\"|'[^'<>]*')"
)
# An element holding only text: `<password>...</password>`.
_XML_ELEMENT = re.compile(r"<(?P<tag>[A-Za-z_][\w.:-]*)(?:[ \t][^<>]*)?>(?P<text>[^<]*)</(?P=tag)>")


def _xml_tag(match: re.Match[str], counts: Counter[str]) -> str:
    """An attribute named for a secret, or `value` beside a `key`/`name` naming one."""
    tag = match.group(0)
    attributes = list(_XML_ATTRIBUTE.finditer(tag))
    paired = _pair_kind({a.group("key").lower(): a.group("value") for a in attributes})
    edits = []
    for attribute in attributes:
        key = attribute.group("key")
        kind = _secret_kind(key) or (paired if key.lower() == _VALUE_KEY else None)
        if kind is None:
            continue
        masked = _masked_under(kind, attribute.group("value"))
        if masked is not None:
            edits.append((attribute.start("value"), attribute.end("value"), masked))
    counts[KEY_NAME_RULE] += len(edits)
    return _splice(tag, edits)


def _xml_element(match: re.Match[str], counts: Counter[str]) -> str:
    whole, text = match.group(0), match.group("text")
    value = text.strip(" \t\r\n")
    kind = _secret_kind(match.group("tag"))
    if not value or kind is None:
        return whole
    masked = _masked_under(kind, value)
    if masked is None:
        return whole
    counts[KEY_NAME_RULE] += 1
    start = match.start("text") - match.start() + text.index(value)
    return whole[:start] + masked + whole[start + len(value) :]


# A mapping in braces holding no other braces but `${...}`: a YAML flow mapping, a
# JSON object, an HCL block. Its fields are a key and a separator, then a quoted
# value or one that ends at a `,`, `;`, `}`, a comment or the end of its line.
_BRACE_GROUP = re.compile(r"\{(?:\$\{[^{}\r\n]*\}|[^{}])*\}")
# A key is found in two steps, each linear: the name, then what must follow it.
# One pattern holding both backtracked through every shorter name on a miss.
_GROUP_KEY_NAME = re.compile(r"(?<![\w.$-])(?P<quote>[\"']?)(?P<key>[\w.-]+)")
_GROUP_KEY_TAIL = re.compile(r"[ \t]*(?P<separator>[:=])(?!=)[ \t]*")
_QUOTED_VALUE = re.compile(_QUOTED)
_GROUP_VALUE_END = re.compile(r"[ \t\r]*(?:[,;}#]|$)", re.MULTILINE)
# Runs to the first `,` `;` `}` `#` or line end, and ends on a non-space.
_GROUP_PLAIN_VALUE = re.compile(r"[^\s,;{}\"'](?:[^\r\n,;{}#]*[^\s,;{}#])?")
# The rest of a line holding a YAML flow mapping as its whole value. Anything else
# around the braces - `=`, a call, a `;` - is code, whose unquoted values are names.
_FLOW_LIST_PREFIX = re.compile(r"[ \t]*(?:-[ \t]+)?")
_FLOW_KEY_PREFIX = re.compile(r"[ \t]*[\"']?[\w.-]+[\"']?:[ \t]+")
_FLOW_SUFFIX = re.compile(r"[ \t]*(?:#[^\r\n]*)?\r?(?=\n|\Z)")
# A Terraform variable block, whose name is a label in front of its braces.
_HCL_VARIABLE = re.compile(r"[ \t]*variable[ \t]+\"(?P<label>[^\"\r\n]+)\"[ \t]*")
_DEFAULT_KEY = "default"


def _group_value(group: str, position: int) -> re.Match[str] | None:
    quoted = _QUOTED_VALUE.match(group, position)
    if quoted and _GROUP_VALUE_END.match(group, quoted.end()):
        return quoted
    return _GROUP_PLAIN_VALUE.match(group, position)


def _group_fields(group: str) -> list[_Field]:
    """Each field of a brace group, read left to right so a value is never a key."""
    fields: list[_Field] = []
    position = 0
    while (key := _next_group_key(group, position)) is not None:
        name, quoted, tail = key
        value = _group_value(group, tail.end())
        position = value.end() if value else tail.end()
        if value:
            fields.append(
                _Field(
                    name,
                    quoted,
                    tail.group("separator"),
                    value.group(0),
                    value.start(),
                    value.end(),
                )
            )
    return fields


def _next_group_key(group: str, position: int) -> tuple[str, bool, re.Match[str]] | None:
    """The next key at or after `position`: its name, whether it was quoted, and
    the separator after it.

    An opening quote with no closing one is not part of the key: the name after
    it is read as an unquoted key, as a single pattern would have read it.
    """
    for name in _GROUP_KEY_NAME.finditer(group, position):
        after = name.end()
        quote = name.group("quote")
        closed = bool(quote) and group.startswith(quote, after)
        tail = _GROUP_KEY_TAIL.match(group, after + len(quote) if closed else after)
        if tail:
            return name.group("key"), closed, tail
    return None


def _is_flow_line(text: str, line_start: int, start: int, end: int) -> bool:
    return (
        "\n" not in text[start:end]
        and (
            _FLOW_LIST_PREFIX.fullmatch(text, line_start, start) is not None
            or _FLOW_KEY_PREFIX.fullmatch(text, line_start, start) is not None
        )
        and _FLOW_SUFFIX.match(text, end) is not None
    )


def _paired_key(
    text: str, line_start: int, start: int, fields: list[_Field]
) -> tuple[str, str] | None:
    """The key holding the secret a group's name names, and how secret it is:
    `value` beside a secret `name`/`key`, or `default` in a Terraform variable
    named for a secret."""
    kind = _pair_kind(_values_by_key(fields))
    if kind:
        return _VALUE_KEY, kind
    label = _HCL_VARIABLE.fullmatch(text, line_start, start)
    kind = _secret_kind(label.group("label")) if label else None
    return (_DEFAULT_KEY, kind) if kind else None


def _field_kind(field: _Field, flow: bool, paired: tuple[str, str] | None) -> str | None:
    # A quoted key naming a secret is the assignment rules' to read.
    if paired and field.key.lower() == paired[0]:
        return paired[1]
    return _secret_kind(field.key) if flow and not field.quoted_key else None


def _masked_field_value(field: _Field, flow: bool, kind: str) -> str | None:
    """The field's value masked, or None. An unquoted value is a YAML scalar only in a
    flow mapping under an unquoted `key:`; anywhere else it is a name in code."""
    value = field.value or ""
    if value[:1] not in ('"', "'"):
        yaml_scalar = flow and not field.quoted_key and field.separator == ":"
        if not yaml_scalar or _CODE_EXPRESSION.match(value):
            return None
    return _masked_under(kind, value)


def _brace_group(match: re.Match[str], line_start: int, counts: Counter[str]) -> str:
    """One brace group's secrets masked; `line_start` is where its first line starts."""
    group, text = match.group(0), match.string
    fields = _group_fields(group)
    flow = _is_flow_line(text, line_start, match.start(), match.end())
    paired = _paired_key(text, line_start, match.start(), fields)
    edits = []
    for field in fields:
        kind = _field_kind(field, flow, paired)
        if kind is None:
            continue
        masked = _masked_field_value(field, flow, kind)
        if masked is not None:
            edits.append((field.start, field.end, masked))
    counts[KEY_NAME_RULE] += len(edits)
    return _splice(group, edits)


# --- YAML read by line: block scalars, list items, Secret manifests ------------
# Each walk takes the file's lines without their line endings, edits them in
# place, and returns how many values it masked.

_BLOCK_INDICATOR = re.compile(r"[|>][-+0-9]*")
_YAML_KEY = re.compile(r"(?P<quote>[\"']?)(?P<key>[\w.-]+)(?P=quote)[ \t]*:(?=[ \t]|$)")
_YAML_VALUE = re.compile(r"[ \t]+(?P<value>" + _QUOTED + r"|[^\s#][^\r\n]*?)(?=[ \t]+#|[ \t]*$)")
_YAML_NO_VALUE = re.compile(r"[ \t]*(?:#.*)?")
_LIST_ITEM = re.compile(r"(?P<lead>[ \t]*-[ \t]+)\S.*")
_SECRET_KIND = re.compile(r"kind:[ \t]*([\"']?)Secret\1[ \t]*(?:#.*)?")
_SECRET_DATA = re.compile(r"(?:data|stringData):[ \t]*(?:#.*)?")


def _yaml_field(line: str, column: int) -> _Field | None:
    """The `key: value` at `column` of a line, or None when it holds none there."""
    key = _YAML_KEY.match(line, column)
    if key is None:
        return None
    value = _YAML_VALUE.match(line, key.end())
    name, quoted = key.group("key"), bool(key.group("quote"))
    if value:
        start, end = value.span("value")
        return _Field(name, quoted, ":", value.group("value"), start, end)
    if _YAML_NO_VALUE.fullmatch(line, key.end()):
        return _Field(name, quoted, ":", None, key.end(), key.end())
    return None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _is_blank(line: str) -> bool:
    return not line.strip(" \t")


def _block_end(lines: list[str], start: int, column: int) -> int:
    """The index after the last line indented deeper than `column`, blank ones kept."""
    end = start
    while end < len(lines) and (_is_blank(lines[end]) or _indent(lines[end]) > column):
        end += 1
    return end


def _mask_block(lines: list[str], start: int, column: int, kind: str) -> int:
    """Mask each line of the block scalar opening at `start`, keeping its indentation.

    A line already holding `[masked]` is left alone: a shape rule got there first,
    and the lines a shape leaves either side of it - a key's armour - are not secret.
    """
    end = _block_end(lines, start, column)
    content = [line.strip(" \t") for line in lines[start:end] if not _is_blank(line)]
    # A credential wrapped over lines is one token; anything else reads as text.
    joined = ("" if kind == _GUARDED else " ").join(content)
    if _not_a_secret(joined, literals=True) or not _admits(kind, joined):
        return 0
    masked = 0
    for index in range(start, end):
        line = lines[index]
        if _is_blank(line) or MASK in line:
            continue
        lines[index] = line[: _indent(line)] + MASK + line[len(line.rstrip(" \t")) :]
        masked = 1
    return masked


def _mask_field(lines: list[str], index: int, column: int, field: _Field, kind: str) -> int:
    """Mask a `key: value` line's value, or the block scalar it opens."""
    if field.value is None:
        return 0
    if _BLOCK_INDICATOR.fullmatch(field.value):
        return _mask_block(lines, index + 1, column, kind)
    masked = _masked_under(kind, field.value)
    if masked is None:
        return 0
    line = lines[index]
    lines[index] = line[: field.start] + masked + line[field.end :]
    return 1


def _key_column(line: str) -> int:
    """Where a line's key starts: after its indentation, and after a list item's `- `."""
    item = _LIST_ITEM.fullmatch(line)
    return len(item.group("lead")) if item else _indent(line)


def _mask_block_scalars(lines: list[str]) -> int:
    masked = 0
    for index, line in enumerate(lines):
        column = _key_column(line)
        field = _yaml_field(line, column)
        if field and field.value and _BLOCK_INDICATOR.fullmatch(field.value):
            kind = _secret_kind(field.key)
            if kind:
                masked += _mask_block(lines, index + 1, column, kind)
    return masked


def _item_fields(lines: list[str], start: int, column: int) -> dict[str, tuple[int, _Field]]:
    """The fields of the list item starting at `start`: its lines at `column`."""
    fields: dict[str, tuple[int, _Field]] = {}
    for index in range(start, _block_end(lines, start + 1, column - 1)):
        field = _yaml_field(lines[index], column)
        if field and (index == start or _indent(lines[index]) == column):
            fields.setdefault(field.key.lower(), (index, field))
    return fields


def _mask_list_items(lines: list[str]) -> int:
    """`- name: DB_PASSWORD` with `value: ...` in the same item: the env-list shape."""
    masked = 0
    for start, line in enumerate(lines):
        item = _LIST_ITEM.fullmatch(line)
        if not item:
            continue
        column = len(item.group("lead"))
        fields = _item_fields(lines, start, column)
        kind = _pair_kind(_values_by_key([field for _, field in fields.values()]))
        if _VALUE_KEY in fields and kind:
            index, field = fields[_VALUE_KEY]
            masked += _mask_field(lines, index, column, field, kind)
    return masked


def _documents(lines: list[str]) -> list[range]:
    """The line ranges of a YAML stream's documents, split at each `---`."""
    starts = [0] + [i + 1 for i, line in enumerate(lines) if line.startswith("---")]
    return [range(start, end) for start, end in zip(starts, [*starts[1:], len(lines)])]


def _mask_secret_data(lines: list[str], start: int) -> int:
    """Every value directly under a Secret's `data:` or `stringData:` at `start`."""
    end = _block_end(lines, start + 1, 0)
    children = [i for i in range(start + 1, end) if not _is_blank(lines[i])]
    if not children:
        return 0
    column = _indent(lines[children[0]])
    masked = 0
    for index in children:
        field = _yaml_field(lines[index], column)
        if field and _indent(lines[index]) == column:
            masked += _mask_field(lines, index, column, field, _MANIFEST)
    return masked


def _secret_data_lines(lines: list[str], document: range) -> list[int]:
    """The `data:` and `stringData:` lines of a document, if it is a Secret."""
    if not any(_SECRET_KIND.fullmatch(lines[i]) for i in document):
        return []
    return [i for i in document if _SECRET_DATA.fullmatch(lines[i])]


def _mask_secret_manifests(lines: list[str]) -> int:
    """A Kubernetes Secret's data, whatever its keys are called: all of it is secret."""
    return sum(
        _mask_secret_data(lines, index)
        for document in _documents(lines)
        for index in _secret_data_lines(lines, document)
    )


def _apply_structures(text: str, counts: Counter[str]) -> str:
    text = _XML_TAG.sub(lambda match: _xml_tag(match, counts), text)
    text = _XML_ELEMENT.sub(lambda match: _xml_element(match, counts), text)
    # Line starts found once: searching back from each group is quadratic on a
    # minified file, which is one line holding thousands of them.
    breaks = [found.start() for found in re.finditer("\n", text)]

    def line_start(position: int) -> int:
        before = bisect.bisect_left(breaks, position)
        return breaks[before - 1] + 1 if before else 0

    text = _BRACE_GROUP.sub(
        lambda match: _brace_group(match, line_start(match.start()), counts), text
    )
    # By line, with each line's `\r` set aside so a CRLF file keeps it.
    raw = text.split("\n")
    lines = [line.removesuffix("\r") for line in raw]
    for walk in (_mask_block_scalars, _mask_list_items, _mask_secret_manifests):
        counts[KEY_NAME_RULE] += walk(lines)
    return "\n".join(line + "\r" * original.endswith("\r") for line, original in zip(lines, raw))


def mask(text: str, counts: Counter[str] | None = None) -> str:
    """`text` with every secret value these rules recognise replaced by `[masked]`.

    `counts` accumulates the values masked per rule name, for the run report.
    Masking masked text finds nothing more: `[masked]` is never read as a value.
    """
    tally: Counter[str] = Counter()
    for rule, pattern in rules():
        text = _apply(text, rule, pattern, tally)
    text = _apply_query_secrets(text, tally)
    text = _apply_key_names(text, tally)
    text = _apply_structures(text, tally)
    if counts is not None:
        counts.update(tally)
    return text


__all__ = ["KEY_NAME_RULE", "MASK", "URL_QUERY_RULE", "mask", "rules"]
