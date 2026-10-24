"""Acceptance tests for specs/02-indexing-retrieval-answers.md (AC numbers in the test names).
Embeddings, reranking, Pinecone and the LLM are replaced by small fakes; no network calls."""

import hashlib
import math
import os
import re

import pytest

from src.config import ConfigError, RagSettings, require_keys
from src.embeddings.embedder import Embedder
from src.embeddings.vector_store import InMemoryVectorStore, PineconeVectorStore
from src.pipeline.index import INDEX_FAILED, INDEXED, INDEXING, REPLACED, Indexer
from src.pipeline.ingest import ingest_files
from src.pipeline.rag_chain import NO_DOCUMENTS_MESSAGE, NOT_FOUND_MESSAGE, QuestionError, RagChain, ServiceError
from src.pipeline.store import SessionStore
from src.processing.chunkers import chunk_element, split_text
from src.retrieval.reranker import Reranker

SETTINGS = RagSettings(min_score=0.15, chunk_size=200, chunk_overlap=40, table_chunk_size=300, index_confirm_timeout_s=0)
WORD = re.compile(r"[a-z0-9]+")


class FakeEmbedder(Embedder):
    """Hashed bag of words: texts sharing words get high cosine similarity."""

    dimension = 256

    def _vector(self, text):
        vec = [0.0] * self.dimension
        for word in WORD.findall(text.lower()):
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dimension] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts):
        self.calls = getattr(self, "calls", 0) + len(texts)
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)


class FakeReranker(Reranker):
    def rerank(self, question, matches, top_n):
        q = set(WORD.findall(question.lower()))
        scored = [(m, float(len(q & set(WORD.findall(m.metadata.get("text", "").lower()))))) for m in matches]
        return sorted(scored, key=lambda p: p[1], reverse=True)[:top_n]


class FakeLLM:
    """Answers by quoting passage [1], or returns a scripted reply."""

    def __init__(self, reply=None):
        self.reply, self.calls, self.prompts = reply, 0, []

    def __call__(self, system, user):
        self.calls += 1
        self.prompts.append(user)
        if self.reply is not None:
            return self.reply(user) if callable(self.reply) else self.reply
        first = user.split("[1] ", 1)[1].split("\n", 2)[1]
        return f"{first[:80]} [1]"


@pytest.fixture
def rag(vision):
    store = InMemoryVectorStore()
    embedder = FakeEmbedder()
    llm = FakeLLM()
    indexer = Indexer(embedder=embedder, vector_store=store, settings=SETTINGS, confirm_poll_s=0)
    chain = RagChain(embedder=embedder, vector_store=store, reranker=FakeReranker(), llm=llm, settings=SETTINGS, llm_wait_max=0)

    def ingest_and_index(paths, session_id=None):
        batch = ingest_files(paths, session_id, vision=vision)
        indexer.index_batch(batch)
        return batch

    return type("Rag", (), dict(store=store, embedder=embedder, llm=llm, indexer=indexer, chain=chain, add=staticmethod(ingest_and_index)))


def element(**overrides):
    base = dict(
        doc_id="d" * 32,
        version_id="v1",
        element_id="e00001",
        type="text",
        page=3,
        heading_path=["Results"],
        title="Results",
        content_text="",
        content_structured={},
        metadata={"session_id": "s", "filename": "report.pdf", "location": "p.3", "page": 3},
        content_hash="h",
    )
    base.update(overrides)
    return base


def test_ac1_text_is_split_with_overlap_and_keeps_citations():
    text = " ".join(f"Sentence number {i} talks about monsoon rainfall." for i in range(40))
    chunks = chunk_element(element(content_text=text), SETTINGS)
    assert len(chunks) > 1 and all(len(c.text) <= SETTINGS.chunk_size for c in chunks)
    assert chunks[1].text[:20] in chunks[0].text[-SETTINGS.chunk_overlap :]  # chunk 2 starts with chunk 1's tail
    meta = chunks[0].metadata
    assert (meta["doc_id"], meta["element_id"], meta["filename"], meta["page"], meta["type"]) == (
        "d" * 32,
        "e00001",
        "report.pdf",
        3,
        "text",
    )
    assert meta["heading_path"] == ["Results"] and chunks[0].embed_text.startswith("Results\n")


def test_ac1_large_tables_repeat_the_header_and_keep_row_ranges():
    rows = [[f"city{i}", str(i)] for i in range(60)]
    header = "| city | rain |\n|---|---|"
    text = header + "\n" + "\n".join(f"| {c} | {r} |" for c, r in rows)
    structured = {"header": ["city", "rain"], "rows": rows, "row_numbers": list(range(2, 62))}
    meta = {"session_id": "s", "filename": "rain.xlsx", "location": "Data, rows 2-61", "sheet": "Data", "row_start": 2, "row_end": 61}
    chunks = chunk_element(element(type="table", content_text=text, content_structured=structured, metadata=meta, page=None), SETTINGS)
    assert len(chunks) > 1 and all(c.text.startswith(header) for c in chunks)
    assert chunks[0].metadata["row_start"] == 2 and chunks[-1].metadata["row_end"] == 61
    assert chunks[1].metadata["row_start"] == chunks[0].metadata["row_end"] + 1
    assert chunks[1].metadata["location"].startswith("Data, rows ")


