import os
import re
import uuid

DATA_ROOT = "./data"

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def new_session_id() -> str:
    """Creates a random session id (32 hex chars). Also used as the Pinecone namespace."""
    return uuid.uuid4().hex


def validate_session_id(session_id: str) -> str:
    """Rejects anything that is not a generated session id.

    Session ids come from clients (no auth), so this also blocks path traversal.
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"Invalid session id: {session_id!r}")
    return session_id


def get_session_dir(session_id: str) -> str:
    """Returns ./data/<session_id>, creating it if needed."""
    path = os.path.normpath(os.path.join(DATA_ROOT, validate_session_id(session_id)))
    os.makedirs(path, exist_ok=True)
    return path
