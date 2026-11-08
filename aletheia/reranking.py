"""Local cross-encoder reranking for Aletheia recall."""
from __future__ import annotations

from typing import Sequence


class CrossEncoderReranker:
    """Rerank a candidate pool; loading and inference failures are reported to the caller."""

    _instance: CrossEncoderReranker | None = None

    def __new__(cls, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.model_name = model_name
            cls._instance._model = None
        return cls._instance

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(self.model_name, local_files_only=True)

    def rerank(self, query: str, items: Sequence, top_k: int = 10) -> list:
        """Score factual records first, followed by summaries in their original order."""
        if not items:
            return []
        self._load()
        factual = [it for it in items if not getattr(it, "is_summary", False)]
        summaries = [it for it in items if getattr(it, "is_summary", False)]
        if not factual:
            return list(items)[:top_k]
        pairs = [(query, item.text) for item in factual]
        scores = self._model.predict(pairs, show_progress_bar=False)
        scored = sorted(zip(scores, factual), key=lambda x: (-x[0], x[1].record_id))
        reranked = [item for _, item in scored[:top_k]]
        return (reranked + summaries[:max(0, top_k - len(reranked))])[:top_k]
