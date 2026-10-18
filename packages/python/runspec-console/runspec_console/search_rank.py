"""
search_rank.py — BM25F lexical ranking for search-mode runnable discovery.

Search mode (``[llm] tool_mode = "search"``) hides the fleet behind
``search_runnables``; the quality of what the agent finds is only as good as the
ranking. Each runnable becomes a document of weighted fields (``runnable_doc``):
its name, package, description and arg names, and — so a task-shaped query like
"add a cron job" or "retry a failed pipeline" reaches a many-verb runnable — its
subcommand names and descriptions, arg descriptions and choice options, the
``runspec.toml`` example descriptions, and any team-wiki facts about it.

Scoring is BM25F: each field's term frequency is length-normalised against that
field's own corpus average before the fields are weighted and summed, then
saturated once per query term and weighted by rarity (idf). Normalising per
field matters here: a fifty-verb runnable carries pages of command text, and a
single whole-document length would let that bulk drown its name match.

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
# runnable (its name) or the package it lives in (group); a subcommand name is
# the verb they ask for ("add", "retry-pipeline"); arg names are a decent signal;
# the description is prose that mentions many incidental terms, so it's the
# baseline. The rest is longer text that confirms a match more than it makes one
# — subcommand descriptions, arg descriptions and options, example descriptions,
# wiki facts — so it weighs less than the runnable's own description.
DEFAULT_FIELD_WEIGHTS: dict[str, float] = {
    "name": 3.0,
    "group": 2.0,
    "commands": 2.0,
    "args": 1.5,
    "description": 1.0,
    "command_text": 0.6,
    "examples": 0.6,
    "wiki": 0.5,
    "arg_text": 0.4,
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


def _arg_text(args: Any, skip_inherited: bool = False) -> list[str]:
    """Description and choice options of each arg (``password`` included — its
    description says what it is for, and no value is ever indexed)."""
    out: list[str] = []
    for a in args or []:
        if not isinstance(a, dict) or (skip_inherited and a.get("inherited")):
            continue
        if a.get("description"):
            out.append(str(a["description"]))
        out.extend(str(o) for o in a.get("options") or [])
    return out


def runnable_doc(
    rep: dict[str, Any],
    *,
    groups: list[str] | None = None,
    wiki: list[str] | None = None,
) -> dict[str, Any]:
    """The search document for one runnable: field → text (or list of texts).

    ``rep`` is a discovery record; ``groups`` every venv/package it ships in (a
    runnable collapsed across several is still found by any one of them);
    ``wiki`` the team-wiki fact texts about it. Subcommands are walked to any
    depth; a command's inherited globals are skipped since the runnable's own
    ``args`` already carry them."""
    commands: list[str] = []
    command_text: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        for cname, child in (node.get("commands") or {}).items():
            if not isinstance(child, dict):
                continue
            commands.append(str(cname))
            if child.get("description"):
                command_text.append(str(child["description"]))
            for a in child.get("args") or []:
                if isinstance(a, dict) and not a.get("inherited") and a.get("name"):
                    command_text.append(str(a["name"]))
            command_text.extend(_arg_text(child.get("args"), skip_inherited=True))
            walk(child)

    walk(rep)
    raw = rep.get("rawSpec") if isinstance(rep.get("rawSpec"), dict) else {}
    examples = [
        str(e.get("description") or "")
        for e in (raw or {}).get("examples") or []
        if isinstance(e, dict)
    ]
    return {
        "name": str(rep.get("name") or ""),
        "group": " ".join(
            groups if groups is not None else [str(rep.get("group") or "")]
        ),
        "description": str(rep.get("description") or ""),
        "args": [
            str(a.get("name") or "")
            for a in rep.get("args") or []
            if isinstance(a, dict)
        ],
        "arg_text": _arg_text(rep.get("args")),
        "commands": commands,
        "command_text": command_text,
        "examples": examples,
        "wiki": list(wiki or []),
    }


def bm25f_scores(
    query: str,
    docs: list[dict[str, Any]],
    *,
    k1: float = 1.2,
    b: float = 0.75,
    weights: dict[str, float] | None = None,
) -> list[float]:
    """BM25F relevance score for each doc, aligned to ``docs`` order.

    Each doc is a mapping of field → text (or a list of texts); fields missing
    from a doc are empty. Per query term, each field's frequency is normalised by
    that field's length against its corpus average, the fields are weighted and
    summed, and the sum is saturated once and scaled by idf. An empty query (or
    empty corpus) returns all zeros — the caller treats that as "browse" and
    ranks everything equally.

    ``k1`` controls term-frequency saturation; ``b`` the length normalisation —
    the standard BM25 defaults. Pure/deterministic."""
    w = weights or DEFAULT_FIELD_WEIGHTS
    # Unique query terms, order-stable (repeats in the query don't inflate a match).
    q_terms = list(dict.fromkeys(tokenize(query)))
    n = len(docs)
    if not q_terms or n == 0:
        return [0.0] * n

    doc_fields = [{f: _field_tokens(d, f) for f in w} for d in docs]
    # Mean length of each field over the corpus (docs where it is empty count:
    # a field most runnables lack has a small average, so the few long ones are
    # normalised down hard, which is the point).
    avg = {f: sum(len(df[f]) for df in doc_fields) / n for f in w}

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
    for df in doc_fields:
        norm = {
            f: (1.0 - b + b * (len(toks) / avg[f])) if avg[f] > 0 else 1.0
            for f, toks in df.items()
        }
        s = 0.0
        for t in q_terms:
            wtf = sum(w[f] * toks.count(t) / norm[f] for f, toks in df.items() if toks)
            if wtf <= 0:
                continue
            s += idf[t] * (wtf * (k1 + 1.0)) / (wtf + k1)
        scores.append(s)
    return scores
