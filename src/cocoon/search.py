"""Vendored BM25 ranker.

Tiny implementation (Robertson/Spärck-Jones BM25 with the standard
k1=1.5, b=0.75 defaults). Picked over substring scoring because it
weighs rare terms higher and normalizes for document length, which
matters when ranking short endpoint summaries against multi-word
queries. Picked over a dependency (rank-bm25, etc.) because the
arithmetic fits in 30 lines.

Tokenization is deliberately dumb: lowercased ASCII word chunks.
That's enough for the endpoint-summary domain; richer normalization
(stemming, synonyms) can swap in by replacing `tokenize`.
"""

import math
import re
from typing import Sequence

_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def rank(query: str, documents: Sequence[str], *, k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Return BM25 score per document, same order as the input sequence."""
    q_terms = tokenize(query)
    if not q_terms or not documents:
        return [0.0] * len(documents)

    tokenized = [tokenize(doc) for doc in documents]
    lengths = [len(d) for d in tokenized]
    avgdl = sum(lengths) / len(lengths) if lengths else 0.0
    n_docs = len(tokenized)

    # df: how many documents contain each unique query term.
    df: dict[str, int] = {term: sum(1 for d in tokenized if term in d) for term in set(q_terms)}

    scores = [0.0] * n_docs
    for i, doc in enumerate(tokenized):
        if not doc:
            continue
        length_norm = 1 - b + b * (lengths[i] / avgdl) if avgdl else 1.0
        tf: dict[str, int] = {}
        for term in doc:
            if term in df:
                tf[term] = tf.get(term, 0) + 1
        s = 0.0
        for term, freq in tf.items():
            idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * (freq * (k1 + 1)) / (freq + k1 * length_norm)
        scores[i] = s
    return scores
