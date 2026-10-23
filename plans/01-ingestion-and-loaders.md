# Plan 1: Ingestion and loaders

**Goal:** any uploaded file or URL becomes a list of clean, citable *elements* in one common schema, isolated per session.

**Status:** started. Session folders, file routing and the scraper exist. The PDF loader exists only as a notebook.

## Element schema (output of every loader)

From `RAG/Dcoument Loaders/Pdf loader/notes.txt`: `doc_id`, `version_id`, `element_id`, `type` (text|table|image|chart), `page`, `heading_path`, `title`, `content_text`, `content_structured`, `metadata`, `content_hash`.

Add to `metadata`: `filename` or `source_url`, `session_id`, and a location (`page`, or `sheet` + `row_start`/`row_end`, or `slide`) so every element can be cited.

## Steps

1. **Package layout.** Move code out of notebooks and `utilities/` into the structure in `folder.txt` (`src/loaders`, `src/processing`, `src/embeddings`, `src/retrieval`, `src/pipeline`). Keep `utilities/session.py` and `ingestion.py` working during the move.
2. **Loader interface.** One function or class per format: `load(path, session_id) -> list[Element]`. A registry maps extension to loader, so a new format is one new file. Unknown or corrupt files return an error entry for the user, never an exception that kills the batch.
3. **Loaders**, in this order:
   - `.txt`/`.md`: plain read.
   - `.pdf`: port `test.ipynb` (PyMuPDF text blocks, tables to CSV, images with surrounding text, Groq vision description).
   - `.docx`: `python-docx` paragraphs, headings (fills `heading_path`) and tables.
   - `.csv`/`.xlsx`/`.xls`: `pandas`, batched rows to Markdown tables, keeping sheet and row range.
   - images: vision description (OCR for text-heavy images).
   - `.pptx`: slide titles, text, images (add after the core formats work).
   - URLs: existing scraper output (HTML text plus linked files) routed through the same loaders.
4. **Hashing and versions.** Fill `content_hash` per element and `doc_id` from the file hash, so re-uploading the same file is detected. Set `version_id` when the same filename arrives with different content.
5. **Limits and safety.** Max file size and file count per upload, extension allow-list, no executing or unpacking archives, scraper timeouts and a same-domain/size cap. Give vision calls retries and a per-batch concurrency limit (rate limits).
6. **Fix leftovers.** Dedupe imports in `scrapper.py`, update or delete `scrapper.ipynb`, remove `docling.py` if Gemini isn't used (or rename it to what it does).

## Done when

- Each sample in `inputs/` plus a PDF and a URL load into elements that pass a schema check.
- A corrupt file in a batch is reported and the rest still load.
- Two sessions uploading `data.csv` never see each other's files.
- Loader tests run with pytest (add pytest as a dev dependency).

## Risks

- Scanned PDFs and image-heavy documents make vision calls slow and costly: cap images per document and cache descriptions by hash.
- Some pages need JavaScript or block scrapers: fall back gracefully and tell the user.
