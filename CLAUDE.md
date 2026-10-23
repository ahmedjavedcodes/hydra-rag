# HydraRAG

Dynamic RAG system: anyone can upload any type of file (PDF, Word, spreadsheet, image, text) or give a URL. The system turns it into a searchable knowledge base, answers questions about it, and cites the sources of every answer. The project is early-stage: ingestion and PDF extraction exist as scripts/notebooks; chunking, embeddings, Pinecone, reranking, the Groq answer chain, and the FastAPI + Next.js app are not built yet.

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
uv sync                                   # install deps into .venv
uv run python -m tests.test_ingestion     # run from repo root; a script, not pytest (copies inputs/* into ./data/<new session id>)
uv run python main.py                     # placeholder, prints a greeting
```

Run scripts from the repo root. `utilities/ingestion.py` adds the root to `sys.path` and writes to `./data`, which is relative to the working directory.

There is no test suite, linter, or CI config yet.

## Layout

- `utilities/session.py` — session ids and folders. There are no accounts or auth, so a session is a random 32-hex id (`new_session_id()`) that the client keeps and sends with every upload and question; `validate_session_id` rejects anything else (path traversal). The session id is also the Pinecone namespace.
- `utilities/ingestion.py` — `ingest_user_inputs(inputs, session_id=None)` / `run_ingestion`: copies local files into `./data/<session_id>/{images,spreadsheets,documents,others}` by extension (`FOLDER_MAPPINGS`). Omit `session_id` to start a new session (it is returned in the result); pass it to add files to an existing one. Same-named files get a `_1`, `_2` suffix instead of overwriting.
- `utilities/scrapper.py` — URL scraping into `./data/<session_id>/web/<domain>_<id>/` via `run_scrapper(url, session_id)`: Playwright (local Chrome, `channel="chrome"`) for JS-rendered pages, BeautifulSoup/requests for parsing and downloading linked files, sorted into the same subfolders via `get_target_subfolder`. Imports are duplicated mid-file.
- `utilities/docling.py` — Gemini 2.5 Flash helper that describes images or converts table images to Markdown. Despite the filename it does not use Docling. Not on the Groq path.
- `RAG/Dcoument Loaders/Pdf loader/` — PDF loader experiments (`test.ipynb`, PyMuPDF/`fitz`): extracts texts, tables (as CSV), and images with surrounding-text context; images are described by a Groq vision model (`qwen/qwen3.6-27b`) with image downscaling and tenacity retries. The folder name "Dcoument" is misspelled; paths in code and notes use that spelling.
- `inputs/` — sample files (csv, docx, png, txt) for testing ingestion. `data/` is generated at runtime.
- `notes.txt` — design notes: embedding model `BAAI/bge-large-en-v1.5` (1024 dims), vision model `Qwen/Qwen2.5-VL-7B-Instruct`, and a per-format library table (PyMuPDF for PDF, python-docx, pandas for xlsx/csv, python-pptx, TextLoader for txt/md).
- `RAG/Dcoument Loaders/Pdf loader/notes.txt` — target element schema for extracted content: `doc_id`, `version_id`, `element_id`, `type` (text|table|image|chart), `page`, `heading_path`, `title`, `content_text`, `content_structured`, `metadata`, `content_hash`.
- `folder.txt` — aspirational `src/` package layout (loaders, processing, embeddings, retrieval, pipeline). The current code does not follow it; treat it as a plan, not as the structure.

## Configuration

Secrets live in `.env` (gitignored, never commit): `HF_TOKEN`, `GROQ_API_KEY`. `utilities/docling.py` also expects `GEMINI_API_KEY`. Add `PINECONE_API_KEY` (not configured yet). The WhatsApp server will also need `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` and `SESSION_SECRET`. Load them with `python-dotenv`.

## Known issues

- `utilities/scrapper.py` has duplicated imports mid-file, and `utilities/scrapper.ipynb` still has the old single-folder version of the scraper.

## Conventions

- Match the existing style: type-annotated functions with docstrings, `print` for progress, `os.path` for paths.
- Keep new retrieval settings consistent with the design above (top 10 at score ≥ 0.80, rerank to top 3, 1024-dim bge-large embeddings) unless the user changes them.
