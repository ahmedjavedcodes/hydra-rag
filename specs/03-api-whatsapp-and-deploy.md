# Spec: API, WhatsApp Channel and Deployment

## Problem Statement

The ingestion and answering code (specs 01 and 02) is only a library. People need a way to use it without creating an account. The owner will connect it to WhatsApp using a Meta business access token, so people send files or links in a WhatsApp chat, ask questions, and get answers with sources in the same chat. A server must receive Meta's webhook calls, run the RAG pipeline, and send replies.

Without this server there is no product. The risks are specific to a public, anonymous service: forged webhook calls, duplicate processing when Meta retries slow replies, one person's documents reaching another, and uncapped cost from strangers messaging the number.

## Functional Requirements

**Core API (channel-agnostic)**
1. `POST /sessions` creates a session and returns its id.
2. `POST /sessions/{id}/files` accepts one or more files and returns a per-file status.
3. `POST /sessions/{id}/urls` adds a URL to scrape and index.
4. `GET /sessions/{id}/documents` lists documents with state (`queued`, `processing`, `ready`, `failed` plus reason).
5. `DELETE /sessions/{id}/documents/{doc_id}` removes one document.
6. `POST /sessions/{id}/chat` takes a question and returns `answer` and `sources`.
7. `DELETE /sessions/{id}` removes the session's files and vectors.
8. Indexing runs in the background; the upload call returns before indexing finishes.
9. Limits (file size, files per request, allow-listed types, question length) are enforced at the API edge, and requests are rate limited per client.

**WhatsApp channel**
10. `GET /webhook/whatsapp` answers Meta's verification handshake: returns `hub.challenge` as plain text when `hub.verify_token` matches `WHATSAPP_VERIFY_TOKEN`, otherwise 403.
11. `POST /webhook/whatsapp` verifies the `X-Hub-Signature-256` signature (HMAC-SHA256 of the raw body with `WHATSAPP_APP_SECRET`) before parsing, and rejects invalid ones.
12. The webhook returns 200 immediately and processes the message in the background.
13. Each message id is processed once; repeated deliveries are ignored.
14. Delivery status events (`sent`, `delivered`, `read`, `failed`) are logged and not answered.
15. The sender's phone number maps to a session: `session_id` = first 32 hex characters of `HMAC-SHA256(SESSION_SECRET, wa_id)`. The same number always gets the same session.
16. Message routing:
    - `text` that is only a URL: scrape and index it;
    - `text` starting with `/`: run a command;
    - other `text`: treat as a question;
    - `document` and `image`: download the media with the access token, store it in the session, index it;
    - any other type: reply that it is not supported and list what is.
17. Commands: `/help`, `/docs` (list documents and states), `/delete <n>` (remove document number n from the `/docs` list), `/clear` (delete the whole session), `/source <n>` (show the passage behind source n of the last answer).
18. Replies go through the Graph API (`POST /{phone-number-id}/messages`, `type: "text"`). Answers are plain text, split into several messages when over 4096 characters, with a short sources list: `Sources: [1] report.pdf, p.3  [2] data.xlsx, rows 10-24`.
19. The bot acknowledges each received file ("Got it, indexing report.pdf") and sends a second message when it is ready.

**Operations**
20. Session folders and Pinecone namespaces are deleted after a configured period of inactivity.
21. Logs and basic metrics (upload failures, answer latency, "not found" rate, daily usage per number).
22. Daily per-number and global caps on uploads, questions and vision/LLM calls.

**Out of scope for v1 (deferred):** Next.js web frontend, streaming answers, WhatsApp templates and server-initiated messages, voice notes, group chats, interactive buttons and lists, multiple languages, user accounts.

## Behaviour

**First contact.** A person messages the WhatsApp number. The server derives their session and replies with a short welcome that says what to send (a file or a link) and that `/help` lists commands.

**Adding a document.** The person sends a PDF. The webhook verifies and records the message, returns 200, and a background task downloads the file, runs ingestion (spec 01) and indexing (spec 02). The bot replies "Got it, indexing report.pdf". When the document state is `ready` it replies "report.pdf is ready. Ask me anything about it." (only if the 24-hour conversation window is still open).

