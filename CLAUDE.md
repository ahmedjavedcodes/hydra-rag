# HydraRAG

Dynamic RAG system: anyone can upload any type of file (PDF, Word, spreadsheet, image, text) or give a URL. The system turns it into a searchable knowledge base, answers questions about it, and cites the sources of every answer. Ingestion (plan/spec 01) and indexing, retrieval and grounded answers (plan/spec 02) are built in `src/`. The FastAPI/WhatsApp server (plan 03) is not built yet. Plans are in `plans/`, specs with acceptance criteria in `specs/`.

## Target architecture

Ingestion: `Any file (PDF/DOCX/XLSX/...)` → file rules by type → load document → chunking → embeddings → Pinecone.

Query: query → embed query → Pinecone similarity search → top 10 (score ≥ `MIN_SCORE`, default 0.50; the original design said 0.80, see Known issues) → rerank to top 3 → Groq LLM → grounded answer.

Planned stack: Python 3.13, LangChain, Pinecone, Groq, FastAPI, Next.js.

### Channels

The first client is WhatsApp through the Meta Cloud API: a FastAPI webhook receives messages and media, calls the RAG pipeline, and replies through the Graph API with the business access token. The session id for a WhatsApp user is derived from their `wa_id` with an HMAC (see `plans/03-api-frontend-and-deploy.md`). Verify `X-Hub-Signature-256` on every webhook POST, return 200 fast and process in the background, and dedupe by message id. The pipeline itself must stay channel-agnostic so a web frontend can reuse it.

### Vector store: Pinecone

- All vectors live in a Pinecone index (no local Chroma/FAISS). Index dimension is 1024 (`BAAI/bge-large-en-v1.5`), metric cosine. Credentials go in `.env` (`PINECONE_API_KEY`).
- Uploads are dynamic and anonymous, so isolate each session's documents with a Pinecone **namespace** equal to the session id, and always query within one namespace (optionally filtered by `doc_id`). Never query across namespaces. Sessions are unauthenticated, so the unguessable id is the only access control.
- Every vector carries the metadata needed for citations. Use the element schema in the Layout section: `doc_id`, `element_id`, `type`, `page`, `heading_path`, `title`, plus `filename` or `source_url` and the chunk text. Pinecone metadata is limited to 40KB per record, so keep chunk text short.
- Use `content_hash` to skip re-embedding unchanged content, and `version_id` when a file is re-uploaded.

### Answers and sources

Every answer must be grounded in the retrieved chunks and return its sources: file name or URL, page (or sheet/row range for spreadsheets, slide number for presentations), and the supporting passage. If no chunk clears the score threshold (`MIN_SCORE`), say the documents don't contain the answer instead of letting the LLM guess.

### Dynamic file handling

Don't assume a fixed file set. Route by extension and content type (see `FOLDER_MAPPINGS` and `notes.txt`), and make the per-format loader a pluggable unit that outputs the common element schema, so supporting a new format means adding one loader. Handle unsupported or corrupt files by reporting them to the user, not by crashing the batch.

## Commands

Dependencies are managed with `uv` (`pyproject.toml` + `uv.lock`); `requirements.txt` is empty.

```
uv sync                                   # install deps (and the dev group: pytest, ipykernel)
uv run pytest                             # test suite (tests/test_loaders.py, tests/test_web_loader.py); no network or API calls
uv run pytest tests/test_loaders.py -k ac4    # one test; test names carry the spec acceptance-criterion number
uv run python -m tests.test_ingestion     # legacy script: copies inputs/* into ./data/<new session id> (set PYTHONIOENCODING=utf-8 on Windows)
uv run python main.py                     # placeholder, prints a greeting
```

Run the app locally (two terminals):

```
uv run uvicorn src.api.app:app --port 8000          # REST API (loads models + connects to Pinecone at startup)
cd web && npm install && npm run dev                # test UI on http://localhost:3000 (NEXT_PUBLIC_API_URL defaults to http://localhost:8000)
```

Full ingestion from Python: `from src.pipeline.ingest import ingest_files, ingest_url` (see the module docstring).

```
uv run python -m scripts.try_rag inputs/data.docx -q "What is ...?"   # live end-to-end run (Groq + Pinecone + local models); deletes its session afterwards
uv run python -m scripts.calibrate_threshold questions.json          # report hit/false-answer rates per MIN_SCORE (no LLM calls)
```

Run scripts from the repo root. `utilities/ingestion.py` adds the root to `sys.path` and writes to `./data`, which is relative to the working directory.

Tests patch `utilities.session.DATA_ROOT` to a temp folder and use a fake vision model (`tests/conftest.py`), and the URL tests run a local HTTP server with the SSRF guard patched. `tests/test_rag.py` uses a hashed bag-of-words embedder, a word-overlap reranker, a scripted LLM and `InMemoryVectorStore` (test-only; production is Pinecone). There is no linter or CI config yet.

