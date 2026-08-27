import { api, apiUpload, type Page } from "./client";

export type BookStatus =
  | "uploaded"
  | "parsing"
  | "chunking"
  | "embedding"
  | "ready"
  | "failed";

/** The one field that must never be misread. See ScopeChip. */
export type BookScope = "canon" | "personal";

/** What sort of book this is. Sets the tutor's register and nothing else — it is
 *  never a search filter, because a misclassified book must not become material
 *  nobody can reach. */
export type BookKind = "fiction" | "nonfiction" | "academic" | "reference";

export type Book = {
  id: string;
  title: string;
  author: string | null;
  /** All three are written by ingest, best effort, and null until it has run. */
  kind: BookKind | null;
  genre: string | null;
  summary: string | null;
  scope: BookScope;
  /** `zip` is one book uploaded in parts, combined server-side at parse time. */
  source_format: "pdf" | "docx" | "pptx" | "txt" | "md" | "zip";
  page_count: number | null;
  status: BookStatus;
  error: string | null;
  needs_ocr: boolean;
  created_at: string;
  updated_at: string;
  /** Computed by the server from the caller. The owner id itself is deliberately
   *  not sent — the UI only needs to know whether to offer Share and Delete. */
  owner_is_me: boolean;
  /** Owner only, and null until the first ingest run finishes. */
  ingest_trace: IngestTrace | null;
};

/**
 * What the worker did while reading this book — the Advanced panel on a book card.
 *
 * Sent to the book's owner only; it is null on somebody else's shared book. Every
 * field is content-free by the recorder's construction: counts, durations, member
 * filenames and reasons, never a line of the book itself.
 */
export type IngestTrace = {
  format: string;
  source_format: string;
  started_at: string;
  finished_at: string;
  steps: { step: string; detail: string; ms: number }[];
  /** ZIP uploads only: the archive's parts, in the order they were combined. */
  manifest: { name: string; pages?: number }[];
  manifest_total: number;
  summary: {
    outcome: "ready" | "failed";
    reason: string | null;
    byte_size: number;
    wall_ms: number;
    pages?: number;
    chapters?: number;
    chunks?: number;
    vectors?: number;
  };
};

/** Statuses that mean work is still happening, so the list should keep polling. */
export const IN_PROGRESS: BookStatus[] = ["uploaded", "parsing", "chunking", "embedding"];

export const listBooks = (scope?: BookScope) =>
  api<Page<Book>>(`/books${scope ? `?scope=${scope}` : ""}`);

/**
 * Upload. There is no scope parameter, and that is the point: every upload lands
 * private to whoever made it, and sharing is a separate deliberate act below.
 *
 * `title` is optional. Left empty, the server names the book after the file — the
 * derivation lives there rather than here so that every client gets the same answer,
 * and so the one place that decides what a book is called is the one that stores it.
 */
export function uploadBook(
  file: File,
  title?: string,
  author?: string,
  onProgress?: (fraction: number) => void,
) {
  const form = new FormData();
  form.set("file", file);
  // Only send the field when it has something in it: an empty string is a title the
  // server would have to treat as absent anyway, and omitting it says so honestly.
  if (title?.trim()) form.set("title", title.trim());
  if (author?.trim()) form.set("author", author.trim());
  return apiUpload<{ id: string; status: BookStatus }>("/books", form, onProgress);
}

/** Share this book with everyone, or take it back. Audited either way. */
export const setBookShared = (id: string, shared: boolean) =>
  api<Book>(`/books/${id}/scope`, { method: "PATCH", body: JSON.stringify({ shared }) });

export const retryBook = (id: string) =>
  api<{ id: string; status: BookStatus }>(`/books/${id}/retry`, { method: "POST" });

export const deleteBook = (id: string) =>
  api<{ id: string; status: BookStatus }>(`/books/${id}`, { method: "DELETE" });
