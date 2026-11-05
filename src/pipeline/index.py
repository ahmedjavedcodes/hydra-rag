"""Indexing: a document's elements -> chunks -> embeddings -> Pinecone (namespace = session id).

    from src.pipeline.index import Indexer
    indexer = Indexer()
    indexer.index_batch(ingest_files(["report.pdf"]))

Index state per document (manifest field `index_state`): pending -> indexing -> indexed, or failed.
A document is only answerable once it is `indexed`, i.e. all its vectors were fetched back from Pinecone.
"""

import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.config import RagSettings, rag_settings
from src.embeddings.embedder import Embedder, default_embedder
from src.embeddings.vector_store import VectorStore, default_vector_store
from src.pipeline.store import READY, SessionStore
from src.processing.chunkers import Chunk, chunk_elements
from src.schema import BatchResult
from utilities.session import get_session_dir, validate_session_id

INDEX_PENDING, INDEXING, INDEXED, INDEX_FAILED, REPLACED = "pending", "indexing", "indexed", "failed", "replaced"


@dataclass
class IndexResult:
    doc_id: str
    index_state: str
    chunks: int = 0
    upserted: int = 0
    skipped: int = 0
    reason: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


class _EmbeddingCache:
    """Process-wide cache of document embeddings by content hash, so identical text is embedded once."""

    def __init__(self, max_items: int = 50_000):
        self._items: Dict[str, List[float]] = {}
        self._lock = threading.Lock()
        self.max_items = max_items

    def get(self, key):
        with self._lock:
            return self._items.get(key)

    def put(self, key, vector):
        with self._lock:
            if len(self._items) >= self.max_items:
                self._items.pop(next(iter(self._items)))
            self._items[key] = vector


_cache = _EmbeddingCache()


def _truncate(text: str, limit: int) -> str:
    """Keeps metadata text well under Pinecone's 40 KB record limit (UTF-8 bytes, not characters)."""
    text = text[:limit]
    while len(text.encode("utf-8")) > limit:
        text = text[: int(len(text) * 0.9)]
    return text


