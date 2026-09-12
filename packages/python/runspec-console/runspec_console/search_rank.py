"""
search_rank.py — BM25(F) lexical ranking for search-mode runnable discovery.

Search mode (``[llm] tool_mode = "search"``) hides the fleet behind
``search_runnables``; the quality of what the agent finds is only as good as the
ranking. This replaces the earlier flat substring-overlap score with a BM25-style
ranking over a runnable's **name, description, group and argument names**, each
field weighted (a hit in the name counts for more than one buried in description
prose), with the usual BM25 term-frequency saturation and length normalisation so
a long description can't dominate by sheer word count and a common term (one in
every runnable) contributes little.

Pure and dependency-free (stdlib ``math`` + ``re``), so it's unit-testable without
the bridge, and deterministic — equal scores break ties by the caller's stable
ordering.
"""

from __future__ import annotations

import math
import re
from typing import Any

_TOKEN_RE = re.compile(r"\w+")


def tokenize(text: str) -> list[str]:
    """Lowercased word tokens. Shared by the query and every field so matching is
    symmetric."""
    return _TOKEN_RE.findall(str(text or "").lower())


# Per-field multipliers. The operator usually queries by what they *call* the
# runnable (its name) or the package it lives in (group); arg names are a decent
# signal; the description is prose that mentions many incidental terms, so it's the
# baseline weight.
DEFAULT_FIELD_WEIGHTS: dict[str, float] = {
    "name": 3.0,
    "group": 2.0,
    "args": 1.5,
    "description": 1.0,
}


def _field_tokens(doc: dict[str, Any], field: str) -> list[str]:
    """Tokens for one field of a doc. A list/tuple field (e.g. ``args`` — the arg
    names) is flattened; everything else is stringified and tokenised."""
    val = doc.get(field)
    if isinstance(val, (list, tuple)):
        toks: list[str] = []
        for v in val:
            toks.extend(tokenize(v))
        return toks
    return tokenize(val if isinstance(val, str) else str(val or ""))


def bm25f_scores(
    query: str,
    docs: list[dict[str, Any]],
    *,
    k1: float = 1.2,
    b: float = 0.75,
    weights: dict[str, float] | None = None,
) -> list[float]:
    """BM25F-style relevance score for each doc, aligned to ``docs`` order.

    Each doc is a mapping of field → text (or, for ``args``, a list of names). The
    score combines per-field weighted term frequencies into one BM25 saturation per
    query term, normalised by the doc's weighted length against the corpus average.
    An empty query (or empty corpus) returns all zeros — the caller treats that as
    "browse" and ranks everything equally.

    ``k1`` controls term-frequency saturation; ``b`` the length normalisation —
    the standard BM25 defaults. Pure/deterministic."""
    w = weights or DEFAULT_FIELD_WEIGHTS
    # Unique query terms, order-stable (repeats in the query don't inflate a match).
    q_terms = list(dict.fromkeys(tokenize(query)))
    n = len(docs)
    if not q_terms or n == 0:
        return [0.0] * n

    doc_fields = [{f: _field_tokens(d, f) for f in w} for d in docs]
    # Weighted document length (Σ field_weight × field_token_count) and its mean.
    dl = [sum(w[f] * len(toks) for f, toks in df.items()) for df in doc_fields]
    avgdl = (sum(dl) / n) if n else 0.0

    # Document frequency: a term counts once per doc if it appears in *any* field.
    df_count: dict[str, int] = {t: 0 for t in q_terms}
    for df in doc_fields:
        present: set[str] = set()
        for toks in df.values():
            present.update(toks)
        for t in q_terms:
            if t in present:
                df_count[t] += 1

    # BM25 idf, floored at 0 so a term present in every doc can't drive a score
    # negative (it just contributes ~nothing, which is the intent).
    idf = {
        t: max(
            0.0,
            math.log(1.0 + (n - df_count[t] + 0.5) / (df_count[t] + 0.5)),
        )
        for t in q_terms
    }

    scores: list[float] = []
    for i, df in enumerate(doc_fields):
        norm = (1.0 - b + b * (dl[i] / avgdl)) if avgdl > 0 else 1.0
        s = 0.0
        for t in q_terms:
            wtf = sum(w[f] * toks.count(t) for f, toks in df.items())
            if wtf <= 0:
                continue
            s += idf[t] * (wtf * (k1 + 1.0)) / (wtf + k1 * norm)
        scores.append(s)
    return scores