## Layout

- `utilities/session.py` — session ids and folders. There are no accounts or auth, so a session is a random 32-hex id (`new_session_id()`) that the client keeps and sends with every upload and question; `validate_session_id` rejects anything else (path traversal). The session id is also the Pinecone namespace.
- `utilities/ingestion.py` — `ingest_user_inputs(inputs, session_id=None)` / `run_ingestion`: copies local files into `./data/<session_id>/{images,spreadsheets,documents,others}` by extension (`FOLDER_MAPPINGS`). Omit `session_id` to start a new session (it is returned in the result); pass it to add files to an existing one. Same-named files get a `_1`, `_2` suffix instead of overwriting.
- `utilities/ingestion.py` also provides `store_file` / `unique_destination` (exclusive-create, so concurrent same-name uploads never overwrite) and `category_for`, used by the pipeline.
- `utilities/scrapper.py` — URL helpers (`fix_url_schema`, `sanitize_and_generate_dynamic_folder` for `./data/<session_id>/web/<site>/`) and `run_scrapper(url, session_id)`, which now delegates to `ingest_url`.
- `src/config.py` — ingestion limits from env with defaults (20 MB/file, 10 files/upload, 30 images/doc, 30 s URL timeout, 10 linked files/page, 50 rows per table element, 5000 rows/sheet, `VISION_MODEL=qwen/qwen3.8-27b`).
- `src/schema.py` — `Element` (the common element schema), `FileResult`, `BatchResult`, `validate_element`.
- `src/loaders/` — one module per format, registered by extension with `@register(".ext")` in `base.py`; `LoadContext.make()` builds schema-complete elements with citation metadata (`metadata.location` is the human label: "p.3", "Sales, rows 2-51", "slide 4", "slide 2 speaker notes"). Formats: txt/md, pdf (port of the notebook: prose, tables, images, vector charts, scanned pages via vision), docx, pptx, csv/xlsx/xls, png/jpg/webp, html. `vision.py` wraps the Groq vision model with retries, a concurrency limit and a cache; a failed description is kept as an element with `metadata.description_failed`. `web_loader.py` does SSRF-checked fetching (every redirect hop and every Playwright request), HTML to elements, and asset discovery. Unsupported known types (`.ppt`, `.doc`, `.rtf`) get a "save as" hint (`UNSUPPORTED_HINTS`).
- `src/pipeline/ingest.py` — `ingest_files(paths, session_id=None)` and `ingest_url(url, session_id=None)`: limits, duplicate detection (`doc_id` = content hash; same content in a session returns the existing doc), versions (`v1`, `v2` per filename or URL), per-file `FileResult`, a disk error aborts the batch. `src/pipeline/store.py` keeps `./data/<session_id>/manifest.json` (documents and states `received`/`processing`/`ready`/`failed`) and `elements/<doc_id>.jsonl` (input for plan 02).
- `RAG/Dcoument Loaders/Pdf loader/` — PDF loader experiments (`test.ipynb`, PyMuPDF/`fitz`): extracts texts, tables (as CSV), and images with surrounding-text context; images are described by a Groq vision model (`qwen/qwen3.6-27b`) with image downscaling and tenacity retries. The notebook's model id `qwen/qwen3.6-27b` no longer exists on Groq; `src/` uses `qwen/qwen3.8-27b`. The folder name "Dcoument" is misspelled; paths in code and notes use that spelling.
- `inputs/` — sample files (csv, docx, png, txt) for testing ingestion. `data/` is generated at runtime.
- `notes.txt` — design notes: embedding model `BAAI/bge-large-en-v1.5` (1024 dims), vision model `Qwen/Qwen2.5-VL-7B-Instruct`, and a per-format library table (PyMuPDF for PDF, python-docx, pandas for xlsx/csv, python-pptx, TextLoader for txt/md).
- `RAG/Dcoument Loaders/Pdf loader/notes.txt` — target element schema for extracted content: `doc_id`, `version_id`, `element_id`, `type` (text|table|image|chart), `page`, `heading_path`, `title`, `content_text`, `content_structured`, `metadata`, `content_hash`.
- `src/processing/chunkers.py` — elements to chunks: text by size (`CHUNK_SIZE` 800, `CHUNK_OVERLAP` 100) on paragraph/sentence/word boundaries; tables split only above `TABLE_CHUNK_SIZE` with the header repeated and exact spreadsheet row ranges (`row_numbers` from the table loader); image/chart descriptions whole. Chunk id `doc_id:element_id:chunk_n`; the embedded text is prefixed with the heading path.
- `src/embeddings/embedder.py` — `BAAI/bge-large-en-v1.5`, normalised, bge query prefix on queries only. `vector_store.py` — `VectorStore` interface, `PineconeVectorStore` (creates the serverless index `PINECONE_INDEX`, default `hydrarag`, 1024/cosine, on first use; refuses an index with another dimension), `InMemoryVectorStore` for tests. Every method takes the namespace; nothing queries across namespaces.
- `src/retrieval/` — `retrieve()` (top `TOP_K` in the session namespace, drops scores under `MIN_SCORE` and documents that are not `indexed`), `CrossEncoderReranker` (`BAAI/bge-reranker-large`, top `RERANK_TOP_N`).
- `src/pipeline/index.py` — `Indexer`: `index_document`/`index_batch`/`index_session`, skips chunks whose id already holds the same content hash, caches embeddings by hash, removes stale chunk ids, rolls back all of a document's vectors if an upsert fails, confirms vectors are fetchable before `index_state=indexed` (else stays `indexing`; `refresh_status` re-checks), then marks older versions of the same filename/URL `replaced` and deletes their vectors. `delete_document`, `delete_session` (namespace + folder).
- `src/pipeline/rag_chain.py` — `RagChain.ask(session_id, question) -> Answer(answer, found, sources, notes)`. Rejects empty/too-long questions (`QuestionError`), returns "no documents" / "not found" without calling the LLM when nothing is indexed or passes the threshold, calls the Groq model (`ANSWER_MODEL`, default `openai/gpt-oss-120b`) with numbered passages (`prompts.py`), treats `NOT_FOUND` or an answer without valid citations as not found, drops invalid `[n]` and renumbers sources. Service failures raise `ServiceError` with a user-facing message.
- `src/api/app.py` — FastAPI REST API used by the UI: `POST /sessions`, `POST /sessions/{id}/files` (multipart; ingestion is synchronous, indexing runs as a background task), `POST /sessions/{id}/urls`, `GET /sessions/{id}/documents` (state + `index_state`, replaced versions hidden), `DELETE /sessions/{id}/documents/{doc_id}`, `POST /sessions/{id}/chat`, `DELETE /sessions/{id}`, `GET /health`, `GET /config`. Endpoints are sync so they run in the threadpool (Playwright-safe). CORS origins from `CORS_ORIGINS` (default localhost:3000). No rate limits or auth yet (plan 03).
- `web/` — Next.js 16 test UI (single client page `src/app/page.tsx`, API client `src/lib/api.ts`): upload/drag-drop, URL input, document list polling until indexed, chat with expandable sources. Session id kept in localStorage. Read `web/AGENTS.md`: this Next.js version differs from older ones.
- `folder.txt` — the original target layout; `src/` now follows it.