class Indexer:
    def __init__(
        self,
        embedder: Optional[Embedder] = None,
        vector_store: Optional[VectorStore] = None,
        settings: RagSettings = rag_settings,
        confirm_poll_s: float = 1.0,
    ):
        self.embedder = embedder or default_embedder()
        self.vector_store = vector_store or default_vector_store()
        self.settings = settings
        self.confirm_poll_s = confirm_poll_s

    # ------------------------------------------------------------ indexing

    def index_document(self, session_id: str, doc_id: str) -> IndexResult:
        store = SessionStore(session_id)
        record = store.get(doc_id)
        if record is None or record["state"] != READY:
            return IndexResult(doc_id, INDEX_FAILED, reason="document is not ready for indexing")

        chunks = chunk_elements(store.read_elements(doc_id), self.settings)
        result = IndexResult(doc_id, INDEXING, chunks=len(chunks))
        store.update(doc_id, index_state=INDEXING, index_reason=None)
        try:
            # Same id and same content hash already in Pinecone -> nothing to do for that chunk
            existing = self.vector_store.fetch(session_id, [c.id for c in chunks])
            todo = [c for c in chunks if existing.get(c.id, {}).get("content_hash") != c.content_hash]
            result.skipped = len(chunks) - len(todo)
            if todo:
                self.vector_store.upsert(session_id, self._records(todo))
            result.upserted = len(todo)
            # Chunks left over from an earlier, longer chunking of the same document
            current = {c.id for c in chunks}
            stale = [vid for vid in self.vector_store.list_ids(session_id, f"{doc_id}:") if vid not in current]
            if stale:
                self.vector_store.delete_ids(session_id, stale)
        except Exception as e:
            print(f"   ✗ Indexing failed for {record.get('filename')}: {e!r}")
            self._delete_doc_vectors(session_id, doc_id)
            result.index_state, result.reason = INDEX_FAILED, "indexing failed, please try again"
            store.update(doc_id, index_state=INDEX_FAILED, index_reason=result.reason, chunk_count=0)
            return result

        if self._confirm(session_id, [c.id for c in chunks]):
            result.index_state = INDEXED
            store.update(doc_id, index_state=INDEXED, chunk_count=len(chunks))
            self._replace_older_versions(store, record)
        else:
            result.warnings.append("vectors not yet queryable; call refresh_status later")
            store.update(doc_id, chunk_count=len(chunks))
        return result

    def index_session(self, session_id: str) -> List[IndexResult]:
        """Indexes every ready document that is not indexed yet."""
        store = SessionStore(session_id)
        return [
            self.index_document(session_id, doc_id)
            for doc_id, doc in store.documents().items()
            if doc["state"] == READY and doc.get("index_state") not in (INDEXED, REPLACED)
        ]

    def index_batch(self, batch: BatchResult) -> List[IndexResult]:
        """Indexes the documents an ingestion batch produced (duplicates are skipped when already indexed)."""
        store = SessionStore(batch.session_id)
        results = []
        # Smallest documents first, so a big spreadsheet doesn't keep small files waiting
        for file in sorted(batch.files, key=lambda f: f.element_count):
            if file.status != "ok" or not file.doc_id:
                continue
            doc = store.get(file.doc_id) or {}
            if doc.get("index_state") in (INDEXED, REPLACED):
                results.append(IndexResult(file.doc_id, doc["index_state"], chunks=doc.get("chunk_count", 0)))
                continue
            results.append(self.index_document(batch.session_id, file.doc_id))
        return results

    def refresh_status(self, session_id: str, doc_id: str) -> str:
        """Re-checks a document that stayed `indexing` because Pinecone had not caught up."""
        store = SessionStore(session_id)
        doc = store.get(doc_id)
        if not doc or doc.get("index_state") != INDEXING:
            return (doc or {}).get("index_state", "unknown")
        ids = self.vector_store.list_ids(session_id, f"{doc_id}:")
        if ids and len(ids) >= doc.get("chunk_count", 0):
            store.update(doc_id, index_state=INDEXED)
            self._replace_older_versions(store, doc)
            return INDEXED
        return INDEXING

    def _records(self, chunks: List[Chunk]) -> List[Dict]:
        vectors: Dict[str, List[float]] = {}
        missing = []
        for chunk in chunks:
            cached = _cache.get(chunk.content_hash)
            if cached is not None:
                vectors[chunk.content_hash] = cached
            else:
                missing.append(chunk)
        unique = {c.content_hash: c.embed_text for c in missing}
        for key, vector in zip(unique, self.embedder.embed_documents(list(unique.values()))):
            vectors[key] = vector
            _cache.put(key, vector)
        return [
            {
                "id": c.id,
                "values": vectors[c.content_hash],
                "metadata": {**c.metadata, "text": _truncate(c.text, self.settings.metadata_text_limit)},
            }
            for c in chunks
        ]

    def _confirm(self, session_id: str, ids: List[str]) -> bool:
        """Waits until every vector can be fetched back (Pinecone writes are eventually consistent)."""
        if not ids:
            return True
        deadline = time.monotonic() + self.settings.index_confirm_timeout_s
        while True:
            if len(self.vector_store.fetch(session_id, ids)) == len(ids):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.confirm_poll_s)

    def _replace_older_versions(self, store: SessionStore, record: Dict) -> None:
        """Once a new version is queryable, the vectors of older versions of the same file/URL go."""
        for doc_id, doc in store.documents().items():
            if (
                doc_id != record["doc_id"]
                and doc.get("name_key") == record.get("name_key")
                and doc.get("index_state") in (INDEXED, INDEXING, INDEX_FAILED)
            ):
                self._delete_doc_vectors(store.session_id, doc_id)
                store.update(doc_id, index_state=REPLACED)

    # ------------------------------------------------------------ deletion

    def _delete_doc_vectors(self, session_id: str, doc_id: str) -> None:
        ids = self.vector_store.list_ids(session_id, f"{doc_id}:")
        if ids:
            self.vector_store.delete_ids(session_id, ids)

    def delete_document(self, session_id: str, doc_id: str) -> bool:
        """Removes a document's vectors, elements, file and record. False when there was nothing to delete."""
        store = SessionStore(session_id)
        self._delete_doc_vectors(session_id, doc_id)
        return store.remove(doc_id) is not None

    def delete_session(self, session_id: str) -> None:
        """Removes the session's namespace and its whole folder (idempotent)."""
        validate_session_id(session_id)
        self.vector_store.delete_namespace(session_id)
        folder = get_session_dir(session_id)
        shutil.rmtree(folder, ignore_errors=True)
        if os.path.exists(folder):
            raise OSError(f"could not delete session folder {folder}")
