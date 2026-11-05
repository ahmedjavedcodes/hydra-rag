"""Question answering over one session's documents, with sources.

    from src.pipeline.rag_chain import RagChain
    answer = RagChain().ask(session_id, "What was Q3 revenue?")
    answer.answer, answer.sources, answer.found
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import RagSettings, rag_settings
from src.embeddings.embedder import Embedder, default_embedder
from src.embeddings.vector_store import VectorStore, default_vector_store
from src.pipeline.index import INDEX_PENDING, INDEXED, INDEXING
from src.pipeline.prompts import ANSWER_SYSTEM_PROMPT, NOT_FOUND_TOKEN, answer_user_prompt
from src.pipeline.store import READY, SessionStore
from src.retrieval.reranker import Reranker, default_reranker
from src.retrieval.retriever import retrieve
from utilities.session import validate_session_id

NOT_FOUND_MESSAGE = "I couldn't find the answer to that in your documents."
NO_DOCUMENTS_MESSAGE = "No documents are ready yet. Send a file or a link first, then ask your question."

_CITATION = re.compile(r"\[(\d+)\]")
# Some models (e.g. gpt-oss) cite with full-width brackets or line ranges: 【1】, ［1］, 【1†L2-L4】, [1†L3]
_WIDE_CITATION = re.compile(r"[【［\[]\s*(\d+)\s*(?:†[^】］\]]*)?[】］\]]")


class QuestionError(ValueError):
    """The question was rejected before any search (empty or too long)."""


class ServiceError(RuntimeError):
    """Pinecone, the embedding model or Groq failed after retries. The message is user-friendly."""


@dataclass
class Source:
    index: int
    doc_id: str
    filename: str
    location: str
    passage: str
    score: float
    source_url: Optional[str] = None
    page: Optional[int] = None
    sheet: Optional[str] = None
    row_start: Optional[int] = None
    row_end: Optional[int] = None
    slide: Optional[int] = None
    rerank_score: Optional[float] = None


@dataclass
class Answer:
    answer: str
    found: bool
    sources: List[Source] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def groq_llm(settings: RagSettings = rag_settings) -> Callable[[str, str], str]:
    """Returns llm(system, user) -> text, backed by the Groq answer model."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_groq import ChatGroq

    kwargs = {}
    if settings.answer_model.startswith("openai/gpt-oss"):
        # Reasoning tokens count towards max_tokens; keep them small so the answer isn't cut off
        kwargs["reasoning_effort"] = settings.answer_reasoning_effort
    model = ChatGroq(model=settings.answer_model, temperature=0, max_tokens=4096, **kwargs)

    def call(system: str, user: str) -> str:
        return model.invoke([SystemMessage(content=system), HumanMessage(content=user)]).content

    return call


def _source_label(meta: dict) -> str:
    name = meta.get("filename") or meta.get("source_url") or "document"
    if meta.get("source_url") and name == "page.html":
        name = meta["source_url"]
    location = meta.get("location")
    return f"{name}, {location}" if location else name


class RagChain:
    def __init__(
        self,
        embedder: Optional[Embedder] = None,
        vector_store: Optional[VectorStore] = None,
        reranker: Optional[Reranker] = None,
        llm: Optional[Callable[[str, str], str]] = None,
        settings: RagSettings = rag_settings,
        llm_attempts: int = 4,
        llm_wait_max: float = 15,
    ):
        self.embedder = embedder or default_embedder()
        self.vector_store = vector_store or default_vector_store()
        self.reranker = reranker or default_reranker()
        self.settings = settings
        llm = llm or groq_llm(settings)
        self.llm = retry(
            stop=stop_after_attempt(llm_attempts),
            wait=wait_exponential(multiplier=2, min=0 if llm_wait_max == 0 else 2, max=llm_wait_max),
            reraise=True,
        )(llm)

    def ask(self, session_id: str, question: str) -> Answer:
        validate_session_id(session_id)
        question = (question or "").strip()
        if not question:
            raise QuestionError("Please type a question.")
        if len(question) > self.settings.max_question_chars:
            raise QuestionError(f"That question is too long (limit {self.settings.max_question_chars} characters).")

        docs = SessionStore(session_id).documents()
        indexed = {d_id for d_id, d in docs.items() if d.get("index_state") == INDEXED}
        processing = [
            d.get("filename") or d.get("source_url")
            for d in docs.values()
            if d["state"] in ("received", "processing")
            or (d["state"] == READY and d.get("index_state", INDEX_PENDING) in (INDEX_PENDING, INDEXING))
        ]
        notes = [f"Still processing: {', '.join(processing)}"] if processing else []
        if not indexed:
            return Answer(NO_DOCUMENTS_MESSAGE, found=False, notes=notes)

        try:
            matches = retrieve(
                self.embedder,
                self.vector_store,
                session_id,
                question,
                self.settings.top_k,
                self.settings.min_score,
                allowed_doc_ids=indexed,
            )
        except Exception as e:
            print(f"   ✗ Retrieval failed: {e!r}")
            raise ServiceError("There was a temporary problem searching your documents. Please try again in a moment.")
        if not matches:
            return Answer(NOT_FOUND_MESSAGE, found=False, notes=notes)

        ranked = self.reranker.rerank(question, matches, self.settings.rerank_top_n)
        passages = [(_source_label(m.metadata), m.metadata.get("text", "")) for m, _ in ranked]
        try:
            raw = self.llm(ANSWER_SYSTEM_PROMPT, answer_user_prompt(question, passages))
        except Exception as e:
            print(f"   ✗ Answer model failed: {e!r}")
            raise ServiceError("The answer service is busy. Please try again in a moment.")

        return self._parse(raw or "", ranked, notes)

    def _parse(self, raw: str, ranked, notes: List[str]) -> Answer:
        text = _WIDE_CITATION.sub(r"[\1]", raw.strip())
        if not text or text.upper().startswith(NOT_FOUND_TOKEN):
            return Answer(NOT_FOUND_MESSAGE, found=False, notes=notes)

        # Keep only citations that point at a passage; renumber them 1..n in order of first use
        order: List[int] = []
        for number in (int(n) for n in _CITATION.findall(text)):
            if 1 <= number <= len(ranked) and number not in order:
                order.append(number)
        if not order:
            print(f"   ⚠ Answer had no valid citations, treated as not found: {text[:200]!r}")
            return Answer(NOT_FOUND_MESSAGE, found=False, notes=notes)
        renumber = {old: new for new, old in enumerate(order, start=1)}

        def fix(match):
            number = int(match.group(1))
            return f"[{renumber[number]}]" if number in renumber else ""

        text = re.sub(r"[ \t]+", " ", _CITATION.sub(fix, text)).strip()

        sources = []
        for old in order:
            match, rerank_score = ranked[old - 1]
            meta = match.metadata
            sources.append(
                Source(
                    index=renumber[old],
                    doc_id=meta.get("doc_id", ""),
                    filename=meta.get("filename", ""),
                    location=meta.get("location", ""),
                    passage=meta.get("text", ""),
                    score=round(match.score, 4),
                    source_url=meta.get("source_url"),
                    page=meta.get("page"),
                    sheet=meta.get("sheet"),
                    row_start=meta.get("row_start"),
                    row_end=meta.get("row_end"),
                    slide=meta.get("slide"),
                    rerank_score=round(rerank_score, 4),
                )
            )
        return Answer(text, found=True, sources=sources, notes=notes)
