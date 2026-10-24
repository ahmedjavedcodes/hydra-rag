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
