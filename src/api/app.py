"""REST API over the RAG pipeline (the channel-agnostic part of plan 03), used by the web UI.

    uv run uvicorn src.api.app:app --port 8000

Endpoints take the session id in the path; there are no accounts, so the unguessable id is the
only access control. Sync endpoints run in FastAPI's threadpool, which also keeps Playwright's
sync API out of the event loop.
"""

import os
import shutil
import tempfile
import threading
from typing import List

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.config import ConfigError, require_keys, settings as ingest_settings
from src.loaders import supported_extensions
from src.pipeline.index import Indexer
from src.pipeline.ingest import ingest_files, ingest_url
from src.pipeline.rag_chain import QuestionError, RagChain, ServiceError
from src.pipeline.store import SessionStore
from utilities.session import get_session_dir, new_session_id, validate_session_id

app = FastAPI(title="HydraRAG API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

_services = {}
_services_lock = threading.Lock()


def services():
    """Indexer and RagChain share the embedder and Pinecone client; created once, on first use."""
    with _services_lock:
        if not _services:
            require_keys()
            _services["indexer"] = Indexer()
            _services["chain"] = RagChain(embedder=_services["indexer"].embedder, vector_store=_services["indexer"].vector_store)
        return _services["indexer"], _services["chain"]


@app.on_event("startup")
def warm_up() -> None:
    """Loads models and connects to Pinecone in the background so the first question is faster."""

    def load():
        try:
            services()
            print("HydraRAG services ready")
        except Exception as e:
            print(f"   ✗ Could not start services: {e}")

    threading.Thread(target=load, daemon=True).start()


def _session(session_id: str) -> str:
    try:
        return validate_session_id(session_id)
    except ValueError:
        raise HTTPException(400, "invalid session id")


def _index_in_background(batch) -> None:
    try:
        indexer, _ = services()
        indexer.index_batch(batch)
    except Exception as e:
        print(f"   ✗ Background indexing failed: {e!r}")


class UrlRequest(BaseModel):
    url: str


class ChatRequest(BaseModel):
    question: str


@app.get("/health")
def health():
    try:
        require_keys()
    except ConfigError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "services_loaded": bool(_services)}


@app.get("/config")
def config():
    return {
        "supported_extensions": supported_extensions(),
        "max_file_mb": ingest_settings.max_file_mb,
        "max_files_per_upload": ingest_settings.max_files_per_upload,
    }


@app.post("/sessions")
def create_session():
    session_id = new_session_id()
    get_session_dir(session_id)
    return {"session_id": session_id}


@app.post("/sessions/{session_id}/files")
def upload_files(session_id: str, background: BackgroundTasks, files: List[UploadFile] = File(...)):
    session_id = _session(session_id)
    if len(files) > ingest_settings.max_files_per_upload:
        raise HTTPException(413, f"too many files (limit {ingest_settings.max_files_per_upload} per upload)")
    tmp_dir = tempfile.mkdtemp(prefix="hydrarag-upload-")
    try:
        paths = []
        for upload in files:
            name = os.path.basename(upload.filename or "upload") or "upload"
            path = os.path.join(tmp_dir, name)
            if os.path.exists(path):  # same name twice in one request
                stem, ext = os.path.splitext(name)
                path = os.path.join(tmp_dir, f"{stem}_{len(paths)}{ext}")
            with open(path, "wb") as f:
                shutil.copyfileobj(upload.file, f)
            paths.append(path)
        batch = ingest_files(paths, session_id)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    background.add_task(_index_in_background, batch)
    return batch.to_dict()


@app.post("/sessions/{session_id}/urls")
def add_url(session_id: str, body: UrlRequest, background: BackgroundTasks):
    session_id = _session(session_id)
    batch = ingest_url(body.url, session_id)
    background.add_task(_index_in_background, batch)
    return batch.to_dict()


@app.get("/sessions/{session_id}/documents")
def list_documents(session_id: str):
    session_id = _session(session_id)
    docs = SessionStore(session_id).documents().values()
    keys = ("doc_id", "filename", "source_url", "version_id", "state", "reason", "index_state", "element_count", "chunk_count", "warnings", "created_at")
    rows = [{k: d.get(k) for k in keys} for d in docs if d.get("index_state") != "replaced"]
    return {"documents": sorted(rows, key=lambda d: d["created_at"] or "")}


@app.delete("/sessions/{session_id}/documents/{doc_id}")
def delete_document(session_id: str, doc_id: str):
    session_id = _session(session_id)
    indexer, _ = services()
    return {"deleted": indexer.delete_document(session_id, doc_id)}


@app.post("/sessions/{session_id}/chat")
def chat(session_id: str, body: ChatRequest):
    session_id = _session(session_id)
    try:
        _, chain = services()
        return chain.ask(session_id, body.question).to_dict()
    except QuestionError as e:
        raise HTTPException(400, str(e))
    except ServiceError as e:
        raise HTTPException(503, str(e))
    except ConfigError as e:
        raise HTTPException(500, str(e))


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    session_id = _session(session_id)
    indexer, _ = services()
    indexer.delete_session(session_id)
    return {"deleted": True}
