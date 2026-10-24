from typing import Iterable, List, Optional

from src.embeddings.embedder import Embedder
from src.embeddings.vector_store import Match, VectorStore


def retrieve(
    embedder: Embedder,
    vector_store: VectorStore,
    session_id: str,
    question: str,
    top_k: int,
    min_score: float,
    allowed_doc_ids: Optional[Iterable[str]] = None,
) -> List[Match]:
    """Dense search inside one session's namespace; drops matches under `min_score` and, when given,
    matches from documents that are not answerable (e.g. replaced versions not yet cleaned up)."""
    matches = vector_store.query(session_id, embedder.embed_query(question), top_k)
    allowed = set(allowed_doc_ids) if allowed_doc_ids is not None else None
    return [
        m
        for m in matches
        if m.score >= min_score and (allowed is None or m.metadata.get("doc_id") in allowed)
    ]
