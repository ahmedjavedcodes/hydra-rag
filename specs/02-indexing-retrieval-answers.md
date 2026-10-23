# Spec: Indexing, Retrieval and Grounded Answers

## Problem Statement

Once files are turned into elements (see `01-ingestion-and-loaders.md`), nothing makes them searchable or lets anyone ask questions about them. The target behaviour is: a person asks a question, the system finds the relevant passages in only that person's documents, an LLM answers from those passages, and the answer says exactly where it came from.

Without this step the project is a file converter. The risks that make a spec worthwhile here are wrong answers (the LLM guessing beyond the documents), leaking one person's documents into another's answers (sessions are anonymous), and an untested relevance threshold that could make the system say "not found" for questions it could answer.

## Functional Requirements

1. **Chunking.** Elements become chunks:
   - text: split by heading, then by size with overlap;
   - tables: one chunk if small, otherwise groups of rows with the header row repeated in every chunk;
   - images and charts: the description is the chunk.
   Every chunk keeps its element's citation fields (`doc_id`, `element_id`, filename or URL, page, sheet/row range or slide number, `heading_path`, `type`).
2. **Embedding.** Chunks are embedded with `BAAI/bge-large-en-v1.5` (1024 dimensions), normalised, in batches. Queries use the model's query instruction prefix; documents do not.
3. **Indexing.** Vectors are upserted into a Pinecone index (1024 dimensions, cosine) in the namespace equal to the session id. Vector id is `doc_id:element_id:chunk_n`, so re-indexing overwrites. Metadata stores the citation fields plus the chunk text (truncated).
4. **Skip unchanged content.** A chunk whose `content_hash` is already indexed in the session is not embedded or upserted again.
5. **Retrieval.** A question is embedded and searched in the session's namespace only, returning up to 10 matches. Matches below the score threshold (default 0.80, configurable) are dropped.
6. **Reranking.** Remaining matches are reranked with a cross-encoder and the best 3 are kept.
7. **Grounded answer.** A Groq text model receives the question and the numbered passages and must answer using only them. The result is structured: `answer`, and `sources`, each with `index`, filename or URL, page, sheet/row range or slide number, quoted passage and score. Citations in the answer text use `[n]` matching `sources`.
8. **No-answer path.** If no match passes the threshold, or the model states the passages don't answer the question, the result is "not found in your documents" with an empty `sources` list. In the first case the LLM is not called.
9. **Document and session deletion.** One document (all its vectors, by `doc_id`) or a whole session (the namespace plus the session's files) can be deleted.
10. **Index status.** Each document reports `indexing` until all its vectors are confirmed queryable, then `ready`.
11. **Configuration.** Keys and settings (`PINECONE_API_KEY`, `PINECONE_INDEX`, `GROQ_API_KEY`, `HF_TOKEN`, model names, `TOP_K`, `MIN_SCORE`, `RERANK_TOP_N`) are read from `.env`; startup fails with a clear message when a required one is missing.

**Out of scope for v1 (deferred):** hybrid search with BM25, query rewriting, multi-turn conversation memory, streaming tokens, answer caching, cross-session or shared knowledge bases, languages other than English.

## Behaviour

**Indexing flow**
1. Receive the elements of a document from the loaders, with the session id.
2. Chunk, then drop chunks whose `content_hash` is already indexed in the namespace.
3. Embed the remaining chunks in batches and upsert them with metadata. Document state is `indexing`.
4. When Pinecone reports the vectors as queryable, state becomes `ready`.

**Question flow**
1. Receive `session_id` and question text.
2. If the session has no `ready` documents, answer "no documents yet" and say how to add one.
3. Embed the question, search the namespace for the top 10, drop matches under the threshold.
4. If none remain, return "not found" with no sources.
5. Rerank, keep the top 3, build the prompt with numbered passages.
6. Call the LLM, parse the answer, attach `sources` for the passages actually cited in the answer text.
7. Return `answer` and `sources`.

**Prompt rules** the LLM is given: answer only from the numbered passages; cite as `[n]`; if the passages do not contain the answer, say so; do not mention information from outside the passages.

## Constraints

- Pinecone is the only vector store; no local Chroma or FAISS. Index metric is cosine, dimension 1024. The session id is the namespace.
- Queries always specify a namespace; there is no code path that searches across namespaces.
- Pinecone metadata is limited to 40 KB per record: store a truncated chunk text and keep other fields small.
- Pinecone upserts are eventually consistent, so a query immediately after an upload may miss new vectors.
- Embedding and reranker models run through `sentence-transformers` (already a dependency) and load once at startup. Memory use and cold start on CPU must be accepted or a hosted inference option chosen before deployment.
- Groq rate limits apply: wrap LLM and vision calls with retries (tenacity).
- English text only for v1, as `bge-large-en-v1.5` is an English model.
- Python 3.13, `uv`; secrets in `.env`.
- **Assumptions (change if wrong):** reranker is `BAAI/bge-reranker-large`; chunk size about 800 characters with 100 overlap (tunable); the answer LLM is any Groq text model set by `.env`; the 0.80 threshold is a starting value to be calibrated, not a proven one.

## Edge Cases and Error Handling

| Trigger | Expected Response |
|---|---|
| Question asked before any document is `ready` | "No documents are ready yet" plus the list of documents still `indexing`, if any |
| Question asked while some documents are still indexing | Answer from ready documents and add a note that others are still being processed |
| No match above the score threshold | "Not found in your documents", no sources, LLM not called |
| Matches pass the threshold but the model says the passages don't answer | Return "not found" and no sources |
| Empty, whitespace-only or very long question (over a configured limit) | Reject with a clear message; no embedding or LLM call |
| Question not in English | Answer is attempted; no guarantee. Not treated as an error |
| Pinecone unavailable or times out | Retry with backoff; then return a "temporary problem, try again" error and log the cause. No partial indexing is marked `ready` |
| Pinecone upsert partially fails | Document marked `failed` with the count of failed chunks; already-written vectors are deleted by `doc_id` so no half-indexed document remains |
| Embedding model not loaded or out of memory | Startup check fails fast; at runtime return a service error, never wrong vectors |
| Groq rate limit or timeout | Retry with backoff up to the configured attempts, then return a "busy, try again" error |
| LLM returns an answer citing a source number that does not exist | Drop the invalid citations and log; if no valid citation remains, treat as "not found" |
| Document re-uploaded unchanged | Chunks skipped by `content_hash`; no new vectors; state returns to `ready` |
| Document re-uploaded with changes | New `version_id`; old vectors for the previous version deleted after the new ones are queryable |
| Chunk text exceeds metadata limit | Metadata text is truncated at the limit; the full chunk is still embedded |
| Table split across chunks | Each chunk repeats the header row so rows are interpretable alone |
| Two sessions index and ask at the same time | Each works only in its own namespace; results never mix |
| Delete requested for a document or session that does not exist | Idempotent: report "nothing to delete", no error |
| Query immediately after upload misses new content | Prevented by the `indexing`/`ready` state; the answer notes documents still processing |
| Missing `PINECONE_API_KEY`, `GROQ_API_KEY` or `HF_TOKEN` | Startup fails with a message naming the missing setting |

## Acceptance Criteria

- [ ] **AC1 (FR1).** Given a long PDF section, a large table and an image description, when chunked, then text chunks respect the size limit with overlap, every table chunk starts with the header row, and each chunk has its citation fields.
- [ ] **AC2 (FR2, FR3).** Given a chunk, when embedded and upserted, then the stored vector has 1024 dimensions, lives in the session namespace, and its id is `doc_id:element_id:chunk_n`.
- [ ] **AC3 (FR4, edge cases).** Given a document already indexed, when the same file is uploaded again, then the number of vectors in the namespace does not change.
- [ ] **AC4 (FR5, FR6, FR7).** Given a PDF, a DOCX, a CSV and a URL in one session, when a question is asked about each, then the answer is correct and `sources` name the right file and the right page or row range.
- [ ] **AC5 (FR7).** Given an answer, when its `[n]` citations are checked, then each matches an item in `sources` and each source includes the supporting passage.
- [ ] **AC6 (FR8).** Given a question about something not in the documents, when asked, then the result is "not found in your documents" with an empty `sources` list and the LLM was not called (for the below-threshold case).
- [ ] **AC7 (FR5, edge cases).** Given two sessions with different content, when session B asks a question that only session A's content answers, then B gets "not found" and no data from A.
- [ ] **AC8 (FR9).** Given a session with documents, when one document is deleted then its vectors are gone and the others still answer; when the session is deleted then its namespace and files are gone.
- [ ] **AC9 (FR10, edge cases).** Given a document just uploaded, when asked about before indexing finishes, then the response says it is still processing; after it reports `ready`, the question is answered.
- [ ] **AC10 (FR5, calibration).** Given a labelled set of at least 20 question/document pairs (including some with no answer), when run, then the chosen threshold gives a documented hit rate and false-answer rate, and the threshold value is stored in configuration with those results recorded.
- [ ] **AC11 (FR11, edge cases).** Given a missing API key, when the app starts, then it exits with a message naming the key.
- [ ] **AC12 (edge cases).** Given Groq or Pinecone fail transiently, when a question or upload runs, then retries happen and the final failure message is user-friendly; given an upsert fails halfway, then no partial document remains.
- [ ] **AC13 (edge cases).** Given a changed re-upload, when it completes, then only the new version's vectors exist for that document.
