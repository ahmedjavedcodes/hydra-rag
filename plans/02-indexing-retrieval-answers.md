# Plan 2: Chunking, Pinecone indexing, retrieval and grounded answers

**Goal:** turn elements into vectors in Pinecone, then answer questions from them with sources.

**Depends on:** Plan 1 (the element schema).

## Pipeline

Ingestion: elements, then chunking, then embeddings, then Pinecone upsert.
Query: embed query, Pinecone search (top 10, score >= 0.80), rerank to top 3, Groq, then an answer with sources.

## Steps

1. **Config.** Settings module reading `.env`: `PINECONE_API_KEY`, `PINECONE_INDEX`, `GROQ_API_KEY`, `HF_TOKEN`, model names, `TOP_K=10`, `MIN_SCORE=0.80`, `RERANK_TOP_N=3`. Fail at startup with a clear message when a key is missing.
2. **Chunking.** Text: split by heading, then by size with overlap (use `docling-core` chunking or LangChain splitters). Tables: keep a table as one chunk when small, otherwise split by row groups and repeat the header row. Images/charts: the description is the chunk. Each chunk keeps its element's citation fields.
3. **Embeddings.** `BAAI/bge-large-en-v1.5` (1024 dims) through `sentence-transformers`, loaded once at startup. Normalize vectors. Use the bge query instruction prefix for queries, not for documents. Batch embedding.
4. **Pinecone index.** Create a serverless index once (dimension 1024, metric cosine). Namespace is the session id. Vector id is `doc_id:element_id:chunk_n`, so re-uploads overwrite instead of duplicating. Skip chunks whose `content_hash` already exists. Metadata: `doc_id`, `filename` or `source_url`, `page` or sheet/row range, `type`, `heading_path`, `text` (truncated to stay far below the 40KB metadata limit).
5. **Retrieval.** Query inside the session namespace only. Keep results with score >= 0.80, then rerank with a cross-encoder (for example `BAAI/bge-reranker-large` via `sentence-transformers`) and keep the top 3. If nothing passes the threshold, return "not found in your documents" without calling the LLM.
6. **Answer chain.** Groq text model (configurable). The prompt says: answer only from the numbered context, cite sources as `[1]`, say when the context doesn't answer. Return a structured result: `answer`, and `sources` (index, filename or URL, page or sheet/rows, quoted passage, score). Support streaming later.
7. **Delete and cleanup.** Functions to delete one document (by `doc_id` filter) and a whole session (delete namespace). Run the session cleanup on an expiry schedule.

## Done when

- Upload a PDF, a DOCX, a CSV and a URL into one session, then ask one question about each and get a correct answer with the right page or row range cited.
- A question about something not in the documents returns "not found" with no sources.
- A second session cannot retrieve the first session's content (test this explicitly).
- Re-uploading the same file adds no new vectors.

## Risks and checks

- **The 0.80 threshold may be wrong.** Cosine scores for bge-large vary by query type and are often lower than 0.80 for correct matches. Build a small labelled question set and calibrate before fixing the number. Make it a setting, not a constant.
- Pinecone upserts are eventually consistent: a query right after an upload may miss it, so the API should report "indexing" until the upsert is confirmed.
- Loading the embedding and reranker models on CPU is slow to start and large in memory: decide early whether the API hosts them or calls a hosted inference endpoint.
- Groq free-tier rate limits: add retries with backoff (tenacity is already a dependency).
