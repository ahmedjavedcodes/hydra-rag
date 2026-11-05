import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value else default
    except ValueError:
        return default


@dataclass(frozen=True)
class IngestionSettings:
    """Ingestion limits. Every value can be overridden through the environment (.env)."""

    max_file_mb: int = _env_int("MAX_FILE_MB", 20)
    max_files_per_upload: int = _env_int("MAX_FILES_PER_UPLOAD", 10)
    max_images_per_doc: int = _env_int("MAX_IMAGES_PER_DOC", 30)
    url_timeout_s: int = _env_int("URL_TIMEOUT_S", 30)
    max_linked_files: int = _env_int("MAX_LINKED_FILES", 10)
    rows_per_element: int = _env_int("ROWS_PER_ELEMENT", 50)
    max_rows_per_sheet: int = _env_int("MAX_ROWS_PER_SHEET", 5000)
    max_text_element_chars: int = _env_int("MAX_TEXT_ELEMENT_CHARS", 2000)
    min_image_px: int = _env_int("MIN_IMAGE_PX", 64)
    vision_model: str = os.getenv("VISION_MODEL", "qwen/qwen3.8-27b")
    vision_max_concurrency: int = _env_int("VISION_MAX_CONCURRENCY", 3)
    vision_max_attempts: int = _env_int("VISION_MAX_ATTEMPTS", 5)

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024


settings = IngestionSettings()


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    try:
        return float(value) if value else default
    except ValueError:
        return default


class ConfigError(Exception):
    """A required setting is missing or invalid. The message names the setting."""


@dataclass(frozen=True)
class RagSettings:
    """Chunking, embedding, Pinecone, retrieval and answer settings (all overridable from .env)."""

    pinecone_index: str = os.getenv("PINECONE_INDEX", "hydrarag")
    pinecone_cloud: str = os.getenv("PINECONE_CLOUD", "aws")
    pinecone_region: str = os.getenv("PINECONE_REGION", "us-east-1")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5")
    embedding_dimension: int = _env_int("EMBEDDING_DIMENSION", 1024)
    reranker_model: str = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-large")
    answer_model: str = os.getenv("ANSWER_MODEL", "openai/gpt-oss-120b")
    answer_reasoning_effort: str = os.getenv("ANSWER_REASONING_EFFORT", "low")
    top_k: int = _env_int("TOP_K", 10)
    # Measured on the sample inputs with bge-large + query prefix: correct matches 0.53-0.65,
    # unrelated questions <= 0.45. 0.80 rejected every question. Calibrate with scripts/calibrate_threshold.py.
    min_score: float = _env_float("MIN_SCORE", 0.50)
    rerank_top_n: int = _env_int("RERANK_TOP_N", 3)
    chunk_size: int = _env_int("CHUNK_SIZE", 800)
    chunk_overlap: int = _env_int("CHUNK_OVERLAP", 100)
    # Tables are only split when larger than this (rows lose meaning when cut apart)
    table_chunk_size: int = _env_int("TABLE_CHUNK_SIZE", 2000)
    max_question_chars: int = _env_int("MAX_QUESTION_CHARS", 1000)
    # Chunk text stored in Pinecone metadata (the record limit is 40 KB)
    metadata_text_limit: int = _env_int("METADATA_TEXT_LIMIT", 8000)
    index_confirm_timeout_s: int = _env_int("INDEX_CONFIRM_TIMEOUT_S", 60)


rag_settings = RagSettings()

REQUIRED_KEYS = ("PINECONE_API_KEY", "GROQ_API_KEY")


def require_keys(names=REQUIRED_KEYS) -> None:
    """Fails fast at startup when a required key is missing. HF_TOKEN is optional: the default
    models are public, a token only raises Hugging Face download rate limits."""
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        raise ConfigError(f"missing required setting(s) in .env: {', '.join(missing)}")
