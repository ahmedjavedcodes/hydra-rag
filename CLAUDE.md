# HydraRAG

Dynamic RAG system: anyone can upload any type of file (PDF, Word, spreadsheet, image, text) or give a URL. The system turns it into a searchable knowledge base, answers questions about it, and cites the sources of every answer. Ingestion (plan/spec 01) is built in `src/`: files and URLs become citable elements per session. Chunking, embeddings, Pinecone, reranking, the Groq answer chain (plan 02) and the FastAPI/WhatsApp server (plan 03) are not built yet. Plans are in `plans/`, specs with acceptance criteria in `specs/`.

## Target architecture

Ingestion: `Any file (PDF/DOCX/XLSX/...)` → file rules by type → load document → chunking → embeddings → Pinecone.

Query: query → embed query → Pinecone similarity search → top 10 (score ≥ 0.80) → rerank to top 3 → Groq LLM → grounded answer.

Planned stack: Python 3.13, LangChain, Pinecone, Groq, FastAPI, Next.js.

### Channels

The first client is WhatsApp through the Meta Cloud API: a FastAPI webhook receives messages and media, calls the RAG pipeline, and replies through the Graph API with the business access token. The session id for a WhatsApp user is derived from their `wa_id` with an HMAC (see `plans/03-api-frontend-and-deploy.md`). Verify `X-Hub-Signature-256` on every webhook POST, return 200 fast and process in the background, and dedupe by message id. The pipeline itself must stay channel-agnostic so a web frontend can reuse it.

### Vector store: Pinecone

- All vectors live in a Pinecone index (no local Chroma/FAISS). Index dimension is 1024 (`BAAI/bge-large-en-v1.5`), metric cosine. Credentials go in `.env` (`PINECONE_API_KEY`).
- Uploads are dynamic and anonymous, so isolate each session's documents with a Pinecone **namespace** equal to the session id, and always query within one namespace (optionally filtered by `doc_id`). Never query across namespaces. Sessions are unauthenticated, so the unguessable id is the only access control.
- Every vector carries the metadata needed for citations. Use the element schema in the Layout section: `doc_id`, `element_id`, `type`, `page`, `heading_path`, `title`, plus `filename` or `source_url` and the chunk text. Pinecone metadata is limited to 40KB per record, so keep chunk text short.
- Use `content_hash` to skip re-embedding unchanged content, and `version_id` when a file is re-uploaded.

### Answers and sources

Every answer must be grounded in the retrieved chunks and return its sources: file name or URL, page (or sheet/row range for spreadsheets, slide number for presentations), and the supporting passage. If no chunk clears the 0.80 score threshold, say the documents don't contain the answer instead of letting the LLM guess.

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

Full ingestion from Python: `from src.pipeline.ingest import ingest_files, ingest_url` (see the module docstring).

Run scripts from the repo root. `utilities/ingestion.py` adds the root to `sys.path` and writes to `./data`, which is relative to the working directory.

Tests patch `utilities.session.DATA_ROOT` to a temp folder and use a fake vision model (`tests/conftest.py`), and the URL tests run a local HTTP server with the SSRF guard patched. There is no linter or CI config yet.

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
- `folder.txt` — target `src/` layout. `src/loaders` and `src/pipeline` exist; `processing`, `embeddings` and `retrieval` come with plan 02.

## Configuration

Secrets live in `.env` (gitignored, never commit): `HF_TOKEN`, `GROQ_API_KEY`. Add `PINECONE_API_KEY` (not configured yet). The WhatsApp server will also need `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` and `SESSION_SECRET`. Load them with `python-dotenv`.

## Known issues

- The manifest lock is per process (threading). Running several worker processes on one session needs a file or database lock.
- The SSRF check resolves the host before the request, so DNS rebinding is not fully prevented.
- Playwright's sync API cannot run inside an asyncio event loop: call `ingest_url` from a thread (e.g. FastAPI background task in a threadpool) in plan 03.
- Session folders are never cleaned up yet (planned in plan 03).

## Conventions

- Match the existing style: type-annotated functions with docstrings, `print` for progress, `os.path` for paths.
- Keep new retrieval settings consistent with the design above (top 10 at score ≥ 0.80, rerank to top 3, 1024-dim bge-large embeddings) unless the user changes them.
