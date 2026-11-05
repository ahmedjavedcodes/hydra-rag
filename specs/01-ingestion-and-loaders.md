# Spec: Ingestion and Loaders

## Problem Statement

HydraRAG must answer questions about whatever a person gives it: PDFs, Word files, spreadsheets, images, text files and web pages. Today, files are only copied into folders by extension (`utilities/ingestion.py`), the scraper downloads pages but does not extract content, and PDF extraction exists only as an experiment in `RAG/Dcoument Loaders/Pdf loader/test.ipynb`. Nothing turns a file into content that can be indexed and cited.

Without this step there is nothing to embed, and answers cannot name the file and page they came from. This spec covers everything between "a file or URL arrives" and "a list of clean, citable elements exists". Chunking and indexing are in `02-indexing-retrieval-answers.md`; transport (WhatsApp, REST) is in `03-api-whatsapp-and-deploy.md`.

## Functional Requirements

1. **Session isolation.** Every upload belongs to a session (`utilities/session.py`) and is stored under `./data/<session_id>/`. A session never reads another session's files.
2. **Type routing.** A registry maps a file extension to one loader. Adding a format means adding one loader module and one registry entry, with no change to the pipeline.
3. **Common output.** Every loader returns a list of elements with these fields: `doc_id`, `version_id`, `element_id`, `type` (`text`, `table`, `image`, `chart`), `page`, `heading_path`, `title`, `content_text`, `content_structured`, `metadata`, `content_hash`.
4. **Citation data.** `metadata` always includes `session_id`, `filename` or `source_url`, and a location: `page` for PDFs, `sheet` + `row_start` + `row_end` for spreadsheets, `slide` for presentations.
5. **Loaders for v1:** `.txt`, `.md`, `.pdf`, `.docx`, `.pptx`, `.csv`, `.xlsx`, `.xls`, images (`.png`, `.jpg`, `.jpeg`, `.webp`), and URLs.
   - `.txt`/`.md`: raw text.
   - `.pdf`: text blocks, tables (as CSV text), embedded images described by a vision model with surrounding text as context (existing notebook logic).
   - `.docx`: paragraphs, headings (to fill `heading_path`), tables.
   - `.pptx`: for each slide, in order: title, body text (including text inside grouped shapes), tables, speaker notes, chart data (categories and series as a table, `type` = `chart`), and each picture described by the vision model with the slide's text as surrounding context. Every element carries `slide` (1-based) and the slide title in `title`. Speaker notes are separate elements marked `metadata.notes = true` so answers can say "speaker notes".
   - spreadsheets: rows converted to Markdown tables in batches, keeping sheet name and row range.
   - images: a text description from the vision model, plus any text visible in the image.
   - URLs: scrape the page (Playwright for JavaScript pages, BeautifulSoup for parsing), extract page text as elements, and send linked documents and images through the matching loaders.
6. **Duplicate detection.** `doc_id` is derived from the file's content hash. Uploading identical content to the same session returns the existing `doc_id` instead of a new document. The same filename with different content creates a new `version_id`.
7. **Same-name files** in a session never overwrite each other (`name_1.ext`).
8. **Per-file result.** Ingestion returns one result per input: `ok` with element count, or `failed` / `unsupported` with a human-readable reason. One bad file never stops the others.
9. **Limits enforced before processing.** File size, number of files per request, extension allow-list, images per document, and URL fetch timeout.
10. **Package layout.** Loader code moves into the `src/` structure in `folder.txt`; the session and file-routing code keep their current behaviour.

**Out of scope for v1 (deferred):** legacy `.ppt` (the old binary format), audio, video, `.doc`/`.rtf` conversion, archive files (`.zip`), password-protected files, OCR of scanned PDFs beyond what the vision model returns, crawling more than one page of a site.

## Behaviour

**Upload of files**
1. Caller passes file paths (or bytes already saved to a temp path) and an optional `session_id`. Without one, a new session id is created and returned.
2. Each file is checked against the limits and the allow-list.
3. The file is copied to `./data/<session_id>/<category>/`, where `<category>` is `documents`, `spreadsheets`, `images` or `others`.
4. Its content hash gives the `doc_id`. If that `doc_id` already exists in the session, the file is reported as `ok (duplicate)` and not processed again.
5. The loader for its extension runs and returns elements. Elements get `element_id`, `content_hash` and the citation metadata.
6. The batch result lists every file with its status.

**Upload of a URL**
1. The URL is normalised (`fix_url_schema`), checked against the URL rules in Constraints, then fetched.
2. Page content and linked files are stored under `./data/<session_id>/web/<site>/` and processed like uploaded files.
3. Elements from the page carry `source_url`.

**Document states:** `received` → `processing` → `ready` or `failed`. The state is stored per document so callers (the API and the WhatsApp bot) can report progress.

## Constraints

- Python 3.13, managed with `uv`; extend the existing modules (`utilities/session.py`, `utilities/ingestion.py`, `utilities/scrapper.py`) instead of rewriting them.
- PDF extraction uses PyMuPDF (`pymupdf`), the package already declared in `pyproject.toml`.
- The vision model is called through Groq (`langchain-groq`). Calls need retries with exponential backoff (tenacity, already used in the notebook) and a limit on parallel calls.
- The scraper uses Playwright with the locally installed Chrome (`channel="chrome"`) for JavaScript pages.
- Sessions have no authentication; the unguessable session id is the only access control, and ids are validated with `validate_session_id` before any path is built.
- URL fetching must refuse private and local addresses (localhost, 10.x, 172.16-31.x, 192.168.x, link-local, cloud metadata addresses) to prevent server-side request forgery.
- Never execute or unpack uploaded files.
- Secrets stay in `.env` and out of git.
- **Assumed defaults (change if wrong, all configurable):** max file size 20 MB, max 10 files per upload, max 30 images described per document, URL fetch timeout 30 seconds, extension allow-list as listed in requirement 5.

