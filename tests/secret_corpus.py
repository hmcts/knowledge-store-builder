"""Realistic files an estate commits, with invented secrets planted in them.

Each file under `masking_corpus/` is a template: `@@S:<name>@@` marks a planted
secret and `@@K:<name>@@` a piece of text kept as it is. The values are assembled
here at runtime, from parts where a secret scanner knows the shape, so no file in
this repository holds one whole. Rendering a template twice - once with the
planted values, once with `[masked]` in their place - gives the input a worker's
file would hold and, written by hand through the markers, the only output masking
may produce from it.
"""

from __future__ import annotations

import re
from pathlib import Path

CORPUS = Path(__file__).resolve().parent / "masking_corpus"
MASK = "[masked]"

_MARKER = re.compile(r"@@(?P<kind>[SK]):(?P<name>\w+)@@")

# Values a shape rule must recognise, assembled from parts.
_SHAPED = {
    "helm_sdk_key": "sdk" + "-0a1b2c3d-0000-4000-8000-00000000abcd",
    "dotenv_aws_id": "AK" + "IA" + "ZZFAKE0123456789",
    "dotenv_slack_path": "T000" + "/B000/" + "fakeSlackPath9",
    "sh_github_token": "gh" + "p_" + "Fake" * 9,
    "sh_bearer": "Fake.Bearer" + "_Token9",
    "pem_body": "\n    MIIEfake" + "KeyBodyLineA\n    fakeKeyBodyLineB\n    ",
}
KEPT = {
    "pem_head": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
    "pem_tail": "-----END " + "RSA PRIVATE" + " KEY-----",
}


def planted_value(name: str) -> str:
    """The invented secret planted at `@@S:<name>@@`: distinct per name, and
    never a substring of another, because each ends in `9x`."""
    if name in _SHAPED:
        return _SHAPED[name]
    return "Zq" + "".join(part.title() for part in name.split("_")) + "9x"


def planted_traces(name: str) -> list[str]:
    """What must not survive of a planted value: the value, or each line of it."""
    return [line.strip() for line in planted_value(name).split("\n") if line.strip()]


def render(template: str, *, masked: bool) -> str:
    def value(match: re.Match[str]) -> str:
        name = match.group("name")
        if match.group("kind") == "K":
            return KEPT[name]
        return MASK if masked else planted_value(name)

    return _MARKER.sub(value, template)


def planted_names(template: str) -> list[str]:
    return [m.group("name") for m in _MARKER.finditer(template) if m.group("kind") == "S"]


def corpus() -> list[tuple[str, str]]:
    """(file name as an estate would commit it, template), in name order."""
    return [
        (path.name.removesuffix(".in"), path.read_text(encoding="utf-8"))
        for path in sorted(CORPUS.glob("*.in"))
    ]