**Asking a question.** The person sends a question. The background task runs retrieval and the answer chain (spec 02) and replies with the answer plus the sources list. If nothing relevant is found, it replies that the documents don't contain the answer, with no sources.

**Commands.** `/docs` lists numbered documents with their state. `/delete 2` removes document 2 and confirms. `/clear` asks for confirmation (`/clear confirm`) and then deletes everything. `/source 1` replies with the file, location and quoted passage for source 1 of the last answer.

**Document states** as in spec 01: `queued` → `processing` → `ready` or `failed`. A question asked while a document is processing is answered from ready documents with a note.

**REST clients** follow the same flows through the endpoints in requirements 1-7, with the session id in an `X-Session-Id` header or the path.

## Constraints

- FastAPI on Python 3.13, managed with `uv`; call the ingestion and RAG code through channel-neutral functions, so the webhook contains no RAG logic.
- WhatsApp uses the Meta Cloud API. The webhook URL must be public HTTPS; development uses a tunnel (ngrok or Cloudflare Tunnel).
- Use a System User permanent access token. The temporary developer-console token expires after about 24 hours.
- Secrets in `.env` only: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_API_VERSION`, `SESSION_SECRET`, plus the keys from spec 02. Never log tokens, secrets or message contents.
- `SESSION_SECRET` is permanent: changing it orphans all existing sessions.
- Meta only allows free-form replies within 24 hours of the person's last message; after that, only approved templates. v1 never sends messages on its own initiative.
- Meta's media size and type limits apply to downloads; enforce the lower of Meta's limit and the project limit (spec 01).
- While the app is in development mode, only registered test numbers can message the bot; broader use needs business verification.
- WhatsApp replies are plain text with only WhatsApp formatting (`*bold*`, `_italic_`), up to 4096 characters per message.
- No user authentication: the webhook signature protects the WhatsApp channel; the unguessable session id protects REST sessions. CORS limited to the allowed origins.
- Background tasks and the duplicate-message store must survive a process restart for the cases in the edge-case table (use an expiring store such as Redis, or a database table).
- **Assumptions (change if wrong):** one WhatsApp business number; one server instance for v1; inactivity expiry 7 days; per-number cap 50 questions and 10 files per day; REST rate limit 30 requests per minute per IP.

## Edge Cases and Error Handling

| Trigger | Expected Response |
|---|---|
| `GET` verification with wrong `hub.verify_token` | 403, nothing returned |
| `POST` with missing or invalid `X-Hub-Signature-256` | 403; body not parsed or processed; event logged without payload |
| Meta retries the same message (same id) | 200 returned, message ignored, no second reply |
| Retry arrives while the first is still processing | 200 returned, ignored (id recorded as soon as received) |
| Server restarts mid-processing | On the next retry or message the person gets the result of the lost task or an error message; a document stuck in `processing` is marked `failed` by a timeout and the person is told to resend |
| Delivery status events | Logged; failed deliveries logged with Meta's error code; no reply sent |
| Message type unsupported (audio, video, sticker, location, contact) | Reply naming the supported types |
| Document type or size not allowed | Reply with the reason ("file too large, limit 20 MB") and nothing stored |
| Media download fails or the media link has expired | Reply "couldn't download that file, please send it again"; retry once first |
| Ingestion fails for a file | Reply with the reason from spec 01; other documents unaffected |
| Question with no documents in the session | Reply that no documents are added yet and how to add one |
| Question while documents still processing | Answer from ready ones plus "still processing: name" |
| Text message with an unknown command (`/foo`) | Reply with the command list |
| `/delete` or `/source` with a number that doesn't exist | Reply "no document/source with that number" and show `/docs` |
| `/clear` without confirmation | Ask to repeat as `/clear confirm`; nothing deleted |
| Answer longer than 4096 characters | Split at paragraph boundaries into several messages, in order |
| Graph API send fails (rate limit, 5xx, invalid token) | Retry with backoff; on persistent failure log it; token errors raise an operator alert, not a user message |
| Access token expired or revoked | Logged as critical; health endpoint reports unhealthy |
| 24-hour window closed when a background task finishes | Do not send; the result is available when the person next writes ("Your file report.pdf is ready") |
| Person exceeds daily or rate limits | One reply that the limit was reached and when it resets; no further processing |
| Two messages from the same person arrive at once | Processed independently; document numbering and `/docs` remain consistent |
| Two different numbers upload the same file | Separate sessions; nothing shared or deduplicated across them |
| Invalid session id on a REST call | 400 |
| REST upload over limits or wrong type | 413 or 415 with a clear message |
| Unhandled server error | 500 with a generic message and an error id; no stack trace; details only in logs |
| Pinecone or Groq outage | Person gets "temporary problem, please try again in a moment"; nothing partially indexed (spec 02) |
| Webhook sent over HTTP or to a wrong path | Not served; only the HTTPS webhook path is registered |

## Acceptance Criteria

- [ ] **AC1 (FR1-7).** Given the REST API, when a client creates a session, uploads a PDF, waits until the document is `ready`, asks a question, lists and deletes documents, then each call returns the documented response and the answer cites the right page.
- [ ] **AC2 (FR8).** Given a large upload, when it is sent, then the response returns before indexing finishes and the document state moves through `queued`, `processing`, `ready`.
- [ ] **AC3 (FR9, edge cases).** Given a file over the limit, a wrong type or the 31st request in a minute, when sent, then the API returns 413, 415 or 429 and stores nothing.
- [ ] **AC4 (FR10).** Given Meta's verification request with the right token, when called, then the challenge is returned as plain text; with a wrong token, 403.
- [ ] **AC5 (FR11, edge cases).** Given a webhook call with a bad signature, when posted, then it gets 403 and no processing happens; with a valid signature it is accepted.
- [ ] **AC6 (FR12, FR13, edge cases).** Given the same message id delivered twice, when both arrive, then both get 200 and exactly one reply is sent.
- [ ] **AC7 (FR14).** Given a `delivered` status event, when received, then it is logged and no message is sent.
- [ ] **AC8 (FR15, edge cases).** Given two different phone numbers, when each uploads a document and asks about the other's content, then each gets "not found" and the session folders and namespaces differ; given one number messaging twice, the session is the same.
- [ ] **AC9 (FR16, FR19).** Given a PDF sent on WhatsApp, when received, then the person gets "Got it, indexing..." and later a "ready" message, and a following question about it is answered with the right page.
- [ ] **AC10 (FR16).** Given a message containing only a URL, when received, then the page is scraped and indexed; given an audio message, the reply lists supported types.
- [ ] **AC11 (FR17).** Given documents in a session, when the person sends `/docs`, `/delete 1`, `/source 1` and `/clear confirm`, then each does what Behaviour describes, and `/clear` without `confirm` deletes nothing.
- [ ] **AC12 (FR18, edge cases).** Given an answer over 4096 characters, when sent, then it arrives as ordered parts; every answer with sources ends with the sources list in the documented format.
- [ ] **AC13 (edge cases).** Given the Graph API fails transiently, when a reply is sent, then it is retried and delivered; given an invalid token, the failure is logged critically and the health endpoint reports unhealthy.
- [ ] **AC14 (FR20, FR22).** Given a session inactive for longer than the expiry, when the cleanup job runs, then its files and namespace are deleted; given a number over its daily cap, it gets one limit message and no further processing.
- [ ] **AC15 (FR21, constraints).** Given normal use, when logs are inspected, then no tokens, secrets, message text or raw phone numbers appear.
- [ ] **AC16 (edge cases).** Given the server restarts mid-processing, when it comes back, then no document stays in `processing` past the timeout and the person can resend.
- [ ] **AC17 (FR1-22).** Given the deployed app behind HTTPS, when a test number sends a PDF and a link and asks a question about each, then both answers arrive in the chat with correct sources, with no sign-up.
