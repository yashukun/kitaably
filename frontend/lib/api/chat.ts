import { api, apiDownload, apiStream, type Page } from "./client";

export type Citation = {
  chunk_id: string;
  book_id: string;
  book_title: string;
  /** Display only. Null until ingest has classified the book. */
  genre: string | null;
  page: number | null;
  scope: "canon" | "personal";
};

/** What the reader was doing. The server decides this, not the client. */
export type Intent =
  | "question"
  | "follow_up"
  | "greeting"
  | "chitchat"
  | "meta"
  | "unclear";

/** The two intents that go to the books. Everything else is answered without a
 *  search, so the UI should not promise one is happening. */
export const SEARCHES: Intent[] = ["question", "follow_up"];

/** How the question's material was gathered (server-side, D22/D23). */
export type QueryShape = "focused" | "overview" | "lookup" | "compare" | "metadata";

export type PipelineStep = {
  step: string;
  detail: string;
  ms: number | null;
};

/**
 * What actually ran behind one answer — the "Advanced" disclosure's data.
 *
 * Ephemeral by design: it arrives on the SSE stream and is never persisted, so it
 * exists only for turns asked in this session. Everything in it is the caller's
 * own retrieval over material they can already see.
 */
export type Pipeline = {
  intent: Intent;
  /** Null for conversational turns, which never reach the books. */
  shape: QueryShape | null;
  /** The extracted subject a lookup/compare actually searched on. */
  topic: string | null;
  /** Set only when retrieval ran on something other than what was typed
   *  (a condensed follow-up). */
  query: string | null;
  steps: PipelineStep[];
  books: { title: string; chunks?: number; share?: number }[];
  sources: number;
  outcome:
    | "answered"
    | "book_facts"
    | "refusal"
    | "no_mentions"
    | "pick_book"
    | "needs_two_books"
    | "conversational";
};

export type ChatSession = {
  id: string;
  title: string | null;
  created_at: string;
  last_message_at: string;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  intent: Intent | null;
  citations: Citation[];
  created_at: string;
};

export const listChatSessions = () => api<Page<ChatSession>>("/chat/sessions");

/** A conversation carries no scope of its own. What it can reach is recomputed
 *  from the signed-in caller on every question. */
export const createChatSession = (title?: string) =>
  api<ChatSession>("/chat/sessions", {
    method: "POST",
    body: JSON.stringify({ title: title || null }),
  });

/**
 * The transcript. This is what makes a conversation survive a reload — the rows
 * were always in the database, and for a while nothing here asked for them, which
 * is exactly what "it loses my history" looked like from the outside.
 */
export const listMessages = (sessionId: string) =>
  api<Page<ChatMessage>>(`/chat/sessions/${sessionId}/messages`);

export type ExportFormat = "json" | "md";

/**
 * Download a conversation as a file. Server-rendered from the canonical
 * transcript — the same rows `listMessages` returns — so an export is complete
 * even if this tab has only streamed part of the conversation.
 */
export async function exportConversation(sessionId: string, format: ExportFormat) {
  const { blob, filename } = await apiDownload(
    `/chat/sessions/${sessionId}/export?format=${format}`,
  );
  return { blob, filename: filename ?? `conversation.${format}` };
}

/**
 * Stream an answer. Intent arrives first, then citations, then tokens, then `done`.
 *
 * `bookIds` narrows the search to books the reader picked. It can only ever
 * subtract: scope is derived server-side from the signed-in caller, and these ids
 * are applied on top of that, so an id for somebody else's book selects nothing.
 * Empty means "all of my material", which is the default.
 */
export function askQuestion(
  sessionId: string,
  content: string,
  handlers: {
    onIntent: (intent: Intent) => void;
    /** The pipeline trace, before the first token. Optional: older streams and
     *  conversational replies may never send one. */
    onPipeline?: (pipeline: Pipeline) => void;
    onCitations: (citations: Citation[]) => void;
    onToken: (text: string) => void;
    onError: (message: string) => void;
    /** `ms` is the streaming time — generation, not retrieval, which the
     *  pipeline trace itemises stage by stage. */
    onDone: (ms?: number) => void;
  },
  options?: { bookIds?: string[]; signal?: AbortSignal },
) {
  return apiStream(
    `/chat/sessions/${sessionId}/messages`,
    { content, book_ids: options?.bookIds ?? [] },
    (event, data) => {
      if (event === "intent") handlers.onIntent(data.intent as Intent);
      else if (event === "pipeline") handlers.onPipeline?.(data.pipeline as Pipeline);
      else if (event === "citations") handlers.onCitations(data.citations as Citation[]);
      else if (event === "token") handlers.onToken(data.text as string);
      else if (event === "error") handlers.onError(data.message as string);
      else if (event === "done") handlers.onDone(data.ms as number | undefined);
    },
    options?.signal,
  );
}
