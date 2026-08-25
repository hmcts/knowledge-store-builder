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
import sys
from collections import Counter


from . import config
from . import graph_files
from . import io


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
    graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
    print(
        graph_files.stale_note(config.GRAPH_PATH, graph.get("nodes", []), "the semantic layer"),
        end="",
        file=sys.stderr,
    )
    texts = [n.get("label") or "" for n in graph["nodes"]]
    if config.LABELS_PATH.exists():
        texts += list(json.loads(config.LABELS_PATH.read_text(encoding="utf-8")).values())
    if config.SUMMARIES_PATH.exists():
        texts += list(json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8")).values())

    # Document frequency, not raw occurrences: each token counts once per text,
    # so a word repeated inside a single label cannot qualify on its own. The
    # old behaviour counted occurrences, which contradicted this constant's
    # name and the printed "df >=" claim.
    frequency: Counter = Counter()
    for text in texts:
        frequency.update({t for t in TOKEN.findall(text.lower()) if t not in STOP})

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


def write_manifest(vocab: list[str], dimensions: int) -> None:
    """Reproducibility manifest beside the committed neighbour map.

    Everything that decided the artefact - model, library version, thresholds,
    and a hash of the exact vocabulary - so a rebuild can state whether like
    was compared with like. No timestamps: the manifest must be as
    deterministic as the artefact it describes.
    """
    import hashlib
    from importlib import metadata

    try:
        fastembed_version = metadata.version("fastembed")
    except metadata.PackageNotFoundError:
        fastembed_version = "unknown"
    io.write_json(
        config.SYNONYMS_PATH.parent / "manifest.json",
        {
            "model": config.EMBEDDING_MODEL,
            "dimensions": dimensions,
            "fastembed": fastembed_version,
            "max_vocab": MAX_VOCAB,
            "min_df": MIN_DF,
            "min_similarity": MIN_SIMILARITY,
            "neighbours_per_token": NEIGHBOURS,
            "vocabulary_size": len(vocab),
            "vocabulary_sha256": hashlib.sha256("\n".join(vocab).encode("utf-8")).hexdigest(),
        },
        indent=1,
    )


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

    io.write_gzip_json(config.SYNONYMS_PATH, neighbours)
    write_manifest(vocab, dimensions=int(vectors.shape[1]))

    size_kb = config.SYNONYMS_PATH.stat().st_size / 1024
    print(
        f"{len(neighbours)} tokens with neighbours ({kept_pairs} pairs, "
        f"sim >= {MIN_SIMILARITY}) -> {config.SYNONYMS_PATH} ({size_kb:.0f} KB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
