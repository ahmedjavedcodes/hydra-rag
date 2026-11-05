export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type FileResult = {
  source: string;
  status: "ok" | "failed" | "unsupported";
  reason: string | null;
  doc_id: string | null;
  element_count: number;
  duplicate: boolean;
  warnings: string[];
};

export type BatchResult = { session_id: string; files: FileResult[]; error: string | null };

export type DocumentRow = {
  doc_id: string;
  filename: string | null;
  source_url: string | null;
  version_id: string;
  state: "received" | "processing" | "ready" | "failed";
  reason: string | null;
  index_state: "pending" | "indexing" | "indexed" | "failed" | null;
  element_count: number;
  chunk_count: number | null;
  warnings: string[];
};

export type Source = {
  index: number;
  filename: string;
  source_url: string | null;
  location: string;
  passage: string;
  score: number;
  rerank_score: number | null;
};

export type Answer = { answer: string; found: boolean; sources: Source[]; notes: string[] };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, init);
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (body?.detail) message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep the generic message */
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  createSession: () => request<{ session_id: string }>("/sessions", { method: "POST" }),
  config: () => request<{ supported_extensions: string[]; max_file_mb: number; max_files_per_upload: number }>("/config"),
  health: () => request<{ ok: boolean; error?: string; services_loaded?: boolean }>("/health"),
  upload: (sessionId: string, files: File[]) => {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    return request<BatchResult>(`/sessions/${sessionId}/files`, { method: "POST", body: form });
  },
  addUrl: (sessionId: string, url: string) => request<BatchResult>(`/sessions/${sessionId}/urls`, json({ url })),
  documents: (sessionId: string) => request<{ documents: DocumentRow[] }>(`/sessions/${sessionId}/documents`),
  deleteDocument: (sessionId: string, docId: string) =>
    request<{ deleted: boolean }>(`/sessions/${sessionId}/documents/${docId}`, { method: "DELETE" }),
  chat: (sessionId: string, question: string) => request<Answer>(`/sessions/${sessionId}/chat`, json({ question })),
  deleteSession: (sessionId: string) => request<{ deleted: boolean }>(`/sessions/${sessionId}`, { method: "DELETE" }),
};
