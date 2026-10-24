"""Vector store interface with a Pinecone implementation. One namespace per session; every call
takes the namespace explicitly, and there is no method that searches across namespaces."""

import math
import os
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import ConfigError, RagSettings, rag_settings

_retry = retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=1, max=10), reraise=True)


@dataclass
class Match:
    id: str
    score: float
    metadata: Dict


class VectorStore:
    def upsert(self, namespace: str, records: List[Dict]) -> None:
        """records: {"id", "values", "metadata"}"""
        raise NotImplementedError

    def query(self, namespace: str, vector: List[float], top_k: int, filter: Optional[Dict] = None) -> List[Match]:
        raise NotImplementedError

    def fetch(self, namespace: str, ids: List[str]) -> Dict[str, Dict]:
        """Returns id -> metadata for the ids that exist."""
        raise NotImplementedError

    def list_ids(self, namespace: str, prefix: str) -> List[str]:
        raise NotImplementedError

    def delete_ids(self, namespace: str, ids: List[str]) -> None:
        raise NotImplementedError

    def delete_namespace(self, namespace: str) -> None:
        raise NotImplementedError


def _require_namespace(namespace: str) -> None:
    if not namespace:
        raise ValueError("a namespace (session id) is required")


class PineconeVectorStore(VectorStore):
    """Serverless Pinecone index, created on first use (dimension from settings, metric cosine)."""

    def __init__(self, settings: RagSettings = rag_settings, api_key: Optional[str] = None):
        from pinecone import Pinecone, ServerlessSpec

        api_key = api_key or os.getenv("PINECONE_API_KEY")
        if not api_key:
            raise ConfigError("missing required setting in .env: PINECONE_API_KEY")
        self.pc = Pinecone(api_key=api_key)
        name = settings.pinecone_index
        if not self.pc.has_index(name):
            print(f"Creating Pinecone index '{name}' ({settings.embedding_dimension} dims, cosine)...")
            self.pc.create_index(
                name=name,
                dimension=settings.embedding_dimension,
                metric="cosine",
                spec=ServerlessSpec(cloud=settings.pinecone_cloud, region=settings.pinecone_region),
            )
        self.index = self.pc.Index(name=name)
        dimension = self.index.describe_index_stats().dimension
        if dimension and dimension != settings.embedding_dimension:
            raise ConfigError(
                f"Pinecone index '{name}' has dimension {dimension}, expected {settings.embedding_dimension}"
            )

    @_retry
    def _upsert_batch(self, namespace, batch):
        self.index.upsert(vectors=batch, namespace=namespace)

    def upsert(self, namespace, records):
        _require_namespace(namespace)
        for i in range(0, len(records), 100):
            self._upsert_batch(namespace, records[i : i + 100])

    @_retry
    def query(self, namespace, vector, top_k, filter=None):
        _require_namespace(namespace)
        kwargs = {"vector": vector, "top_k": top_k, "namespace": namespace, "include_metadata": True}
        if filter:
            kwargs["filter"] = filter
        response = self.index.query(**kwargs)
        return [Match(m.id, float(m.score), dict(m.metadata or {})) for m in response.matches]

    @_retry
    def _fetch_batch(self, namespace, ids):
        return self.index.fetch(ids=ids, namespace=namespace).vectors

    def fetch(self, namespace, ids):
        _require_namespace(namespace)
        found = {}
        for i in range(0, len(ids), 100):
            for vid, vector in self._fetch_batch(namespace, ids[i : i + 100]).items():
                found[vid] = dict(vector.metadata or {})
        return found

    @_retry
    def list_ids(self, namespace, prefix):
        _require_namespace(namespace)
        ids = []
        try:
            for page in self.index.list(prefix=prefix, namespace=namespace):
                ids.extend(item.id for item in page if item.id)
        except Exception as e:
            if _is_not_found(e):
                return []
            raise
        return ids

    def delete_ids(self, namespace, ids):
        _require_namespace(namespace)
        for i in range(0, len(ids), 1000):
            self._delete_batch(namespace, ids[i : i + 1000])

    @_retry
    def _delete_batch(self, namespace, ids):
        self.index.delete(ids=ids, namespace=namespace)

    def delete_namespace(self, namespace):
        _require_namespace(namespace)
        try:
            self.index.delete(delete_all=True, namespace=namespace)
        except Exception as e:
            if not _is_not_found(e):
                raise


def _is_not_found(error: Exception) -> bool:
    return "404" in str(error) or "not found" in str(error).lower()


class InMemoryVectorStore(VectorStore):
    """Same interface, kept in memory. For tests and offline experiments only (not for production)."""

    def __init__(self):
        self.namespaces: Dict[str, Dict[str, Dict]] = {}
        self._lock = threading.Lock()

    def upsert(self, namespace, records):
        _require_namespace(namespace)
        with self._lock:
            space = self.namespaces.setdefault(namespace, {})
            for record in records:
                space[record["id"]] = {"values": list(record["values"]), "metadata": dict(record["metadata"])}

    def query(self, namespace, vector, top_k, filter=None):
        _require_namespace(namespace)
        with self._lock:
            space = dict(self.namespaces.get(namespace, {}))
        matches = []
        for vid, record in space.items():
            if filter and any(record["metadata"].get(k) != (v.get("$eq") if isinstance(v, dict) else v) for k, v in filter.items()):
                continue
            matches.append(Match(vid, _cosine(vector, record["values"]), record["metadata"]))
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:top_k]

    def fetch(self, namespace, ids):
        space = self.namespaces.get(namespace, {})
        return {vid: dict(space[vid]["metadata"]) for vid in ids if vid in space}

    def list_ids(self, namespace, prefix):
        return [vid for vid in self.namespaces.get(namespace, {}) if vid.startswith(prefix)]

    def delete_ids(self, namespace, ids):
        with self._lock:
            space = self.namespaces.get(namespace, {})
            for vid in ids:
                space.pop(vid, None)

    def delete_namespace(self, namespace):
        with self._lock:
            self.namespaces.pop(namespace, None)


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


_default: Optional[VectorStore] = None
_default_lock = threading.Lock()


def default_vector_store() -> VectorStore:
    global _default
    with _default_lock:
        if _default is None:
            _default = PineconeVectorStore()
        return _default
