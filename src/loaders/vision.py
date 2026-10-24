import base64
import hashlib
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional, Tuple

from PIL import Image
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings

IMAGE_PROMPT = """
Task: Analyze the image and surrounding document text as a Vision-Language Model for a RAG system.

Surrounding text:
{context}

Generate ONE self-contained, retrieval-friendly description that enables an LLM to answer questions about the image without seeing it.

Rules:

Start exactly with: "This image contains..."
Identify the visual type and purpose.
Extract key visible information: titles, captions, labels, legends, headers, values, units, dates, names, equations, and technical terms.
Transcribe any readable text in the image.
For charts/graphs, describe measurements, comparisons, exact or approximate values, highest/lowest items, ordering, trends, and important relationships.
For tables, describe purpose, headers, relevant rows, values, comparisons, and patterns.
Use surrounding text to explain context, meaning, and relevance.
Include important conclusions supported by the visual.
Preserve useful technical terminology and synonyms for retrieval.
Prioritize factual accuracy and semantic information over decorative details.
Mention colors only when they identify meaningful data or categories.
Never invent or guess information. Mark unclear values as approximate.
Avoid unnecessary repetition and verbose visual descriptions.

OUTPUT CONSTRAINT:

Output ONLY the final description.
The description MUST be between 250 and 350 tokens.
Aim for approximately 300 tokens.
""".strip()

PAGE_PROMPT = """
This is a scanned page from a document. Transcribe all readable text in reading order, keeping headings,
lists and table rows. Then, if the page has figures or charts, add one short paragraph describing each.
Never invent text that is not visible. Output only the transcription and descriptions.
""".strip()


class VisionError(Exception):
    """Raised when an image could not be described after all retries."""


def compress_bytes_to_b64(raw_bytes: bytes, max_dim: int = 768) -> str:
    """Downscales image to 768px max dimension to keep token usage low."""
    img = Image.open(io.BytesIO(raw_bytes))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=75)
    b64_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64_str}"


def _groq_invoke(prompt: str, image_url: str) -> str:
    """Calls the Groq vision model once (no retries)."""
    from langchain_core.messages import HumanMessage
    from langchain_groq import ChatGroq

    llm = ChatGroq(
        model=settings.vision_model,
        temperature=0.1,
        max_tokens=4096,
        reasoning_effort="none",
    )
    message = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
    )
    return llm.invoke([message]).content.strip()


class VisionDescriber:
    """Describes images with a vision LLM, with retries, a concurrency limit and a cache by image hash.

    `invoke(prompt, image_data_url) -> str` is the single model call; tests inject a fake one.
    """

    def __init__(
        self,
        invoke: Optional[Callable[[str, str], str]] = None,
        max_attempts: int = settings.vision_max_attempts,
        max_concurrency: int = settings.vision_max_concurrency,
        wait_min: float = 4,
        wait_max: float = 20,
    ):
        self._invoke = invoke or _groq_invoke
        self.max_concurrency = max(1, max_concurrency)
        self._cache: dict = {}
        self._lock = threading.Lock()
        self._invoke_with_retry = retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=2, min=wait_min, max=wait_max),
            reraise=True,
        )(self._invoke)

    def describe(self, image_bytes: bytes, context: str = "", kind: str = "image") -> str:
        """Returns a description, or raises VisionError after all retries fail."""
        key = hashlib.sha256(image_bytes + kind.encode() + context.encode("utf-8")).hexdigest()
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        prompt = PAGE_PROMPT if kind == "page" else IMAGE_PROMPT.format(context=context or "None provided.")
        try:
            image_url = compress_bytes_to_b64(image_bytes)
        except Exception as e:
            raise VisionError(f"unreadable image: {e}") from e
        try:
            description = self._invoke_with_retry(prompt, image_url)
        except Exception as e:
            raise VisionError(str(e)) from e

        with self._lock:
            self._cache[key] = description
        return description

    def describe_many(self, jobs: List[Tuple[bytes, str, str]]) -> List[Tuple[Optional[str], Optional[str]]]:
        """Describes (image_bytes, context, kind) jobs in parallel.

        Returns (description, error) per job, in the same order. Failures never raise.
        """

        def run(job):
            image_bytes, context, kind = job
            try:
                return self.describe(image_bytes, context, kind), None
            except VisionError as e:
                return None, str(e)

        if not jobs:
            return []
        with ThreadPoolExecutor(max_workers=min(self.max_concurrency, len(jobs))) as pool:
            return list(pool.map(run, jobs))


class UnavailableVision(VisionDescriber):
    """Used when no GROQ_API_KEY is configured: every description fails fast."""

    def __init__(self):
        super().__init__(invoke=self._fail, max_attempts=1)

    @staticmethod
    def _fail(prompt: str, image_url: str) -> str:
        raise VisionError("vision model not configured (GROQ_API_KEY missing)")


_default: Optional[VisionDescriber] = None


def default_vision() -> VisionDescriber:
    """Shared describer for the process (so the description cache is shared)."""
    global _default
    if _default is None:
        _default = VisionDescriber() if os.getenv("GROQ_API_KEY") else UnavailableVision()
    return _default