## Configuration

Secrets live in `.env` (gitignored, never commit): `HF_TOKEN`, `GROQ_API_KEY`. `PINECONE_API_KEY` is required for indexing and questions (`require_keys()` checks it with `GROQ_API_KEY`; `HF_TOKEN` is optional). RAG settings (`PINECONE_INDEX`, `EMBEDDING_MODEL`, `RERANKER_MODEL`, `ANSWER_MODEL`, `TOP_K`, `MIN_SCORE`, `RERANK_TOP_N`, chunk sizes) are in `src/config.py` (`RagSettings`). The WhatsApp server will also need `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` and `SESSION_SECRET`. Load them with `python-dotenv`.

## Known issues

- The manifest lock is per process (threading). Running several worker processes on one session needs a file or database lock.
- The SSRF check resolves the host before the request, so DNS rebinding is not fully prevented.
- Playwright's sync API cannot run inside an asyncio event loop: call `ingest_url` from a thread (e.g. FastAPI background task in a threadpool) in plan 03.
- Session folders are never cleaned up yet (planned in plan 03).
- `MIN_SCORE` defaults to 0.50, not the 0.80 in the original design: with bge-large and its query prefix, correct matches on the sample inputs scored 0.53-0.65 and unrelated questions at most 0.45, so 0.80 rejected everything. It is still only a first measurement; run `scripts/calibrate_threshold.py` on a labelled set.
- The content-hash skip works per chunk id (re-indexing the same document is free); identical text in a different document is re-upserted, but its embedding comes from the in-process cache.
- `gpt-oss` answer models cite as `【1】` or `【1†L2-L4】`; `rag_chain.py` normalises these to `[1]`, and `ANSWER_REASONING_EFFORT` (default `low`) keeps reasoning from using up the token budget.
- When huggingface.co is unreachable the embedder/reranker fall back to the local model cache (`load_cached_or_remote`).
- Embedding and reranker models load on first use (several GB of RAM on CPU); the first question in a process is slow.

## Conventions

- Match the existing style: type-annotated functions with docstrings, `print` for progress, `os.path` for paths.
- Keep new retrieval settings consistent with the design above (top 10 at score ≥ `MIN_SCORE`, rerank to top 3, 1024-dim bge-large embeddings) unless the user changes them.
