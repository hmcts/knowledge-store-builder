"""Semantic token-neighbour index - GraphRAG phase 2, computed at build time.

Lexical matching cannot bridge vocabulary gaps: a question about "court
outcomes" never matches nodes labelled "results". Embedding models can -
but shipping one to the browser would break the explorer's double-click-a-
file deployment (file:// pages cannot fetch model assets). So the model
runs HERE, once, at build time on a maintainer's machine, and only its
distilled output ships: for every distinctive token in the graph's
vocabulary (node labels, community summaries, business features), its
nearest semantic neighbours by MiniLM cosine similarity.

At query time the explorer expands question terms through this committed
map - a pure lookup, deterministic, no licence, no model, no network.

Requires the optional embedding dependencies (build-time only):

    pip install fastembed numpy     # ~50 MB, downloads MiniLM on first run

Then:

    knowledgestore semantic
      -> knowledge/semantic/token-neighbours.json.gz  (committed)
"""

from __future__ import annotations

import json
import re
from collections import Counter


from . import config
from . import io

GRAPH_PATH = config.GRAPH_PATH
LABELS_PATH = config.LABELS_PATH
SUMMARIES_PATH = config.SUMMARIES_PATH
OUTPUT = config.SYNONYMS_PATH

TOKEN = re.compile(r"[a-z]{4,24}")
MIN_DF = 3
MAX_VOCAB = 15000
NEIGHBOURS = 6
MIN_SIMILARITY = 0.55
STOP = set(
    """also been being both does each from have having here into itself more most
    only other over same some such than that them then there these they this those
    under until upon very were what when where which while will with would your
    should shall could about after before because between during against""".split()
)


def collect_vocabulary() -> list[str]:
    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    texts = [n.get("label") or "" for n in graph["nodes"]]
    if LABELS_PATH.exists():
        texts += list(json.loads(LABELS_PATH.read_text(encoding="utf-8")).values())
    if SUMMARIES_PATH.exists():
        texts += list(json.loads(SUMMARIES_PATH.read_text(encoding="utf-8")).values())

    frequency: Counter = Counter()
    for text in texts:
        frequency.update(t for t in TOKEN.findall(text.lower()) if t not in STOP)

    vocab = [t for t, n in frequency.most_common(MAX_VOCAB) if n >= MIN_DF]
    return vocab


def nearest_neighbours(vocab: list[str], row, token: str) -> list:
    """Top semantic neighbours for one token, skipping trivial shared-stem
    pairs - lexical matching already covers those (result/results)."""
    near: list = []
    for j in row.argsort()[::-1][1 : NEIGHBOURS * 4]:
        score = float(row[j])
        if score < MIN_SIMILARITY:
            break
        other = vocab[j]
        if other.startswith(token[:4]) and token.startswith(other[:4]):
            continue
        near.append([other, round(score, 3)])
        if len(near) == NEIGHBOURS:
            break
    return near


def main() -> int:
    try:
        import numpy as np
        from fastembed import TextEmbedding
    except ImportError:
        print(
            "Build-time embedding dependencies missing.\n"
            "Install with: pip install 'hmcts-knowledge-store-builder[semantic]'"
        )
        return 1

    vocab = collect_vocabulary()
    print(f"Vocabulary: {len(vocab)} tokens (df >= {MIN_DF})")

    model = TextEmbedding(config.EMBEDDING_MODEL)
    vectors = np.array(list(model.embed(vocab)), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    print(f"Embedded {len(vocab)} tokens ({vectors.shape[1]} dims)")

    similarity = vectors @ vectors.T
    neighbours: dict[str, list] = {}
    kept_pairs = 0
    for i, token in enumerate(vocab):
        near = nearest_neighbours(vocab, similarity[i], token)
        if near:
            neighbours[token] = near
            kept_pairs += len(near)

    io.write_gzip_json(OUTPUT, neighbours)

    size_kb = OUTPUT.stat().st_size / 1024
    print(
        f"{len(neighbours)} tokens with neighbours ({kept_pairs} pairs, "
        f"sim >= {MIN_SIMILARITY}) -> {OUTPUT} ({size_kb:.0f} KB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