def test_ac1_image_descriptions_stay_whole_and_empty_elements_are_skipped():
    description = "This image contains " + "a detailed chart " * 40
    assert len(chunk_element(element(type="image", content_text=description), SETTINGS)) == 1
    assert chunk_element(element(type="image", content_text=""), SETTINGS) == []


def test_split_text_never_exceeds_size():
    text = "word " * 1000 + "x" * 900
    assert all(len(c) <= 300 for c in split_text(text, 300, 50))


def test_ac2_vectors_live_in_the_session_namespace_with_stable_ids(rag, samples):
    batch = rag.add([samples["pdf"]])
    doc_id = batch.files[0].doc_id
    space = rag.store.namespaces[batch.session_id]
    assert space and all(re.fullmatch(rf"{doc_id}:e\d{{5}}:\d+", vid) for vid in space)
    assert all(len(v["values"]) == FakeEmbedder.dimension for v in space.values())
    assert all(v["metadata"]["text"] for v in space.values())
    assert SessionStore(batch.session_id).get(doc_id)["index_state"] == INDEXED


def test_ac3_reupload_adds_no_vectors(rag, samples):
    batch = rag.add([samples["docx"]])
    count = len(rag.store.namespaces[batch.session_id])
    calls = rag.embedder.calls
    rag.add([samples["docx"]], batch.session_id)
    again = rag.indexer.index_document(batch.session_id, batch.files[0].doc_id)
    assert len(rag.store.namespaces[batch.session_id]) == count
    assert again.upserted == 0 and again.skipped == again.chunks
    assert rag.embedder.calls == calls


def test_ac4_answers_cite_the_right_file_and_location(rag, samples):
    batch = rag.add([samples["pdf"], samples["docx"], samples["xlsx"], samples["pptx"]])
    sid = batch.session_id
    cases = [
        ("Which region led growth in the results?", "report.pdf", "p.2"),
        ("How much does the basic plan cost per month?", "guide.docx", 'section "Pricing"'),
        ("Sales Region North Q1 Q2", "numbers.xlsx", "Sales, rows 2-4"),
        ("What should be mentioned about the WhatsApp channel?", "deck.pptx", "slide 2 speaker notes"),
    ]
    for question, filename, location in cases:
        answer = rag.chain.ask(sid, question)
        assert answer.found, question
        assert (answer.sources[0].filename, answer.sources[0].location) == (filename, location), question


def test_ac5_citations_match_sources_and_are_renumbered(rag, samples):
    batch = rag.add([samples["pdf"], samples["docx"]])
    rag.llm.reply = "North led growth [2]. Plans cost money [7]. See also [2][1]."
    answer = rag.chain.ask(batch.session_id, "Which region led growth and what does the plan cost?")
    assert answer.found
    assert "[7]" not in answer.answer
    numbers = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer.answer)})
    assert numbers == [s.index for s in answer.sources] == [1, 2]
    assert all(s.passage for s in answer.sources)


def test_ac6_below_threshold_is_not_found_without_calling_the_llm(rag, samples):
    batch = rag.add([samples["docx"]])
    answer = rag.chain.ask(batch.session_id, "zebra quantum volcano")
    assert answer.answer == NOT_FOUND_MESSAGE and answer.sources == [] and not answer.found
    assert rag.llm.calls == 0


def test_ac6_model_saying_not_found_returns_no_sources(rag, samples):
    batch = rag.add([samples["docx"]])
    rag.llm.reply = "NOT_FOUND"
    answer = rag.chain.ask(batch.session_id, "What does the basic plan cost?")
    assert answer.answer == NOT_FOUND_MESSAGE and answer.sources == []
    rag.llm.reply = "The plan costs ten dollars."  # no citation at all -> not trusted
    assert not rag.chain.ask(batch.session_id, "What does the basic plan cost?").found


def test_ac7_sessions_never_see_each_other(rag, samples):
    a = rag.add([samples["docx"]])
    b = rag.add([samples["csv"]])
    answer = rag.chain.ask(b.session_id, "How much does the basic plan cost per month?")
    assert not answer.found or all(s.filename != "guide.docx" for s in answer.sources)
    assert all(v["metadata"]["filename"] == "data.csv" for v in rag.store.namespaces[b.session_id].values())
    assert set(rag.store.namespaces) == {a.session_id, b.session_id}


