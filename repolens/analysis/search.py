"""Keyword retrieval of the code most relevant to a feature question.

Deliberately simple and deterministic: it decides which line ranges are sent
to the model, so its behaviour must be easy to inspect and test.
"""

from __future__ import annotations

import math
import re

from repolens.analysis.dependencies import module_containers, module_name
from repolens.analysis.languages import file_role, is_lockfile
from repolens.analysis.models import Snippet
from repolens.ingestion.models import RepoSnapshot

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "how", "does",
    "do", "is", "are", "what", "where", "when", "which", "who", "why", "this", "that",
    "it", "its", "be", "by", "from", "as", "at", "work", "works", "working", "code",
    "file", "files", "function", "feature", "explain", "me", "show", "i", "can", "we",
    "get", "use", "used", "using", "about", "into", "there", "their", "they", "you",
    "become", "becomes", "happen", "happens", "turn", "turns", "make", "made", "end", "up",
}

# Feature questions are about behaviour, so code outranks prose and data files.
ROLE_WEIGHT = {"source": 1.0, "test": 0.8, "prompt": 0.7, "config": 0.6, "docs": 0.6, "data": 0.4,
               "other": 0.5}

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def query_terms(query: str) -> list[str]:
    """Lower-cased terms from a question; ``getUserName`` becomes get/user/name."""
    terms: list[str] = []
    for word in _WORD.findall(query):
        pieces = [word] + _CAMEL.split(word)
        for p in pieces:
            t = p.lower()
            if len(t) >= 3 and t not in STOPWORDS and t not in terms:
                terms.append(t)
    return terms


def _stem_variants(term: str) -> tuple[str, ...]:
    variants = {term}
    for suffix in ("ing", "ion", "ed", "es", "s"):
        if term.endswith(suffix) and len(term) - len(suffix) >= 4:
            variants.add(term[: -len(suffix)])
    return tuple(variants)


def search(
    snapshot: RepoSnapshot,
    query: str,
    max_snippets: int = 8,
    window: int = 60,
) -> list[Snippet]:
    terms = query_terms(query)
    if not terms:
        return []
    variants = {t: _stem_variants(t) for t in terms}

    # Rarer terms are more informative (IDF over files).
    doc_freq = {t: 0 for t in terms}
    lowered = {p: f.text.lower() for p, f in snapshot.files.items() if not is_lockfile(p)}
    for text in lowered.values():
        for t in terms:
            if any(v in text for v in variants[t]):
                doc_freq[t] += 1
    n_docs = max(len(lowered), 1)
    idf = {t: math.log(1 + n_docs / (1 + df)) for t, df in doc_freq.items()}

    snippets: list[Snippet] = []
    for path, text in lowered.items():
        path_l = path.lower()
        path_score = sum(3 * idf[t] for t in terms if any(v in path_l for v in variants[t]))
        lines = text.splitlines()
        line_hits = [
            {t for t in terms if any(v in line for v in variants[t])} for line in lines
        ]
        if not any(line_hits) and not path_score:
            continue
        # Slide a window and keep the best-scoring range in this file.
        best = (0.0, 1, min(len(lines), window), set())
        step = max(window // 3, 1)
        for start in range(0, max(len(lines) - window, 0) + 1, step):
            seg = line_hits[start : start + window]
            matched = set().union(*seg) if seg else set()
            score = sum(idf[t] for t in matched) * 2 + sum(len(s) for s in seg) * 0.1
            if score > best[0]:
                best = (score, start + 1, min(start + window, len(lines)), matched)
        total = (best[0] + path_score) * ROLE_WEIGHT.get(file_role(path), 0.5)
        if total <= 0:
            continue
        snippets.append(Snippet(path, best[1], max(best[2], best[1]), round(total, 3), sorted(best[3])))

    snippets.sort(key=lambda s: (-s.score, s.path))
    picked = _diversify(snippets, max_snippets, module_containers(sorted(snapshot.files)))
    return _cover_name_collisions(picked, snippets, terms, variants, sorted(snapshot.files), max_snippets)


def _cover_name_collisions(picked, ranked, terms, variants, paths, limit):
    """If the question names e.g. "router" and two different routers exist, make sure the
    best hit from each one is present, replacing the weakest other hits if needed."""
    from repolens.analysis.structure import name_collisions

    out = list(picked)
    for name, places in name_collisions(paths):
        if not any(any(v in name for v in variants[t]) for t in terms):
            continue
        for place in places:
            if any(s.path == place or s.path.startswith(place) for s in out):
                continue
            best = next((s for s in ranked if s.path == place or s.path.startswith(place)), None)
            if best is None:
                continue
            if len(out) >= limit:
                protected = {s.path for s in out if any(s.path == p or s.path.startswith(p) for p in places)}
                weakest = next((s for s in reversed(out) if s.path not in protected), None)
                if weakest is None:
                    continue
                out.remove(weakest)
            out.append(best)
    return sorted(out, key=lambda s: (-s.score, s.path))


def _diversify(snippets: list[Snippet], limit: int, containers, per_module: int = 2) -> list[Snippet]:
    """Cap hits per module in the first pass so one busy package cannot crowd out
    a similarly named one elsewhere (e.g. ``router/`` vs ``control_plane/router/``)."""
    picked: list[Snippet] = []
    counts: dict[str, int] = {}
    for s in snippets:
        mod = module_name(s.path, containers)
        if counts.get(mod, 0) < per_module:
            picked.append(s)
            counts[mod] = counts.get(mod, 0) + 1
        if len(picked) >= limit:
            return picked
    for s in snippets:
        if len(picked) >= limit:
            break
        if s not in picked:
            picked.append(s)
    return sorted(picked, key=lambda s: (-s.score, s.path))
