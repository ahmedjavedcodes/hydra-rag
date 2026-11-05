import threading
from typing import List, Optional, Tuple

from src.config import RagSettings, rag_settings
from src.embeddings.vector_store import Match


class Reranker:
    def rerank(self, question: str, matches: List[Match], top_n: int) -> List[Tuple[Match, float]]:
        raise NotImplementedError


class CrossEncoderReranker(Reranker):
    """Cross-encoder (default `BAAI/bge-reranker-large`) scoring (question, passage) pairs."""

    def __init__(self, settings: RagSettings = rag_settings):
        from sentence_transformers import CrossEncoder

        from src.embeddings.embedder import load_cached_or_remote

        self.model = load_cached_or_remote(CrossEncoder, settings.reranker_model)

    def rerank(self, question, matches, top_n):
        if not matches:
            return []
        scores = self.model.predict([(question, m.metadata.get("text", "")) for m in matches], show_progress_bar=False)
        ranked = sorted(zip(matches, (float(s) for s in scores)), key=lambda pair: pair[1], reverse=True)
        return ranked[:top_n]


_default: Optional[Reranker] = None
_lock = threading.Lock()


def default_reranker() -> Reranker:
    global _default
    with _lock:
        if _default is None:
            _default = CrossEncoderReranker()
        return _default