def test_ac8_delete_document_and_session(rag, samples, data_root):
    batch = rag.add([samples["docx"], samples["csv"]])
    sid = batch.session_id
    docx_id, csv_id = batch.files[0].doc_id, batch.files[1].doc_id
    assert rag.indexer.delete_document(sid, docx_id)
    assert not any(vid.startswith(docx_id) for vid in rag.store.namespaces[sid])
    assert any(vid.startswith(csv_id) for vid in rag.store.namespaces[sid])
    assert rag.chain.ask(sid, "city population Lahore").found
    assert not rag.indexer.delete_document(sid, docx_id)  # idempotent: nothing to delete

    rag.indexer.delete_session(sid)
    assert sid not in rag.store.namespaces
    assert not os.path.exists(os.path.join(data_root, sid))
    rag.indexer.delete_session(sid)  # idempotent


def test_ac9_questions_before_indexing_report_processing(rag, samples, vision):
    batch = ingest_files([samples["docx"]], vision=vision)  # ingested, not indexed
    answer = rag.chain.ask(batch.session_id, "What does the plan cost?")
    assert answer.answer == NO_DOCUMENTS_MESSAGE and "guide.docx" in answer.notes[0]

    rag.add([samples["csv"]], batch.session_id)
    answer = rag.chain.ask(batch.session_id, "city population Lahore")
    assert answer.found and any("guide.docx" in n for n in answer.notes)


def test_ac9_lagging_index_stays_indexing_until_confirmed(rag, samples, vision):
    class Lagging(InMemoryVectorStore):
        visible = False

        def fetch(self, namespace, ids):
            return super().fetch(namespace, ids) if self.visible else {}

    store = Lagging()
    indexer = Indexer(embedder=FakeEmbedder(), vector_store=store, settings=SETTINGS, confirm_poll_s=0)
    batch = ingest_files([samples["docx"]], vision=vision)
    doc_id = batch.files[0].doc_id
    result = indexer.index_document(batch.session_id, doc_id)
    assert result.index_state == INDEXING
    assert SessionStore(batch.session_id).get(doc_id)["index_state"] == INDEXING
    assert indexer.refresh_status(batch.session_id, doc_id) == INDEXED


def test_ac11_missing_keys_are_named(monkeypatch):
    monkeypatch.delenv("PINECONE_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "x")
    with pytest.raises(ConfigError, match="PINECONE_API_KEY"):
        require_keys()
    with pytest.raises(ConfigError, match="PINECONE_API_KEY"):
        PineconeVectorStore()


def test_ac12_llm_retries_then_answers(rag, samples):
    batch = rag.add([samples["docx"]])
    attempts = {"n": 0}

    def flaky(user):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("429")
        return "Ten dollars per month [1]."

    rag.llm.reply = flaky
    assert rag.chain.ask(batch.session_id, "What does the basic plan cost?").found
    assert attempts["n"] == 3

    rag.llm.reply = lambda user: (_ for _ in ()).throw(RuntimeError("503"))
    with pytest.raises(ServiceError, match="busy"):
        rag.chain.ask(batch.session_id, "What does the basic plan cost?")


def test_ac12_search_outage_is_a_friendly_error(rag, samples, monkeypatch):
    batch = rag.add([samples["docx"]])
    monkeypatch.setattr(rag.store, "query", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    with pytest.raises(ServiceError, match="temporary problem"):
        rag.chain.ask(batch.session_id, "What does the basic plan cost?")


def test_ac12_failed_upsert_leaves_no_partial_document(samples, vision):
    class HalfBroken(InMemoryVectorStore):
        def upsert(self, namespace, records):
            super().upsert(namespace, records[:2])
            raise ConnectionError("connection reset")

    store = HalfBroken()
    indexer = Indexer(embedder=FakeEmbedder(), vector_store=store, settings=SETTINGS, confirm_poll_s=0)
    batch = ingest_files([samples["pdf"]], vision=vision)
    result = indexer.index_document(batch.session_id, batch.files[0].doc_id)
    assert result.index_state == INDEX_FAILED
    assert store.namespaces.get(batch.session_id, {}) == {}
    assert SessionStore(batch.session_id).get(batch.files[0].doc_id)["index_state"] == INDEX_FAILED


def test_ac13_changed_reupload_replaces_old_vectors(rag, samples, tmp_path):
    first = rag.add([samples["txt"]])
    old_id = first.files[0].doc_id
    changed = tmp_path / "v2" / "notes.txt"
    changed.parent.mkdir()
    changed.write_text("Brand new paragraph about vector search.\n")
    second = rag.add([str(changed)], first.session_id)
    new_id = second.files[0].doc_id
    ids = list(rag.store.namespaces[first.session_id])
    assert ids and all(vid.startswith(new_id) for vid in ids)
    assert SessionStore(first.session_id).get(old_id)["index_state"] == REPLACED
    assert rag.chain.ask(first.session_id, "vector search paragraph").sources[0].doc_id == new_id


@pytest.mark.parametrize("question", ["", "   ", "x" * 2000])
def test_bad_questions_are_rejected_before_searching(rag, samples, question):
    batch = rag.add([samples["docx"]])
    with pytest.raises(QuestionError):
        rag.chain.ask(batch.session_id, question)
    assert rag.llm.calls == 0


def test_invalid_session_id_is_rejected(rag):
    with pytest.raises(ValueError):
        rag.chain.ask("../etc", "hello")
