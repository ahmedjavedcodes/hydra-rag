import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional

from src.schema import Element
from utilities.session import get_session_dir

# Document states
RECEIVED, PROCESSING, READY, FAILED = "received", "processing", "ready", "failed"

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _session_lock(session_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(session_id, threading.Lock())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


class SessionStore:
    """Per-session manifest (`manifest.json`: documents and their states) and extracted elements
    (`elements/<doc_id>.jsonl`). Thread-safe within one process."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.root = get_session_dir(session_id)  # validates the id
        self.manifest_path = os.path.join(self.root, "manifest.json")
        self.elements_dir = os.path.join(self.root, "elements")

    def _load(self) -> dict:
        if not os.path.exists(self.manifest_path):
            return {"documents": {}}
        with open(self.manifest_path, encoding="utf-8") as f:
            return json.load(f)

    @contextmanager
    def _edit(self) -> Iterator[dict]:
        with _session_lock(self.session_id):
            manifest = self._load()
            yield manifest
            _atomic_write(self.manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False))

    def documents(self) -> Dict[str, dict]:
        with _session_lock(self.session_id):
            return self._load()["documents"]

    def get(self, doc_id: str) -> Optional[dict]:
        return self.documents().get(doc_id)

    def reserve(self, doc_id: str, name_key: str, record: dict) -> Optional[dict]:
        """Registers a new document as `received`, atomically.

        Returns the existing record instead when the same content is already ready or in progress
        (a duplicate). A previously failed document is reset and processed again.
        """
        with self._edit() as manifest:
            docs = manifest["documents"]
            existing = docs.get(doc_id)
            if existing and existing["state"] != FAILED:
                return dict(existing)
            if existing:
                version_id = existing["version_id"]
            else:
                same_name = sum(1 for d in docs.values() if d.get("name_key") == name_key)
                version_id = f"v{same_name + 1}"
            docs[doc_id] = {
                **record,
                "doc_id": doc_id,
                "name_key": name_key,
                "version_id": version_id,
                "state": RECEIVED,
                "reason": None,
                "element_count": 0,
                "warnings": [],
                "created_at": _now(),
                "updated_at": _now(),
            }
        return None

    def update(self, doc_id: str, **fields) -> dict:
        with self._edit() as manifest:
            record = manifest["documents"][doc_id]
            record.update(fields, updated_at=_now())
            return dict(record)

    def write_elements(self, doc_id: str, elements: List[Element]) -> str:
        os.makedirs(self.elements_dir, exist_ok=True)
        path = os.path.join(self.elements_dir, f"{doc_id}.jsonl")
        _atomic_write(path, "".join(json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in elements))
        return path

    def read_elements(self, doc_id: str) -> List[dict]:
        path = os.path.join(self.elements_dir, f"{doc_id}.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