## Edge Cases and Error Handling

| Trigger | Expected Response |
|---|---|
| Extension not in the registry (`.exe`, `.mp3`) | Status `unsupported` with the list of supported types; other files in the batch continue |
| File larger than the size limit | Rejected before copying, status `failed: file too large (limit N MB)` |
| Corrupt or unreadable file (broken PDF, invalid `.docx`) | Status `failed` with a short reason; no partial elements stored |
| Password-protected PDF or Office file | Status `failed: file is password protected` |
| Empty file or file with no extractable text | Status `failed: no readable content` |
| Scanned PDF with no text layer | Pages are rendered and described by the vision model, within the image cap; if over the cap, only the first N pages are processed and the status says so |
| Same content uploaded twice in one session | `ok (duplicate)`, existing `doc_id` returned, nothing re-processed |
| Same filename, different content | New `version_id`; stored as `name_1.ext`; older version stays until deleted |
| Two uploads at the same moment into one session with the same filename | Distinct stored names (no overwrite, no crash) |
| `.ppt` (legacy binary PowerPoint) | Status `unsupported: please save as .pptx`; other files continue |
| Slide with no text (only a picture or a chart) | Elements come from the picture description or chart data; a slide with nothing readable produces no elements and is not an error |
| Hidden slides | Included, with `metadata.hidden = true` |
| Presentation with many slides or pictures | Pictures beyond the per-document image cap are skipped and the status says how many; text from all slides is still extracted |
| Text in SmartArt or shapes that python-pptx cannot read | Skipped; if a slide ends up with no readable content at all it is noted in the result, never a crash |
| Spreadsheet with multiple sheets, merged cells, or empty rows | Each sheet is its own set of elements; empty rows skipped; merged cells repeat the value |
| Very large spreadsheet | Rows are batched; elements beyond a configured row cap are not created and the status says how many rows were skipped |
| Vision model rate limit (HTTP 429) or timeout | Retry with backoff up to 5 attempts; if still failing, the image element is stored with a `description_failed` flag and the rest of the document continues |
| Invalid session id (`../x`, wrong length) | `ValueError`; nothing is written |
| URL malformed (`https:/site.com`) | Repaired by `fix_url_schema`; if still invalid, `failed: invalid URL` |
| URL resolves to a private or local address | Rejected: `failed: address not allowed` |
| Page times out, blocks the scraper, or returns an error status | `failed` with the HTTP status or "page unreachable"; no folder left behind |
| Page needs login or shows a CAPTCHA | Treated as no readable content; `failed: page not accessible` |
| Linked file on a page is too large or unsupported | Skipped and listed in the result; the page text still counts as ok |
| Disk full or write error | `failed: could not store file`; batch aborted with a clear error |

## Acceptance Criteria

- [ ] **AC1 (FR1).** Given two sessions A and B, when both upload `data.csv`, then the files live in different `./data/<id>/` folders and neither session can list or read the other's file.
- [ ] **AC2 (FR2).** Given a new dummy loader registered for `.xyz`, when a `.xyz` file is ingested, then its elements are produced without changing the pipeline code.
- [ ] **AC3 (FR3, FR4).** Given one sample of each v1 type, when ingested, then every element validates against the schema and includes `session_id`, source name or URL, and a location.
- [ ] **AC4 (FR5).** Given a multi-page PDF with a table and an image, when ingested, then the output has text elements with page numbers, a table element, and an image element with a description.
- [ ] **AC5 (FR5).** Given a `.docx` with headings and a table, when ingested, then elements carry the correct `heading_path` and the table is its own element.
- [ ] **AC6a (FR5).** Given a `.pptx` with a title slide, a bulleted slide, a table, a chart, a picture and speaker notes, when ingested, then elements exist for each with the right `slide` number, the chart is `type` `chart`, the picture has a description, and the notes element has `metadata.notes = true`; given a legacy `.ppt`, the result is `unsupported` with the save-as-pptx hint.
- [ ] **AC6 (FR5).** Given a workbook with two sheets, when ingested, then elements name their sheet and row range.
- [ ] **AC7 (FR5).** Given a public article URL, when ingested, then elements contain the page text with `source_url` set.
- [ ] **AC8 (FR6, edge cases).** Given a file already in the session, when the identical file is uploaded again, then the result is `ok (duplicate)` and no new elements are created; when the same name with different content is uploaded, a new `version_id` is created.
- [ ] **AC9 (FR7, edge cases).** Given two uploads with the same filename, when both are stored, then both files exist.
- [ ] **AC10 (FR8, edge cases).** Given a batch of one valid file, one corrupt PDF and one `.exe`, when ingested, then the result lists `ok`, `failed` and `unsupported` respectively and the valid file's elements are produced.
- [ ] **AC11 (FR9, edge cases).** Given a file over the size limit, a request over the file-count limit, or a URL to `http://127.0.0.1`, when ingested, then each is rejected with a clear reason before any processing.
- [ ] **AC12 (edge cases).** Given the vision model returns 429 three times then succeeds, when an image is processed, then the description is stored; given it always fails, the image element is flagged and the document still finishes.
- [ ] **AC13 (edge cases).** Given session id `../x`, when any ingestion function is called, then it raises `ValueError` and creates no files.
- [ ] **AC14 (FR10).** Given the refactor is done, when `uv run python -m tests.test_ingestion` and the loader tests run, then they pass.
