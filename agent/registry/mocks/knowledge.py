"""Mock adapters for the knowledge kinds: faq, knowledge_base, site_search.

All three are fixture-backed and use the same deterministic lexical scorer
(token overlap, no randomness), so evals are stable and rankings explainable.
Live adapters (Phase 6) swap in: FAQ store, Vertex AI RAG Engine, and
Vertex AI Search — same capability contracts.

Grounding rule support: retrieve() returns an empty chunk list below the score
floor rather than weak matches, so the agent can (and must) say "I don't know"
instead of fabricating a cited answer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ServiceConfig

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "is", "are", "do", "does", "how", "what", "where",
         "can", "i", "my", "to", "of", "in", "on", "for", "you", "your", "it"}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def score(query: str, text: str) -> float:
    q, t = _tokens(query), _tokens(text)
    if not q or not t:
        return 0.0
    # Prefix-stem match: 'verification' ~ 'verify', 'signatures' ~ 'signature'.
    def hit(qw: str) -> bool:
        return any(qw == tw or (min(len(qw), len(tw)) >= 5
                                and (qw.startswith(tw[:5]) and tw.startswith(qw[:5])))
                   for tw in t)
    return round(sum(1 for qw in q if hit(qw)) / len(q), 4)


@dataclass
class _FixtureAdapter:
    service: ServiceConfig
    base_dir: Path
    _data: dict | None = field(default=None, repr=False)

    def _fixtures(self) -> dict:
        if self._data is None:
            path = self.base_dir / (self.service.mock.fixtures or "")
            self._data = json.loads(path.read_text()) if path.is_file() else {}
        return self._data


class MockFaqAdapter(_FixtureAdapter):
    """kind: faq — curated Q&A pairs. lookup() returns scored candidates."""

    MIN_SCORE = 0.34

    def lookup(self, query: str, max_results: int = 3) -> dict:
        entries = self._fixtures().get("faqs", [])
        scored = []
        for e in entries:
            s = max(score(query, e["question"]), score(query, " ".join(e.get("tags", []))))
            if s >= self.MIN_SCORE:
                scored.append({**e, "score": s})
        scored.sort(key=lambda e: (-e["score"], e["question"]))
        return {"matches": scored[:max_results]}


class MockKbAdapter(_FixtureAdapter):
    """kind: knowledge_base — RAG over doc chunks. Chunks carry title+url so
    every answer built from them is citable."""

    MIN_SCORE = 0.5

    def retrieve(self, query: str, top_k: int = 3) -> dict:
        chunks = self._fixtures().get("chunks", [])
        scored = [{**c, "score": score(query, c["text"] + " " + c["title"])}
                  for c in chunks]
        scored = [c for c in scored if c["score"] >= self.MIN_SCORE]
        scored.sort(key=lambda c: (-c["score"], c["title"]))
        return {"chunks": scored[:top_k]}


class MockSearchAdapter(_FixtureAdapter):
    """kind: site_search — website datastore. search() returns page hits."""

    def search(self, query: str, max_results: int = 5) -> dict:
        pages = self._fixtures().get("pages", [])
        scored = [{**p, "score": score(query, p["title"] + " " + p.get("snippet", ""))}
                  for p in pages]
        scored = [p for p in scored if p["score"] > 0]
        scored.sort(key=lambda p: (-p["score"], p["title"]))
        return {"results": scored[:max_results]}
