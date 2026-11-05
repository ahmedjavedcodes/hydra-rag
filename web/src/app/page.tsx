"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Answer, type DocumentRow, type FileResult } from "@/lib/api";

type Message = { role: "user"; text: string } | { role: "assistant"; answer: Answer } | { role: "error"; text: string };

const SESSION_KEY = "hydrarag.session";

function readStoredSession(): string | null {
  try {
    return localStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

function storeSession(id: string | null) {
  try {
    if (id) localStorage.setItem(SESSION_KEY, id);
    else localStorage.removeItem(SESSION_KEY);
  } catch {
    /* storage unavailable: the session just won't survive a reload */
  }
}

function docStatus(doc: DocumentRow): { label: string; tone: string } {
  if (doc.state === "failed") return { label: "failed", tone: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300" };
  if (doc.state !== "ready") return { label: doc.state, tone: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300" };
  if (doc.index_state === "indexed") return { label: "ready", tone: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300" };
  if (doc.index_state === "failed") return { label: "index failed", tone: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300" };
  return { label: "indexing", tone: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300" };
}

const isBusy = (doc: DocumentRow) => doc.state === "received" || doc.state === "processing" || (doc.state === "ready" && doc.index_state !== "indexed" && doc.index_state !== "failed");

export default function Home() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [documents, setDocuments] = useState<DocumentRow[]>([]);
  const [uploadResults, setUploadResults] = useState<FileResult[]>([]);
  const [uploading, setUploading] = useState(false);
  const [url, setUrl] = useState("");
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<string>("checking…");
  const [accept, setAccept] = useState<string>("");
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const chatEnd = useRef<HTMLDivElement>(null);

  const ensureSession = useCallback(async () => {
    if (sessionId) return sessionId;
    const { session_id } = await api.createSession();
    setSessionId(session_id);
    storeSession(session_id);
    return session_id;
  }, [sessionId]);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    try {
      setDocuments((await api.documents(sessionId)).documents);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [sessionId]);

  useEffect(() => {
    setSessionId(readStoredSession());
    api
      .health()
      .then((h) => setHealth(h.ok ? (h.services_loaded ? "ready" : "loading models…") : h.error ?? "not ready"))
      .catch(() => setHealth("API not reachable on " + (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000")));
    api
      .config()
      .then((c) => setAccept(c.supported_extensions.join(",")))
      .catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Poll while something is still processing or indexing
  useEffect(() => {
    if (!documents.some(isBusy)) return;
    const timer = setInterval(refresh, 2000);
    return () => clearInterval(timer);
  }, [documents, refresh]);

  useEffect(() => {
    chatEnd.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function upload(files: File[]) {
    if (!files.length) return;
    setUploading(true);
    setError(null);
    try {
      const id = await ensureSession();
      const batch = await api.upload(id, files);
      setUploadResults(batch.files);
      if (batch.error) setError(batch.error);
      await refresh();
      setDocuments((await api.documents(id)).documents);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function addUrl() {
    if (!url.trim()) return;
    setUploading(true);
    setError(null);
    try {
      const id = await ensureSession();
      const batch = await api.addUrl(id, url.trim());
      setUploadResults(batch.files);
      setUrl("");
      setDocuments((await api.documents(id)).documents);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
    }
  }

  async function ask() {
    const text = question.trim();
    if (!text || !sessionId) return;
    setQuestion("");
    setMessages((m) => [...m, { role: "user", text }]);
    setAsking(true);
    try {
      const answer = await api.chat(sessionId, text);
      setMessages((m) => [...m, { role: "assistant", answer }]);
    } catch (e) {
      setMessages((m) => [...m, { role: "error", text: (e as Error).message }]);
    } finally {
      setAsking(false);
    }
  }

  async function removeDocument(docId: string) {
    if (!sessionId) return;
    try {
      await api.deleteDocument(sessionId, docId);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function newSession() {
    if (sessionId) {
      try {
        await api.deleteSession(sessionId);
      } catch {
        /* the old session may already be gone */
      }
    }
    storeSession(null);
    setSessionId(null);
    setDocuments([]);
    setMessages([]);
    setUploadResults([]);
    setError(null);
  }

  const readyCount = documents.filter((d) => d.index_state === "indexed").length;

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-4 px-4 py-6 lg:flex-row">
      {/* Documents */}
      <section className="flex w-full flex-col gap-4 lg:w-[380px] lg:shrink-0">
        <header>
          <h1 className="text-2xl font-semibold tracking-tight">HydraRAG</h1>
          <p className="text-sm text-neutral-500">Upload documents, then ask questions with cited sources.</p>
          <p className="mt-1 text-xs text-neutral-500">
            API: <span className="font-medium">{health}</span>
          </p>
        </header>

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            upload(Array.from(e.dataTransfer.files));
          }}
          className={`rounded-xl border-2 border-dashed p-5 text-center transition ${
            dragging ? "border-blue-500 bg-blue-50 dark:bg-blue-950/30" : "border-neutral-300 dark:border-neutral-700"
          }`}
        >
          <p className="text-sm">Drop files here or</p>
          <button
            onClick={() => fileInput.current?.click()}
            disabled={uploading}
            className="mt-2 rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
          >
            {uploading ? "Processing…" : "Choose files"}
          </button>
          <input
            ref={fileInput}
            type="file"
            multiple
            accept={accept}
            className="hidden"
            onChange={(e) => upload(Array.from(e.target.files ?? []))}
          />
          <p className="mt-2 text-xs text-neutral-500">PDF, Word, PowerPoint, Excel/CSV, images, text, Markdown, HTML</p>
        </div>

        <div className="flex gap-2">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addUrl()}
            placeholder="Add a web page URL"
            className="min-w-0 flex-1 rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm dark:border-neutral-700 dark:bg-neutral-900"
          />
          <button
            onClick={addUrl}
            disabled={uploading || !url.trim()}
            className="rounded-lg border border-neutral-300 px-3 py-2 text-sm font-medium disabled:opacity-50 dark:border-neutral-700"
          >
            Add
          </button>
        </div>

        {error && <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-300">{error}</p>}

        {uploadResults.some((r) => r.status !== "ok" || r.warnings.length) && (
          <ul className="space-y-1 text-xs">
            {uploadResults
              .filter((r) => r.status !== "ok" || r.warnings.length)
              .map((r, i) => (
                <li key={i} className="rounded-md bg-amber-50 px-2 py-1 text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
                  <span className="font-medium">{r.source}</span>: {r.status === "ok" ? r.warnings.join("; ") : `${r.status} – ${r.reason}`}
                </li>
              ))}
          </ul>
        )}

        <div className="rounded-xl border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900">
          <div className="flex items-center justify-between border-b border-neutral-200 px-4 py-2 dark:border-neutral-800">
            <h2 className="text-sm font-semibold">Documents ({documents.length})</h2>
            <button onClick={newSession} className="text-xs text-neutral-500 hover:text-red-600">
              New session
            </button>
          </div>
          {documents.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-neutral-500">No documents yet.</p>
          ) : (
            <ul className="divide-y divide-neutral-200 dark:divide-neutral-800">
              {documents.map((doc) => {
                const status = docStatus(doc);
                return (
                  <li key={doc.doc_id} className="flex items-start gap-2 px-4 py-2">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium" title={doc.source_url ?? doc.filename ?? ""}>
                        {doc.source_url ?? doc.filename}
                      </p>
                      <p className="text-xs text-neutral-500">
                        {doc.state === "failed" ? doc.reason : `${doc.element_count} elements${doc.chunk_count ? ` · ${doc.chunk_count} chunks` : ""} · ${doc.version_id}`}
                      </p>
                    </div>
                    <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${status.tone}`}>{status.label}</span>
                    <button onClick={() => removeDocument(doc.doc_id)} className="shrink-0 text-xs text-neutral-400 hover:text-red-600" aria-label="Delete document">
                      ✕
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        {sessionId && <p className="break-all text-[11px] text-neutral-400">Session {sessionId}</p>}
      </section>

      {/* Chat */}
      <section className="flex min-h-[70vh] flex-1 flex-col rounded-xl border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900">
        <div className="flex-1 space-y-4 overflow-y-auto p-4">
          {messages.length === 0 && (
            <div className="flex h-full items-center justify-center text-center text-sm text-neutral-500">
              {readyCount ? "Ask anything about your documents." : "Add a document to start. Questions work once a document shows ready."}
            </div>
          )}
          {messages.map((message, i) =>
            message.role === "user" ? (
              <div key={i} className="ml-auto max-w-[80%] rounded-2xl bg-neutral-900 px-4 py-2 text-sm text-white dark:bg-white dark:text-neutral-900">
                {message.text}
              </div>
            ) : message.role === "error" ? (
              <div key={i} className="max-w-[85%] rounded-2xl bg-red-50 px-4 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-300">
                {message.text}
              </div>
            ) : (
              <div key={i} className="max-w-[85%] space-y-2">
                <div className="whitespace-pre-wrap rounded-2xl bg-neutral-100 px-4 py-2 text-sm dark:bg-neutral-800">{message.answer.answer}</div>
                {message.answer.notes.map((note, j) => (
                  <p key={j} className="text-xs text-amber-700 dark:text-amber-400">
                    {note}
                  </p>
                ))}
                {message.answer.sources.length > 0 && (
                  <div className="space-y-2">
                    {message.answer.sources.map((source) => (
                      <details key={source.index} className="rounded-lg border border-neutral-200 px-3 py-2 text-xs dark:border-neutral-700">
                        <summary className="cursor-pointer">
                          <span className="font-semibold">[{source.index}]</span> {source.source_url ?? source.filename}, {source.location}
                          <span className="ml-2 text-neutral-400">score {source.score.toFixed(2)}</span>
                        </summary>
                        <p className="mt-2 whitespace-pre-wrap text-neutral-600 dark:text-neutral-300">{source.passage}</p>
                      </details>
                    ))}
                  </div>
                )}
              </div>
            ),
          )}
          {asking && <p className="text-sm text-neutral-500">Thinking…</p>}
          <div ref={chatEnd} />
        </div>
        <div className="flex gap-2 border-t border-neutral-200 p-3 dark:border-neutral-800">
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !asking && ask()}
            placeholder={sessionId ? "Ask a question about your documents" : "Upload a document first"}
            disabled={!sessionId}
            className="min-w-0 flex-1 rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm disabled:opacity-50 dark:border-neutral-700 dark:bg-neutral-950"
          />
          <button
            onClick={ask}
            disabled={!sessionId || asking || !question.trim()}
            className="rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
          >
            Ask
          </button>
        </div>
      </section>
    </main>
  );
}
