import threading
from typing import List, Optional

from src.config import RagSettings, rag_settings

# bge-*-en-v1.5 query instruction: added to queries only, never to documents
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def load_cached_or_remote(loader, model_name: str):
    """Loads a Hugging Face model; when the Hub is unreachable, falls back to the local cache."""
    try:
        return loader(model_name)
    except Exception as e:
        print(f"   ⚠ Could not reach Hugging Face for {model_name} ({type(e).__name__}); using the local cache")
        return loader(model_name, local_files_only=True)


class Embedder:
    """Interface: normalised vectors for documents and queries."""

    dimension: int

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> List[float]:
        raise NotImplementedError


class SentenceTransformerEmbedder(Embedder):
    """`BAAI/bge-large-en-v1.5` (1024 dims) through sentence-transformers, loaded once per process."""

    def __init__(self, settings: RagSettings = rag_settings, batch_size: int = 32):
        from sentence_transformers import SentenceTransformer

        self.model_name = settings.embedding_model
        self.model = load_cached_or_remote(SentenceTransformer, self.model_name)
        get_dimension = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        self.dimension = get_dimension()
        self.batch_size = batch_size
        self.query_prefix = BGE_QUERY_PREFIX if "bge" in self.model_name.lower() else ""

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        vectors = self.model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> List[float]:
        return self.model.encode(self.query_prefix + text, normalize_embeddings=True).tolist()


_default: Optional[Embedder] = None
_lock = threading.Lock()


def default_embedder() -> Embedder:
    global _default
    with _lock:
        if _default is None:
            _default = SentenceTransformerEmbedder()
        return _default
