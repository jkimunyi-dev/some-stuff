"""Cross-encoder reranker (non-generative: relevance scores only, as the rules allow).

Model: cross-encoder/ms-marco-MiniLM-L6-v2 (~22M parameters). Loaded once, lazily, thread-safe. If it cannot be loaded
(no package / no weights), callers keep their BM25 order. Set QA_RERANK=0 to disable.
"""
from __future__ import annotations

import os
import threading

MODEL_NAME = os.environ.get("QA_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L6-v2")
_model = None
_failed = False
_lock = threading.Lock()


def _get():
    global _model, _failed
    if _failed or os.environ.get("QA_RERANK", "1") == "0":
        return None
    with _lock:
        if _model is None and not _failed:
            try:
                from sentence_transformers import CrossEncoder
                _model = CrossEncoder(MODEL_NAME, max_length=384)
            except Exception as e:  # missing package or weights: fall back silently to BM25 order
                print("reranker unavailable:", type(e).__name__, str(e)[:120], flush=True)
                _failed = True
    return _model


def rerank(query: str, texts: list[str], k: int) -> list[int] | None:
    """Indices of the k most relevant texts, best first; None when the reranker is unavailable."""
    m = _get()
    if m is None or not texts:
        return None
    with _lock:  # one forward pass at a time (small model; avoids GPU/CPU contention between worker threads)
        scores = m.predict([(query, t[:1500]) for t in texts], batch_size=32, show_progress_bar=False)
    order = sorted(range(len(texts)), key=lambda i: -float(scores[i]))
    return order[:k]
